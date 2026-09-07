"""Le routage : l'issue d'un nœud devient l'état suivant de l'item.

Toujours appelé sous verrou de l'item, dans la transaction de l'appelant.
Résout l'arête, débite l'escalade, ouvre le passage, bouge l'item, arme un
WAIT. Rien d'autre ne bouge un item.

Deux compteurs bornent un item, et ils ne se confondent jamais. Les
tentatives par nœud sont un amortisseur local : elles se comptent sur le
passage courant, et une réponse humaine d'escalade en ouvre un nouveau —
« nouveau cycle, pleine marge ». Le budget d'escalades de l'item, lui, ne
se régénère jamais : c'est lui, et lui seul, qui termine. Il compte les
tours de boucle, pas les traversées : la première visite d'un nœud
d'escalade dans le passage courant est gratuite, la re-entrée décompte.

Les tentatives amortissent une panne, pas un dépassement. Une tentative
`crashed`, `invalid_result` ou `stalled` a droit à sa seconde chance sur
place. Une tentative `timed_out` ou `starved`, non : la relancer à
l'identique retomberait au même endroit. Elle escalade tout de suite.
"""

import datetime as dt
import json

from .failure import write_failure_trace
from .policy import MAX_ATTEMPTS, now

RETRIABLE = ("crashed", "invalid_result", "stalled")
ESCALATE_NOW = ("timed_out", "starved")


def route(conn, item, bundle, run, outcome: str, kind: str) -> None:
    """Résout l'arête, débite l'escalade, ouvre le passage, bouge l'item."""
    nodes = bundle["nodes"]
    node = nodes[run["node"]]
    on_kernel = bundle["on_kernel"]

    # Seul un run porte un journal et un rapport. Réponses humaines et
    # échéances passent aussi ici, mais n'inventent pas une tentative. La
    # trace se prend avant une éventuelle conversion en `budget_exhausted` :
    # elle dit l'issue que le nœud a réellement rendue.
    repeated = None
    if run.get("id") is not None:
        repeated = _repeated_failure(conn, item, run, outcome)
        if repeated is not None:
            run = _note_repeat(conn, run, repeated)
        write_failure_trace(item["id"], run, outcome)

    target = _target(node, on_kernel, run, outcome, repeated)

    # une réponse humaine sur un nœud d'escalade ouvre un passage : en aval,
    # les tentatives repartent de zéro — l'humain vient de juger qu'un cycle
    # complet valait le coup. Le budget d'escalades, lui, reste débité.
    cycle = item["cycle"] + (1 if kind == "answer" and node.get("escalade") else 0)
    if _loops_back(conn, item, nodes, run["node"], target, cycle):
        outcome, target = _debit(conn, item, on_kernel, outcome, target)

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
    if not terminal and nodes[target]["block"] == "WAIT":
        _arm_wait(conn, item["id"], target, nodes[target]["config"])


def _note_repeat(conn, run: dict, repeated: int) -> dict:
    """Le run porte la relance supprimée, en base comme dans sa trace."""
    message = f"relance identique supprimée — runs {repeated} et {run['id']}"
    result = {**(run.get("result") or {}), "retry": message}
    conn.execute("UPDATE node_run SET result = %s WHERE id = %s",
                 (json.dumps(result), run["id"]))
    return {**run, "result": result}


def _target(node: dict, on_kernel: dict, run: dict, outcome: str,
            repeated: int | None) -> str:
    """L'arête déclarée, ou la réponse du noyau à sa propre issue."""
    if outcome in (node.get("edges") or {}):
        return node["edges"][outcome]
    if outcome in ESCALATE_NOW:
        # Un dépassement et un fournisseur affamé ne sont pas des pannes
        # transitoires : escalade tout de suite, quel que soit le compteur.
        return on_kernel["escalate_to"]
    if outcome in RETRIABLE:
        # défaut central : réessayer sur place, puis escalader. `stalled` est
        # de cette famille — un agent pendu n'a rien produit, donc rien brûlé.
        if repeated is None and run.get("attempt", 0) < MAX_ATTEMPTS:
            return run["node"]
        return on_kernel["escalate_to"]
    return on_kernel["exhausted_to"]  # budget_exhausted, wall_deadline


def _loops_back(conn, item, nodes: dict, source: str, target: str, cycle: int) -> bool:
    """Le budget ne paie que les tours de boucle : traverser un nœud
    d'escalade pour la première fois du passage est gratuit."""
    return (nodes[target].get("escalade") and target != source
            and _boucle(conn, item, nodes, target, cycle))


def _debit(conn, item, on_kernel: dict, outcome: str, target: str) -> tuple[str, str]:
    """Un tour de boucle : débité, ou converti en `budget_exhausted`."""
    if item["escalations"] <= 0:
        return "budget_exhausted", on_kernel["exhausted_to"]
    conn.execute(
        "UPDATE work_item SET escalations = escalations - 1 WHERE id = %s",
        (item["id"],),
    )
    return outcome, target


def _arm_wait(conn, item_id: int, target: str, cfg: dict) -> None:
    """Un WAIT s'arme dans la même transaction que l'entrée dans l'état :
    sinon la réponse peut arriver avant la souscription (réveil perdu)."""
    conn.execute(
        "INSERT INTO question (item_id, node, text, options, owner, deadline) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (item_id, target, cfg["question"], json.dumps(cfg["options"]),
         cfg["owner"], now() + dt.timedelta(minutes=cfg["deadline_minutes"])),
    )


def _boucle(conn, item, nodes: dict, target: str, cycle: int) -> bool:
    """L'entrée dans ce nœud d'escalade est-elle un tour de boucle ?

    Un WAIT d'escalade fait exception — il n'est jamais réservé, donc n'a
    aucune ligne `node_run`, et surtout chaque entrée y est une escalade
    humaine, donc un tour par définition.
    """
    if nodes[target].get("block") == "WAIT":
        return True
    return conn.execute(
        "SELECT 1 FROM node_run WHERE item_id = %s AND node = %s AND cycle = %s",
        (item["id"], target, cycle),
    ).fetchone() is not None


# --------------------------------------------------------- relance identique


def _retry_batch(conn, item: dict, run: dict, attempt: int) -> dict | None:
    """Rend candidat → (run, signature) pour un lot entièrement comparable."""
    rows = conn.execute(
        "SELECT id, candidate, result FROM node_run WHERE item_id = %s "
        "AND node = %s AND cycle = %s AND attempt = %s ORDER BY candidate",
        (item["id"], run["node"], run["cycle"], attempt),
    ).fetchall()
    batch = {}
    for row in rows:
        signature = (row.get("result") or {}).get("_retry_signature")
        if row.get("candidate") is None or not signature:
            return None
        batch[row["candidate"]] = (row["id"], signature)
    return batch or None


def _previous_single(conn, item: dict, run: dict, signature: str) -> int | None:
    """Le run précédent d'un nœud simple, s'il porte la même signature."""
    previous = conn.execute(
        "SELECT id, result FROM node_run WHERE item_id = %s AND node = %s "
        "AND cycle = %s AND candidate IS NULL AND attempt < %s "
        "ORDER BY attempt DESC LIMIT 1",
        (item["id"], run["node"], run["cycle"], run["attempt"]),
    ).fetchone()
    if previous and (previous.get("result") or {}).get("_retry_signature") == signature:
        return previous["id"]
    return None


def _previous_batch(conn, item: dict, run: dict) -> int | None:
    """Le run homologue du lot précédent, si le lot entier se répète."""
    previous = conn.execute(
        "SELECT max(attempt) AS attempt FROM node_run WHERE item_id = %s "
        "AND node = %s AND cycle = %s AND attempt < %s",
        (item["id"], run["node"], run["cycle"], run["attempt"]),
    ).fetchone()
    if previous["attempt"] is None:  # un agrégat rend toujours sa ligne
        return None
    current_batch = _retry_batch(conn, item, run, run["attempt"])
    previous_batch = _retry_batch(conn, item, run, previous["attempt"])
    if (current_batch is None or previous_batch is None
            or set(current_batch) != set(previous_batch)):
        return None
    if any(current_batch[candidate][1] != previous_batch[candidate][1]
           for candidate in current_batch):
        return None
    return previous_batch[run["candidate"]][0]


def _repeated_failure(conn, item: dict, run: dict, outcome: str) -> int | None:
    """Rend le run antérieur si toute la tentative répète le même échec.

    Un nœud simple compare son run précédent. Un fan-out compare les lots
    complets, candidat par candidat : le progrès d'un seul candidat suffit à
    conserver la relance du lot entier.
    """
    signature = (run.get("result") or {}).get("_retry_signature")
    if outcome not in ("crashed", "invalid_result") or not signature:
        return None
    if run.get("candidate") is None:
        return _previous_single(conn, item, run, signature)
    return _previous_batch(conn, item, run)
