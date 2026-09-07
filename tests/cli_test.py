"""Le test de la CLI `graphatom` : chaque commande en sous-processus, sur sa base jetable.

Ce que la ligne de commande promet, code de sortie et message compris :

  1. `init-db` annonce ce que la migration a changé, puis qu'elle n'a rien
     changé ; bloquée par un verrou tenu ailleurs, elle échoue vite en
     nommant le bloqueur — pid, état, dernière requête
  2. `publish` rend la révision, `admit` le numéro d'item, `items` et
     `journal` lisent la base telle quelle
  3. `questions` montre la question ouverte ; `answer` refuse l'inconnue,
     l'option hors contrat, la question déjà réglée — et une réponse valide
     n'avance pas l'item : c'est `tick` qui route
  4. `drop-agent-db` refuse une base sans le préfixe jetable, refuse sans
     instance jetable, et drop la base de l'item quand tout est là
  5. `github-sync` et `split-close` sans GITHUB_TOKEN s'arrêtent avant tout
     effet ; `build-quota` sans commande est une erreur d'usage, avec une
     commande rend son code de sortie
  6. `serve` écoute où on lui dit, signe du nom donné ; `run` bat en base

Usage : uv run python tests/cli_test.py
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

# web_outils épingle GRAPHATOM_DSN sur la base jetable : il passe avant graphatom
from web_outils import (  # noqa: E402
    BUNDLE_VITRINE, OPTIONS, ROOT, TMP, appel, avancer, port_libre, question_de,
)
from outils import attendre, etat  # noqa: E402

from graphatom import db, heartbeat  # noqa: E402

# ni jeton GitHub ni instance jetable : chaque preuve pose ce qu'elle veut
ENV = {k: v for k, v in os.environ.items()
       if k not in ("GITHUB_TOKEN", "GRAPHATOM_AGENT_DSN")}
JETABLE = f"{db.AGENT_DB_PREFIX}777"


# le point d'entrée installé — celui que `uv run graphatom` lance — dans le venv du test
GRAPHATOM = str(Path(sys.executable).with_name("graphatom"))


def cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """`graphatom <args>` par le point d'entrée installé, sortie capturée, ne lève pas."""
    return subprocess.run([GRAPHATOM, *args], cwd=ROOT, capture_output=True, text=True,
                          env=env or ENV)


def lignes(out: subprocess.CompletedProcess) -> list[str]:
    return out.stdout.strip().splitlines()


def refus(out: subprocess.CompletedProcess, message: str) -> None:
    assert out.returncode == 1 and out.stderr.strip() == message, (out.returncode, out.stderr)


def demon(*args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "graphatom.cli", *args], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=ENV, start_new_session=True)


def arret(proc: subprocess.Popen) -> str:
    """SIGINT au démon, et sa sortie. Sous coverage et sous charge, l'arrêt
    prend le temps d'écrire ses données : le budget est large, pas la preuve."""
    os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    try:
        return proc.communicate(timeout=120)[0]
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        raise AssertionError("le démon ne s'arrête pas sur SIGINT en deux minutes")


# ------------------------------------------------------------------ preuves


def preuve_init_db() -> None:
    """1. la migration dit ce qu'elle change ; bloquée, elle nomme le bloqueur."""
    neuve = lignes(cli("init-db", "--drop"))
    assert neuve[0] == "base initialisée", neuve
    colonnes = [l for l in neuve if l.startswith("  + ")]
    assert neuve[1] == f"migration : la forme des tables a changé — {len(colonnes)} colonne(s)"
    assert "  + question.options (jsonb)" in colonnes, colonnes
    assert lignes(cli("init-db")) == ["base initialisée", "migration : aucun changement de forme"]

    tiers = psycopg.connect(db.DSN, autocommit=False, row_factory=dict_row)
    pid = tiers.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    tiers.execute("SELECT count(*) FROM subject")  # une transaction jamais refermée
    try:
        bloquee = cli("init-db", env=ENV | {"GRAPHATOM_LOCK_TIMEOUT": "200ms"})
    finally:
        tiers.close()
    assert bloquee.returncode == 1 and bloquee.stdout == "", bloquee
    assert bloquee.stderr.strip() == (
        f"migration abandonnée après {db.DDL_ESSAIS} essais — verrou tenu par "
        f"pid {pid} (idle in transaction) : SELECT count(*) FROM subject"), bloquee.stderr
    assert lignes(cli("init-db"))[0] == "base initialisée", "le bloqueur parti, elle passe"
    print("1. init-db : colonnes annoncées, rien la seconde fois, bloqueur nommé ✓")


def preuve_lecture(conn) -> tuple[int, str]:
    """2. publish, admit, items, journal : la base telle quelle."""
    bundle = TMP / "vitrine.json"
    bundle.write_text(json.dumps(BUNDLE_VITRINE))
    rev = cli("publish", str(bundle)).stdout.strip()
    assert len(rev) == 64 and conn.execute(
        "SELECT name FROM graph_revision WHERE id = %s", (rev,)).fetchone()["name"] == "web-vitrine"
    item_id = int(cli("admit", rev, "cli:sujet").stdout)
    assert etat(conn, item_id)["state"] == "ingest"
    assert lignes(cli("items")) == [f"#{item_id} cli:sujet g1 état=ingest v1 esc=2 actif"]
    admis = conn.execute("SELECT at FROM event WHERE item_id = %s", (item_id,)).fetchone()
    assert lignes(cli("journal", str(item_id))) == [
        f"v 1 {admis['at']:%H:%M:%S} admitted  ingest"]
    print("2. publish → révision, admit → item, items et journal lus en base ✓")
    return item_id, rev


def preuve_reponse(conn, item_id: int) -> None:
    """3. questions et answer : les refus, la réponse, et le tick qui route."""
    for submitted in ({"outcome": "ok"}, {"outcome": "ok"}, {"outcome": "pass"}):
        avancer(conn, item_id, submitted)
    q = question_de(conn, item_id)
    assert lignes(cli("questions")) == [
        f"[{q['id']}] item {item_id} → test (avant {q['deadline']:%H:%M})",
        "    On livre ?", f"    options : {', '.join(OPTIONS)}"]
    refus(cli("answer", str(q["id"] + 1000), "go"), "question inconnue")
    refus(cli("answer", str(q["id"]), "bof"), f"option invalide — attendu : {', '.join(OPTIONS)}")
    assert question_de(conn, item_id)["state"] == "open", "un refus n'écrit pas"
    prise = cli("answer", str(q["id"]), "go", "--by", "cli-test")
    assert lignes(prise) == ["réponse enregistrée — le waiter routera au prochain tick"], prise
    q = question_de(conn, item_id)
    assert (q["state"], q["answer"], q["answered_by"]) == ("answered", "go", "cli-test"), q
    assert etat(conn, item_id)["state"] == "attend", "answer n'avance pas l'item"
    refus(cli("answer", str(q["id"]), "stop"), "question déjà réglée (answered)")
    assert lignes(cli("questions")) == [], "plus rien d'ouvert"
    assert lignes(cli("tick")) == ["1"], "un tick : la réponse routée"
    fini = etat(conn, item_id)
    assert fini["state"] == "fini" and lignes(cli("items")) == [
        f"#{item_id} cli:sujet g1 état=fini v{fini['version']} esc=2 "
        f"{fini['terminal_at']:%H:%M:%S}"]
    dernier = conn.execute("SELECT * FROM event WHERE item_id = %s ORDER BY item_version DESC",
                           (item_id,)).fetchone()
    assert lignes(cli("journal", str(item_id)))[-1] == \
        f"v {dernier['item_version']} {dernier['at']:%H:%M:%S} answer    attend → fini [go]"
    print("3. questions, answer refuse et enregistre, tick route, journal complet ✓")


def bases() -> set[str]:
    with psycopg.connect(db.DSN, autocommit=True) as conn:
        return {r[0] for r in conn.execute("SELECT datname FROM pg_database")}


def preuve_drop_agent_db() -> None:
    """4. drop-agent-db : deux garde-fous, puis le vrai drop."""
    refus(cli("drop-agent-db"), "« graphatom » n'est pas une base jetable — rien de droppé")
    dsn_jetable = make_conninfo(db.DSN, dbname=JETABLE)
    refus(cli("drop-agent-db", env=ENV | {"GRAPHATOM_DSN": dsn_jetable}),
          "GRAPHATOM_AGENT_DSN absente — pas d'instance où dropper")
    os.environ["GRAPHATOM_AGENT_DSN"] = db.DSN
    assert db.agent_dsn(777) == dsn_jetable and JETABLE in bases()
    del os.environ["GRAPHATOM_AGENT_DSN"]
    droppee = cli("drop-agent-db", env=ENV | {"GRAPHATOM_DSN": dsn_jetable,
                                              "GRAPHATOM_AGENT_DSN": db.DSN})
    assert lignes(droppee) == [f"base jetable droppée : {JETABLE}"], droppee
    assert JETABLE not in bases() and "graphatom" in bases()
    print("4. drop-agent-db : préfixe exigé, instance exigée, la base de l'item droppée ✓")


def preuve_sans_jeton(conn) -> None:
    """5. sans GITHUB_TOKEN, rien ne part ; build-quota exige une commande."""
    revisions = conn.execute("SELECT count(*) AS n FROM graph_revision").fetchone()["n"]
    refus(cli("github-sync", "--repo", "o/r", "--graph", str(TMP / "vitrine.json")),
          "GITHUB_TOKEN manquant")
    refus(cli("split-close", "--repo", "o/r", "--mother", "1", "--children", "2", "3"),
          "GITHUB_TOKEN manquant")
    assert conn.execute("SELECT count(*) AS n FROM graph_revision").fetchone()["n"] == revisions, \
        "le canal s'arrête avant de publier quoi que ce soit"
    sans = cli("build-quota")
    assert sans.returncode == 2 and sans.stderr.strip().endswith(
        "graphatom: error: build-quota demande une commande après --"), sans.stderr
    assert cli("build-quota", "--").returncode == 2, "`--` seul n'est pas une commande"
    avec = cli("build-quota", "--", sys.executable, "-c", "import sys; sys.exit(7)",
               env=ENV | {"GRAPHATOM_QUOTA_DSN": db.DSN, "GRAPHATOM_WORKSPACE": str(TMP)})
    assert avec.returncode == 7, (avec.returncode, avec.stderr)
    print("5. github-sync / split-close → GITHUB_TOKEN manquant ; build-quota → usage, ou le code ✓")


def preuve_demons(conn) -> None:
    """6. serve écoute et signe ; run bat en base."""
    port = port_libre()
    serveur = demon("serve", "--port", str(port), "--by", "cli-serve")
    base = f"http://127.0.0.1:{port}/"

    def repond():
        try:
            return appel(base, "GET", "/api/questions")[0] == 200
        except OSError:
            return False
    assert attendre(repond, 20), "serve n'écoute pas sur le port demandé"
    assert len(json.loads(appel(base, "GET", "/api/questions")[2])["token"]) == 32
    sortie = arret(serveur)
    assert f"canal humain sur {base} (réponses signées « cli-serve »)" in sortie, sortie

    conn.execute("DELETE FROM heartbeat")
    rail = demon("run")
    assert attendre(lambda: heartbeat.last(conn, heartbeat.RAIL) is not None, 20), \
        "run ne tamponne pas son battement"
    arret(rail)
    assert rail.returncode != 0, "interrompu, le worker ne feint pas une sortie propre"
    print("6. serve écoute sur --port et signe --by ; run bat en base ✓")


def main() -> None:
    preuve_init_db()
    with db.connect() as conn:
        item_id, _rev = preuve_lecture(conn)
        preuve_reponse(conn, item_id)
        preuve_drop_agent_db()
        preuve_sans_jeton(conn)
        preuve_demons(conn)
    time.sleep(0.1)
    print("\ncli : OK — chaque commande dit ce qu'elle a fait, ou pourquoi elle refuse")


if __name__ == "__main__":
    main()
