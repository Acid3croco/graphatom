"""Le test de reconnexion : perdre Postgres est un cas nominal, pas une panne.

Scénario : admettre un item, lancer l'ordonnanceur, puis couper de force
toutes ses connexions (`pg_terminate_backend` — ce que fait un redémarrage
de la base) plusieurs fois pendant qu'il travaille :

  1. le processus survit à chaque coupure
  2. il l'a bien vue et annonce sa reconnexion
  3. l'item termine quand même, sans intervention
  4. le journal est une chaîne contiguë v1..vN, sans trou ni doublon
  5. un thread de bloc privé de base fait crasher son run, pas le process :
     le run reste orphelin, le faucheur le classe crashed et le noyau retente
  6. sans base du tout : le backoff est borné, et toute autre exception
     traverse la boucle au lieu d'être avalée

Usage : uv run python tests/reconnect_test.py
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from outils import kill_group, provision_postgres, sh  # noqa: E402

# une base à lui, détruite avec lui : jamais celle que GRAPHATOM_DSN désigne
os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-reconnect")

from graphatom import blocks, db, kernel, scheduler  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# comme le crash-test : le test travaille dans un répertoire à lui, jamais
# dans ROOT/data, qui peut être le data/ vivant d'un rail (workspaces d'items)
WORK = Path(tempfile.mkdtemp(prefix="graphatom-reconnect-test-"))
blocks.DATA_DIR = WORK / "data"  # les blocs joués en direct écrivent là aussi
# hermétisme : ce dépôt-ci est le seul univers du test. L'agent qui nous
# lance a dans son environnement le clone et l'instance de la production —
# un ordonnanceur lancé d'ici n'en voit ni l'un ni l'autre.
os.environ["GRAPHATOM_REPO_DIR"] = str(ROOT)
os.environ.pop("GRAPHATOM_AGENT_DSN", None)
COUPURES = 3       # trois coupures d'affilée : le backoff doit rester borné
ENTRE_S = 2.0      # temps laissé au worker pour retravailler entre deux
TIMEOUT_S = 150    # un run fauché en vol coûte un bail (30 s) avant d'être retenté


def scheduler_proc(journal: Path) -> subprocess.Popen:
    # comme le crash-test : le binaire du venv, dans son propre groupe
    return subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "graphatom"), "run"],
        cwd=WORK, start_new_session=True,
        stdout=journal.open("w"), stderr=subprocess.STDOUT,
    )


def couper(conn: psycopg.Connection) -> int:
    """Toutes les autres connexions de la base, coupées net côté serveur."""
    tues = conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()"
    ).fetchall()
    return len(tues)


def survit_aux_coupures(conn, proc, item_id: int, journal: Path) -> None:
    for i in range(1, COUPURES + 1):
        time.sleep(ENTRE_S)
        n = couper(conn)
        time.sleep(0.5)
        if proc.poll() is not None:
            sys.exit(f"ÉCHEC : l'ordonnanceur est mort à la coupure {i}")
        print(f"1.{i} {n} connexion(s) coupée(s), ordonnanceur vivant ✓")

    deadline = time.time() + 10
    while time.time() < deadline and "base injoignable" not in journal.read_text():
        time.sleep(0.5)
    lignes = [l for l in journal.read_text().splitlines() if "base injoignable" in l]
    if not lignes:
        sys.exit("ÉCHEC : aucune coupure vue par l'ordonnanceur")
    print(f"2. coupure vue et annoncée : {lignes[0][:90]}… ✓")

    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s", (item_id,)
        ).fetchone()
        if item["terminal_at"] is not None:
            break
        time.sleep(0.5)
    else:
        sys.exit("ÉCHEC : l'item n'a pas terminé après les coupures")
    assert item["state"] in ("close", "close_unresolved"), item["state"]
    print(f"3. terminal sans intervention : {item['state']} ✓")

    versions = [r["item_version"] for r in conn.execute(
        "SELECT item_version FROM event WHERE item_id = %s ORDER BY item_version",
        (item_id,),
    )]
    assert versions == list(range(1, len(versions) + 1)), versions
    assert versions[-1] == item["version"]
    print(f"4. journal contigu v1..v{versions[-1]} ✓")


def thread_sans_base(conn, revision: str) -> None:
    """Un bloc privé de base : le run meurt, le process non."""
    item_id = int(sh("admit", revision, "pipeline-y:disk"))
    run = kernel.claim(conn, item_id)
    assert run is not None and run["status"] == "running", run

    def morte() -> psycopg.Connection:
        raise psycopg.OperationalError("connection is bad (simulé)")

    vues, vrai = [], db.connect
    threading.excepthook = lambda args: vues.append(args.exc_type)
    db.connect = morte
    th = threading.Thread(
        target=scheduler._execute, args=(run["id"], item_id), daemon=True
    )
    th.start()
    th.join(5)
    db.connect = vrai
    threading.excepthook = threading.__excepthook__

    assert not th.is_alive(), "le thread du bloc ne s'est pas terminé"
    assert vues == [psycopg.OperationalError], vues
    etat = conn.execute(
        "SELECT status FROM node_run WHERE id = %s", (run["id"],)
    ).fetchone()["status"]
    assert etat == "running", etat
    print("5.1 le thread meurt sur sa connexion, le process vit ✓")

    conn.execute(  # le bail du run orphelin, arrivé à échéance
        "UPDATE node_run SET lease_expires_at = now() - interval '1 second' "
        "WHERE id = %s", (run["id"],),
    )
    kernel.reap(conn)
    run = conn.execute(
        "SELECT * FROM node_run WHERE id = %s", (run["id"],)
    ).fetchone()
    item = conn.execute(
        "SELECT * FROM work_item WHERE id = %s", (item_id,)
    ).fetchone()
    assert (run["status"], run["outcome"]) == ("faulted", "crashed"), run
    assert item["state"] == run["node"] and item["terminal_at"] is None, item
    print(f"5.2 faucheur : run {run['status']}/{run['outcome']}, "
          f"item retenté sur « {item['state']} » ✓")


class Assez(Exception):
    """Sentinelle : assez de tours de backoff observés, on sort de la boucle."""


def boucle_sans_base() -> None:
    """Base absente : le backoff plafonne, et rien d'inattendu n'est rattrapé.

    La boucle ne rattrape que ses deux incidents de connexion —
    `OperationalError` et le plan caché périmé d'une migration. Tout le reste
    la traverse, et c'est ce que vérifie 6.2.
    """
    attentes, vrai_connect, vrai_sleep = [], db.connect, time.sleep

    def refusee() -> psycopg.Connection:
        raise psycopg.OperationalError("connection refused (simulé)")

    def espion(s: float) -> None:
        attentes.append(s)
        if len(attentes) == 8:
            raise Assez

    db.connect, time.sleep = refusee, espion
    try:
        scheduler.run_forever()
    except Assez:
        pass
    finally:
        db.connect, time.sleep = vrai_connect, vrai_sleep
    assert attentes == [1, 2, 4, 8, 16, 30, 30, 30], attentes
    print(f"6.1 backoff borné : {attentes} ✓")

    def buguee() -> psycopg.Connection:
        raise RuntimeError("bug de programmation (simulé)")

    db.connect = buguee
    try:
        scheduler.run_forever()
    except RuntimeError:
        print("6.2 une exception non-connexion traverse la boucle ✓")
    else:
        sys.exit("ÉCHEC : run_forever a avalé une exception non attendue")
    finally:
        db.connect = vrai_connect


def main() -> None:
    sh("init-db", "--drop")
    revision = sh("publish", "examples/supervision.json")
    item_id = int(sh("admit", revision, "pipeline-x:oom"))
    print(f"révision {revision[:12]}…, item {item_id}")

    journal = WORK / "reconnect_test.log"
    proc = scheduler_proc(journal)
    try:
        with db.connect() as conn:
            survit_aux_coupures(conn, proc, item_id, journal)
    finally:
        kill_group(proc, signal.SIGTERM)

    with db.connect() as conn:  # ordonnanceur arrêté : le thread est seul en piste
        thread_sans_base(conn, revision)

    boucle_sans_base()

    print("\nreconnexion : OK — une base qui s'absente ne tue pas le worker")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
