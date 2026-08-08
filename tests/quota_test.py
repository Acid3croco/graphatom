"""Le quota global des opérations lourdes, sur une base PostgreSQL réelle.

Les preuves reprennent les critères de l'issue : plafond entre items, attente
sans tentative ni bail perdu, libération après échec ou révocation, et mort
de la session détentrice. Le test crée sa propre base car il termine une
session PostgreSQL et ne doit pas toucher les runs de son item hôte.

Usage : uv run python tests/quota_test.py
"""

import datetime as dt
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, graph, kernel, quota  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = os.environ.get("GRAPHATOM_AGENT_DSN") or db.DSN
BASE = f"graphatom_quota_test_{os.getpid()}"
DSN = make_conninfo(INSTANCE, dbname=BASE)
PYTHON = sys.executable
PROCS: list[subprocess.Popen] = []


def base(creer: bool) -> None:
    ordre = (sql.SQL("CREATE DATABASE {}") if creer
             else sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)"))
    with psycopg.connect(make_conninfo(INSTANCE, dbname="postgres"),
                         autocommit=True) as conn:
        conn.execute(ordre.format(sql.Identifier(BASE)))
    if creer:
        db.DSN = os.environ["GRAPHATOM_DSN"] = DSN
        db.init_db()


def lance(item_id: int, secondes: float = 0.5, code: int = 0,
          quota_n: int = 2, run: dict | None = None) -> subprocess.Popen:
    workspace = Path(tempfile.mkdtemp(prefix=f"quota-item-{item_id}-"))
    commande = ("import sys,time; print('travail', flush=True); "
                f"time.sleep({secondes}); sys.exit({code})")
    env = {**os.environ,
           "GRAPHATOM_QUOTA_DSN": DSN,
           "GRAPHATOM_MAX_BUILDS": str(quota_n),
           "GRAPHATOM_ITEM_ID": str(item_id),
           "GRAPHATOM_WORKSPACE": str(workspace),
           "GRAPHATOM_LEASE_S": "600"}
    if run:
        env["GRAPHATOM_RUN_ID"] = str(run["id"])
    proc = subprocess.Popen(
        [PYTHON, "-m", "graphatom.cli", "build-quota", "--",
         PYTHON, "-c", commande],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    PROCS.append(proc)
    return proc


def attend_charge(conn, n: int, delai_s: float = 5) -> None:
    limite = time.monotonic() + delai_s
    while time.monotonic() < limite:
        if charge(conn) == n:
            return
        time.sleep(0.05)
    raise AssertionError(f"charge {charge(conn)} au lieu de {n}")


def charge(conn) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' "
        "AND granted AND classid = %s AND objsubid = 2",
        (quota.BUILD_LOCK_NAMESPACE,),
    ).fetchone()["n"]


def termine(proc: subprocess.Popen, delai_s: float = 8) -> str:
    try:
        out, _ = proc.communicate(timeout=delai_s)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=8)
        raise AssertionError(f"preneur jamais terminé : {out}")
    return out


def bundle() -> dict:
    return {
        "name": "quota", "entry": "travail",
        "budgets": {"escalations": 1, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {"block": "ACT", "config": {"lease_s": 600},
                        "edges": {"ok": "fini"}},
            "fini": {"terminal": True}, "abandon": {"terminal": True},
        },
    }


def plafond_global(conn) -> None:
    """1. Plus de candidats que de places, jamais plus de N dépenses."""
    procs = [lance(100 + k, secondes=1.0, quota_n=2) for k in range(5)]
    maximum = 0
    while any(p.poll() is None for p in procs):
        maximum = max(maximum, charge(conn))
        assert maximum <= 2, maximum
        time.sleep(0.05)
    assert maximum == 2, maximum
    assert all(p.returncode == 0 for p in procs)
    print("1. cinq candidats de cinq items, quota 2 : maximum observé 2 ✓")


def attente_gratuite(conn) -> None:
    """2. Le run qui attend garde sa tentative et reçoit un bail neuf."""
    rev = graph.publish(conn, bundle())
    item_id = kernel.admit(conn, rev, f"quota:{uuid.uuid4().hex}")
    run = kernel.claim(conn, item_id)
    conn.execute("UPDATE node_run SET lease_expires_at = now() + interval '1 second' "
                 "WHERE id = %s", (run["id"],))

    holder = lance(800001, secondes=5, quota_n=1)
    attend_charge(conn, 1)
    waiter = lance(item_id, secondes=0.1, quota_n=1, run=run)
    time.sleep(2)
    relu = conn.execute("SELECT * FROM node_run WHERE id = %s", (run["id"],)).fetchone()
    assert relu["attempt"] == 1 and relu["status"] == "running", relu
    assert relu["lease_expires_at"] > dt.datetime.now(dt.timezone.utc), relu
    holder.send_signal(signal.SIGTERM)
    termine(holder)
    assert waiter.wait(timeout=8) == 0, termine(waiter)
    print("2. attente forcée : tentative 1, run actif, bail prolongé, puis succès ✓")


def rendue_sur_toutes_les_fins(conn) -> None:
    """3 et 4. Échec, révocation et session morte rendent chaque place."""
    failed = lance(810001, secondes=0.1, code=7, quota_n=1)
    assert failed.wait(timeout=5) == 7, termine(failed)
    suivant = lance(810002, secondes=0.1, quota_n=1)
    assert suivant.wait(timeout=5) == 0, termine(suivant)
    print("3. commande en échec : code 7 conservé, place rendue au suivant ✓")

    holder = lance(820001, secondes=20, quota_n=1)
    attend_charge(conn, 1)
    waiter = lance(820002, secondes=0.1, quota_n=1)
    holder.send_signal(signal.SIGTERM)
    termine(holder)
    assert waiter.wait(timeout=8) == 0, termine(waiter)
    print("   candidat révoqué par SIGTERM : place rendue au suivant ✓")

    holder = lance(830001, secondes=20, quota_n=1)
    attend_charge(conn, 1)
    pid = conn.execute(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
        "AND classid = %s AND objsubid = 2 LIMIT 1",
        (quota.BUILD_LOCK_NAMESPACE,),
    ).fetchone()["pid"]
    waiter = lance(830002, secondes=0.1, quota_n=1)
    conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
    assert waiter.wait(timeout=8) == 0, termine(waiter)
    assert holder.wait(timeout=8) == 75, termine(holder)
    print(f"4. session PostgreSQL {pid} tuée : dépense arrêtée, place reprise ✓")


def equite(conn) -> None:
    """Le même item ne prend pas toutes les places quand N vaut deux."""
    premier = lance(840001, secondes=2, quota_n=2)
    attend_charge(conn, 1)
    meme_item = lance(840001, secondes=0.1, quota_n=2)
    autre_item = lance(840002, secondes=0.1, quota_n=2)
    assert autre_item.wait(timeout=3) == 0, termine(autre_item)
    assert meme_item.poll() is None, "le même item a pris la place réservée au voisin"
    premier.send_signal(signal.SIGTERM)
    termine(premier)
    assert meme_item.wait(timeout=5) == 0, termine(meme_item)
    print("5. quota 2 : un item garde au plus une place, un voisin avance ✓")


def main() -> None:
    base(creer=True)
    with db.connect() as conn:
        plafond_global(conn)
        attente_gratuite(conn)
        rendue_sur_toutes_les_fins(conn)
        equite(conn)
    print("\nquota : OK — plafond global, attente gratuite et sessions sûres")


if __name__ == "__main__":
    try:
        main()
    finally:
        for proc in PROCS:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        base(creer=False)
