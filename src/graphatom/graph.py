"""Définition, validation statique et publication d'un graph.

Un graph est une donnée : un bundle JSON canonique, adressé par contenu.
La validation refuse le bundle avant toute exécution — un graph publié
ne peut plus échouer sur sa structure.
"""

import hashlib
import json

import psycopg

from .executors import SUPPORTED_CLIS

BLOCK_KINDS = {"FETCH", "JUDGE", "ACT", "CHECK", "EFFECT", "WAIT"}

KERNEL_OUTCOMES = {"crashed", "starved", "stalled", "timed_out", "invalid_result",
                   "budget_exhausted", "wall_deadline"}

# Le fan-out de candidats : un nœud déclare des variantes de sa propre config,
# jouées en concurrence puis réduites à une seule issue. Ici, seule la
# déclaration est validée — rien ne s'exécute encore différemment.
#
# Le jeu des réductions légales est une table « type de bloc → réductions
# permises », tranchée à la publication et non découverte à l'exécution : une
# réduction par vote sur un nœud qui produit du code n'a aucun sens.
#
# `first_pass` — le premier candidat dont l'issue passe gagne — est celle qui
# n'attend personne, et elle est permise partout où un fan-out l'est.
# `keep_n` garde les n candidats qui ont réussi et les passe en aval à un juge
# au lieu d'en élire un : elle n'a de sens que là où les candidats produisent
# un travail qu'on peut comparer, c'est-à-dire sur un ACT. Ailleurs — une
# lecture, un constat, un effet — il n'y a rien à départager, et elle est
# refusée à la publication. `best_by` n'est livrée nulle part. Un WAIT garde
# le jeu vide : une question posée à un humain ne se multiplie pas.
FANOUT_REDUCERS: dict[str, set[str]] = {
    "FETCH": {"first_pass"},
    "JUDGE": {"first_pass"},
    "ACT": {"first_pass", "keep_n"},
    "CHECK": {"first_pass"},
    "EFFECT": {"first_pass"},
    "WAIT": set(),
}

# La borne dure du nombre de candidats d'un nœud — variantes × répétitions.
# Une config fautive ne doit pas lancer mille agents.
FANOUT_MAX_CANDIDATES = 8

# La borne dure du `n` de `keep_n`, et elle est étroite exprès : le juge lit N
# diffs entiers, son contexte explose au-delà de trois et la valeur marginale
# d'un quatrième finaliste s'effondre. En deçà de deux, il n'y a personne à
# départager — c'est `first_pass` qu'on voulait, en plus cher.
FANOUT_KEEP_MIN = 2
FANOUT_KEEP_MAX = 3

# Le nœud qui départage les finalistes le dit par cette clé de config : elle
# nomme le nœud de fan-out dont il reçoit les candidats. Sa présence fait le
# nœud arbitre — le bloc court-circuite, appelle un modèle ou renvoie en
# arrière selon ce qu'il trouve, au lieu de jouer un agent comme un JUDGE
# ordinaire. Voir `blocks.judge`.
JUDGE_SOURCE = "finalists_from"

# Les trois issues fermées d'un nœud arbitre, et il les déclare toutes les
# trois : un finaliste unique, un élu parmi plusieurs, aucun finaliste.
JUDGE_OUTCOMES = ("sole", "chosen", "none")


class GraphError(Exception):
    pass


GRAPH_AGENT_KEYS = {"cli", "model"}
NODE_AGENT_KEYS = {"cli", "model", "cmd", "prompt", "timeout_s", "silence_s"}


def _validate_agent_values(place: str, agent: dict, allowed: set[str]) -> None:
    """Valide les réglages déclaratifs, sans accepter de clé sensible cachée."""
    if not isinstance(agent, dict):
        raise GraphError(f"{place} : agent n'est pas un objet")
    unknown = set(agent) - allowed
    if unknown:
        raise GraphError(f"{place} : réglage agent inconnu {sorted(unknown)}")
    if "cli" in agent:
        cli = agent["cli"]
        if not isinstance(cli, str) or cli not in SUPPORTED_CLIS:
            raise GraphError(f"{place} : CLI d'agent inconnue {cli!r}")
    if "model" in agent and (not isinstance(agent["model"], str)
                             or not agent["model"].strip()):
        raise GraphError(f"{place} : modèle d'agent invalide {agent['model']!r}")


def _validate_agents(bundle: dict) -> None:
    """Valide les défauts du graph et les surcharges de tous ses nœuds."""
    defaults = bundle.get("agent")
    if defaults is not None:
        _validate_agent_values("graph", defaults, GRAPH_AGENT_KEYS)

    for name, spec in bundle["nodes"].items():
        if spec.get("terminal"):
            continue
        local = (spec.get("config") or {}).get("agent")
        fanout = (spec.get("config") or {}).get("fanout")
        variants = fanout.get("variants") or [] if isinstance(fanout, dict) else []
        for index, variant in enumerate(variants):
            override = variant.get("agent") if isinstance(variant, dict) else None
            if override is not None:
                _validate_agent_values(f"{name}.fanout.variants[{index}]", override,
                                       NODE_AGENT_KEYS)
        if local is None:
            continue
        _validate_agent_values(name, local, NODE_AGENT_KEYS)
        if "cmd" not in local and "cli" not in local and not (defaults or {}).get("cli"):
            raise GraphError(f"{name} : agent sans cmd ni CLI structurée")


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
    if reduce == "keep_n":
        _validate_keep_n(name, fanout)

    candidats = len(variants) * repeat
    if candidats > FANOUT_MAX_CANDIDATES:
        raise GraphError(f"{name} : {len(variants)} variantes × {repeat} = {candidats} "
                         f"candidats, au-delà de la limite dure "
                         f"FANOUT_MAX_CANDIDATES = {FANOUT_MAX_CANDIDATES}")


def _validate_solo(name: str, spec: dict) -> None:
    """Le drapeau d'exclusion d'un nœud : un booléen, jamais sur un WAIT."""
    config = spec.get("config") or {}
    if "solo" not in config:
        return
    solo = config["solo"]
    if not isinstance(solo, bool):
        raise GraphError(f"{name} : solo doit être un booléen, vu {solo!r}")
    if solo and spec["block"] == "WAIT":
        raise GraphError(f"{name} : un nœud WAIT ne peut pas être solo")


def _validate_keep_n(name: str, fanout: dict) -> None:
    """Le `n` de `keep_n` : présent, entier, et dans la borne dure.

    Le message nomme la borne. Une config qui la rate se corrige en la
    lisant, sans aller ouvrir le code du noyau.
    """
    if "n" not in fanout:
        raise GraphError(f"{name} : réduction keep_n sans n — la borne est "
                         f"{FANOUT_KEEP_MIN} ≤ n ≤ {FANOUT_KEEP_MAX}")
    n = fanout["n"]
    if isinstance(n, bool) or not isinstance(n, int):
        raise GraphError(f"{name} : keep_n.n doit être un entier, vu {n!r} — la "
                         f"borne est {FANOUT_KEEP_MIN} ≤ n ≤ {FANOUT_KEEP_MAX}")
    if not FANOUT_KEEP_MIN <= n <= FANOUT_KEEP_MAX:
        raise GraphError(f"{name} : keep_n.n = {n} hors de la borne dure "
                         f"{FANOUT_KEEP_MIN} ≤ n ≤ {FANOUT_KEEP_MAX} — au-delà, le "
                         f"juge lit trop de diffs ; en deçà, il n'a rien à départager")


def judge_source(spec: dict) -> str | None:
    """Le nœud de fan-out dont ce nœud reçoit les finalistes. None sinon.

    C'est le seul signe qui distingue un JUDGE arbitre d'un JUDGE ordinaire :
    il est déclaré, jamais deviné.
    """
    return (spec.get("config") or {}).get(JUDGE_SOURCE)


def _validate_judge(name: str, spec: dict, nodes: dict) -> None:
    """Le nœud arbitre : son bloc, sa source, et ses trois issues.

    Sa source doit être un nœud déclaré qui réduit bien par `keep_n` — sans
    quoi il n'y aurait jamais qu'un candidat à lui présenter, et le nœud
    entier serait un tampon. Ses trois issues sont fermées et obligatoires :
    un finaliste unique, un élu, aucun finaliste.
    """
    source = judge_source(spec)
    if source is None:
        return
    if spec.get("block") != "JUDGE":
        raise GraphError(f"{name} : {JUDGE_SOURCE} demande un bloc JUDGE, "
                         f"vu {spec.get('block')}")
    if source not in nodes:
        raise GraphError(f"{name}.{JUDGE_SOURCE} → nœud non déclaré {source}")
    amont = (nodes[source].get("config") or {}).get("fanout") or {}
    if amont.get("reduce") != "keep_n":
        raise GraphError(f"{name}.{JUDGE_SOURCE} → {source} ne réduit pas par "
                         f"keep_n : rien à départager")
    manquantes = [o for o in JUDGE_OUTCOMES if o not in (spec.get("edges") or {})]
    if manquantes:
        raise GraphError(f"{name} : nœud arbitre sans arête {manquantes} — les "
                         f"trois issues {list(JUDGE_OUTCOMES)} sont fermées")


def fanout_variants(spec: dict) -> list[dict]:
    """Les variantes d'un nœud, une par candidat — `[]` sans fan-out.

    L'énumération est déterministe et se lit à l'endroit : chaque variante
    dans l'ordre déclaré, répétée `repeat` fois. Le candidat k tient donc sa
    variante de `k // repeat`, et deux répétitions d'une même variante ne se
    distinguent que par leur workspace.

    Une liste vide, c'est « pas de fan-out » : le nœud garde son run unique,
    et rien de ce qui suit ne le concerne.
    """
    fanout = (spec.get("config") or {}).get("fanout")
    if not fanout:
        return []
    return [variant for variant in fanout["variants"]
            for _ in range(fanout.get("repeat", 1))]


def candidate_node(spec: dict, candidate: int) -> dict:
    """Le nœud tel que le voit un candidat : sa variante posée sur la config.

    Une variante est un fragment de config : les clés qu'elle nomme
    surchargent celles du nœud, celles qu'elle tait sont héritées. Les objets
    fusionnent clé à clé — une variante qui ne change que `agent.cmd` garde
    le prompt et les budgets du nœud. Le reste du nœud, ses arêtes en tête,
    est celui de tout le monde : un candidat ne réécrit pas le graph.
    """
    variant = fanout_variants(spec)[candidate]
    return spec | {"config": _overlay(spec.get("config") or {}, variant)}


def _overlay(base: dict, over: dict) -> dict:
    """`over` posé sur `base` : les objets fusionnent, tout le reste remplace."""
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _overlay(merged[key], value)
        else:
            merged[key] = value
    return merged


def validate(bundle: dict) -> None:
    for key in ("name", "entry", "nodes", "budgets", "on_kernel"):
        if key not in bundle:
            raise GraphError(f"champ manquant : {key}")

    nodes = bundle["nodes"]
    _validate_agents(bundle)
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
        _validate_solo(name, spec)
        _validate_fanout(name, spec)
        _validate_judge(name, spec, nodes)
        edges = spec.get("edges") or {}
        if not edges:
            raise GraphError(f"{name} : aucun nœud non-terminal sans arête")
        for outcome, target in edges.items():
            if outcome in KERNEL_OUTCOMES:
                raise GraphError(f"{name}.{outcome} : issue noyau dans l'espace domaine")
            if target not in nodes:
                raise GraphError(f"{name}.{outcome} → nœud non déclaré {target}")
        if spec.get("file") and name not in edges.values():
            raise GraphError(f"{name} : file sans arête sur lui-même")
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

    # le sous-graph privé des arêtes d'escalade est acyclique. Une file fait
    # exception, et elle seule : un nœud qui déclare `file` a le droit de se
    # renvoyer sur lui-même, parce qu'il attend une ressource que personne ne
    # lui rendra plus vite — la boucle est bornée par le temps que chaque tour
    # coûte, donc par le `wall_deadline` de l'item, et non par le budget
    # d'escalades. L'exception ne porte que sur l'arête réflexive : une boucle
    # plus longue qui passe par la file reste refusée.
    escalade = {n for n, spec in nodes.items() if spec.get("escalade")}
    plain: dict[str, list[str]] = {
        n: [t for t in (spec.get("edges") or {}).values()
            if t not in escalade and not (spec.get("file") and t == n)]
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
