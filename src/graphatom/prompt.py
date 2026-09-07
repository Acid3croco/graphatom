"""Le prompt d'une tentative : le nœud, le contrat, la passation, la reprise.

Le savoir ne circule pas le long de la chaîne : chaque nœud démarre en
contexte neuf. Chaque nœud agent laisse donc une passation — ce qu'il a fait,
ce qu'il a appris, ce qu'il n'a pas fait —, et le prompt du suivant la porte
avec l'issue et la queue du journal de son prédécesseur immédiat. Rien de plus
loin, et tout borné : un agent noyé sous l'historique choisit mal.

Et une relance est une reprise, jamais une répétition : le prompt de la
tentative suivante porte l'état déjà là — le `git diff` du worktree et les
fichiers du workspace. Repartir à l'aveugle, c'est payer le trajet deux fois.
La question n'est pas « est-ce la première tentative ? » mais « y a-t-il
quelque chose à reprendre ? » : une tentative 1 d'un passage neuf, ouverte
par un `retry` d'escalade, hérite du travail que le passage précédent a
laissé, et son prompt le porte comme n'importe quelle relance. Le motif est
nommé : un budget dépassé et une pendaison ne laissent pas la même chose.
"""

import os
from pathlib import Path

from . import executors
from .bundle import judge_source
from .context import Context
from .failure import tail
from .workspace import (OUTCOME_NAME, attempt_log, item_workspace,
                        passation_path, run_workspace, work_files)
from .worktree import run_git

VARIANT_TOKENS = ("label", "strategy")  # ce qu'une variante de fan-out interpole
GIT_CHARS = 8000  # l'état du worktree cité dans le prompt d'une reprise
GIT_TIMEOUT_S = 20
# la passation du prédécesseur citée dans le prompt. Resserrée à la mesure :
# les 809 prompts déjà rendus par le rail pèsent 2 750 caractères en médiane,
# et trois sections courtes tiennent largement dans 2 500 — au-delà, ce n'est
# plus une passation, c'est le journal que personne ne lit
PASSATION_CHARS = 2500
PASSATION_SECTIONS = ("## Fait", "## Appris", "## Pas fait")
BASE_REF = "origin/main"  # la base des worktrees d'item : ce qui est commité s'y compare

DEATHS = {  # ce qui a tué la tentative précédente, dit en clair au repreneur
    "timed_out": "a dépassé son budget : le couperet l'a coupée en plein travail, "
                 "et ce qu'elle avait fait est resté là",
    "stalled": "est restée pendue : le chien de garde l'a coupée sans qu'elle ait "
               "produit un octet — ce qui suit vient donc d'avant elle",
    "crashed": "s'est arrêtée sans rendre d'issue lisible, et ce qu'elle avait "
               "fait est resté là",
}


def fill(ctx: Context, text: str, subject: str) -> str:
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


def build(ctx: Context, workspace: Path, subject: str,
          resolved: executors.Executor | None = None) -> str:
    """Le prompt de la tentative : celui du nœud, le contrat, et la reprise."""
    resolved = resolved or executors.resolve(ctx.bundle, ctx.node)
    outcomes = sorted(ctx.node.get("edges") or {})
    outcome_path = workspace / OUTCOME_NAME
    if resolved.prompt is None:
        raise ValueError(f"{ctx.run['node']} : exécution agent sans prompt résolu")
    return os.path.expandvars(
        fill(ctx, resolved.prompt, subject)
    ) + ctx.extra + (
        "\n\n--- Contrat GraphAtom ---\n"
        f"Tu es le bloc « {ctx.run['node']} » d'un rail d'exécution. "
        f"Ton workspace : {workspace}\n"
        + _commun(ctx) +
        f"Avant de terminer, écris impérativement {outcome_path} : "
        f'{{"outcome": <une valeur parmi {outcomes}>, "summary": "<une phrase>"}}\n'
        "Sans ce fichier, ta tentative est classée crashed et sera retentée."
        + _demande_passation(ctx, workspace, resolved)
    ) + _passation(ctx) + _reprise(ctx, workspace)


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


# ------------------------------------------------------------------ passation


def _demande_passation(ctx: Context, workspace: Path,
                       resolved: executors.Executor) -> str:
    """Ce qu'un nœud laisse au suivant : trois sections, et rien de plus.

    La forme est courte et fermée, sinon la passation devient un journal que
    personne ne lit. La troisième section est la seule qui manquait vraiment :
    un renoncement que personne ne nomme ne laisse aucune trace, et le nœud
    d'après ne le rattrape pas. Un agent ne l'écrit que si on la lui demande
    nommément — c'est pour ça qu'elle est demandée en toutes lettres, et
    demandée même en cas de succès.
    """
    # Une commande déterministe n'apprend rien : son contrat coupe la
    # passation au lieu d'en fabriquer une qui prétendrait venir d'un modèle.
    if not resolved.handoff:
        return ""
    fichier = workspace / f"passation-{ctx.run['node']}.md"
    return (
        f"\nÉcris aussi {fichier} — la passation que le nœud suivant lira, et "
        "sa seule fenêtre sur ce que tu as compris. Trois sections courtes, "
        "rien de plus, et « rien » quand il n'y a rien à dire :\n\n"
        "## Fait\n<ce que tu as fait, en une ou deux phrases>\n\n"
        "## Appris\n<ce que tu as découvert et qui n'était pas dans l'énoncé : "
        "une contrainte du dépôt, un piège, un chemin qui ne marche pas>\n\n"
        "## Pas fait\n<ce que tu as laissé de côté, et pourquoi : le "
        "renoncement, la question ouverte, le doute. Écris-la même quand tu "
        "réussis — un renoncement tu, personne ne le rattrape.>"
    )


def passation_invalide(path: Path) -> str | None:
    """Dit pourquoi une passation ne tient pas le contrat, sinon rien.

    Une issue de succès ne peut pas réutiliser le fichier de la tentative
    précédente : `agent.attempt` le retire avant de lancer l'agent. Ce
    contrôle porte donc sur un artefact neuf. Les trois titres sont uniques,
    dans l'ordre, et chaque section dit au moins « rien ». La borne
    s'applique au fichier rendu comme au texte transmis : un journal déguisé
    en passation n'est pas une passation valide.
    """
    try:
        texte = path.read_text(errors="replace").strip()
    except OSError:
        return f"passation absente : {path.name}"
    if len(texte) > PASSATION_CHARS:
        return (f"passation trop longue : {len(texte)} caractères, "
                f"borne {PASSATION_CHARS}")
    lignes = texte.splitlines()
    titres = tuple(ligne.strip() for ligne in lignes
                   if ligne.lstrip().startswith("## "))
    if titres != PASSATION_SECTIONS:
        return (f"trois sections exactes attendues : {PASSATION_SECTIONS}, "
                f"vues {titres}")
    return _sections_vides(lignes)


def _sections_vides(lignes: list[str]) -> str | None:
    """La première section ouvre le texte, et aucune n'est vide.

    Les trois titres sont déjà uniques et dans l'ordre — `passation_invalide`
    l'a exigé juste avant — : il ne reste qu'à vérifier que rien ne les
    précède, et que chacune dit au moins « rien ».
    """
    positions = [i for i, ligne in enumerate(lignes)
                 if ligne.strip() in PASSATION_SECTIONS]
    if positions[0] != 0:
        return "sections de passation précédées de texte"
    for i, section in enumerate(PASSATION_SECTIONS):
        fin = positions[i + 1] if i + 1 < len(positions) else len(lignes)
        if not "\n".join(lignes[positions[i] + 1:fin]).strip():
            return f"section {section!r} vide"
    return None


def _predecessor(ctx: Context) -> dict | None:
    """Le run qui a fait entrer l'item dans son état courant.

    L'événement de routage le nomme déjà : `routing.route` y écrit le run
    décisif. Le dernier événement qui mène à ce nœud est donc le seul à lire,
    et la requête ne remonte pas plus loin — c'est ce qui borne la
    transmission au prédécesseur immédiat, sans historique accumulé.

    Un événement sans run — l'admission d'un nœud d'entrée, une réponse
    humaine, une échéance de WAIT — n'a pas de prédécesseur à donner : rien
    n'est injecté, et aucun bloc creux n'est posé.
    """
    event = ctx.conn.execute(
        "SELECT run_id FROM event WHERE item_id = %s AND to_state = %s "
        "ORDER BY item_version DESC LIMIT 1",
        (ctx.item["id"], ctx.run["node"]),
    ).fetchone()
    run_id = event.get("run_id") if event else None
    if run_id is None:
        return None
    return ctx.conn.execute(
        "SELECT id, node, cycle, attempt, candidate, outcome FROM node_run "
        "WHERE id = %s",
        (run_id,),
    ).fetchone()


def _passation(ctx: Context) -> str:
    """Ce que le prédécesseur immédiat a rendu, appris, et laissé tomber.

    Trois choses, et rien d'autre : son issue, sa passation, la queue de son
    journal. Borné des deux côtés : en taille, et en profondeur — le
    prédécesseur immédiat, jamais l'histoire de l'item.

    Une exception, et une seule : le nœud arbitre. Lui ne continue pas le
    travail de son prédécesseur, il le juge — et son prédécesseur est un
    finaliste, dont la passation nommerait la CLI, le modèle et la variante.
    C'est exactement ce que le dossier du juge cache. Rien ne lui est posé.
    """
    if judge_source(ctx.node):
        return ""
    previous = _predecessor(ctx)
    if previous is None:  # nœud d'entrée, ou entrée par un WAIT : rien à porter
        return ""
    texte = _borne(passation_path(ctx.item["id"], previous), PASSATION_CHARS)
    log = attempt_log(run_workspace(ctx.item["id"], previous), previous)
    return (
        f"\n\n--- Passation de « {previous['node']} », le nœud d'avant ---\n"
        f"Il a rendu l'issue « {previous['outcome']} ». Ce qu'il a compris "
        "n'est nulle part ailleurs que ci-dessous : lis-le, reprends après "
        "lui, et ne rejuge pas son issue.\n\n"
        f"{texte or '(aucune passation rendue)'}\n\n"
        f"Queue de son journal :\n\n```\n{tail(log)}\n```"
    )


def _borne(path: Path, limite: int) -> str:
    """Le contenu d'un fichier, borné en taille. Vide s'il n'y en a pas."""
    try:
        texte = path.read_text(errors="replace").strip()
    except OSError:
        return ""
    return texte if len(texte) <= limite else texte[:limite] + "\n… (tronqué)"


# -------------------------------------------------------------------- reprise


def death(previous: dict) -> str:
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


def _git(worktree: Path, *args: str) -> tuple[int, str]:
    """Une commande git dans le worktree de l'item : son code et sa sortie.

    Un git qui rate n'est pas un échec de la tentative. Le délai est plus
    court que celui du dépôt : c'est l'état d'un seul worktree pour un
    prompt, pas une opération qui peut attendre un verrou.
    """
    return run_git(worktree, *args, timeout=GIT_TIMEOUT_S)


def _committed(worktree: Path) -> str:
    """Les commits de la branche de l'item qu'`origin/main` n'a pas encore.

    Pas d'`origin/main` sous la main — un dépôt de test, un fetch jamais
    fait : rien, et le statut reste seul juge.
    """
    if _git(worktree, "rev-parse", "--verify", "--quiet", BASE_REF)[0] != 0:
        return ""
    code, out = _git(worktree, "log", "--oneline", f"{BASE_REF}..HEAD")
    return out if code == 0 else ""


def _worktree_work(worktree: Path | None) -> bool:
    """Le worktree porte-t-il du travail à reprendre ?

    Deux formes, et une seule suffit : ce qui n'est pas commité, et ce qui
    l'est déjà sans être fusionné. Pas de worktree, ou un git qui rate :
    faux — le workspace reste alors le seul signal.
    """
    if worktree is None:
        return False
    code, out = _git(worktree, "status", "--short")
    return (code == 0 and bool(out)) or bool(_committed(worktree))


def _reprise(ctx: Context, workspace: Path) -> str:
    """L'état laissé par une tentative antérieure — jamais une répétition.

    Deux conditions, toutes deux mécaniques. Il faut une tentative antérieure
    du même nœud — quel que soit son passage. Et il faut quelque chose à
    reprendre : du travail dans le worktree, ou un fichier d'agent dans le
    workspace. Sans l'un ni l'autre, aucun bloc — un agent qui recommence
    vraiment de zéro ne doit pas lire un état imaginaire.
    """
    previous = _last_attempt(ctx)
    if previous is None:  # ce nœud n'a jamais tourné sur cet item
        return ""
    worktree = ctx.worktree  # celui du candidat, ou celui de l'item
    if not work_files(workspace) and not _worktree_work(worktree):
        return ""  # rien à reprendre : pas de reprise inventée
    # la liste, elle, ne cache rien : les traces des tentatives passées se
    # lisent aussi, et leur journal dit souvent où celle d'avant s'est arrêtée
    files = sorted(p.name for p in workspace.iterdir() if p.is_file())
    return (
        f"\n\n--- Reprise de la tentative {previous['attempt']} du passage "
        f"{previous['cycle']} ---\n"
        f"La tentative précédente de « {ctx.run['node']} » {death(previous)}. "
        "Lis l'état ci-dessous et continue là où le travail s'est arrêté — ne "
        "recommence pas de zéro.\n\n"
        f"État du worktree {worktree or '(aucun)'} :\n\n"
        f"```\n{_git_state(worktree)}\n```\n\n"
        "Fichiers déjà écrits dans ton workspace :\n"
        + "\n".join(f"- {name}" for name in files or ["(aucun)"])
    )


def _git_state(worktree: Path | None) -> str:
    """L'état git du worktree de l'item, borné en taille.

    Trois vues, dans l'ordre où on les lit : le statut, le diff, et le
    journal face à `origin/main` — sans lui, un worktree commité au fil de
    l'eau se lirait vide. Un git qui rate n'est pas un échec de la
    tentative : sa plainte part dans le prompt telle quelle.
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
