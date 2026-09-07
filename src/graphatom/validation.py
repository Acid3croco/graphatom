"""Validation statique d'un bundle — refusé avant toute exécution.

Un graph publié ne peut plus échouer sur sa structure : tout ce qui suit
lève `GraphError` à la publication, jamais au tick. Les vérifications se
lisent dans l'ordre où elles sont jouées : la forme, les agents, les
exécutions, les portes, puis nœud par nœud, puis le graph entier.
"""

from .bundle import (BLOCK_KINDS, FANOUT_KEEP_MAX, FANOUT_KEEP_MIN,
                     FANOUT_MAX_CANDIDATES, FANOUT_REDUCERS, JUDGE_OUTCOMES,
                     JUDGE_SOURCE, KERNEL_OUTCOMES, GraphError, judge_source,
                     overlay)
from .executors import SUPPORTED_CLIS
from .gates import GATES

GRAPH_AGENT_KEYS = {"cli", "model", "effort", "timeout_s"}
NODE_AGENT_KEYS = {"cli", "model", "effort", "prompt", "passation"}
EXECUTION_KEYS = {"kind", "cmd", "timeout_s", "silence_s"}
EXECUTION_KINDS = {"agent", "command"}
EXECUTABLE_BLOCKS = {"JUDGE", "ACT", "CHECK"}
WAIT_KEYS = ("question", "options", "owner", "deadline_minutes")


def validate(bundle: dict) -> None:
    for key in ("name", "entry", "nodes", "budgets", "on_kernel"):
        if key not in bundle:
            raise GraphError(f"champ manquant : {key}")

    nodes = bundle["nodes"]
    _validate_agents(bundle)
    _validate_executions(bundle)
    _validate_gates(bundle)
    if bundle["entry"] not in nodes:
        raise GraphError(f"entry inconnu : {bundle['entry']}")
    _validate_on_kernel(bundle)
    if not any(spec.get("terminal") for spec in nodes.values()):
        raise GraphError("aucun nœud terminal")
    for name, spec in nodes.items():
        _validate_node(name, spec, nodes)
    _validate_reachable(bundle)
    _validate_acyclic(nodes)


# ------------------------------------------------------------- les emplacements


def _places(bundle: dict):
    """Chaque nœud, puis chacune de ses variantes posée sur sa config.

    Une variante est validée comme le nœud qu'elle devient : sa config est
    l'overlay, et son nom dit d'où elle vient.
    """
    for name, spec in bundle["nodes"].items():
        config = spec.get("config") or {}
        yield name, spec, config
        for index, variant in enumerate(_variants(spec)):
            yield f"{name}.fanout.variants[{index}]", spec, overlay(config, variant)


def _variants(spec: dict) -> list[dict]:
    """Les variantes déclarées qui sont des objets — les autres sont refusées ailleurs."""
    fanout = (spec.get("config") or {}).get("fanout")
    variants = fanout.get("variants") if isinstance(fanout, dict) else None
    if not isinstance(variants, list):
        return []
    return [variant for variant in variants if isinstance(variant, dict)]


# --------------------------------------------------------------------- agents


def _validate_agents(bundle: dict) -> None:
    """Valide les défauts du graph et les surcharges de tous ses nœuds."""
    defaults = bundle.get("agent")
    if defaults is not None:
        _validate_agent_values("graph", defaults, GRAPH_AGENT_KEYS)

    for name, spec in bundle["nodes"].items():
        if spec.get("terminal"):
            continue
        config = spec.get("config") or {}
        local = config.get("agent")
        if local is not None:
            _validate_agent_values(name, local, NODE_AGENT_KEYS)
            if "execution" not in config:
                raise GraphError(f"{name} : agent sans execution — un agent "
                                 "réel se déclare par config.execution")
            if "cli" not in {**(defaults or {}), **local}:
                raise GraphError(f"{name} : agent sans CLI structurée")
        for index, variant in enumerate(_variants(spec)):
            override = variant.get("agent")
            if override is not None:
                _validate_agent_values(f"{name}.fanout.variants[{index}]",
                                       override, NODE_AGENT_KEYS)


def _validate_agent_values(place: str, agent: dict, allowed: set[str]) -> None:
    """Valide les réglages déclaratifs, sans accepter de clé sensible cachée."""
    if not isinstance(agent, dict):
        raise GraphError(f"{place} : agent n'est pas un objet")
    unknown = set(agent) - allowed
    if unknown:
        raise GraphError(f"{place} : réglage agent inconnu {sorted(unknown)}")
    if "cli" in agent and (not isinstance(agent["cli"], str)
                           or agent["cli"] not in SUPPORTED_CLIS):
        raise GraphError(f"{place} : CLI d'agent inconnue {agent['cli']!r}")
    if "model" in agent and not _text(agent["model"]):
        raise GraphError(f"{place} : modèle d'agent invalide {agent['model']!r}")
    if "effort" in agent and agent["effort"] is not None and not _text(agent["effort"]):
        raise GraphError(f"{place} : effort d'agent invalide {agent['effort']!r}")
    if "timeout_s" in agent and not _positive(agent["timeout_s"]):
        raise GraphError(f"{place} : timeout d'agent invalide {agent['timeout_s']!r}")
    if "passation" in agent and not isinstance(agent["passation"], bool):
        raise GraphError(
            f"{place} : passation doit être un booléen, vu {agent['passation']!r}"
        )


def _text(value) -> bool:
    """Une chaîne non vide — ce qu'un modèle, un effort ou un prompt doit être."""
    return isinstance(value, str) and bool(value.strip())


def _positive(value) -> bool:
    """Un nombre strictement positif, sans accepter un booléen comme entier."""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and value > 0)


# ----------------------------------------------------------------- exécutions


def _validate_executions(bundle: dict) -> None:
    """Valide le contrat explicite qui choisit agent ou commande.

    Dès qu'un nœud adopte `execution`, aucune clé historique ne peut
    reprendre la priorité en silence. La CLI, elle, est déjà tenue par
    `_validate_agents` : un agent sans CLI structurée n'arrive pas ici.
    """
    for place, spec, config in _places(bundle):
        execution = config.get("execution")
        if execution is not None:
            _validate_execution(place, spec, config, execution)


def _validate_execution(place: str, spec: dict, config: dict, execution) -> None:
    if spec.get("terminal"):
        raise GraphError(f"{place} : un terminal ne peut pas avoir execution")
    if spec.get("block") not in EXECUTABLE_BLOCKS:
        raise GraphError(
            f"{place} : execution refusée sur un bloc {spec.get('block')}"
        )
    if not isinstance(execution, dict):
        raise GraphError(f"{place} : execution n'est pas un objet")
    unknown = set(execution) - EXECUTION_KEYS
    if unknown:
        raise GraphError(f"{place} : réglage execution inconnu {sorted(unknown)}")
    kind = execution.get("kind")
    if kind not in EXECUTION_KINDS:
        raise GraphError(
            f"{place} : execution.kind invalide {kind!r} — "
            f"attendu : {sorted(EXECUTION_KINDS)}"
        )
    for key in ("timeout_s", "silence_s"):
        if key in execution and not _positive(execution[key]):
            raise GraphError(f"{place} : execution.{key} invalide {execution[key]!r}")

    local = config.get("agent")
    if kind == "command":
        _validate_command(place, execution, local)
    else:
        _validate_agent_execution(place, execution, local)


def _validate_command(place: str, execution: dict, local) -> None:
    if not _text(execution.get("cmd")):
        raise GraphError(f"{place} : execution command sans cmd")
    if local is not None:
        raise GraphError(
            f"{place} : execution command ne peut pas déclarer agent"
        )


def _validate_agent_execution(place: str, execution: dict, local) -> None:
    if local is None:
        raise GraphError(f"{place} : execution agent sans config.agent")
    if not _text(local.get("prompt")):
        raise GraphError(f"{place} : execution agent sans prompt")
    if "cmd" in execution and not _text(execution["cmd"]):
        raise GraphError(f"{place} : execution agent avec cmd invalide")


# --------------------------------------------------------------------- portes


def _validate_gates(bundle: dict) -> None:
    """Porte et activation déclarées : registre fermé, variantes comprises."""
    for place, spec, config in _places(bundle):
        gate = config.get("gate")
        if gate is not None:
            if spec.get("block") not in EXECUTABLE_BLOCKS:
                raise GraphError(f"{place} : gate sur un bloc {spec.get('block')}")
            if gate not in GATES:
                raise GraphError(f"{place} : porte inconnue {gate!r} — "
                                 f"livrées : {sorted(GATES)}")
        activation = config.get("activation")
        if activation is not None and not isinstance(activation, bool):
            raise GraphError(
                f"{place} : activation doit être un booléen, vu {activation!r}")


# ---------------------------------------------------------------- le noyau


def _validate_on_kernel(bundle: dict) -> None:
    """Toute cible on_kernel est une arête comme une autre.

    Les deux clés du noyau sont obligatoires, et aucune clé ne peut viser un
    nœud fantôme.
    """
    on_kernel = bundle["on_kernel"]
    for key in ("escalate_to", "exhausted_to"):
        if key not in on_kernel:
            raise GraphError(f"on_kernel.{key} manquant")
    for key, target in on_kernel.items():
        if target not in bundle["nodes"]:
            raise GraphError(f"on_kernel.{key} → nœud non déclaré {target}")


# ------------------------------------------------------------------ un nœud


def _validate_node(name: str, spec: dict, nodes: dict) -> None:
    if spec.get("terminal"):
        if spec.get("edges"):
            raise GraphError(f"{name} : un terminal n'a pas d'arêtes")
        return
    if spec.get("block") not in BLOCK_KINDS:
        raise GraphError(f"{name} : bloc inconnu {spec.get('block')}")
    _validate_solo(name, spec)
    _validate_fanout(name, spec)
    _validate_judge(name, spec, nodes)
    edges = _validate_edges(name, spec, nodes)
    if spec["block"] == "WAIT":
        _validate_wait(name, spec, edges)


def _validate_edges(name: str, spec: dict, nodes: dict) -> dict:
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
    return edges


def _validate_wait(name: str, spec: dict, edges: dict) -> None:
    cfg = spec.get("config") or {}
    for key in WAIT_KEYS:
        if key not in cfg:
            raise GraphError(f"{name} : WAIT sans {key}")
    missing = set(cfg["options"]) - set(edges)
    if missing:
        raise GraphError(f"{name} : options sans arête : {missing}")
    if "expired" not in edges:
        raise GraphError(f"{name} : l'expiration d'un WAIT doit nommer son arête")


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


# ------------------------------------------------------------ le graph entier


def _validate_reachable(bundle: dict) -> None:
    """Atteignabilité depuis entry — les cibles on_kernel sont de vraies arêtes."""
    nodes = bundle["nodes"]
    seen: set[str] = set()
    stack = [bundle["entry"], *bundle["on_kernel"].values()]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend((nodes[n].get("edges") or {}).values())
    unreachable = set(nodes) - seen
    if unreachable:
        raise GraphError(f"nœuds inatteignables : {unreachable}")


def _validate_acyclic(nodes: dict) -> None:
    """Le sous-graph privé des arêtes d'escalade est acyclique.

    Une file fait exception, et elle seule : un nœud qui déclare `file` a le
    droit de se renvoyer sur lui-même, parce qu'il attend une ressource que
    personne ne lui rendra plus vite — la boucle est bornée par le temps que
    chaque tour coûte, donc par le `wall_deadline` de l'item, et non par le
    budget d'escalades. L'exception ne porte que sur l'arête réflexive : une
    boucle plus longue qui passe par la file reste refusée.
    """
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
