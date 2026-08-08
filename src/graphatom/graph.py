"""Définition, validation statique et publication d'un graph.

Un graph est une donnée : un bundle JSON canonique, adressé par contenu.
La validation refuse le bundle avant toute exécution — un graph publié
ne peut plus échouer sur sa structure.
"""

import hashlib
import json

import psycopg

BLOCK_KINDS = {"FETCH", "JUDGE", "ACT", "CHECK", "EFFECT", "WAIT"}

KERNEL_OUTCOMES = {"crashed", "stalled", "timed_out", "invalid_result",
                   "budget_exhausted", "wall_deadline"}

# Le fan-out de candidats : un nœud déclare des variantes de sa propre config,
# jouées en concurrence puis réduites à une seule issue. Ici, seule la
# déclaration est validée — rien ne s'exécute encore différemment.
#
# Le jeu des réductions légales est une table « type de bloc → réductions
# permises », tranchée à la publication et non découverte à l'exécution : une
# réduction par vote sur un nœud qui produit du code n'a aucun sens.
# `first_pass` — le premier candidat dont l'issue passe gagne — est la seule
# réduction livrée à ce jour ; `keep_n` et `best_by` viendront plus tard et ne
# sont donc légales nulle part. Un WAIT garde le jeu vide : une question posée
# à un humain ne se multiplie pas.
FANOUT_REDUCERS: dict[str, set[str]] = {
    "FETCH": {"first_pass"},
    "JUDGE": {"first_pass"},
    "ACT": {"first_pass"},
    "CHECK": {"first_pass"},
    "EFFECT": {"first_pass"},
    "WAIT": set(),
}

# La borne dure du nombre de candidats d'un nœud — variantes × répétitions.
# Une config fautive ne doit pas lancer mille agents.
FANOUT_MAX_CANDIDATES = 8


class GraphError(Exception):
    pass


def canonical(bundle: dict) -> str:
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(bundle: dict) -> str:
    return hashlib.sha256(canonical(bundle).encode()).hexdigest()


def _validate_fanout(name: str, spec: dict) -> None:
    """Le fan-out déclaré d'un nœud : sa forme, sa réduction, sa borne.

    Sans clé `fanout` dans la config, il n'y a rien à dire — un bundle qui
    ignore le fan-out se valide exactement comme avant.
    """
    fanout = (spec.get("config") or {}).get("fanout")
    if fanout is None:
        return

    kind = spec["block"]
    if kind == "WAIT":
        raise GraphError(f"{name} : un nœud WAIT ne peut pas être en fan-out")
    if not isinstance(fanout, dict):
        raise GraphError(f"{name} : fanout n'est pas un objet")

    # une variante surcharge les clés de la config du nœud ; ce qu'elle ne
    # surcharge pas est hérité — n'importe quelle clé, mais un objet
    variants = fanout.get("variants")
    if not isinstance(variants, list) or not variants:
        raise GraphError(f"{name} : fanout sans variants, ou variants vide")
    for variant in variants:
        if not isinstance(variant, dict):
            raise GraphError(f"{name} : variante qui n'est pas un objet : {variant!r}")

    # repeat multiplie chaque variante ; absent, il vaut une fois
    repeat = fanout.get("repeat", 1)
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
        raise GraphError(f"{name} : fanout.repeat doit être un entier ≥ 1, vu {repeat!r}")

    if "reduce" not in fanout:
        raise GraphError(f"{name} : fanout sans reduce")
    reduce = fanout["reduce"]
    permises = FANOUT_REDUCERS[kind]
    if reduce not in permises:
        raise GraphError(f"{name} : réduction {reduce!r} refusée sur un bloc {kind} — "
                         f"permises : {sorted(permises) or 'aucune'}")

    candidats = len(variants) * repeat
    if candidats > FANOUT_MAX_CANDIDATES:
        raise GraphError(f"{name} : {len(variants)} variantes × {repeat} = {candidats} "
                         f"candidats, au-delà de la limite dure "
                         f"FANOUT_MAX_CANDIDATES = {FANOUT_MAX_CANDIDATES}")


def validate(bundle: dict) -> None:
    for key in ("name", "entry", "nodes", "budgets", "on_kernel"):
        if key not in bundle:
            raise GraphError(f"champ manquant : {key}")

    nodes = bundle["nodes"]
    if bundle["entry"] not in nodes:
        raise GraphError(f"entry inconnu : {bundle['entry']}")

    # toute cible on_kernel est une arête comme une autre : les deux clés du
    # noyau sont obligatoires, et aucune clé ne peut viser un nœud fantôme
    on_kernel = bundle["on_kernel"]
    for key in ("escalate_to", "exhausted_to"):
        if key not in on_kernel:
            raise GraphError(f"on_kernel.{key} manquant")
    for key, target in on_kernel.items():
        if target not in nodes:
            raise GraphError(f"on_kernel.{key} → nœud non déclaré {target}")

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
        _validate_fanout(name, spec)
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

    # atteignabilité depuis entry — les cibles on_kernel sont de vraies arêtes
    seen: set[str] = set()
    stack = [bundle["entry"], *on_kernel.values()]
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
