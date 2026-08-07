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

Deux compteurs bornent un item, et ils ne se confondent jamais. Les
tentatives par nœud sont un amortisseur local : elles se comptent sur le
passage courant, et une réponse humaine d'escalade en ouvre un nouveau —
« nouveau cycle, pleine marge ». Le budget d'escalades de l'item, lui, ne
se régénère jamais : c'est lui, et lui seul, qui termine. Il compte les
tours de boucle, pas les traversées : la première visite d'un nœud
d'escalade dans le passage courant est gratuite, la re-entrée décompte —
un item au budget épuisé finit donc son chemin nominal, il ne peut juste
plus boucler.
"""

import datetime as dt
import json

import psycopg

from .blocks import revoke_orphan
from .graph import KERNEL_OUTCOMES, load_bundle

LEASE_SECONDS = 30
MAX_ATTEMPTS = 3  # défaut central, par passage : réessayer, puis escalader

UTC = dt.timezone.utc


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


# ---------------------------------------------------------------- admission


def admit(conn: psycopg.Connection, revision: str, subject_key: str,
          title: str | None = None) -> int:
    """Crée (ou retrouve) le sujet, ouvre une occurrence — si la lignée le permet.

    Le titre est celui que le canal a sous la main au moment de l'admission :
    il est stocké là, une fois, et personne n'ira le rechercher ailleurs. Un
    sujet sans titre — un autre canal, un autre format — reste sans titre.
    """
    with conn.transaction():
        bundle = load_bundle(conn, revision)
        subject = conn.execute(
            "INSERT INTO subject (graph, subject_key, title) VALUES (%s, %s, %s) "
            "ON CONFLICT (graph, subject_key) DO UPDATE "
            "SET title = COALESCE(EXCLUDED.title, subject.title) "
            "RETURNING id, lineage_budget",
            (bundle["name"], subject_key, title),
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

        # les tentatives se comptent sur le passage courant : celles qu'un
        # passage précédent a brûlées restent en base, plus au décompte
        attempt = conn.execute(
            "SELECT count(*) AS n FROM node_run "
            "WHERE item_id = %s AND node = %s AND cycle = %s",
            (item_id, item["state"], item["cycle"]),
        ).fetchone()["n"] + 1
        fence = item["fence"] + 1
        conn.execute("UPDATE work_item SET fence = %s WHERE id = %s", (fence, item_id))
        lease_s = float((node.get("config") or {}).get("lease_s", LEASE_SECONDS))
        run = conn.execute(
            "INSERT INTO node_run (item_id, node, cycle, attempt, status, fence, "
            "expected_version, lease_expires_at) "
            "VALUES (%s, %s, %s, %s, 'running', %s, %s, %s) RETURNING *",
            (item_id, item["state"], item["cycle"], attempt, fence, item["version"],
             now() + dt.timedelta(seconds=lease_s)),
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


def _boucle(conn, item, nodes: dict, target: str, cycle: int) -> bool:
    """L'entrée dans ce nœud d'escalade est-elle un tour de boucle ?

    Le budget compte les tours, pas les passages nominaux : la première
    visite d'un nœud d'escalade dans le passage courant est gratuite, la
    re-entrée décompte. Un WAIT d'escalade fait exception — il n'est jamais
    réservé, donc n'a aucune ligne `node_run`, et surtout chaque entrée y
    est une escalade humaine, donc un tour par définition.
    """
    if nodes[target].get("block") == "WAIT":
        return True
    return conn.execute(
        "SELECT 1 FROM node_run WHERE item_id = %s AND node = %s AND cycle = %s",
        (item["id"], target, cycle),
    ).fetchone() is not None


def _route(conn, item, bundle, run, outcome: str, kind: str) -> None:
    """Résout l'arête, débite l'escalade, ouvre le passage, bouge l'item.

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

    # une réponse humaine sur un nœud d'escalade ouvre un passage : en aval,
    # les tentatives repartent de zéro — l'humain vient de juger qu'un cycle
    # complet valait le coup. Le budget d'escalades, lui, reste débité.
    cycle = item["cycle"] + (1 if kind == "answer" and node.get("escalade") else 0)

    # le budget ne paie que les tours de boucle : traverser un nœud
    # d'escalade pour la première fois du passage est gratuit
    if (nodes[target].get("escalade") and target != run["node"]
            and _boucle(conn, item, nodes, target, cycle)):
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
        "UPDATE work_item SET state = %s, version = %s, cycle = %s, terminal_at = %s "
        "WHERE id = %s",
        (target, version, cycle, terminal, item["id"]),
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
    """Runs au bail expiré : révoque (fence++ et pgid), classe crashed, route.

    La révocation a deux moitiés : l'autorité en base, et le processus. Un
    agent lancé par un worker mort ne peut plus rien appliquer, mais il
    travaille encore — le pgid laissé dans le workspace le tue.
    """
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
        # hors transaction : la grâce du SIGTERM ne tient pas les verrous
        revoke_orphan(item["id"], run["id"])
    return len(expired)
