"""Le bureau d'un item et de ses runs : où chaque trace du rail est rangée.

`DATA_DIR` est la racine ; les tests la font pointer sur un répertoire
temporaire, et c'est ici — nulle part ailleurs — qu'ils la posent. Tout ce
qui nomme un fichier du workspace passe par ce module : le bloc qui écrit,
le faucheur qui relit, la vitrine qui sert.

Un candidat de fan-out a son propre workspace sous celui de l'item —
`data/item-<N>/c<k>/`. Rien d'autre ne le distingue de son voisin.
"""

from pathlib import Path

DATA_DIR = Path("data")  # les tests le font pointer sur un répertoire temporaire
OUTBOX_NAME = "effects_outbox.log"  # sous DATA_DIR, résolu au moment de l'effet
PGID_FILE = "agent.pgid"  # la trace qui survit au worker, écrasée à chaque tentative
OUTCOME_NAME = "outcome.json"  # transitoire : purgé avant chaque tentative
STARVED_NAME = "starved.json"  # idem — une panne de crédits nommée par l'adaptateur
PROMPT_NAME = "prompt.md"  # l'interface avec le cmd, archivée après la tentative
USAGE_NAME = "usage.json"  # idem — ce que l'agent veut bien dire de sa consommation
EVENT_NAMES = ("codex.jsonl", "opencode-events.jsonl")  # flux brut du fournisseur
FAILURE_NAME = "failure.json"  # emplacement stable du dernier échec de l'item
VERDICT_NAME = "verdict.md"  # le verdict du juge, comme `validate.md` du validate
# les traces que le rail écrit lui-même, par tentative : jamais du travail d'agent
RAIL_PREFIXES = ("agent-", "command-", "prompt-", "usage-")


def item_workspace(item_id: int) -> Path:
    """Le répertoire de travail d'un item — connu du bloc comme du faucheur."""
    return DATA_DIR / f"item-{item_id}"


def run_workspace(item_id: int, run: dict) -> Path:
    """Le répertoire de travail d'un run : celui de l'item, ou celui du candidat.

    Un candidat de fan-out a le sien à l'intérieur, `data/item-<N>/c<k>/` :
    sans ça, les journaux, les fichiers d'issue et les traces de pgid des K
    candidats s'écraseraient, et une révocation viserait le mauvais groupe de
    processus. Un run sans fan-out n'a pas de numéro de candidat, et son
    workspace reste celui de l'item — exactement comme avant.
    """
    workspace = item_workspace(item_id)
    candidate = run.get("candidate")
    return workspace if candidate is None else workspace / f"c{candidate}"


def attempt_name(run: dict) -> str:
    """Le nom d'une tentative — nœud, passage, tentative : celui de ses traces."""
    return f"{run['node']}-{run['cycle']}-{run['attempt']}"


def attempt_log(workspace: Path, run: dict) -> Path:
    """Le journal d'une tentative, dans le workspace de son item."""
    return workspace / f"agent-{attempt_name(run)}.log"


def attempt_command(workspace: Path, run: dict) -> Path:
    """La commande effective d'une tentative, hors de son journal de progrès.

    Le chien de garde mesure le journal de l'agent. Y écrire la commande du
    framework ferait donc passer un agent muet pour un agent actif. Cette
    pièce séparée reste durable et exacte, sans devenir un faux signal de
    travail produit par l'agent.
    """
    return workspace / f"command-{attempt_name(run)}.json"


def failure_path(item_id: int) -> Path:
    """L'emplacement unique de la trace du dernier échec d'un item."""
    return item_workspace(item_id) / FAILURE_NAME


def passation_path(item_id: int, run: dict) -> Path:
    """La passation d'un run : un fichier par nœud, dans le workspace du run.

    Le nom porte le nœud parce que les nœuds d'un item partagent un
    workspace : un nom unique s'écraserait d'un nœud à l'autre. Un candidat
    de fan-out écrit la sienne dans son `c<k>/`, comme tout son travail.
    """
    return run_workspace(item_id, run) / f"passation-{run['node']}.md"


def work_files(workspace: Path) -> list[str]:
    """Les fichiers du workspace qu'un agent a écrits — pas les traces du rail.

    Le rail écrit lui-même le journal, le prompt et l'usage de chaque
    tentative, puis les range sous le nom de celle-ci : ces fichiers-là
    existent même après une tentative qui n'a rien fait. Les compter comme du
    travail rendrait toute relance « reprise », y compris celle d'un agent
    pendu qui n'a laissé qu'un journal vide — et une reprise inventée est
    pire que pas de reprise du tout.
    """
    traces = {PGID_FILE, OUTCOME_NAME, STARVED_NAME, PROMPT_NAME, USAGE_NAME}
    return sorted(p.name for p in workspace.iterdir()
                  if p.is_file() and p.name not in traces
                  and not p.name.startswith(RAIL_PREFIXES))
