"""Le test du verrou : une transaction oubliée par un tiers ne met rien à terre.

La panne du 2026-08-08, rejouée en entier : un client externe en lecture
seule laisse une transaction ouverte et inactive, elle tient un
`AccessShareLock` sur `subject`, la migration d'`init-db` demande son
`AccessExclusiveLock` derrière — et la file d'attente des verrous étant
FIFO, toutes les lectures s'empilent derrière la migration. Le site meurt
alors que la base et le worker vont bien.

Trois protections, trois constats, sur une base réelle :

  1. la transaction inactive meurt d'elle-même : passé le délai posé sur le
     rôle, la base termine la session, et le problème disparaît tout seul
  2. `init-db` bloqué par un verrou échoue vite, en nommant le pid et la
     requête de la session bloquante — jamais une attente sans limite
  3. le bloqueur parti de lui-même, un nouveau démarrage réussit : la
     reprise ne demande aucune intervention, et `pg_terminate_backend`
     n'est appelé nulle part ici

Le test travaille dans une base à lui, créée sur l'instance de
`GRAPHATOM_DSN` et droppée à la sortie : il tient des verrous et fait
mourir des sessions, ce qu'on ne fait pas dans une base partagée.

Usage : uv run python tests/verrou_test.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE = f"graphatom_verrou_test_{os.getpid()}"  # la base du test, à lui seul
IDLE_COURT = "3s"    # constat 1 : on attend ce délai, il reste court
IDLE_LONG = "20s"    # constats 2 et 3 : le bloqueur doit survivre à init-db
LOCK = "1s"          # ce que la migration attend, par essai
PORTE_S = 60         # le délai de la porte docker : init-db échoue bien avant


def maintenance() -> str:
    """La DSN du test, pointée sur la base de maintenance de son instance."""
    return make_conninfo(db.DSN, dbname="postgres")


def base_du_test(creer: bool) -> None:
    """Crée ou drop la base du test. Le reste du fichier ne voit qu'elle."""
    ordre = (sql.SQL("CREATE DATABASE {}") if creer
             else sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)"))
    with psycopg.connect(maintenance(), autocommit=True) as conn:
        conn.execute(ordre.format(sql.Identifier(BASE)))
    if creer:
        # `db.DSN` est lu par tout ce qui se connecte ici ; l'environnement
        # l'est par les `graphatom init-db` lancés en sous-processus
        db.DSN = os.environ["GRAPHATOM_DSN"] = make_conninfo(db.DSN, dbname=BASE)


def init_db(idle: str, lock: str = LOCK) -> subprocess.CompletedProcess:
    """`graphatom init-db`, avec les deux délais du passage. Ne lève pas."""
    env = {**os.environ,
           "GRAPHATOM_IDLE_TX_TIMEOUT": idle,
           "GRAPHATOM_LOCK_TIMEOUT": lock}
    return subprocess.run(
        ["uv", "run", "graphatom", "init-db"],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )


def tiers() -> tuple[psycopg.Connection, int]:
    """Un client tiers en lecture seule, qui oublie de refermer sa transaction.

    Ce n'est pas le rail : c'est le script de surveillance de l'opérateur,
    celui de la panne. `autocommit=False` : la lecture ouvre une transaction
    que personne ne referme, et le verrou reste tenu.
    """
    conn = psycopg.connect(db.DSN, autocommit=False, row_factory=dict_row)
    pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    # la lecture en dernier : c'est elle que `pg_stat_activity` gardera comme
    # dernière requête de la session, et donc elle que l'erreur doit nommer
    conn.execute("SELECT count(*) FROM subject")
    return conn, pid


def vivante(pid: int) -> bool:
    """La session `pid` est-elle encore là ? Constaté depuis une autre session."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT state FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
    return row is not None


def attend_la_fin(pid: int, delai_s: float) -> float:
    """Attend que la base termine la session. Rend le temps qu'elle a mis."""
    debut = time.time()
    while time.time() - debut < delai_s:
        if not vivante(pid):
            return time.time() - debut
        time.sleep(0.5)
    sys.exit(f"ÉCHEC : la session {pid} tient toujours après {delai_s} s")


def transaction_inactive_meurt() -> None:
    """1. Une transaction laissée inactive est terminée par la base."""
    out = init_db(IDLE_COURT)
    assert out.returncode == 0, out.stderr
    conn, pid = tiers()
    assert vivante(pid), "la session tierce n'est pas là"
    print(f"1.1 session tierce {pid} : transaction ouverte, plus rien ne bouge ✓")

    mis = attend_la_fin(pid, 20)
    assert not vivante(pid)
    try:  # la connexion est morte côté serveur : le tiers l'apprend à sa requête
        conn.execute("SELECT 1")
        sys.exit("ÉCHEC : la connexion tierce répond encore")
    except psycopg.Error as err:
        print(f"1.2 terminée par la base en {mis:.1f} s "
              f"(délai {IDLE_COURT}) — « {str(err).splitlines()[0][:64]} » ✓")
    finally:
        conn.close()


def migration_bloquee_echoue_vite(conn: psycopg.Connection, pid: int) -> None:
    """2. `init-db` bloqué échoue avant la porte docker, et nomme le bloqueur."""
    debut = time.time()
    out = init_db(IDLE_LONG)
    mis = time.time() - debut

    assert out.returncode != 0, f"init-db a réussi sous le verrou : {out.stdout}"
    assert mis < PORTE_S, f"init-db a mis {mis:.1f} s, la porte docker en donne {PORTE_S}"
    assert str(pid) in out.stderr, out.stderr
    assert "FROM subject" in out.stderr, out.stderr
    print(f"2.1 init-db a échoué en {mis:.1f} s (< {PORTE_S} s) : "
          f"« {out.stderr.strip().splitlines()[-1][:96]}… » ✓")


def la_reprise_est_automatique(conn: psycopg.Connection, pid: int) -> None:
    """3 et 5. Le bloqueur meurt seul, le démarrage suivant passe — rien à la main.

    `conn` n'est jamais refermée ici : c'est la base qui doit terminer la
    session, pas le test. Une connexion qu'on ferme soi-même prouverait
    seulement qu'un client bien élevé ne pose pas de problème.
    """
    mis = attend_la_fin(pid, 40)
    print(f"3.1 le bloqueur {pid} est parti seul en {mis:.1f} s, "
          f"sans pg_terminate_backend ✓")

    out = init_db(IDLE_LONG)
    assert out.returncode == 0, out.stderr
    assert "base initialisée" in out.stdout, out.stdout
    print(f"3.2 démarrage suivant, sans autre action : « {out.stdout.splitlines()[0]} » ✓")


def main() -> None:
    base_du_test(creer=True)
    transaction_inactive_meurt()
    # le délai long vaut dès la session suivante : le bloqueur doit tenir
    # pendant tout `init-db`, sinon la migration passerait sans rien prouver
    assert init_db(IDLE_LONG).returncode == 0
    conn, pid = tiers()
    migration_bloquee_echoue_vite(conn, pid)
    la_reprise_est_automatique(conn, pid)
    print("\nverrou : OK — le tiers oublie sa transaction, le site tient")


if __name__ == "__main__":
    try:
        main()
    finally:
        # la base du test s'en va avec ses délais : `DROP DATABASE` emporte
        # les réglages que le rôle portait pour elle
        base_du_test(creer=False)
