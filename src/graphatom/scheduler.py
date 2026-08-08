"""L'ordonnanceur : un seul processus, un tick à trois passes.

    0. beat    — le battement du worker, tamponné avant le travail
    1. reap    — bails expirés → révocation, timed_out ou crashed, routage
    2. wait    — réponses arrivées et échéances de WAIT, wall_deadline
    3. dispatch— pour chaque item actif sans run : claim → bloc → apply

Le battement est écrit dans le tick, comme le reste : pas de thread dédié,
pas de timer. Il ne compte pas comme du travail — un rail au repos bat
quand même. Ce que le worker ne peut plus dire quand il meurt, son silence
le dit à sa place : voir `heartbeat`.

Chaque bloc s'exécute dans son propre thread avec sa propre connexion :
un agent qui travaille dix minutes ne bloque ni le faucheur ni les
autres items. claim() garantit qu'un item n'a qu'un run à la fois.

Tuer ce processus n'importe quand est un cas nominal, pas une panne :
c'est le contrat que le crash-test vérifie. Perdre la base l'est aussi :
la boucle se reconnecte avec un backoff borné et reprend où elle en est.
"""

import threading
import time

import psycopg

from . import heartbeat, kernel
from .blocks import BLOCKS, Context
from .graph import load_bundle

RECONNECT_MAX_S = 30.0  # plafond du backoff : une base absente n'est jamais abandonnée


def tick(conn: psycopg.Connection) -> int:
    heartbeat.beat(conn, heartbeat.RAIL)
    did = kernel.reap(conn)
    did += _settle_waits(conn)
    did += _dispatch(conn)
    return did


def run_forever(poll_s: float = 0.5) -> None:
    """Ticks à l'infini — une coupure de la base est un incident nominal.

    Postgres qui disparaît (redémarrage, docker, réseau) ne tue pas le
    worker : on ferme la connexion morte, on attend 1 s, 2 s, 4 s… plafonné
    à RECONNECT_MAX_S, on en rouvre une et on reprend les ticks. Rien n'est
    perdu : tout l'état est dans la base, et le faucheur rattrape au retour
    les runs restés orphelins.

    Seule l'OperationalError est rattrapée. Toute autre exception fait
    crasher le processus, bruyamment : elle n'était pas attendue.
    """
    from .db import connect

    wait_s = 1.0
    while True:
        try:
            with connect() as conn:
                while True:
                    did = tick(conn)
                    wait_s = 1.0  # un tick passé : la base répond, on repart de 1 s
                    if did == 0:
                        time.sleep(poll_s)
        except psycopg.OperationalError as exc:
            print(f"base injoignable : {exc} — reconnexion dans {wait_s:.0f}s",
                  flush=True)
            time.sleep(wait_s)
            wait_s = min(wait_s * 2, RECONNECT_MAX_S)


def _settle_waits(conn: psycopg.Connection) -> int:
    n = 0
    answered = conn.execute(
        "SELECT q.*, w.state AS item_state FROM question q "
        "JOIN work_item w ON w.id = q.item_id "
        "WHERE q.state = 'answered' AND w.terminal_at IS NULL AND w.state = q.node"
    ).fetchall()
    for q in answered:
        with conn.transaction():
            conn.execute("UPDATE question SET state = 'closed' WHERE id = %s", (q["id"],))
            kernel.apply_item(conn, q["item_id"], q["answer"], kind="answer")
        n += 1

    expired = conn.execute(
        "SELECT q.* FROM question q JOIN work_item w ON w.id = q.item_id "
        "WHERE q.state = 'open' AND q.deadline < now() "
        "AND w.terminal_at IS NULL AND w.state = q.node"
    ).fetchall()
    for q in expired:
        with conn.transaction():
            conn.execute("UPDATE question SET state = 'expired' WHERE id = %s", (q["id"],))
            kernel.apply_item(conn, q["item_id"], "expired", kind="deadline")
        n += 1

    walled = conn.execute(
        "SELECT id FROM work_item WHERE terminal_at IS NULL AND wall_deadline < now()"
    ).fetchall()
    for row in walled:
        kernel.apply_item(conn, row["id"], "wall_deadline", kind="wall")
        n += 1
    return n


def _execute(run_id: int, item_id: int) -> None:
    """Un bloc, un thread, une connexion. L'issue est appliquée à la fin."""
    from .db import connect

    with connect() as conn:
        run = conn.execute("SELECT * FROM node_run WHERE id = %s", (run_id,)).fetchone()
        item = conn.execute("SELECT * FROM work_item WHERE id = %s", (item_id,)).fetchone()
        bundle = load_bundle(conn, item["revision"])
        node = bundle["nodes"][run["node"]]
        try:
            result = BLOCKS[node["block"]](Context(conn, run, item, node, bundle))
        except Exception as exc:  # le bloc a le droit d'échouer, pas de router
            result = {"outcome": "crashed", "error": str(exc)}
        kernel.apply(conn, run_id, result)


def _dispatch(conn: psycopg.Connection) -> int:
    items = conn.execute(
        "SELECT id FROM work_item WHERE terminal_at IS NULL ORDER BY id"
    ).fetchall()
    n = 0
    for row in items:
        run = kernel.claim(conn, row["id"])
        if run is None:
            continue
        n += 1
        threading.Thread(
            target=_execute, args=(run["id"], row["id"]), daemon=True
        ).start()
    return n
