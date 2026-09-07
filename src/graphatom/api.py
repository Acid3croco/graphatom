"""L'API JSON de `graphatom serve` — les mêmes vues que les pages, en données.

    /api/items       la liste, avec l'état, les liens issue et PR
    /api/item/<id>   l'item entier : graph, journal, runs, questions,
                     critères, fichiers du workspace
    /api/item/<id>/run/<run>/trace   les octets nouveaux des traces d'un run
    /api/questions   les questions ouvertes, et le jeton de `POST /answer`
    /api/heartbeat   les deux battements bruts : le worker, et le canal GitHub
    /api/load        la charge : runs en vol, et les plafonds qui la bornent
    /api/graphs      les révisions publiées : nom, date, items qui la portent
    /api/graph/<rév> le bundle entier de cette révision, config comprise

Un client qui demande du JSON en reçoit toujours, y compris quand ça rate :
une erreur ici est un objet à `error`, jamais une page, jamais une trace.
"""

import datetime as dt
import json
import re
from urllib.parse import parse_qs, unquote

from . import channel, executors, heartbeat, quota, scheduler, views
from .bundle import candidate_node, fanout_variants, judge_source
from .workspace import attempt_command, attempt_log, attempt_name, run_workspace

TRACE = re.compile(r"/api/item/(\d+)/run/(\d+)/trace")
TRACE_NAMES = ("events", "log", "command")


def jsonable(value):
    """Ce que `json` ne sait pas rendre : un horodatage, en ISO 8601.

    Le reste passe en texte plutôt qu'en 500 : une valeur inattendue au fond
    d'un JSONB d'agent ne doit pas emporter la réponse entière.
    """
    return value.isoformat() if isinstance(value, dt.datetime) else str(value)


# --------------------------------------------------------------------- items


def item_row(row: dict) -> dict:
    """Un item en JSON : ce que la table `/items` montre, plus ses deux liens.

    `status` dit la vie de l'item — actif ou terminal —, `state` reste le
    nœud courant du graph.
    """
    return {
        "id": row["id"],
        "subject_key": row["subject_key"],
        "title": row["title"],
        "graph": row["graph"],
        "generation": row["generation"],
        "state": row["state"],
        "status": "terminal" if row["terminal_at"] else "active",
        "version": row["version"],
        "cycle": row["cycle"],
        "escalations": row["escalations"],
        "total_cost_usd": row["total_cost_usd"],
        "reported_cost_usd": row.get("reported_cost_usd"),
        "issue_url": views.issue_href(row["subject_key"]),
        "pr_url": views.pr_url(row["id"]),
        "terminal_at": row["terminal_at"],
        "updated_at": row["updated_at"],
    }


def items(conn) -> list[dict]:
    """La table `/items` en JSON — mêmes lignes que la page."""
    return [item_row(r) for r in views.items_data(conn)]


# --------------------------------------------------------------------- graph


def candidate(bundle: dict, spec: dict, index: int, variant: dict) -> dict:
    """Une variante et les champs de son exécuteur résolu."""
    resolved = executors.resolve(bundle, candidate_node(spec, index))
    agent = {key: value for key, value in {
        "cli": resolved.cli,
        "model": resolved.model,
        "effort": resolved.effort,
        "cmd": resolved.cmd,
        "cmd_uses_executor": resolved.kind == "composed",
    }.items() if value is not None
             and not (key == "cmd_uses_executor" and value is False)}
    return {"variant": variant, "agent": agent, "cmd": resolved.cmd}


def fanout(bundle: dict, spec: dict) -> dict | None:
    """Le fan-out d'un nœud, un candidat par entrée — `None` sans fan-out.

    L'entrée `k` de `candidates` est celle du run qui porte `candidate = k` :
    sa variante telle qu'elle est déclarée, et son agent effectif.
    """
    found = (spec.get("config") or {}).get("fanout")
    if not found:
        return None
    return {
        "reduce": found["reduce"],
        "repeat": found.get("repeat", 1),
        "candidates": [candidate(bundle, spec, k, variant)
                       for k, variant in enumerate(fanout_variants(spec))],
    }


def _node(bundle: dict, name: str, spec: dict) -> dict:
    node = {"name": name, "block": spec.get("block"),
            "terminal": bool(spec.get("terminal")),
            "escalade": bool(spec.get("escalade"))}
    if found := fanout(bundle, spec):
        node["fanout"] = found
    # un nœud arbitre nomme sa source : c'est ce qui permet au client de
    # séparer le prix du jugement de celui des candidats
    if source := judge_source(spec):
        node["finalists_from"] = source
    return node


def graph(bundle: dict, current: str) -> dict:
    """De quoi redessiner le graph sans relire la base."""
    nodes = bundle["nodes"]
    return {
        "name": bundle["name"],
        "entry": bundle["entry"],
        "on_kernel": bundle["on_kernel"],
        "current": current,
        "nodes": [_node(bundle, n, spec) for n, spec in nodes.items()],
        "edges": [{"from": n, "outcome": outcome, "to": target}
                  for n, spec in nodes.items()
                  for outcome, target in (spec.get("edges") or {}).items()],
    }


def graphs(conn) -> list[dict]:
    """Les révisions publiées, la plus récente de chaque nom en tête.

    Le compte d'items est ce qui sépare une révision vivante d'une révision
    oubliée : un item en vol épingle la sienne.
    """
    rows = conn.execute(
        "SELECT g.id, g.name, g.published_at, "
        "(SELECT count(*) FROM work_item w WHERE w.revision = g.id) AS items "
        "FROM graph_revision g ORDER BY g.name, g.published_at DESC, g.id"
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "published_at": r["published_at"],
             "items": r["items"]} for r in rows]


def graph_revision(conn, revision: str) -> dict | None:
    """Le bundle d'une révision, tel qu'il a été publié. None si inconnue."""
    row = conn.execute(
        "SELECT g.id, g.bundle, g.published_at, "
        "(SELECT count(*) FROM work_item w WHERE w.revision = g.id) AS items "
        "FROM graph_revision g WHERE g.id = %s", (revision,)).fetchone()
    if row is None:
        return None
    return row["bundle"] | {"revision": row["id"], "items": row["items"],
                            "published_at": row["published_at"]}


# ---------------------------------------------------------------------- item


def question(q: dict) -> dict:
    """Une question en JSON, avec le contexte de son item quand la requête l'a joint."""
    payload = {
        "question_id": q["id"], "item_id": q["item_id"], "node": q["node"],
        "text": q["text"], "options": list(q["options"]), "owner": q["owner"],
        "deadline": q["deadline"], "state": q["state"],
        "answer": q["answer"], "answered_by": q["answered_by"],
    }
    if "subject_key" in q:  # les questions ouvertes portent l'item avec elles
        payload |= {"subject_key": q["subject_key"], "item_title": q["item_title"],
                    "item_state": q["item_state"],
                    "issue_url": views.issue_href(q["subject_key"])}
    return payload


def _run(r: dict, run_span: dict) -> dict:
    # `finished_at` est la seule date d'un candidat qui a perdu : il n'a
    # produit aucune transition, donc aucun span — mais il a bien couru
    return {"id": r["id"], "node": r["node"], "cycle": r["cycle"],
            "attempt": r["attempt"], "candidate": r.get("candidate"),
            "status": r["status"], "outcome": r["outcome"], "fence": r["fence"],
            "lease_expires_at": r["lease_expires_at"],
            "finished_at": r["finished_at"],
            "duration_s": run_span.get(r["id"]),
            "usage": views.usage(r), "result": r["result"]}


def item(conn, item_id: int) -> dict | None:
    """La page d'un item en données. None quand l'item n'existe pas."""
    data = views.item_data(conn, item_id)
    if data is None:
        return None
    found, bundle = data["item"], data["bundle"]
    spans = data["spans"]
    return {
        "item": item_row(found) | {
            "revision": found["revision"],
            "lineage_budget": found["lineage_budget"],
            "wall_deadline": found["wall_deadline"],
            "duration_s": data["duration_s"],
            "usage": data["usage"],
            "usage_split": data["usage_split"],
        },
        "graph": graph(bundle, found["state"]),
        "journal": [{"version": ev["item_version"], "at": ev["at"], "kind": ev["kind"],
                     "from_state": ev["from_state"], "to_state": ev["to_state"],
                     "outcome": ev["outcome"], "run_id": ev["run_id"],
                     "duration_s": spans.get(ev["item_version"])}
                    for ev in data["events"]],
        "runs": [_run(r, data["run_span"]) for r in data["runs"]],
        "effects": [{"op_id": f["op_id"], "logical_key": f["logical_key"],
                     "target_uri": f["target_uri"], "observation": f["observation"]}
                    for f in data["effects"]],
        "questions": [question(q) for q in data["questions"]],
        "criteria": data["criteria"],
        "files": data["files"],
        "decision": data["decision"],
    }


def questions(conn, token: str) -> dict:
    """Les questions ouvertes, et le jeton qu'attend `POST /answer`."""
    return {"token": token,
            "questions": [question(q) for q in channel.open_questions(conn)]}


# --------------------------------------------------------------------- trace


def _trace_paths(workspace, run: dict) -> dict:
    name = attempt_name(run)
    codex = workspace / f"codex-{name}.jsonl"
    opencode = workspace / f"opencode-events-{name}.jsonl"
    if not codex.is_file() and not opencode.is_file():  # le run est encore actif
        codex = workspace / "codex.jsonl"
        opencode = workspace / "opencode-events.jsonl"
    events = codex if codex.is_file() else opencode
    provider = ("codex" if codex.is_file()
                else "opencode" if opencode.is_file() else None)
    return {
        "events": (events, provider, "application/x-ndjson"),
        "log": (attempt_log(workspace, run), "log", "text/plain"),
        "command": (attempt_command(workspace, run), "command", "application/json"),
    }


def _source(path, kind: str, media_type: str, offset: int) -> dict:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError(f"curseur {kind} invalide")
    if not path.is_file():
        return {"type": kind, "state": "missing", "content": "",
                "offset": offset, "next_offset": offset, "media_type": media_type}
    data = path.read_bytes()
    return {"type": kind, "state": "empty" if not data else "available",
            "content": data[offset:].decode(errors="replace"),
            "offset": offset, "next_offset": len(data), "media_type": media_type}


def run_trace(conn, item_id: int, run_id: int,
              cursor: dict[str, int] | None = None) -> dict | None:
    """Les octets nouveaux des trois traces d'un run, sans chemin du client.

    Le curseur garde une position par fichier : le journal et les événements
    ne grandissent pas au même rythme. `None` refuse aussi bien un item ou un
    run inconnu qu'un run qui appartient à un autre item.
    """
    if conn.execute("SELECT id FROM work_item WHERE id = %s", (item_id,)).fetchone() is None:
        return None
    run = conn.execute(
        "SELECT id, item_id, node, cycle, attempt, candidate, status "
        "FROM node_run WHERE id = %s", (run_id,)
    ).fetchone()
    if run is None or run["item_id"] != item_id:
        return None
    positions = cursor or {}
    sources = {}
    for name, (path, kind, media_type) in _trace_paths(run_workspace(item_id, run), run).items():
        sources[name] = _source(path, kind, media_type, positions.get(name, 0))
    return {"item_id": item_id, "run_id": run_id, "status": run["status"],
            **sources, "cursor": {name: sources[name]["next_offset"] for name in sources}}


def _cursor(fields: dict) -> dict:
    """Le curseur du client : un objet JSON, ou des décalages en clair."""
    cursor = json.loads(fields.get("cursor", ["{}"])[0])
    if "offset" in fields:
        cursor = {name: int(fields["offset"][0]) for name in TRACE_NAMES}
    for name in TRACE_NAMES:
        if f"{name}_offset" in fields:
            cursor[name] = int(fields[f"{name}_offset"][0])
    if not isinstance(cursor, dict):
        raise ValueError("le curseur doit être un objet")
    return cursor


# --------------------------------------------------------------- battements


def beat(at: dt.datetime | None) -> dict:
    """Un battement brut : son horodatage, son âge, et s'il est périmé."""
    return {"at": at, "ago_s": heartbeat.age_s(at), "stale": heartbeat.stalled(at)}


def beats(rail: dt.datetime | dict | None, sync: dt.datetime | None) -> dict:
    """Les deux battements, pour un client qui pose l'en-tête lui-même.

    Un objet par batteur : le worker et le canal GitHub sont deux processus
    séparés, chacun peut mourir seul.
    """
    if isinstance(rail, dict):
        worker = beat(rail.get("at")) | {
            "sha": rail.get("worker_sha"),
            "started_at": rail.get("worker_started_at"),
        }
    else:
        worker = beat(rail) | {"sha": None, "started_at": None}
    return {heartbeat.RAIL: worker, heartbeat.GITHUB_SYNC: beat(sync)}


def load(conn) -> dict:
    """La charge du rail : runs et constructions, avec leurs plafonds.

    `running` au niveau de `max_runs`, c'est un rail qui n'est pas bloqué
    mais plein — les runs réservables attendent leur tour.
    """
    return {"running": scheduler.en_vol(conn),
            "max_runs": scheduler.MAX_RUNS,
            "max_runs_per_item": scheduler.MAX_RUNS_PER_ITEM,
            "max_active_items": scheduler.MAX_ACTIVE_ITEMS,
            "solo": scheduler.etat_solo(conn),
            "builds": quota.en_vol(conn),
            "max_builds": quota.MAX_BUILDS}


# ------------------------------------------------------------------- routes


COLLECTIONS = {
    "/api/items": lambda conn, _token: items(conn),
    "/api/questions": questions,
    "/api/heartbeat": lambda conn, _token: beats(
        heartbeat.worker(conn), heartbeat.last(conn, heartbeat.GITHUB_SYNC)),
    "/api/load": lambda conn, _token: load(conn),
    "/api/graphs": lambda conn, _token: graphs(conn),
}


def _trace_route(conn, match: re.Match, query: str) -> tuple[int, object]:
    try:
        payload = run_trace(conn, int(match[1]), int(match[2]), _cursor(parse_qs(query)))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return 400, {"error": f"curseur invalide : {exc}"}
    if payload is None:
        return 404, {"error": "item ou run inconnu"}
    return 200, payload


def dispatch(conn, path: str, query: str, token: str) -> tuple[int, object]:
    """Le statut et le corps d'une route JSON — une route inconnue est un 404."""
    if path in COLLECTIONS:
        return 200, COLLECTIONS[path](conn, token)
    if match := TRACE.fullmatch(path):
        return _trace_route(conn, match, query)
    if path.startswith("/api/item/") and path[10:].isdigit():
        payload = item(conn, int(path[10:]))
        return (404, {"error": f"item {path[10:]} inconnu"}) if payload is None else (200, payload)
    if path.startswith("/api/graph/"):
        revision = unquote(path[11:])
        payload = graph_revision(conn, revision)
        return (404, {"error": f"révision {revision} inconnue"}) if payload is None else (200, payload)
    return 404, {"error": f"route inconnue : {path}"}
