"""Ce que la vitrine sait d'un item — une seule lecture, deux rendus.

Les pages HTML (`pages`) et l'API JSON (`api`) lisent ici : mêmes requêtes,
mêmes durées, mêmes totaux de tokens. Une projection, pas un second modèle.
Rien ne s'y écrit.
"""

import datetime as dt
import json
import re
from urllib.parse import quote

from . import executors
from .bundle import KERNEL_OUTCOMES, judge_source
from .graph import load_bundle
from .workspace import item_workspace

GH_SUBJECT = re.compile(r"gh:([\w.-]+/[\w.-]+)#(\d+)")
LETTRES = "ABCDEFGH"

# un item et son sujet, plus la date de sa dernière transition : le journal
# la porte déjà, aucune colonne à tenir à jour pour le savoir
ITEM_SELECT = (
    "SELECT w.*, s.subject_key, s.graph, s.title, s.lineage_budget, "
    "(SELECT max(at) FROM event e WHERE e.item_id = w.id) AS updated_at "
    "FROM work_item w JOIN subject s ON s.id = w.subject_id "
)


def issue_href(subject_key: str) -> str | None:
    """L'URL de l'issue quand le sujet en est une, sinon None.

    Le frontend ne connaît GitHub que comme un format de sujet parmi
    d'autres : tout ce qui ne matche pas `gh:<owner>/<repo>#<num>` n'a ni
    lien ni numéro, et le noyau reste agnostique.
    """
    m = GH_SUBJECT.fullmatch(subject_key)
    return f"https://github.com/{m[1]}/issues/{m[2]}" if m else None


def release(item_id: int) -> dict | None:
    """`release.json` du workspace, tel quel — None sans fichier.

    Écrit par le nœud release à côté de son markdown : trois clés lues
    telles quelles, jamais un parseur de prose. Un JSON cassé lève, et
    chaque rendu décide de ce qu'il en dit.
    """
    path = item_workspace(item_id) / "release.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def pr_url(item_id: int) -> str | None:
    """L'URL de la PR seule — None sans fichier, sans URL, ou JSON cassé."""
    try:
        found = release(item_id)
    except json.JSONDecodeError:  # la page HTML le signale déjà, en toutes lettres
        return None
    return found.get("pr_url") if found else None


# ---------------------------------------------------------------------- coût


def reported_cost_value(run: dict) -> int | float | None:
    """Le coût rapporté, ou None quand le fournisseur n'en donne aucun."""
    result = run.get("result")
    usage = result.get("usage") if isinstance(result, dict) else None
    value = usage.get("total_cost_usd") if isinstance(usage, dict) else None
    return value if type(value) in (int, float) else None


def runs(conn, where: str, params: tuple) -> list[dict]:
    """Les runs, tels qu'ils sont en base."""
    return conn.execute("SELECT nr.* FROM node_run nr " + where, params).fetchall()


def _costs(conn, rows: list[dict]) -> dict[int, tuple[float, int]]:
    """Par item : la somme des coûts rapportés, et combien de runs en ont un."""
    costs = {row["id"]: (0.0, 0) for row in rows}
    if not rows:
        return costs
    for run in runs(conn, "WHERE nr.item_id = ANY(%s)", ([row["id"] for row in rows],)):
        reported = reported_cost_value(run)
        if run.get("item_id") in costs and reported is not None:
            total, count = costs[run["item_id"]]
            costs[run["item_id"]] = (total + reported, count + 1)
    return costs


def items_data(conn) -> list[dict]:
    """Tous les items, du plus récent au plus ancien — même requête pour la
    page et pour l'API.

    Le coût est celui que les fournisseurs rapportent dans l'usage des runs,
    tous statuts confondus. Un usage absent vaut zéro.
    """
    rows = [dict(row) for row in
            conn.execute(ITEM_SELECT + "ORDER BY w.id DESC").fetchall()]
    costs = _costs(conn, rows)
    for row in rows:
        total, count = costs[row["id"]]
        row["total_cost_usd"] = total
        row["reported_cost_usd"] = total if count else None
    return rows


# ------------------------------------------------------- durées et tokens


def spans(events: list[dict]) -> dict[int, float]:
    """Le temps passé dans chaque état, par version d'item.

    Aucune colonne de durée en base : le journal porte déjà un horodatage
    par transition, et l'écart avec la précédente est la durée du séjour
    qu'elle clôt — tentatives comprises, un retry étant lui-même une
    transition.
    """
    return {e["item_version"]: (e["at"] - prev["at"]).total_seconds()
            for prev, e in zip(events, events[1:])}


def usage(run: dict) -> dict:
    """L'usage rapporté par le run, tel quel."""
    found = (run["result"] or {}).get("usage")
    return dict(found) if isinstance(found, dict) else {}


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def totals(lot: list[dict]) -> dict[str, float]:
    """Le total par type de token de l'item.

    Rien n'est codé en dur : on additionne les clés numériques des usages
    trouvés, quels que soient leurs noms.
    """
    found: dict[str, float] = {}
    for run in lot:
        for key, value in usage(run).items():
            if _numeric(value):
                found[key] = found.get(key, 0) + value
    return found


def _arbitres(bundle: dict) -> set[str]:
    """Les nœuds arbitres du graph : ceux dont le prix est celui du jugement."""
    return {n for n, spec in bundle["nodes"].items() if judge_source(spec)}


def cost_split(bundle: dict, lot: list[dict]) -> dict[str, dict[str, float]]:
    """Ce que le jugement a coûté, face à ce que la génération a coûté.

    Trois parts, disjointes et exhaustives : les runs des nœuds arbitres, les
    runs de candidats de fan-out, et tout le reste. Un item qui n'est jamais
    passé par un juge n'a que la troisième.
    """
    arbitres = _arbitres(bundle)
    parts = {"judgement": [], "candidates": [], "other": []}
    for run in lot:
        if run["node"] in arbitres:
            parts["judgement"].append(run)
        elif run.get("candidate") is not None:
            parts["candidates"].append(run)
        else:
            parts["other"].append(run)
    return {part: totals(runs_) for part, runs_ in parts.items()}


# ----------------------------------------------------------------- workspace


def files(item_id: int) -> list[dict]:
    """Les fichiers du workspace : leur nom, leur taille, l'URL qui les sert."""
    workspace = item_workspace(item_id)
    paths = sorted(p for p in workspace.iterdir() if p.is_file()) if workspace.is_dir() else []
    return [{"name": p.name, "size": p.stat().st_size,
             "href": f"/item/{item_id}/file/{quote(p.name)}"} for p in paths]


def criteria(item_id: int) -> str | None:
    """`criteria.md` du workspace, tel quel — None quand le cycle n'en a pas."""
    path = item_workspace(item_id) / "criteria.md"
    return path.read_text() if path.is_file() else None


# ------------------------------------------------------------------ décision


def _finalists(source_runs: list[dict]) -> list[dict]:
    """Les finalistes `applied` de la dernière tentative, dans l'ordre du juge."""
    batch = max(((r["cycle"], r["attempt"]) for r in source_runs), default=None)
    return sorted(
        (r for r in source_runs
         if (r["cycle"], r["attempt"]) == batch
         and r["candidate"] is not None and r["status"] == "applied"
         and r["outcome"] and r["outcome"] not in KERNEL_OUTCOMES),
        key=lambda r: (r["finished_at"], r["id"]),
    )


def _elected(run: dict, mapping: list[dict]) -> dict | None:
    result = run.get("result") or {}
    if run["outcome"] == "chosen":
        letter = result.get("elu")
    else:
        letter = "A" if run["outcome"] == "sole" and mapping else None
    return next((entry for entry in mapping if entry["letter"] == letter), None)


def decision(item_id: int, bundle: dict, lot: list[dict]) -> dict | None:
    """La dernière décision d'arbitrage, avec ses lettres explicites.

    La projection historique reprend exactement l'ordre du juge : finalistes
    ``applied`` de la dernière tentative, triés par date de fin puis par id.
    Elle ne devient visible qu'après la décision, donc l'entrée du juge reste
    anonyme.
    """
    judges = {name: spec for name, spec in bundle["nodes"].items()
              if judge_source(spec)}
    decided = [r for r in lot if r["node"] in judges and r["status"] == "applied"
               and r["outcome"] in ("chosen", "sole", "none")]
    if not decided:
        return None
    run = max(decided, key=lambda r: r["id"])
    spec = judges[run["node"]]
    source = judge_source(spec)
    mapping = [{"letter": letter, "candidate": r["candidate"]}
               for letter, r in zip(LETTRES, _finalists(
                   [r for r in lot if r["node"] == source]))]
    executor = executors.resolve(bundle, spec)
    verdict = item_workspace(item_id) / "verdict.md"
    return {
        "type": run["outcome"], "source": source, "finalists": mapping,
        "selected": _elected(run, mapping),
        "summary": (run.get("result") or {}).get("summary"),
        "verdict": verdict.read_text() if verdict.is_file() else None,
        "judge": ({"cli": executor.cli, "model": executor.model,
                   "effort": executor.effort}
                  if run["outcome"] == "chosen" else None),
    }


# ---------------------------------------------------------------------- item


def item_data(conn, item_id: int) -> dict | None:
    """Tout ce que la page d'un item et son JSON ont besoin de savoir.

    Une seule passe en base pour les deux rendus : l'item, son bundle, son
    journal, ses runs, ses effets, ses questions — et l'arithmétique qui en
    sort, durées et tokens comprises. `None` quand l'item n'existe pas.
    """
    item = conn.execute(ITEM_SELECT + "WHERE w.id = %s", (item_id,)).fetchone()
    if item is None:
        return None
    bundle = load_bundle(conn, item["revision"])
    events = conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version", (item_id,)
    ).fetchall()
    lot = runs(conn, "WHERE nr.item_id = %s ORDER BY nr.id", (item_id,))
    item = dict(item)
    reported = [value for run in lot
                if (value := reported_cost_value(run)) is not None]
    item["total_cost_usd"] = sum(reported)
    item["reported_cost_usd"] = sum(reported) if reported else None
    durations = spans(events)
    end = item["terminal_at"] or dt.datetime.now(dt.timezone.utc)
    return {
        "item": item, "bundle": bundle, "events": events, "runs": lot,
        "effects": conn.execute(
            "SELECT * FROM effect WHERE item_id = %s ORDER BY op_id", (item_id,)
        ).fetchall(),
        "questions": conn.execute(
            "SELECT * FROM question WHERE item_id = %s ORDER BY id", (item_id,)
        ).fetchall(),
        "spans": durations,
        # un run n'a de durée que s'il a produit une transition : superseded
        # et stale n'en ont jamais eu, running pas encore
        "run_span": {e["run_id"]: durations[e["item_version"]]
                     for e in events if e["run_id"] and e["item_version"] in durations},
        "duration_s": (end - events[0]["at"]).total_seconds() if events else 0.0,
        "usage": totals(lot),
        "usage_split": cost_split(bundle, lot),
        "criteria": criteria(item_id),
        "files": files(item_id),
        "decision": decision(item_id, bundle, lot),
    }
