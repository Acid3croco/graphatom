"""L'interface de bloc, les six blocs stub, et l'exécuteur d'agent.

Un bloc reçoit un contexte et retourne {"outcome": ...} plus ce qu'il veut.
Il ne touche jamais la base : le noyau réserve avant, applique après.
Les stubs simulent le travail via la config du nœud.

Un nœud ACT / CHECK / JUDGE peut déclarer `config.agent` : un vrai agent
CLI fait alors le travail. Le contrat est minuscule — le bloc écrit
`prompt.md` dans le workspace, résout l'adaptateur configuré ou la commande
explicite, puis lit `outcome.json`. Pas de
fichier d'issue valide = crashed, et le noyau route comme d'habitude.

Une extension optionnelle : si la tentative laisse un `usage.json`, le
bloc le fusionne dans le résultat du run — les types de tokens tels que
l'agent les rapporte, sans que personne ici les interprète. C'est le
`cmd` du graph qui produit ce fichier, jamais le noyau. Pas de
`usage.json` : rien, et l'agent reste un citoyen de première classe.

Les traces d'une tentative sont auditables après coup : le journal porte
le nœud, le passage et la tentative dans son nom, et le prompt comme
l'usage y sont rangés à la fin. Rien de l'histoire d'un item n'est jamais
réécrit — seul `outcome.json` reste transitoire, puisque son contenu vit
dans le résultat du run en base.

L'agent travaille dans son propre univers : sa `GRAPHATOM_DSN` est la base
jetable de son item — une par item, créée à la volée —, jamais celle du
rail ni celle du voisin. Sa clé de sujet est dans l'environnement : le
cleanup s'en sert pour reconnaître son propre worktree.

Un candidat de fan-out, lui, a son propre workspace sous celui de l'item —
`data/item-<N>/c<k>/` — et son propre atelier git, voir `worktree`. Rien
d'autre ne le distingue de son voisin, sinon la variante que sa config
porte : c'est elle qui s'interpole dans son prompt et dans sa commande.

Le bloc n'a jamais à deviner où est son checkout : `GRAPHATOM_WORKTREE` le
lui dit, celui de son candidat ou celui de son item selon ce qu'il est.

Un JUDGE qui déclare `finalists_from` ne joue pas un agent comme les autres :
il départage les finalistes que la réduction `keep_n` du nœud nommé a laissés
passer. Un seul finaliste, et il est traversé sans dépenser un jeton ;
plusieurs, et un modèle lit leurs diffs — anonymes — puis élit ; aucun, et le
travail repart en amont. Voir `judge` et `_arbitrate`.

L'agent tourne dans son propre groupe de processus : au timeout, c'est
tout le groupe qui est révoqué — un descendant ne survit pas au bail.
Le pgid est aussi persisté dans le workspace : si c'est le worker qui
meurt, le faucheur du suivant y trouve de quoi tuer l'orphelin.

Une tentative crashée rend son autopsie : code de sortie, queue du log et
flag timeout. Le post-mortem se lit dans le résultat du run, pas en
fouillant le workspace à la main.

Un agent muet ne consomme plus son budget entier. Un chien de garde relève
trois signaux mécaniques — la taille du journal, le mtime le plus récent du
workspace, celui du worktree — et coupe dès que les trois n'ont pas bougé
pendant `silence_s`. Aucun modèle, aucune interprétation.

Au couperet — chien de garde ou budget total, peu importe lequel est tombé —
c'est le progrès constaté qui fait l'issue. Un agent qui avait produit
déborde de son budget : `timed_out`, escalade directe, relancer à
l'identique rebrûlerait le même budget pour retomber au même endroit. Un
agent qui n'avait rien produit était pendu : `stalled`, une panne d'infra
comme une autre, que le noyau relance sur place.

Et une relance est une reprise, jamais une répétition : le prompt de la
tentative suivante porte l'état déjà là — le `git diff` du worktree et les
fichiers du workspace. Repartir à l'aveugle, c'est payer le trajet deux fois.
La question n'est pas « est-ce la première tentative ? » mais « y a-t-il
quelque chose à reprendre ? » : une tentative 1 d'un passage neuf, ouverte
par un `retry` d'escalade, hérite du travail que le passage précédent a
laissé, et son prompt le porte comme n'importe quelle relance. Le motif est
nommé : un budget dépassé et une pendaison ne laissent pas la même chose.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import psycopg

from . import db, effects, executors, worktree
from .graph import KERNEL_OUTCOMES, judge_source
from .worktree import run_git, run_worktree

DATA_DIR = Path("data")  # les tests le font pointer sur un répertoire temporaire
OUTBOX_NAME = "effects_outbox.log"  # sous DATA_DIR, résolu au moment de l'effet
GRACE_S = 5.0  # entre le SIGTERM et le SIGKILL du groupe de l'agent
PGID_FILE = "agent.pgid"  # la trace qui survit au worker, écrasée à chaque tentative
OUTCOME_NAME = "outcome.json"  # transitoire : purgé avant chaque tentative
STARVED_NAME = "starved.json"  # idem — une panne de crédits nommée par l'adaptateur
PROMPT_NAME = "prompt.md"  # l'interface avec le cmd, archivée après la tentative
USAGE_NAME = "usage.json"  # idem — ce que l'agent veut bien dire de sa consommation
FAILURE_NAME = "failure.json"  # emplacement stable du dernier échec de l'item
TAIL_LINES = 20  # queue du log rendue dans l'autopsie d'une tentative crashée
TAIL_CHARS = 2000
REPORT_CHARS = 4000  # queue du compte rendu citée dans la trace d'échec
# Le noyau a ses propres issues d'échec. Le domaine garde les noms négatifs
# employés par les graphes. Une issue n'est un échec que si elle est dans l'un
# de ces deux ensembles : une réussite, même non terminale, ne laisse donc pas
# de fausse trace.
DOMAIN_FAILURE_OUTCOMES = {
    "abandon", "abandonner", "conflict", "expired", "fail", "failed",
    "none", "raise", "reformuler", "retry", "revise", "stuck",
    "uncertain", "unclear",
}
AGENT_TIMEOUT_S = 570  # budget d'une tentative d'agent, quand le nœud n'en dit rien
AGENT_SILENCE_S = 180  # silence toléré par le chien de garde, idem
PROBE_S = 5.0  # granularité du relevé de progrès, resserrée par un silence court
POLL_S = 0.2  # granularité de l'attente du process : ce qui borne le budget total
PRUNED_DIRS = {".git", "node_modules", ".next", "__pycache__", ".venv"}
GIT_CHARS = 8000  # l'état du worktree cité dans le prompt d'une reprise
GIT_TIMEOUT_S = 20
BASE_REF = "origin/main"  # la base des worktrees d'item : ce qui est commité s'y compare


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


def failure_path(item_id: int) -> Path:
    """L'emplacement unique de la trace du dernier échec d'un item."""
    return item_workspace(item_id) / FAILURE_NAME


def is_failure_outcome(outcome: str) -> bool:
    """Une issue d'échec est une issue du noyau ou un nom négatif du domaine."""
    return outcome in KERNEL_OUTCOMES or outcome in DOMAIN_FAILURE_OUTCOMES


class Context:
    def __init__(self, conn: psycopg.Connection, run: dict, item: dict,
                 node: dict, bundle: dict):
        self.conn = conn
        self.run = run
        self.item = item
        self.node = node
        self.bundle = bundle
        self.config = node.get("config") or {}
        # ce qu'un bloc ajoute au prompt de son nœud, tel quel : posé après
        # l'interpolation, il n'est ni rempli ni expansé — un diff qui porte
        # un `$HOME` reste un diff, il ne devient pas un chemin
        self.extra = ""
        self.workspace = run_workspace(item["id"], run)
        self.workspace.mkdir(parents=True, exist_ok=True)
        # l'atelier suit le bureau : celui du candidat, ouvert ici s'il ne
        # l'est pas encore, ou celui de l'item — que le shell du graph prépare
        self.worktree = run_worktree(item["id"], run)

    def simulate_work(self) -> None:
        time.sleep(float(self.config.get("duration_s", 0)))


def _agent(ctx: Context) -> dict:
    """Exécute l'agent CLI configuré, et range ses traces sous son nom.

    L'usage de tokens rejoint le résultat quelle que soit l'issue : une
    tentative crashée a consommé, elle aussi, et c'est là qu'on veut le
    voir. L'archivage, lui, a lieu même si le bloc est interrompu.
    """
    workspace = ctx.workspace.resolve()
    try:
        return _attempt(ctx, workspace) | _usage(workspace)
    finally:
        _archive(workspace, attempt_name(ctx.run))


def _usage(workspace: Path) -> dict:
    """L'usage de tokens de la tentative, si l'agent en a laissé un.

    Le format est celui que l'agent rapporte — `input_tokens`, les caches,
    le coût, ce qu'il veut : personne ici ne l'interprète, le noyau ne
    connaît toujours aucun agent. Pas de fichier, fichier illisible ou
    vide : rien du tout, exactement comme avant.
    """
    try:
        usage = json.loads((workspace / USAGE_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return {"usage": usage} if isinstance(usage, dict) and usage else {}


def _archive(workspace: Path, name: str) -> None:
    """Range les traces de nom fixe sous le nom de la tentative.

    `prompt.md` et `usage.json` sont l'interface avec le `cmd` : des noms
    fixes, que le nœud suivant écraserait. La tentative finie, ils
    deviennent des pièces d'archive — `prompt-<nœud>-<passage>-<tentative>.md`,
    `usage-…json`, à côté du journal déjà nommé ainsi. L'histoire complète
    d'un item se lit alors dans son workspace, sans rien de réécrit.
    """
    for fixed in (PROMPT_NAME, USAGE_NAME):
        path = workspace / fixed
        if path.is_file():
            path.replace(path.with_stem(f"{path.stem}-{name}"))


VARIANT_TOKENS = ("label", "strategy")  # ce qu'une variante de fan-out interpole


def _fill(ctx: Context, text: str, subject: str) -> str:
    """Interpole le sujet et la variante du candidat — dans un prompt, ou une commande.

    `{subject_key}` est là depuis toujours. `{label}` et `{strategy}` viennent
    de la variante, que la matérialisation a posée dans la config du nœud :
    c'est ce qui donne à N candidats des angles différents plutôt que N fois
    la même erreur. Un jeton dont la config ne dit rien reste littéral — un
    nœud sans fan-out ne voit donc aucune différence.
    """
    text = text.replace("{subject_key}", subject)
    for token in VARIANT_TOKENS:
        value = ctx.config.get(token)
        if value is not None:
            text = text.replace("{" + token + "}", str(value))
    return text


def _prompt(ctx: Context, workspace: Path, subject: str) -> str:
    """Le prompt de la tentative : celui du nœud, le contrat, et la reprise."""
    outcomes = sorted(ctx.node.get("edges") or {})
    outcome_path = workspace / OUTCOME_NAME
    return os.path.expandvars(
        _fill(ctx, ctx.config["agent"]["prompt"], subject)
    ) + ctx.extra + (
        "\n\n--- Contrat GraphAtom ---\n"
        f"Tu es le bloc « {ctx.run['node']} » d'un rail d'exécution. "
        f"Ton workspace : {workspace}\n"
        + _commun(ctx) +
        f"Avant de terminer, écris impérativement {outcome_path} : "
        f'{{"outcome": <une valeur parmi {outcomes}>, "summary": "<une phrase>"}}\n'
        "Sans ce fichier, ta tentative est classée crashed et sera retentée."
    ) + _reprise(ctx, workspace)


def _commun(ctx: Context) -> str:
    """Où sont les fichiers communs de l'item, quand ce ne sont pas les siens.

    Un candidat de fan-out a son bureau à lui, sous celui de l'item : les
    fichiers que le cycle entier partage — `criteria.md`, `validate.md` — y
    sont d'un cran au-dessus. Le dire ici évite qu'un candidat cherche son
    cahier des charges à côté de son propre journal et ne trouve rien. Un run
    sans candidat n'a rien à distinguer : son workspace est celui de l'item.
    """
    if ctx.run.get("candidate") is None:
        return ""
    return (f"Les fichiers communs du cycle — criteria.md, validate.md — sont dans "
            f"le workspace de l'item : {item_workspace(ctx.item['id']).resolve()}\n")


DEATHS = {  # ce qui a tué la tentative précédente, dit en clair au repreneur
    "timed_out": "a dépassé son budget : le couperet l'a coupée en plein travail, "
                 "et ce qu'elle avait fait est resté là",
    "stalled": "est restée pendue : le chien de garde l'a coupée sans qu'elle ait "
               "produit un octet — ce qui suit vient donc d'avant elle",
    "crashed": "s'est arrêtée sans rendre d'issue lisible, et ce qu'elle avait "
               "fait est resté là",
}


def _death(previous: dict) -> str:
    """La mort précédente, avec la raison du fournisseur s'il l'a donnée."""
    if previous["outcome"] == "starved":
        result = previous.get("result") or {}
        provider = result.get("provider")
        reason = result.get("reason")
        if provider and reason:
            return (f"a perdu son fournisseur {provider} : {reason}. "
                    "Ce qu'elle avait fait est resté là")
    return DEATHS.get(previous["outcome"],
                      f"a rendu « {previous['outcome']} », et ce qu'elle a "
                      "laissé est toujours là")


def _last_attempt(ctx: Context) -> dict | None:
    """La dernière tentative achevée de ce nœud, tous passages confondus.

    Elle dit deux choses : qu'il y a bien eu quelque chose avant — un nœud
    qui n'a jamais tourné n'a rien à reprendre —, et de quoi elle est morte,
    ce que le prompt de la reprise nomme. La tentative en cours n'a pas
    encore d'issue : `outcome IS NOT NULL` l'écarte sans avoir à connaître
    son numéro.

    Le passage ne filtre pas : un `retry` d'escalade ouvre un passage neuf,
    donc une tentative 1, sur un worktree que le passage précédent a rempli.
    C'est exactement le cas qu'une reprise doit couvrir.
    """
    return ctx.conn.execute(
        "SELECT cycle, attempt, outcome, result FROM node_run "
        "WHERE item_id = %s AND node = %s AND outcome IS NOT NULL "
        "ORDER BY cycle DESC, attempt DESC LIMIT 1",
        (ctx.item["id"], ctx.run["node"]),
    ).fetchone()


def _work_files(workspace: Path) -> list[str]:
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
                  and not p.name.startswith(("agent-", "prompt-", "usage-")))


def _git(worktree: Path, *args: str) -> tuple[int, str]:
    """Une commande git dans le worktree de l'item : son code et sa sortie.

    Un git qui rate n'est pas un échec de la tentative. Le code dit à
    l'appelant s'il peut croire la sortie ; la plainte, elle, part telle
    quelle dans le prompt plutôt que de faire tomber le bloc. Le délai est
    plus court que celui du dépôt (`worktree.GIT_TIMEOUT_S`) : c'est l'état
    d'un seul worktree pour un prompt, pas une opération qui peut attendre
    un verrou.
    """
    return run_git(worktree, *args, timeout=GIT_TIMEOUT_S)


def _committed(worktree: Path) -> str:
    """Les commits de la branche de l'item qu'`origin/main` n'a pas encore.

    Depuis qu'on demande aux agents de commiter au fil de l'eau, un worktree
    propre n'est plus un worktree vide : le travail d'une tentative coupée
    peut tenir entier dans ses commits. Pas d'`origin/main` sous la main — un
    dépôt de test, un fetch jamais fait : rien, et le statut reste seul juge.
    """
    if _git(worktree, "rev-parse", "--verify", "--quiet", BASE_REF)[0] != 0:
        return ""
    code, out = _git(worktree, "log", "--oneline", f"{BASE_REF}..HEAD")
    return out if code == 0 else ""


def _worktree_work(worktree: Path | None) -> bool:
    """Le worktree porte-t-il du travail à reprendre ?

    Deux formes, et une seule suffit : ce qui n'est pas commité, que
    `git status` montre du modifié comme du neuf, et ce qui l'est déjà sans
    être fusionné. C'est le signal mécanique de l'issue, sans jugement ni
    modèle. Pas de worktree, ou un git qui rate : faux — le workspace reste
    alors le seul signal, et une reprise sans rien à reprendre ne se pose pas.
    """
    if worktree is None:
        return False
    code, out = _git(worktree, "status", "--short")
    return (code == 0 and bool(out)) or bool(_committed(worktree))


def _reprise(ctx: Context, workspace: Path) -> str:
    """L'état laissé par une tentative antérieure — jamais une répétition.

    Une tentative qui démarre derrière une autre hérite d'un worktree et d'un
    workspace déjà entamés. Redémarrer à l'aveugle, c'est repayer le trajet
    depuis zéro : le prompt porte donc le `git diff` du worktree et la liste
    des fichiers du workspace.

    Deux conditions, toutes deux mécaniques. Il faut une tentative antérieure
    du même nœud — quel que soit son passage, un `retry` d'escalade en ouvre
    un neuf sans rien effacer. Et il faut quelque chose à reprendre : du
    travail dans le worktree, ou un fichier d'agent dans le workspace. Sans
    l'un ni l'autre, aucun bloc — un agent qui recommence vraiment de zéro ne
    doit pas lire un état imaginaire.

    Le motif est nommé, parce qu'un état sans provenance passe pour du
    travail étranger : un budget dépassé, une pendaison et une panne ne
    laissent pas la même chose derrière elles.
    """
    previous = _last_attempt(ctx)
    if previous is None:  # ce nœud n'a jamais tourné sur cet item
        return ""
    worktree = ctx.worktree  # celui du candidat, ou celui de l'item
    if not _work_files(workspace) and not _worktree_work(worktree):
        return ""  # rien à reprendre : pas de reprise inventée
    # la liste, elle, ne cache rien : les traces des tentatives passées se
    # lisent aussi, et leur journal dit souvent où celle d'avant s'est arrêtée
    files = sorted(p.name for p in workspace.iterdir() if p.is_file())
    death = _death(previous)
    return (
        f"\n\n--- Reprise de la tentative {previous['attempt']} du passage "
        f"{previous['cycle']} ---\n"
        f"La tentative précédente de « {ctx.run['node']} » {death}. Lis l'état "
        "ci-dessous et continue là où le travail s'est arrêté — ne recommence "
        "pas de zéro.\n\n"
        f"État du worktree {worktree or '(aucun)'} :\n\n"
        f"```\n{_git_state(worktree)}\n```\n\n"
        "Fichiers déjà écrits dans ton workspace :\n"
        + "\n".join(f"- {name}" for name in files or ["(aucun)"])
    )


def _git_state(worktree: Path | None) -> str:
    """L'état git du worktree de l'item, borné en taille.

    Trois vues, dans l'ordre où on les lit : le statut dit les fichiers neufs
    que le diff ne montre pas encore, le diff dit ce qui a changé, et le
    journal face à `origin/main` dit ce que la tentative d'avant a déjà
    commité — sans lui, un worktree commité au fil de l'eau se lirait vide.
    Un git qui rate n'est pas un échec de la tentative : sa plainte part dans
    le prompt telle quelle.
    """
    if worktree is None:
        return "aucun worktree pour cet item"
    parts = []
    for args in (["status", "--short"], ["diff", "HEAD"]):
        parts.append(f"$ git {' '.join(args)}\n{_git(worktree, *args)[1] or '(rien)'}")
    commits = _committed(worktree)
    if commits:
        parts.append(f"$ git log --oneline {BASE_REF}..HEAD\n{commits}")
    state = "\n\n".join(parts)
    return state if len(state) <= GIT_CHARS else state[:GIT_CHARS] + "\n… (tronqué)"


def _latest_mtime(root: Path | None) -> float:
    """Le mtime le plus récent d'une arborescence, machinerie exclue.

    `.git`, `node_modules` et les caches de build bougent sans qu'un agent y
    soit pour rien — un index git rafraîchi par le voisin n'est pas du
    travail. Les exclure garde le signal mécanique et honnête : ce qui reste
    est du fichier de travail.
    """
    if root is None:
        return 0.0
    latest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS]
        for name in filenames:
            try:
                latest = max(latest, os.stat(Path(dirpath) / name).st_mtime)
            except OSError:  # fichier disparu pendant la marche : il a bougé, tant pis
                pass
    return latest


def _mark(log: Path, workspace: Path, worktree: Path | None) -> tuple:
    """Les trois signaux du progrès d'un agent, en un relevé.

    Taille du journal, mtime le plus récent du workspace, mtime le plus
    récent du worktree. Deux relevés identiques, c'est un agent qui n'a rien
    produit dans l'intervalle : ni un octet, ni un fichier touché.
    """
    try:
        size = log.stat().st_size
    except OSError:  # journal pas encore là : zéro octet, comme un journal vide
        size = 0
    return size, _latest_mtime(workspace), _latest_mtime(worktree)


def _progress(mark: tuple, fresh: tuple) -> bool:
    """L'agent a-t-il produit quoi que ce soit avant le couperet ?

    Deux signaux, et un seul suffit : des octets dans le journal de la
    tentative, ou un fichier du workspace ou du worktree touché depuis le
    relevé de départ. Le journal se juge sur son contenu et non sur sa
    croissance — un agent qui écrit sa première ligne avant même que le
    relevé de départ soit pris a produit, et le compter muet le ferait
    escalader comme une pendaison alors qu'il avait bel et bien démarré.
    """
    return fresh[0] > 0 or fresh[1:] != mark[1:]


def _wait(proc: subprocess.Popen, watched: tuple, mark: tuple,
          budget_s: float, silence_s: float) -> None:
    """Attend l'agent jusqu'au premier des deux couperets.

    Deux budgets, une seule attente. Le budget total borne la tentative ; le
    silence toléré borne l'inactivité. Le relevé de départ est pris une fois
    l'agent lancé — prompt, journal et trace de pgid sont déjà écrits, donc
    tout ce qui bougera ensuite est de l'agent et de lui seul.

    Rend la main quand le process meurt de sa belle mort ; lève
    `TimeoutExpired` quand un couperet tombe, comme `proc.wait` le ferait.
    """
    from .quota import WAIT_FILE

    start = time.monotonic()
    deadline, probe_s = start + budget_s, min(PROBE_S, silence_s / 4)
    quiet_since, next_probe = start, start + probe_s
    last = start
    while True:
        try:
            proc.wait(timeout=POLL_S)
            return  # mort de sa belle mort : l'issue se lit dans outcome.json
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        # Une commande lourde qui attend le quota n'a pas encore commencé.
        # Son marqueur est renouvelé par le preneur en même temps que le bail.
        # Cette durée ne mange donc ni le couperet total ni celui du silence.
        if (watched[1] / WAIT_FILE).exists():
            deadline += now - last
            quiet_since = now
        last = now
        if now >= next_probe:
            next_probe = now + probe_s
            fresh = _mark(*watched)
            if fresh != mark:
                mark, quiet_since = fresh, now
            elif now - quiet_since >= silence_s:
                raise subprocess.TimeoutExpired(
                    "chien de garde : ni un octet ni un fichier touché", silence_s)
        if now >= deadline:
            raise subprocess.TimeoutExpired(proc.args, budget_s)


def _agent_timeout_s(config: dict) -> float:
    """Le couperet du process d'un nœud — dérivé du bail par le kernel.

    L'import est local au corps : `kernel` importe `blocks` à son tour, et
    les deux ne peuvent pas se charger l'un l'autre au module.
    """
    from .kernel import agent_timeout_s
    return agent_timeout_s(config, AGENT_TIMEOUT_S)


def _attempt(ctx: Context, workspace: Path) -> dict:
    """Une tentative d'agent. Contrat : prompt.md → outcome.json."""
    cfg = ctx.config["agent"]
    subject = ctx.conn.execute(
        "SELECT subject_key FROM subject WHERE id = %s", (ctx.item["subject_id"],)
    ).fetchone()["subject_key"]

    outcome_path = workspace / OUTCOME_NAME
    starved_path = workspace / STARVED_NAME
    for transient in (outcome_path, starved_path, workspace / USAGE_NAME):
        transient.unlink(missing_ok=True)  # rien de la tentative précédente
    (workspace / PROMPT_NAME).write_text(_prompt(ctx, workspace, subject))

    env = os.environ | {"GRAPHATOM_WORKSPACE": str(workspace),
                        "GRAPHATOM_SUBJECT_KEY": subject,
                        "GRAPHATOM_ITEM_ID": str(ctx.item["id"]),
                        "GRAPHATOM_RUN_ID": str(ctx.run["id"]),
                        "GRAPHATOM_LEASE_S": str(ctx.config.get("lease_s", 30)),
                        # Copiée avant que la base jetable de l'item remplace
                        # GRAPHATOM_DSN : toutes les places vivent ici.
                        "GRAPHATOM_QUOTA_DSN": db.DSN}
    if ctx.worktree is not None:
        # le bloc ne devine pas son checkout : celui d'un candidat n'est pas
        # celui de son item, et aucune convention de nom ne l'en déduit
        env["GRAPHATOM_WORKTREE"] = str(ctx.worktree)
    dsn = db.agent_dsn(ctx.item["id"])
    if dsn:
        # l'agent ne voit jamais la base du rail : la sienne est jetable, et
        # à lui seul — ce que son ordonnanceur de test y détruit ne regarde
        # ni la production ni les autres items
        env["GRAPHATOM_DSN"] = dsn
    log = attempt_log(workspace, ctx.run)
    pgid_file = workspace / PGID_FILE
    watched = (log, workspace, ctx.worktree)
    resolved = executors.resolve(ctx.bundle, ctx.node)
    env.update(executors.environment(resolved))
    cmd = _fill(ctx, executors.command(resolved), subject)
    with log.open("w") as out:
        out.write(f"$ {cmd}\n")
        out.flush()
        # session dédiée : l'agent est chef de son groupe, ses descendants aussi
        proc = subprocess.Popen(
            cmd, shell=True, cwd=workspace, env=env, start_new_session=True,
            stdout=out, stderr=subprocess.STDOUT,
        )
        try:
            _write_pgid(pgid_file, proc, cmd, ctx.run["id"])
            mark = _mark(*watched)  # nos traces sont écrites : la suite est de l'agent
            _wait(proc, watched, mark,
                  _agent_timeout_s(ctx.config),
                  float(cfg.get("silence_s", AGENT_SILENCE_S)))
        except subprocess.TimeoutExpired as exc:
            # le relevé se prend avant la révocation : ce que le SIGTERM
            # arrache à l'agent n'est pas du travail qu'il a fait
            progress = _progress(mark, _mark(*watched))
            _kill_group(proc)  # le bail expire : le groupe entier est révoqué
            return _autopsy(proc, log, exc, timeout=True, progress=progress)
        except BaseException:  # interruption du bloc : on révoque, puis on remonte
            _kill_group(proc)
            raise  # l'erreur remonte après la révocation : tentative crashed
        finally:  # le worker a tenu jusqu'au bout : la trace n'a plus d'usage
            pgid_file.unlink(missing_ok=True)

    try:
        data = json.loads(outcome_path.read_text())
        outcome, summary = data["outcome"], data.get("summary", "")
    except (OSError, ValueError, KeyError, TypeError) as exc:  # pas d'issue valide
        return _starved(starved_path) or _autopsy(proc, log, exc, timeout=False)
    return {"outcome": outcome, "summary": summary}


def _starved(path: Path) -> dict | None:
    """La panne de fournisseur déclarée par l'adaptateur, si elle est valide.

    Le bloc ne reconnaît aucun message de CLI. Il ne fait que lire le contrat
    de fichier : deux chaînes non vides, le fournisseur et sa raison exacte.
    Un fichier absent ou mal formé ne change rien au verdict `crashed`.
    """
    try:
        data = json.loads(path.read_text())
        provider, reason = data["provider"], data["reason"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(value, str) and value.strip()
               for value in (provider, reason)):
        return None
    return {"outcome": "starved", "provider": provider, "reason": reason}


def _autopsy(proc: subprocess.Popen, log: Path, exc: BaseException,
             timeout: bool, progress: bool = True) -> dict:
    """Le post-mortem d'une tentative ratée, dans le résultat du run.

    Le code de sortie est celui du processus agent — négatif, c'est le
    signal qui l'a tué (-9 pour le SIGKILL de la révocation).

    L'issue dit lequel des trois ratés c'était :

    - `crashed` : la tentative est allée au bout sans rendre d'issue lisible ;
    - `timed_out` : un couperet est tombé sur un agent qui avait produit —
      la tâche déborde vraiment de son budget ;
    - `stalled` : un couperet est tombé sur un agent qui n'avait rien
      produit — il était pendu, c'est de l'infra et pas de la tâche.

    Le post-mortem, lui, est le même des trois côtés : `error`, `exit_code`
    et `log_tail` y sont toujours, et le flag `timeout` redit en clair que
    c'est un couperet qui a tranché.
    """
    if not timeout:
        outcome = "crashed"
    else:
        outcome = "timed_out" if progress else "stalled"
    return {"outcome": outcome,
            "error": f"{type(exc).__name__}: {exc}",
            "timeout": timeout, "exit_code": proc.returncode, "log_tail": _tail(log)}


def lease_autopsy(item_id: int, run: dict, alive: bool, orphaned: bool) -> dict:
    """Le post-mortem d'une tentative fauchée par son bail.

    Même forme que `_autopsy`, sans code de sortie : le faucheur n'a pas de
    handle Popen. Ce qu'il sait, lui, c'est si l'agent travaillait encore à
    l'expiration du bail — un budget dépassé, donc `timed_out` — ou s'il
    était déjà mort — une panne, donc `crashed`. Le journal de la tentative
    se retrouve par le nom de ses traces.

    `orphaned` : le worker qui a réservé ce run n'est plus là — il a
    redémarré, et le run est resté `running` derrière lui. L'issue est la
    même, la cause probable non, et c'est elle qu'on lit : un redémarrage
    emporte tous les fils en vol d'un coup, et leurs agents avec. Le dire
    évite de chercher des agents instables quand c'est le worker qui tombe.
    """
    agent = "encore vivant" if alive else "déjà mort"
    return {"outcome": "timed_out" if alive else "crashed",
            "error": (f"run emporté par un redémarrage du worker — agent orphelin, "
                      f"{agent} : le worker est tombé, pas l'agent"
                      if orphaned else f"bail expiré, agent {agent}"),
            "timeout": alive, "exit_code": None,
            "log_tail": _tail(attempt_log(run_workspace(item_id, run), run))}


def _tail(log: Path) -> str:
    """Les dernières lignes du journal de l'agent, bornées en taille."""
    try:
        lines = log.read_text(errors="replace").splitlines()[-TAIL_LINES:]
    except OSError as exc:  # un log illisible est lui-même une information
        return f"[journal illisible : {exc}]"
    return "\n".join(lines)[-TAIL_CHARS:]


def _attempt_marker(workspace: Path, run: dict) -> Path | None:
    """La trace qui date le début de la tentative, archivée ou encore active."""
    archived = workspace / f"prompt-{attempt_name(run)}.md"
    if archived.is_file():
        return archived
    active = workspace / PROMPT_NAME
    return active if active.is_file() else None


def _attempt_report(workspace: Path, run: dict) -> dict | None:
    """Le compte rendu Markdown écrit pendant cette tentative, s'il existe.

    Le nom du rapport n'est pas une convention du noyau : `deploy` écrit
    `deploy.md`, mais `implement` peut écrire `implementation.md` puis
    `portes.md`. Le prompt de la tentative sert donc de borne temporelle, et
    le dernier Markdown touché après lui est le rapport rendu par ce nœud.
    Les prompts archivés et la trace stable ne sont jamais des candidats.
    """
    marker = _attempt_marker(workspace, run)
    if marker is None:
        return None
    try:
        started = marker.stat().st_mtime_ns
        reports = [
            path for path in workspace.glob("*.md")
            if not path.name.startswith("prompt-")
            and path.name != PROMPT_NAME
            and path.stat().st_mtime_ns >= started
        ]
    except OSError:
        return None
    if not reports:
        return None
    report = max(reports, key=lambda path: (path.stat().st_mtime_ns, path.name))
    try:
        content = report.read_text(errors="replace")[-REPORT_CHARS:]
    except OSError as exc:
        content = f"[compte rendu illisible : {exc}]"
    return {"name": report.name, "content": content}


def write_failure_trace(item_id: int, run: dict, outcome: str) -> Path | None:
    """Remplace la trace stable quand un run rend une issue d'échec.

    Agent, adaptateur de fournisseur et script shell ont tous le même contrat
    de bloc. Le rail connaît donc le nœud et l'issue du run, le journal de sa
    tentative et le dernier compte rendu Markdown écrit pendant cette
    tentative. Pour `starved`, il garde aussi le fournisseur et sa raison.
    Journal et rapport sont bornés par `TAIL_LINES`/`TAIL_CHARS` et
    `REPORT_CHARS`.

    Une réussite ne touche rien. Un item neuf ne reçoit ainsi aucune trace, et
    une réussite ultérieure conserve bien le dernier échec observé. Chaque
    nouvel échec remplace le JSON entier au même chemin, sans accumulation.
    """
    if not is_failure_outcome(outcome):
        return None
    workspace = run_workspace(item_id, run)
    path = failure_path(item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    trace = {
        "node": run["node"],
        "outcome": outcome,
        "log_tail": _tail(attempt_log(workspace, run)),
        "report": _attempt_report(workspace, run),
    }
    result = run.get("result") or {}
    if outcome == "starved":
        for name in ("provider", "reason"):
            value = result.get(name)
            if isinstance(value, str) and value.strip():
                trace[name] = value
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
    return path


def _kill_group(proc: subprocess.Popen) -> None:
    """Révoque le groupe entier : SIGTERM, une grâce, puis SIGKILL.

    L'expiration d'un bail révoque l'autorité — donc aussi celle des
    descendants. Tuer le seul shell laisserait des orphelins (chromium,
    serveurs de test, sous-agents) travailler sans autorité.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:  # déjà parti, rien à révoquer
        return
    _signal_group(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=GRACE_S)  # le chef de groupe part le premier
    except subprocess.TimeoutExpired:
        pass
    _signal_group(pgid, signal.SIGKILL)
    proc.wait()  # récolte le chef de groupe, pas de zombie


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:  # plus personne dans le groupe
        pass


def _write_pgid(path: Path, proc: subprocess.Popen, cmd: str, run_id: int) -> None:
    """Persiste de quoi révoquer l'agent sans handle Popen.

    `start_new_session` fait de l'agent le chef de son groupe : son pid est
    donc le pgid de tout ce qu'il lancera. L'identité dit lequel, le run dit
    à qui la trace appartient, la commande est pour l'humain qui lit le
    fichier. Écriture atomique : le faucheur ne lit jamais une demi-trace.
    """
    trace = {"run": run_id, "pgid": proc.pid, "cmd": cmd,
             "identity": _identity(proc.pid)}
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(trace))
    tmp.replace(path)


def _identity(pid: int) -> dict | None:
    """Ce qui distingue un processus d'un homonyme : son boot et sa naissance.

    Un pid se recycle ; ni la date de naissance (en tops d'horloge depuis le
    boot) ni le boot lui-même ne suivent. La ligne de commande, elle, ne dit
    rien de sûr : elle est encore vide au retour de Popen, et le shell la
    réécrit s'il s'exec-optimise. Pas de /proc, pas d'identité — donc None.
    """
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, IndexError):
        return None
    return {"boot": boot, "starttime": int(fields[19])}  # champ 22 de proc(5)


def _trace(item_id: int, run_id: int) -> tuple[Path, dict] | None:
    """La trace de pgid de ce run, et où elle est. None si elle n'y est pas.

    Le workspace de l'item porte la sienne, et chaque candidat de fan-out la
    sienne dans son sous-répertoire : le faucheur n'a qu'un numéro de run, il
    les regarde donc toutes et suit celle qui le nomme. Une trace illisible
    ou amputée n'en est pas une — elle ne fait tomber personne.
    """
    workspace = item_workspace(item_id)
    for path in (workspace / PGID_FILE, *sorted(workspace.glob(f"c*/{PGID_FILE}"))):
        try:
            trace = json.loads(path.read_text())
            run, _, _ = trace["run"], trace["pgid"], trace["identity"]
        except (OSError, ValueError, KeyError):
            continue
        if run == run_id:
            return path, trace
    return None


def agent_alive(item_id: int, run_id: int) -> bool:
    """L'agent de ce run travaille-t-il encore ? La lecture de `revoke_orphan`,
    sans rien tuer.

    C'est ce que le faucheur interroge pour trancher entre un dépassement de
    budget et une panne. Les mêmes garde-fous, donc : la trace doit être
    celle du run — une tentative suivante l'écrase — et le chef du groupe
    toujours celui qu'on a lancé, un pid se recycle mais pas son identité.
    Pas de trace, trace d'un autre run, identité périmée : plus personne au
    travail.
    """
    found = _trace(item_id, run_id)
    if found is None:
        return False
    trace = found[1]
    who = trace["identity"]
    return who is not None and _identity(trace["pgid"]) == who


def revoke_orphan(item_id: int, run_id: int) -> int | None:
    """Tue le groupe de l'agent d'un run fauché. Rend le pgid tué.

    Le faucheur n'a pas de handle Popen — il n'a que le `agent.pgid` laissé
    dans le workspace de l'item. Révoquer l'autorité en base ne suffit pas :
    l'orphelin continue d'écrire dans le checkout et le workspace.

    Trois garde-fous, parce que tuer un innocent est pire qu'un orphelin :
    la trace doit être celle du run fauché (une tentative suivante l'écrase,
    et un candidat voisin a la sienne), le chef du groupe doit toujours être
    celui qu'on a lancé — un pid se recycle, pas une identité — et le
    faucheur ne se fauche jamais lui-même.
    """
    found = _trace(item_id, run_id)
    if found is None:  # pas d'agent en vol, trace illisible, ou trace d'un autre run
        return None
    path, trace = found
    pgid, who = trace["pgid"], trace["identity"]
    path.unlink(missing_ok=True)  # une trace ne sert qu'à une révocation

    if who is None or _identity(pgid) != who:
        return None  # groupe éteint, ou pid déjà repris par un innocent
    if pgid == os.getpgid(0):
        return None  # trace aberrante : un faucheur qui se tue ne fauche plus rien

    _signal_group(pgid, signal.SIGTERM)
    deadline = time.time() + GRACE_S
    while time.time() < deadline and _identity(pgid) == who:
        time.sleep(0.1)
    # le pgid n'est pas recyclable tant que le groupe a un membre : sans chef,
    # le SIGKILL reste pour les descendants ou ne trouve plus personne
    _signal_group(pgid, signal.SIGKILL)
    print(f"orphelin révoqué : groupe {pgid} ({trace.get('cmd', '?')})", flush=True)
    return pgid


def fetch(ctx: Context) -> dict:
    ctx.simulate_work()
    name = f"evidence-{ctx.run['node']}-{ctx.run['cycle']}-{ctx.run['attempt']}.json"
    evidence = ctx.workspace / name
    evidence.write_text(json.dumps({"materialized": ctx.config.get("materialize", [])}))
    return {"outcome": "ok", "evidence": evidence.name}


def judge(ctx: Context) -> dict:
    if judge_source(ctx.node):  # un JUDGE qui départage des finalistes
        return _arbitrate(ctx)
    if "agent" in ctx.config:
        return _agent(ctx)
    ctx.simulate_work()
    # le stub répond ce que la config scripte ; un vrai JUDGE appelle un agent
    return {"outcome": ctx.config["stub_outcome"]}


# ------------------------------------------------------------------- arbitrage

VERDICT_NAME = "verdict.md"  # le verdict du juge, comme `validate.md` du validate
JUDGE_WORK_CHARS = 30000  # le travail d'un finaliste cité dans le prompt du juge
LETTRES = "ABCDEFGH"  # le nom anonyme d'un finaliste dans le dossier du juge


def _finalists(ctx: Context) -> list[int]:
    """Les candidats que la réduction `keep_n` a laissés passer, dans l'ordre.

    Ce sont les runs encore `applied` de la dernière tentative du nœud de
    fan-out d'amont : `keep_n` a recalé les autres réussites en `superseded`,
    et les échecs portent une issue du noyau. Le statut fait donc foi, sans
    table de plus ni colonne de plus.

    L'ordre est celui des dates de fin, celui-là même dont `keep_n` s'est
    servie : deux juges qui lisent le même item lisent le même dossier.
    """
    source = judge_source(ctx.node)
    rows = ctx.conn.execute(
        "SELECT candidate, status, outcome FROM node_run WHERE item_id = %s "
        "AND node = %s AND (cycle, attempt) = ("
        "  SELECT cycle, attempt FROM node_run WHERE item_id = %s AND node = %s "
        "  ORDER BY cycle DESC, attempt DESC LIMIT 1) "
        "ORDER BY finished_at, id",
        (ctx.item["id"], source, ctx.item["id"], source),
    ).fetchall()
    return [r["candidate"] for r in rows
            if r["candidate"] is not None and r["status"] == "applied"
            and r["outcome"] and r["outcome"] not in KERNEL_OUTCOMES]


def _verdict(ctx: Context, texte: str) -> None:
    """Écrit — ou complète — le verdict du juge dans le workspace de l'item.

    Le nœud écrit son constat là où `validate` écrit le sien : un fichier du
    workspace, que le front sert et que l'humain lit. Quand un modèle est
    passé, il a déjà posé sa comparaison dans le même fichier ; le bloc n'y
    ajoute que la décision, à la fin, en toutes lettres.
    """
    path = ctx.workspace / VERDICT_NAME
    with path.open("a") as f:
        f.write(texte)


def _dossier(ctx: Context, finalists: list[int]) -> str:
    """Le dossier du juge : un finaliste par lettre, son travail, et rien d'autre.

    **Ce qui n'y est pas est le cœur du nœud.** Ni le modèle, ni la CLI, ni le
    `label` ou la `strategy` de la variante, ni même le numéro de candidat :
    un juge qui saurait qu'un finaliste vient d'un « gros » modèle le
    préfèrerait, et la mesure qu'on cherche à faire perdrait tout son sens.
    Il ne reste que les diffs et les critères — de quoi juger le travail, pas
    son auteur.
    """
    parts = [
        f"\n\n--- Les finalistes ({len(finalists)}) ---\n"
        "Chaque finaliste est une implémentation complète, partie du même commit. "
        "Tu ne sais rien de qui l'a produite, et c'est voulu : juge le travail, "
        "pas son auteur."
    ]
    for lettre, candidate in zip(LETTRES, finalists):
        work = worktree.candidate_work(ctx.item["id"], candidate, JUDGE_WORK_CHARS)
        parts.append(f"\n### Finaliste {lettre}\n\n"
                     + (work or "(travail illisible : aucun diff à lire)"))
    return "\n".join(parts)


def _elu(ctx: Context, workspace: Path, finalists: list[int]) -> int | None:
    """La lettre que le juge a rendue, retraduite en numéro de candidat.

    Le contrat du nœud est celui de tout le monde — `outcome.json` —, avec un
    champ de plus : `elu`, la lettre du finaliste choisi. Une lettre absente
    ou qui ne nomme aucun finaliste n'est pas un choix : le bloc rend None, et
    le noyau reverra la tentative.
    """
    try:
        elu = str(json.loads((workspace / OUTCOME_NAME).read_text())["elu"]).strip()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    index = LETTRES.find(elu[:1].upper()) if elu else -1
    return finalists[index] if 0 <= index < len(finalists) else None


def _promote(ctx: Context, candidate: int) -> tuple[str | None, str]:
    """Le travail de l'élu devient celui de l'item, et les ateliers partent.

    L'ordre compte : promouvoir d'abord, ranger ensuite — l'inverse
    détruirait la branche qu'on allait fusionner. Ce que `keep_n` avait
    laissé debout pour le juge disparaît ici, gagnant compris : sa branche
    n'a plus rien d'unique une fois fusionnée.

    Rend l'empêchement s'il y en a un, et le texte à mettre au verdict. Une
    promotion impossible ne range rien : le travail est encore dans les
    ateliers, et le nœud va échouer plutôt qu'avancer par-dessus.
    """
    empechement = worktree.promote(ctx.item["id"], candidate)
    if empechement is not None:
        return empechement, (
            f"Promotion refusée — {empechement}. Le travail de l'élu n'a pas "
            "rejoint la branche de l'item ; les ateliers des finalistes restent "
            "en place.\n")
    retirees = worktree.discard(ctx.item["id"])
    return None, (f"Travail de l'élu promu sur la branche de l'item. Ateliers des "
                  f"finalistes retirés : {', '.join(retirees) or 'aucun'}.\n")


def _promotion_ratee(empechement: str) -> dict:
    """L'issue d'un juge qui n'a pas pu promouvoir son élu.

    Un échec franc du nœud, comme au noyau : mieux vaut rejouer le jugement
    — puis escalader — que faire avancer l'item sur un atelier qui n'a pas
    reçu le travail de l'élu.
    """
    return {"outcome": "crashed",
            "summary": f"promotion de l'élu impossible — {empechement}"}


def _arbitrate(ctx: Context) -> dict:
    """Le nœud arbitre : il choisit entre des finalistes, il ne corrige rien.

    Trois issues fermées, et la première est la plus importante :

    - `sole`, un seul finaliste — le nœud est traversé **sans dépenser un
      seul jeton**. Un juge à qui l'on présente une option unique dit
      toujours oui : ce serait un tampon payant, qui fabriquerait de la
      fausse confiance. Le court-circuit est mécanique, décidé avant
      d'appeler quoi que ce soit ;
    - `chosen`, plusieurs finalistes — un modèle lit les diffs et les
      critères, élit, et dit pourquoi ;
    - `none`, aucun finaliste — retour vers l'amont, comme un échec de
      `validate`, et borné par le budget d'escalade existant.

    Dans les trois cas, le verdict est écrit dans le workspace. Dans les deux
    premiers, le travail de l'élu est promu sur la branche de l'item et les
    ateliers des finalistes sont détruits — et si la promotion est
    impossible, le nœud échoue franchement au lieu de laisser l'item avancer
    sur un atelier qui n'a pas reçu le travail.
    """
    finalists = _finalists(ctx)
    if not finalists:
        _verdict(ctx, f"# Verdict — {ctx.run['node']}\n\n"
                      "Aucun finaliste : rien à départager, le travail repart en "
                      "amont.\n")
        return {"outcome": "none",
                "summary": "aucun finaliste — retour en amont sans jugement"}

    if len(finalists) == 1:
        empechement, rangement = _promote(ctx, finalists[0])
        _verdict(ctx, f"# Verdict — {ctx.run['node']}\n\n"
                      "Élu : finaliste A, et il était seul. Court-circuit mécanique : "
                      "aucun modèle n'a été appelé, aucun jeton dépensé — un juge à "
                      "qui l'on présente une option unique dit toujours oui.\n\n"
                      + rangement)
        if empechement is not None:
            return _promotion_ratee(empechement)
        return {"outcome": "sole",
                "summary": "finaliste unique — traversé sans appel de modèle"}

    workspace = ctx.workspace.resolve()
    ctx.extra = _dossier(ctx, finalists)
    result = _agent(ctx)
    if result["outcome"] != "chosen":  # panne, dépassement, issue illisible
        return result

    candidate = _elu(ctx, workspace, finalists)
    if candidate is None:
        return result | {"outcome": "invalid_result",
                         "error": "issue « chosen » sans lettre de finaliste lisible"}
    lettre = LETTRES[finalists.index(candidate)]
    empechement, rangement = _promote(ctx, candidate)
    _verdict(ctx, f"\n\n## Verdict\n\nÉlu : finaliste {lettre} — "
                  f"{result.get('summary') or 'sans raison rendue'}\n\n" + rangement)
    if empechement is not None:
        return result | _promotion_ratee(empechement)
    return result | {"elu": lettre}


def act(ctx: Context) -> dict:
    if "agent" in ctx.config:
        return _agent(ctx)
    ctx.simulate_work()
    name = f"{ctx.run['node']}-{ctx.run['cycle']}-{ctx.run['attempt']}"
    checkpoint = ctx.workspace / f"checkpoint-{name}.txt"
    checkpoint.write_text(
        f"travail de la tentative {ctx.run['attempt']} du passage {ctx.run['cycle']}\n")
    return {"outcome": "ok", "checkpoint": checkpoint.name}


def check(ctx: Context) -> dict:
    if "agent" in ctx.config:
        return _agent(ctx)
    ctx.simulate_work()
    return {"outcome": ctx.config.get("stub_outcome", "pass")}


def effect(ctx: Context) -> dict:
    """La passerelle en miniature : intention commise avant l'action,
    clé logique au périmètre du sujet, relecture avant ré-exécution."""
    conn, item = ctx.conn, ctx.item
    subject = conn.execute(
        "SELECT graph, subject_key FROM subject WHERE id = %s", (item["subject_id"],)
    ).fetchone()
    logical_key = f"{subject['graph']}:{subject['subject_key']}:{ctx.run['node']}"
    target_uri = ctx.config.get("target", "stub://outbox")

    # l'intention existe avant tout accès au monde
    row = effects.intend(conn, item["id"], ctx.run["id"], logical_key, target_uri,
                         {"intent": ctx.config.get("intent", "noop")})
    if row["observation"] == "applied":  # déjà fait par une tentative passée
        return {"outcome": "applied", "op_id": row["op_id"]}

    # « interroger la cible par la clé logique » — la cible stub est l'outbox
    outbox = DATA_DIR / OUTBOX_NAME
    outbox.parent.mkdir(parents=True, exist_ok=True)
    outbox.touch()
    if logical_key not in outbox.read_text():
        ctx.simulate_work()
        with outbox.open("a") as f:
            f.write(f"{logical_key}\t{row['intent']}\n")

    with conn.transaction():
        effects.mark_applied(conn, op_id=row["op_id"])
    return {"outcome": "applied", "op_id": row["op_id"]}


BLOCKS = {"FETCH": fetch, "JUDGE": judge, "ACT": act, "CHECK": check, "EFFECT": effect}
# WAIT n'a pas de bloc : le noyau arme la question, le waiter route la réponse.
