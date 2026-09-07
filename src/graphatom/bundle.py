"""La forme d'un bundle : ses constantes, son adressage, ses lectures pures.

Un graph est une donnée : un bundle JSON canonique, adressé par contenu.
Ce module ne porte que ce qui se lit sans base ni validation — les
vocabulaires fermés, l'empreinte, et les vues d'un nœud que l'exécution
demande (variantes de fan-out, nœud arbitre). La validation vit dans
`validation`, la publication dans `graph`.
"""

import hashlib
import json

BLOCK_KINDS = {"FETCH", "JUDGE", "ACT", "CHECK", "EFFECT", "WAIT"}

KERNEL_OUTCOMES = {"crashed", "starved", "stalled", "timed_out", "invalid_result",
                   "budget_exhausted", "wall_deadline"}

# Le fan-out de candidats : un nœud déclare des variantes de sa propre config,
# jouées en concurrence puis réduites à une seule issue.
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
# ordinaire. Voir `arbitrage`.
JUDGE_SOURCE = "finalists_from"

# Les trois issues fermées d'un nœud arbitre, et il les déclare toutes les
# trois : un finaliste unique, un élu parmi plusieurs, aucun finaliste.
JUDGE_OUTCOMES = ("sole", "chosen", "none")


class GraphError(Exception):
    pass


def canonical(bundle: dict) -> str:
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(bundle: dict) -> str:
    return hashlib.sha256(canonical(bundle).encode()).hexdigest()


def judge_source(spec: dict) -> str | None:
    """Le nœud de fan-out dont ce nœud reçoit les finalistes. None sinon.

    C'est le seul signe qui distingue un JUDGE arbitre d'un JUDGE ordinaire :
    il est déclaré, jamais deviné.
    """
    return (spec.get("config") or {}).get(JUDGE_SOURCE)


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
    fusionnent clé à clé — une variante qui ne change que le modèle garde
    le prompt, l'exécution et les budgets du nœud. Le reste du nœud, ses
    arêtes en tête, est celui de tout le monde : un candidat ne réécrit pas
    le graph.
    """
    variant = fanout_variants(spec)[candidate]
    return spec | {"config": overlay(spec.get("config") or {}, variant)}


def overlay(base: dict, over: dict) -> dict:
    """`over` posé sur `base` : les objets fusionnent, tout le reste remplace."""
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = overlay(merged[key], value)
        else:
            merged[key] = value
    return merged
