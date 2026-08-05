"""Le noyau : admission, réservation, et l'unique transition apply().

Le cycle de vie d'un nœud :

    item.state = N, aucun run actif
      → claim()   : réservation transactionnelle — run créé, fence bumpé,
                    version attendue épinglée, bail posé
      → le bloc s'exécute, hors transaction
      → apply()   : valide le résultat, route par l'arête déclarée,
                    version++, événement — le changement de nœud
      → item.state = M

Toute mutation d'état d'item passe par _route() sous verrou : résultat de
bloc (apply), réponse humaine ou échéance (apply_item), faucheur (reap).
Rien d'autre ne bouge un item.
"""

import datetime as dt
import json

import psycopg

from .graph import KERNEL_OUTCOMES, load_bundle

LEASE_SECONDS = 30
MAX_ATTEMPTS = 3  # défaut central : réessayer, puis escalader

UTC = dt.timezone.utc


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


# ---------------------------------------------------------------- admission


def admit(conn: psycopg.Connection, revision: str, subject_key: str) -> int:
    """Crée (ou retrouve) le sujet, ouvre une occurrence — si la lignée le permet."""
    with conn.transaction():
        bundle = load_bundle(conn, revision)
        subject = conn.execute(
            "INSERT INTO subject (graph, subject_key) VALUES (%s, %s) "
            "ON CONFLICT (graph, subject_key) DO UPDATE SET graph = EXCLUDED.graph "
            "RETURNING id, lineage_budget",
            (bundle["name"], subject_key),
        ).fetchone()

        active = conn.execute(
            "SELECT id FROM work_item WHERE subject_id = %s AND terminal_at IS NULL",
            (subject["id"],),
        ).fetchone()
        if active:
            raise RuntimeError(f"occurrence active existante : item {active['id']}")

        if subject["lineage_budget"] <= 0:
            raise RuntimeError("budget de lignée épuisé — ré-admission refusée")
        conn.execute(
            "UPDATE subject SET lineage_budget = lineage_budget - 1 WHERE id = %s",
            (subject["id"],),
        )

        generation = conn.execute(
            "SELECT count(*) AS n FROM work_item WHERE subject_id = %s",
            (subject["id"],),
        ).fetchone()["n"] + 1

        budgets = bundle["budgets"]
        deadline = now() + dt.timedelta(hours=budgets["wall_deadline_hours"])
        item = conn.execute(
            "INSERT INTO work_item (subject_id, generation, revision, state, "
            "escalations, wall_deadline) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (subject["id"], generation, revision, bundle["entry"],
             budgets["escalations"], deadline),
        ).fetchone()
        conn.execute(
            "INSERT INTO event (item_id, item_version, kind, to_state) "
            "VALUES (%s, 1, 'admitted', %s)",
            (item["id"], bundle["entry"]),
        )
        conn.execute("UPDATE work_item SET version = 1 WHERE id = %s", (item["id"],))
        return item["id"]


# --------------------------------------------------------------- réservation


def claim(conn: psycopg.Connection, item_id: int) -> dict | None:
    """Réserve un run pour l'état courant de l'item. None si rien à faire."""
    with conn.transaction():
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        if item is None or item["terminal_at"] is not None:
            return None
        bundle = load_bundle(conn, item["revision"])
        node = bundle["nodes"][item["state"]]
        if node.get("terminal") or node["block"] == "WAIT":
            return None
        running = conn.execute(
            "SELECT id FROM node_run WHERE item_id = %s AND status = 'running'",
            (item_id,),
        ).fetchone()
        if running:
            return None

        attempt = conn.execute(
            "SELECT count(*) AS n FROM node_run WHERE item_id = %s AND node = %s",
            (item_id, item["state"]),
        ).fetchone()["n"] + 1
        fence = item["fence"] + 1
        conn.execute("UPDATE work_item SET fence = %s WHERE id = %s", (fence, item_id))
        run = conn.execute(
            "INSERT INTO node_run (item_id, node, attempt, status, fence, "
            "expected_version, lease_expires_at) "
            "VALUES (%s, %s, %s, 'running', %s, %s, %s) RETURNING *",
            (item_id, item["state"], attempt, fence, item["version"],
             now() + dt.timedelta(seconds=LEASE_SECONDS)),
        ).fetchone()
        return run


# ---------------------------------------------------------------- transition


def apply(conn: psycopg.Connection, run_id: int, submitted: dict) -> str:
    """L'unique transition pour un résultat de run. Retourne le statut du run."""
    with conn.transaction():
        run = conn.execute(
            "SELECT * FROM node_run WHERE id = %s FOR UPDATE", (run_id,)
        ).fetchone()
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (run["item_id"],)
        ).fetchone()

        # rejets : chacun est une transition durable du run
        if run["status"] != "running":
            return run["status"]
        for status, bad in (("superseded", run["fence"] != item["fence"]),
                            ("stale", run["expected_version"] != item["version"])):
            if bad:
                conn.execute(
                    "UPDATE node_run SET status = %s, result = %s WHERE id = %s",
                    (status, json.dumps(submitted), run_id),
                )
                return status

        bundle = load_bundle(conn, item["revision"])
        node = bundle["nodes"][run["node"]]
        outcome = submitted.get("outcome")
        if outcome not in (node.get("edges") or {}) and outcome not in KERNEL_OUTCOMES:
            outcome = "invalid_result"

        conn.execute(
            "UPDATE node_run SET status = 'applied', outcome = %s, result = %s "
            "WHERE id = %s",
            (outcome, json.dumps(submitted), run_id),
        )
        _route(conn, item, bundle, run, outcome, kind="result")
        return "applied"


def apply_item(conn: psycopg.Connection, item_id: int, outcome: str, kind: str) -> None:
    """Transition sans run : réponse humaine, échéance de WAIT, wall_deadline."""
    with conn.transaction():
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        if item["terminal_at"] is not None:
            return
        bundle = load_bundle(conn, item["revision"])
        run = {"node": item["state"], "id": None, "attempt": 0}
        _route(conn, item, bundle, run, outcome, kind=kind)


def _route(conn, item, bundle, run, outcome: str, kind: str) -> None:
    """Résout l'arête, débite l'escalade, écrit l'événement, bouge l'item.

    Toujours appelé sous verrou de l'item, dans la transaction de l'appelant.
    """
    nodes = bundle["nodes"]
    node = nodes[run["node"]]
    on_kernel = bundle["on_kernel"]

    if outcome in (node.get("edges") or {}):
        target = node["edges"][outcome]
    elif outcome in ("crashed", "timed_out", "invalid_result"):
        # défaut central : réessayer sur place, puis escalader
        if run.get("attempt", 0) < MAX_ATTEMPTS:
            target = run["node"]
        else:
            target = on_kernel["escalate_to"]
    else:  # budget_exhausted, wall_deadline
        target = on_kernel["exhausted_to"]

    if nodes[target].get("escalade") and target != run["node"]:
        if item["escalations"] <= 0:
            outcome, target = "budget_exhausted", on_kernel["exhausted_to"]
        else:
            conn.execute(
                "UPDATE work_item SET escalations = escalations - 1 WHERE id = %s",
                (item["id"],),
            )

    version = item["version"] + 1
    conn.execute(
        "INSERT INTO event (item_id, item_version, kind, from_state, to_state, "
        "outcome, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (item["id"], version, kind, item["state"], target, outcome, run["id"]),
    )
    terminal = now() if nodes[target].get("terminal") else None
    conn.execute(
        "UPDATE work_item SET state = %s, version = %s, terminal_at = %s WHERE id = %s",
        (target, version, terminal, item["id"]),
    )

    # un WAIT s'arme dans la même transaction que l'entrée dans l'état :
    # sinon la réponse peut arriver avant la souscription (réveil perdu)
    if not terminal and nodes[target]["block"] == "WAIT":
        cfg = nodes[target]["config"]
        conn.execute(
            "INSERT INTO question (item_id, node, text, options, owner, deadline) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (item["id"], target, cfg["question"], json.dumps(cfg["options"]),
             cfg["owner"], now() + dt.timedelta(minutes=cfg["deadline_minutes"])),
        )


# ------------------------------------------------------------------ faucheur


def reap(conn: psycopg.Connection) -> int:
    """Runs au bail expiré : révoque (fence++), classe crashed, route."""
    expired = conn.execute(
        "SELECT id FROM node_run WHERE status = 'running' AND lease_expires_at < %s",
        (now(),),
    ).fetchall()
    for row in expired:
        with conn.transaction():
            run = conn.execute(
                "SELECT * FROM node_run WHERE id = %s FOR UPDATE", (row["id"],)
            ).fetchone()
            if run["status"] != "running":
                continue
            item = conn.execute(
                "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (run["item_id"],)
            ).fetchone()
            conn.execute(  # révocation d'autorité : un zombie ne peut plus appliquer
                "UPDATE work_item SET fence = fence + 1 WHERE id = %s", (item["id"],)
            )
            conn.execute(
                "UPDATE node_run SET status = 'faulted', outcome = 'crashed' "
                "WHERE id = %s", (run["id"],),
            )
            bundle = load_bundle(conn, item["revision"])
            _route(conn, item, bundle, run, "crashed", kind="reaped")
    return len(expired)
