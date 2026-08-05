"""L'ordonnanceur : un seul processus, un tick à trois passes.

    1. reap    — bails expirés → révocation, crashed, routage
    2. wait    — réponses arrivées et échéances de WAIT, wall_deadline
    3. dispatch— pour chaque item actif sans run : claim → bloc → apply

Tuer ce processus n'importe quand est un cas nominal, pas une panne :
c'est le contrat que le crash-test vérifie.
"""

import time

import psycopg

from . import kernel
from .blocks import BLOCKS, Context
from .graph import load_bundle


def tick(conn: psycopg.Connection) -> int:
    did = kernel.reap(conn)
    did += _settle_waits(conn)
    did += _dispatch(conn)
    return did


def run_forever(poll_s: float = 0.5) -> None:
    from .db import connect

    with connect() as conn:
        while True:
            if tick(conn) == 0:
                time.sleep(poll_s)


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
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s", (row["id"],)
        ).fetchone()
        bundle = load_bundle(conn, item["revision"])
        node = bundle["nodes"][run["node"]]
        try:
            result = BLOCKS[node["block"]](Context(conn, run, item, node, bundle))
        except Exception as exc:  # le bloc a le droit d'échouer, pas de router
            result = {"outcome": "crashed", "error": str(exc)}
        kernel.apply(conn, run["id"], result)
    return n
