"""Définition, validation statique et publication d'un graph.

Un graph est une donnée : un bundle JSON canonique, adressé par contenu.
La validation refuse le bundle avant toute exécution — un graph publié
ne peut plus échouer sur sa structure.
"""

import hashlib
import json

import psycopg

BLOCK_KINDS = {"FETCH", "JUDGE", "ACT", "CHECK", "EFFECT", "WAIT"}

KERNEL_OUTCOMES = {"crashed", "timed_out", "invalid_result", "budget_exhausted", "wall_deadline"}


class GraphError(Exception):
    pass


def canonical(bundle: dict) -> str:
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(bundle: dict) -> str:
    return hashlib.sha256(canonical(bundle).encode()).hexdigest()


def validate(bundle: dict) -> None:
    for key in ("name", "entry", "nodes", "budgets", "on_kernel"):
        if key not in bundle:
            raise GraphError(f"champ manquant : {key}")

    nodes = bundle["nodes"]
    if bundle["entry"] not in nodes:
        raise GraphError(f"entry inconnu : {bundle['entry']}")

    on_kernel = bundle["on_kernel"]
    for key in ("escalate_to", "exhausted_to"):
        if on_kernel.get(key) not in nodes:
            raise GraphError(f"on_kernel.{key} doit viser un nœud déclaré")

    terminals = {n for n, spec in nodes.items() if spec.get("terminal")}
    if not terminals:
        raise GraphError("aucun nœud terminal")

    for name, spec in nodes.items():
        if spec.get("terminal"):
            if spec.get("edges"):
                raise GraphError(f"{name} : un terminal n'a pas d'arêtes")
            continue
        if spec.get("block") not in BLOCK_KINDS:
            raise GraphError(f"{name} : bloc inconnu {spec.get('block')}")
        edges = spec.get("edges") or {}
        if not edges:
            raise GraphError(f"{name} : aucun nœud non-terminal sans arête")
        for outcome, target in edges.items():
            if outcome in KERNEL_OUTCOMES:
                raise GraphError(f"{name}.{outcome} : issue noyau dans l'espace domaine")
            if target not in nodes:
                raise GraphError(f"{name}.{outcome} → nœud non déclaré {target}")
        if spec["block"] == "WAIT":
            cfg = spec.get("config") or {}
            for key in ("question", "options", "owner", "deadline_minutes"):
                if key not in cfg:
                    raise GraphError(f"{name} : WAIT sans {key}")
            missing = set(cfg["options"]) - set(edges)
            if missing:
                raise GraphError(f"{name} : options sans arête : {missing}")
            if "expired" not in edges:
                raise GraphError(f"{name} : l'expiration d'un WAIT doit nommer son arête")

    # atteignabilité depuis entry
    seen: set[str] = set()
    stack = [bundle["entry"]]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend((nodes[n].get("edges") or {}).values())
    unreachable = set(nodes) - seen
    if unreachable:
        raise GraphError(f"nœuds inatteignables : {unreachable}")

    # le sous-graph privé des arêtes d'escalade est acyclique
    escalade = {n for n, spec in nodes.items() if spec.get("escalade")}
    plain: dict[str, list[str]] = {
        n: [t for t in (spec.get("edges") or {}).values() if t not in escalade]
        for n, spec in nodes.items()
        if n not in escalade
    }
    color: dict[str, int] = {}

    def dfs(n: str) -> None:
        color[n] = 1
        for t in plain.get(n, []):
            if color.get(t) == 1:
                raise GraphError(f"cycle hors escalade via {n} → {t}")
            if t not in color:
                dfs(t)
        color[n] = 2

    for n in plain:
        if n not in color:
            dfs(n)


def publish(conn: psycopg.Connection, bundle: dict) -> str:
    validate(bundle)
    rev = content_hash(bundle)
    conn.execute(
        "INSERT INTO graph_revision (id, name, bundle) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (rev, bundle["name"], canonical(bundle)),
    )
    return rev


def load_bundle(conn: psycopg.Connection, revision: str) -> dict:
    row = conn.execute(
        "SELECT bundle FROM graph_revision WHERE id = %s", (revision,)
    ).fetchone()
    if row is None:
        raise GraphError(f"révision inconnue : {revision}")
    return row["bundle"]
