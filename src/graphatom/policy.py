"""Ce que tout le noyau partage sans que personne n'en dépende : l'horloge,
les constantes de bail et de tentative, et la mémoire des runs de ce
processus. Une feuille : ce module n'importe rien du rail.
"""

import datetime as dt

UTC = dt.timezone.utc

LEASE_SECONDS = 30
# la marge du reaper sur le couperet de l'agent : le process est coupé
# AVANT le bail, sinon le reaper révoquerait un agent encore vivant qui
# continue d'écrire dans un worktree dont l'item a été repris
AGENT_LEASE_MARGIN_S = 60
AGENT_TIMEOUT_S = 570  # budget d'une tentative d'agent, quand le nœud n'en dit rien
MAX_ATTEMPTS = 3  # défaut central, par passage : réessayer, puis escalader
# « GRAPHATO » en ASCII, dans la plage signée de l'advisory lock PostgreSQL.
# Toutes les voies d'admission nominales prennent la même clé : deux canaux
# concurrents ne peuvent pas voir la place libre puis ouvrir deux items.
ADMISSION_LOCK = 0x475241504841544F

# Les runs que *ce processus-ci* a réservés et qui volent encore. En mémoire,
# donc vide au démarrage : un worker qui vient de naître ne reconnaît aucun
# des runs `running` qu'il trouve en base, et c'est exactement l'information
# qui manquait au faucheur — un run qu'il n'a pas réservé a été emporté par
# la mort du worker d'avant, son agent n'y est pour rien. Voir `reaper`.
# Le tick le remplit, les threads des blocs le vident : un set suffit, `add`
# et `discard` ne se marchent pas dessus.
CLAIMED: set[int] = set()


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def agent_timeout_s(config: dict, default: float = AGENT_TIMEOUT_S) -> float:
    """Le couperet du process d'un nœud, dérivé de son bail.

    Un `timeout_s` encore porté par le nœud est honoré — l'exception reste
    visible, elle n'est plus la norme. Sans lui, le couperet descend du bail
    (`lease_s`) moins `AGENT_LEASE_MARGIN_S` ; un nœud sans bail garde
    `default`, le 570 s historique quand rien n'est dit.
    """
    execution = config.get("execution") or {}
    if "timeout_s" in execution:
        return float(execution["timeout_s"])
    agent = config.get("agent") or {}
    if "timeout_s" in agent:
        return float(agent["timeout_s"])
    lease = config.get("lease_s")
    if lease is not None:
        return float(lease) - AGENT_LEASE_MARGIN_S
    return default
