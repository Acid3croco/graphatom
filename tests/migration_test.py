"""Le test de migration : le rail se déploie sous son propre worker.

Un déploiement joue `schema.sql` pendant que le worker travaille. La forme
d'une table change sous une connexion dont psycopg a fait préparer les
requêtes, et Postgres refuse le plan caché — `cached plan must not change
result type`. Ce n'est pas un accident rare : le rail se déploie lui-même,
donc il migrera son schéma sous son propre worker, encore et encore.

Scénario, sur une base réelle :

  1. `init-db` annonce ce que la migration a changé, et dit qu'il n'y a rien
     eu quand le passage ne change rien
  2. le worker survit à un `ALTER TABLE node_run ADD COLUMN` joué sous ses
     plans cachés : l'item admis juste après va jusqu'au terminal
  3. il l'a dit — l'invalidation du plan et la reconnexion sont dans son
     journal, sans traceback
  4. un run laissé `running` par un worker mort est classé par le worker
     suivant avec un post-mortem qui nomme le redémarrage ; le même run
     fauché par le worker qui l'a réservé garde « bail expiré »

Usage : uv run python tests/migration_test.py
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, kernel  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# comme le test de reconnexion : le test travaille dans un répertoire à lui,
# jamais dans ROOT/data, qui peut être le data/ vivant d'un rail
WORK = Path(tempfile.mkdtemp(prefix="graphatom-migration-test-"))
blocks.DATA_DIR = WORK / "data"
# hermétisme : ce dépôt-ci est le seul univers du test. L'agent qui nous
# lance a dans son environnement le clone et l'instance de la production —
# un ordonnanceur lancé d'ici n'en voit ni l'un ni l'autre.
os.environ["GRAPHATOM_REPO_DIR"] = str(ROOT)
os.environ.pop("GRAPHATOM_AGENT_DSN", None)
COLONNE = "migration_test_col"  # la colonne ajoutée sous le worker en vol
TIMEOUT_S = 60


def sh(*args: str) -> str:
    out = subprocess.run(
        ["uv", "run", "graphatom", *args],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def worker(*args: str, journal: Path | None = None) -> subprocess.Popen:
    """Un worker, lancé comme le crash-test : le binaire du venv, un groupe à lui.

    Le cwd est WORK, jamais ROOT : le `data/` d'un checkout est le workspace
    vivant d'un rail, et un worker de test n'y écrit pas.
    """
    return subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "graphatom"), *args],
        cwd=WORK, start_new_session=True,
        stdout=journal.open("w") if journal else subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def kill_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        pass
    proc.wait()


def terminal(conn, item_id: int) -> dict:
    """L'item, une fois terminal. Sortie en échec s'il n'y arrive pas."""
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s", (item_id,)
        ).fetchone()
        if item["terminal_at"] is not None:
            return item
        time.sleep(0.5)
    sys.exit(f"ÉCHEC : l'item {item_id} n'a pas terminé en {TIMEOUT_S} s")


def migration_annoncee() -> None:
    """1. Le journal du déploiement dit ce que le passage a changé."""
    neuve = sh("init-db", "--drop")
    assert "la forme des tables a changé" in neuve, neuve
    assert "+ node_run.candidate (integer)" in neuve, neuve
    print(f"1.1 base neuve : {neuve.splitlines()[1]} ✓")

    rejoue = sh("init-db")
    assert "aucun changement de forme" in rejoue, rejoue
    assert "a changé" not in rejoue, rejoue
    print(f"1.2 rejoué aussitôt : {rejoue.splitlines()[1]} ✓")

    # une migration pour de vrai : la colonne du fan-out retirée, le passage
    # de `schema.sql` la remet — c'est ce que joue un déploiement
    with db.connect() as conn:
        conn.execute("ALTER TABLE node_run DROP COLUMN candidate")
    migre = sh("init-db")
    assert "la forme des tables a changé — 1 colonne(s)" in migre, migre
    assert "+ node_run.candidate (integer)" in migre, migre
    print(f"1.3 colonne remise par le passage : {migre.splitlines()[2].strip()} ✓")


def survit_a_la_migration(conn, proc, revision: str, journal: Path) -> None:
    """2 et 3. La forme de node_run change sous les plans cachés du worker."""
    avant = int(sh("admit", revision, f"migration-avant:{os.getpid()}"))
    terminal(conn, avant)
    # un item entier, c'est six réservations : psycopg a préparé l'INSERT
    # ... RETURNING * de claim sur la connexion du worker
    print(f"2.1 item {avant} terminal : les plans du worker sont préparés ✓")

    conn.execute(f"ALTER TABLE node_run ADD COLUMN {COLONNE} INT")
    apres = int(sh("admit", revision, f"migration-apres:{os.getpid()}"))
    item = terminal(conn, apres)
    assert proc.poll() is None, "l'ordonnanceur est mort sur la migration"
    assert item["state"] == "close", item["state"]
    print(f"2.2 après l'ALTER : item {apres} {item['state']}, worker vivant ✓")

    texte = journal.read_text()
    lignes = [l for l in texte.splitlines() if "plan caché" in l]
    if not lignes:
        sys.exit("ÉCHEC : le worker n'a pas vu l'invalidation de ses plans")
    assert "reconnexion" in lignes[0], lignes[0]
    assert "Traceback" not in texte, texte[-2000:]
    print(f"3. invalidation nommée et reprise : {lignes[0][:96]}… ✓")


def en_vol(conn, revision: str, prefixe: str) -> dict:
    """Un run réservé par ce processus-ci, dont le bail vient d'expirer."""
    item_id = kernel.admit(conn, revision, f"{prefixe}:{os.getpid()}")
    run = kernel.claim(conn, item_id)
    assert run is not None, "rien à réserver"
    conn.execute(
        "UPDATE node_run SET lease_expires_at = now() - interval '1 second' "
        "WHERE id = %s", (run["id"],),
    )
    return run


def post_mortem(conn, run_id: int) -> dict:
    run = conn.execute(
        "SELECT * FROM node_run WHERE id = %s", (run_id,)
    ).fetchone()
    assert run["status"] == "faulted", run["status"]
    return run["result"]


def redemarrage_nomme(conn, revision: str) -> None:
    """4. Le faucheur d'un worker neuf ne met pas la panne sur le dos de l'agent."""
    perdu = en_vol(conn, revision, "migration-perdu")
    neuf = worker("tick")  # un worker fraîchement démarré : il n'a réservé aucun run
    assert neuf.wait(TIMEOUT_S) == 0, "le tick du worker neuf a échoué"
    poste = post_mortem(conn, perdu["id"])
    assert "redémarrage du worker" in poste["error"], poste
    assert "pas l'agent" in poste["error"], poste
    print(f"4.1 run {perdu['id']} fauché par un worker neuf : "
          f"« {poste['error']} » ✓")

    sien = en_vol(conn, revision, "migration-sien")
    kernel.reap(conn)  # le worker qui l'a réservé : rien n'a redémarré
    poste = post_mortem(conn, sien["id"])
    assert poste["error"].startswith("bail expiré, agent"), poste
    print(f"4.2 run {sien['id']} fauché par son propre worker : "
          f"« {poste['error']} » ✓")


def main() -> None:
    migration_annoncee()
    revision = sh("publish", "examples/supervision.json")

    journal = WORK / "migration_test.log"
    proc = worker("run", journal=journal)
    try:
        with db.connect() as conn:
            survit_a_la_migration(conn, proc, revision, journal)
    finally:
        kill_group(proc, signal.SIGTERM)

    with db.connect() as conn:  # ordonnanceur arrêté : le faucheur est à nous
        redemarrage_nomme(conn, revision)

    print("\nmigration : OK — le schéma bouge sous le worker, il encaisse et le dit")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
