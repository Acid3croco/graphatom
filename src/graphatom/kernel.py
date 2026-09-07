"""Le noyau : admission, réservation, et l'unique transition apply().

Le cycle de vie d'un nœud :

    item.state = N, aucun run actif
      → claim()   : réservation transactionnelle — run créé, fence bumpé,
                    version attendue épinglée, bail posé
      → le bloc s'exécute, hors transaction
      → apply()   : valide le résultat, route par l'arête déclarée,
                    version++, événement — le changement de nœud
      → item.state = M

Toute mutation d'état d'item passe par `routing.route` sous verrou :
résultat de bloc (apply), réponse humaine ou échéance (apply_item),
faucheur (`reaper.reap`). Rien d'autre ne bouge un item.

La promotion du gagnant d'un fan-out n'est pas un ménage d'après-coup :
c'est une conséquence de la décision, dans la même transaction qu'elle. La
réduction élit, promeut, et **seulement ensuite** route l'item — le tout
sous le verrou de l'item, celui-là même que `claim` prend. Voir `_settle`
et `reduction`.
"""

import datetime as dt
import json
from collections.abc import Callable

import psycopg

from . import reduction, routing
from .bundle import KERNEL_OUTCOMES, fanout_variants
from .graph import load_bundle
from .policy import ADMISSION_LOCK, CLAIMED, LEASE_SECONDS, now
from .signature import retry_signature


class LaneOccupied(RuntimeError):
    """L'admission a trouvé l'unique voie occupée par un autre item."""

    def __init__(self, item_id: int, state: str):
        self.item_id = item_id
        self.state = state
        super().__init__(f"voie occupée par l'item {item_id} ({state})")


# ---------------------------------------------------------------- admission


def active_item(conn: psycopg.Connection) -> dict | None:
    """L'unique item non terminal du rail, ou None quand la voie est libre."""
    return conn.execute(
        "SELECT id, state FROM work_item WHERE terminal_at IS NULL "
        "ORDER BY id LIMIT 1"
    ).fetchone()


def admit(conn: psycopg.Connection, revision: str, subject_key: str,
          title: str | None = None,
          prepare: Callable[[int], None] | None = None, *,
          _allow_parallel_for_test: bool = False) -> int:
    """Crée (ou retrouve) le sujet, ouvre une occurrence — si la lignée le permet.

    Le titre est celui que le canal a sous la main au moment de l'admission :
    stocké là, une fois. Par défaut, un seul item non terminal existe dans
    tout le rail ; le verrou de transaction ferme la course entre deux
    canaux d'admission. `_allow_parallel_for_test` n'est qu'un outil de banc
    d'essai des plafonds multi-items.

    `prepare`, s'il existe, prépare les ressources locales du nouvel item
    avant le commit. Une erreur annule ainsi l'admission.
    """
    with conn.transaction():
        if not _allow_parallel_for_test:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADMISSION_LOCK,))
            if active := active_item(conn):
                raise LaneOccupied(active["id"], active["state"])
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
        item_id = _open_item(conn, subject["id"], revision, bundle)
        if prepare:
            prepare(item_id)
        conn.execute(
            "INSERT INTO event (item_id, item_version, kind, to_state) "
            "VALUES (%s, 1, 'admitted', %s)",
            (item_id, bundle["entry"]),
        )
        conn.execute("UPDATE work_item SET version = 1 WHERE id = %s", (item_id,))
        return item_id


def _open_item(conn, subject_id: int, revision: str, bundle: dict) -> int:
    """La ligne de l'occurrence : génération suivante, budgets du graph."""
    generation = conn.execute(
        "SELECT count(*) AS n FROM work_item WHERE subject_id = %s", (subject_id,),
    ).fetchone()["n"] + 1
    budgets = bundle["budgets"]
    deadline = now() + dt.timedelta(hours=budgets["wall_deadline_hours"])
    return conn.execute(
        "INSERT INTO work_item (subject_id, generation, revision, state, "
        "escalations, wall_deadline) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (subject_id, generation, revision, bundle["entry"],
         budgets["escalations"], deadline),
    ).fetchone()["id"]


# --------------------------------------------------------------- réservation


def claim(conn: psycopg.Connection, item_id: int) -> dict | None:
    """Réserve un run pour l'état courant de l'item. None si rien à faire.

    Un nœud en fan-out ne se réserve pas d'un bloc : `claim` rend un candidat
    à la fois, et l'ordonnanceur rappelle tant qu'il en reste. La tentative
    est celle de tous — les K candidats la partagent, avec sa barrière —, et
    chacun a son bail, son numéro et son workspace.

    Le run réservé est noté dans `CLAIMED`, la mémoire du processus : c'est
    ce qui dira au faucheur, plus tard, si ce run est le sien.
    """
    with conn.transaction():
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        if item is None or item["terminal_at"] is not None:
            return None
        node = load_bundle(conn, item["revision"])["nodes"][item["state"]]
        if node.get("terminal") or node["block"] == "WAIT":
            return None
        fanout = len(fanout_variants(node))
        attempt, taken = _attempt_slot(conn, item, fanout)
        # un run d'un autre nœud, d'un autre passage ou d'une autre tentative
        # est encore en vol : l'item n'en mène qu'une à la fois
        running = conn.execute(
            "SELECT id FROM node_run WHERE item_id = %s AND status = 'running' "
            "AND NOT (node = %s AND cycle = %s AND attempt = %s)",
            (item_id, item["state"], item["cycle"], attempt),
        ).fetchone()
        if running:
            return None
        # la barrière est celle de la tentative entière : les candidats la
        # partagent, sinon le premier réservé serait dépassé par le suivant
        fence = item["fence"] + (0 if taken else 1)
        if not taken:
            conn.execute("UPDATE work_item SET fence = %s WHERE id = %s",
                         (fence, item_id))
        candidate = next(k for k in range(fanout or 1) if k not in taken)
        lease_s = float((node.get("config") or {}).get("lease_s", LEASE_SECONDS))
        run = conn.execute(
            "INSERT INTO node_run (item_id, node, cycle, attempt, candidate, status, "
            "fence, expected_version, lease_expires_at) "
            "VALUES (%s, %s, %s, %s, %s, 'running', %s, %s, %s) RETURNING *",
            (item_id, item["state"], item["cycle"], attempt,
             candidate if fanout else None, fence, item["version"],
             now() + dt.timedelta(seconds=lease_s)),
        ).fetchone()
        CLAIMED.add(run["id"])  # ce run est le nôtre, tant qu'on vit
        return run


def _attempt_slot(conn, item: dict, fanout: int) -> tuple[int, set[int]]:
    """La tentative courante du nœud, et les candidats qu'elle a déjà pris.

    Les tentatives se comptent sur le passage courant : celles qu'un passage
    précédent a brûlées restent en base, plus au décompte. Une tentative au
    complet ouvre la suivante.
    """
    rows = conn.execute(
        "SELECT attempt, candidate FROM node_run "
        "WHERE item_id = %s AND node = %s AND cycle = %s",
        (item["id"], item["state"], item["cycle"]),
    ).fetchall()
    attempt = max((r["attempt"] for r in rows), default=0) or 1
    taken = {r["candidate"] or 0 for r in rows if r["attempt"] == attempt}
    if len(taken) >= (fanout or 1):
        return attempt + 1, set()
    return attempt, taken


# ---------------------------------------------------------------- transition


def _locked(conn, run_id: int) -> tuple[dict, dict]:
    """L'item puis son run, verrouillés dans cet ordre — toujours le même.

    La réduction classe les perdants sous le verrou de l'item ; un perdant
    qui rendrait au même instant en prenant les verrous dans l'autre sens
    interbloquerait la course.
    """
    owner = conn.execute(
        "SELECT item_id FROM node_run WHERE id = %s", (run_id,)
    ).fetchone()["item_id"]
    item = conn.execute(
        "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (owner,)
    ).fetchone()
    run = conn.execute(
        "SELECT * FROM node_run WHERE id = %s FOR UPDATE", (run_id,)
    ).fetchone()
    return item, run


def _rejected(conn, item: dict, run: dict, submitted: dict) -> str | None:
    """Les rejets, chacun une transition durable du run. None si le run vole."""
    if run["status"] != "running":
        # révoqué avant d'avoir rendu — par la réduction, ou par le faucheur :
        # ce qu'il rapporte est classé, jamais routé. Un post-mortem déjà
        # écrit reste celui de qui l'a écrit
        conn.execute(
            "UPDATE node_run SET result = %s WHERE id = %s AND result IS NULL",
            (json.dumps(submitted), run["id"]),
        )
        return run["status"]
    for status, bad in (("superseded", run["fence"] != item["fence"]),
                        ("stale", run["expected_version"] != item["version"])):
        if bad:
            conn.execute(
                "UPDATE node_run SET status = %s, result = %s, finished_at = %s "
                "WHERE id = %s",
                (status, json.dumps(submitted), now(), run["id"]),
            )
            return status
    return None


def signed(item_id: int, run: dict, outcome: str, result: dict) -> dict:
    """Le résultat, signé pour la comparaison des relances quand il y a lieu."""
    if outcome not in ("crashed", "invalid_result"):
        return result
    signature = retry_signature(item_id, {**run, "result": result}, outcome)
    if signature is None:
        return result
    return {**result, "_retry_signature": signature}


def apply(conn: psycopg.Connection, run_id: int, submitted: dict) -> str:
    """L'unique transition pour un résultat de run. Retourne le statut du run."""
    with conn.transaction():
        item, run = _locked(conn, run_id)
        rejected = _rejected(conn, item, run, submitted)
        if rejected is not None:
            return rejected
        bundle = load_bundle(conn, item["revision"])
        node = bundle["nodes"][run["node"]]
        outcome = submitted.get("outcome")
        if outcome not in (node.get("edges") or {}) and outcome not in KERNEL_OUTCOMES:
            outcome = "invalid_result"
        submitted = signed(item["id"], {**run, "outcome": outcome}, outcome, submitted)
        run = {**run, "outcome": outcome, "result": submitted}
        conn.execute(
            "UPDATE node_run SET status = 'applied', outcome = %s, result = %s, "
            "finished_at = %s WHERE id = %s",
            (outcome, json.dumps(submitted), now(), run_id),
        )
        revoques, ranger = _settle(conn, item, bundle, run, outcome, kind="result")
        # `keep_n` recale les réussites au-delà de la n-ième, et une promotion
        # impossible faute le gagnant : rendre « applied » mentirait
        statut = conn.execute(
            "SELECT status FROM node_run WHERE id = %s", (run_id,)
        ).fetchone()["status"]
    # rendu et classé : ce run ne vole plus, il sort de la mémoire du
    # processus. Ici et pas à l'entrée : un `apply` qui échoue laisse le run
    # en vol sous ce worker-ci, et le faucheur doit y lire « bail expiré ».
    CLAIMED.discard(run_id)
    reduction.menage(item["id"], revoques, ranger)
    return statut


def apply_item(
    conn: psycopg.Connection,
    item_id: int,
    outcome: str,
    kind: str,
    *,
    before_route: Callable[[], object] | None = None,
) -> None:
    """Transition sans run : réponse humaine, échéance de WAIT, wall_deadline.

    Cette entrée doit ouvrir la transaction de plus haut niveau. L'appelant
    peut lui donner `before_route` pour y joindre une mutation connexe, par
    exemple fermer la question d'un WAIT. Rien ici n'est une réduction :
    l'item quitte son nœud sans qu'aucun candidat ait gagné, et les ateliers
    de ceux qui couraient encore sont détruits.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("apply_item exige une connexion hors transaction")
    with conn.transaction():
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        if before_route is not None:
            before_route()
        if item["terminal_at"] is not None:
            return
        revoked = conn.execute(
            "UPDATE node_run SET status = 'superseded', finished_at = %s "
            "WHERE item_id = %s AND status = 'running' RETURNING id",
            (now(), item_id),
        ).fetchall()
        bundle = load_bundle(conn, item["revision"])
        run = {"node": item["state"], "id": None, "attempt": 0}
        routing.route(conn, item, bundle, run, outcome, kind=kind)
    run_ids = [row["id"] for row in revoked]
    for run_id in run_ids:
        CLAIMED.discard(run_id)
    reduction.menage(item_id, run_ids, True)


def _settle(conn, item, bundle, run, outcome: str, kind: str) -> tuple[list[int], bool]:
    """L'issue d'un run devient — ou non — celle du nœud. Rend le ménage d'après.

    Sans fan-out, l'issue du run est celle du nœud. Avec, trois gestes dans
    cet ordre, et l'ordre *est* le contrat : la réduction élit, la promotion
    porte le travail de l'élu sur la branche de l'item, le routage fait
    avancer l'item — sous le verrou de l'item, que `claim` prend aussi.

    Rend ce qui reste à faire une fois la transaction close : les runs à
    tuer, et s'il faut ranger les ateliers. Voir `reduction.menage`.
    """
    if run["candidate"] is None:
        routing.route(conn, item, bundle, run, outcome, kind=kind)
        return [], False
    fanout = bundle["nodes"][run["node"]]["config"]["fanout"]
    verdict = reduction.reduce(conn, item, run, outcome, fanout)
    if verdict is None:
        return [], False  # la course continue : personne n'a tranché
    elu, issue, revoques = verdict
    # `keep_n` n'élit pas un gagnant : ni promotion ni rangement ici, c'est
    # le nœud arbitre d'aval qui promeut l'élu et range le reste
    if fanout["reduce"] == "keep_n":
        routing.route(conn, item, bundle, elu, issue, kind=kind)
        return revoques, False
    empechement = reduction.promouvoir(item, elu, issue)
    if empechement is not None:
        reduction.faute(conn, elu, empechement)
        routing.route(conn, item, bundle, elu, "crashed", kind=kind)
        return revoques, False  # les ateliers restent : le travail est dedans
    routing.route(conn, item, bundle, elu, issue, kind=kind)
    return revoques, True
