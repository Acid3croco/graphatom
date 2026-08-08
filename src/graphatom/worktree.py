"""Les ateliers git du rail : celui de l'item, et celui de chaque candidat.

Le workspace d'un run est son bureau — journal, prompt, issue ; son atelier
est son checkout git. Un item en a un depuis toujours,
`$GRAPHATOM_REPO_DIR/.worktrees/rail-item-<N>`, préparé par le shell du
graph. Les candidats d'un fan-out écrivent du code eux aussi : sans atelier
à eux, les K se marchent dessus dans celui de l'item.

Chaque candidat a donc le sien, `rail-item-<N>-c<k>`, sur sa propre branche,
partie de celle de l'item. Un bloc n'a rien à deviner : le chemin est dans
son environnement, `GRAPHATOM_WORKTREE`.

La branche d'un candidat est celle de l'item suffixée `-c<k>`, et non
`<branche>/c<k>` : git refuse qu'une référence soit à la fois un fichier et
un répertoire — `refs/heads/rail/issue-134` existant, `refs/heads/rail/
issue-134/c0` ne peut pas être créée. Le suffixe garde la parenté lisible
sans demander à git l'impossible.

Le cycle de vie tient en trois moments :

- `open_run` ouvre l'atelier du candidat, une fois, à son premier run ;
- `promote` fait du travail du gagnant celui de l'item — un `merge
  --ff-only` sur la branche de l'item, sans conflit possible puisque tous
  les candidats sont partis du même commit ;
- `discard` détruit tous les ateliers de candidats, worktrees et branches
  locales. Il passe après la réduction, mais aussi sur n'importe quel chemin
  terminal atteint pendant la course : un item qui part en `wall_deadline`
  au milieu d'un fan-out ne laisse rien derrière lui.

Le garde-fou est celui du cleanup du graph, mot pour mot : on ne détruit
qu'un worktree que ce dépôt enregistre, situé sous
`$GRAPHATOM_REPO_DIR/.worktrees/`, et portant la branche attendue pour ce
candidat-là. Une cible étrangère n'est jamais touchée — elle est nommée à
voix haute, et laissée en place.
"""

import os
import re
import subprocess
import time
from pathlib import Path

WORKTREES = ".worktrees"  # sous le dépôt : la racine de tous les ateliers
GIT_TIMEOUT_S = 60
LOCK_RETRIES = 3  # K `worktree add` concurrents se disputent les verrous du dépôt
LOCK_WAIT_S = 2.0

CANDIDATE_SUFFIX = "-c"  # `rail-item-<N>-c<k>`, et `<branche de l'item>-c<k>`


def repo() -> Path | None:
    """Le dépôt du rail, quand l'environnement en désigne un."""
    path = os.environ.get("GRAPHATOM_REPO_DIR")
    return Path(path) if path else None


def item_path(item_id: int) -> Path | None:
    """Où l'atelier de l'item se trouve — qu'il existe ou non."""
    root = repo()
    return None if root is None else root / WORKTREES / f"rail-item-{item_id}"


def item_worktree(item_id: int) -> Path | None:
    """L'atelier git de l'item, quand le canal lui en a préparé un.

    La convention est celle du graph : `$GRAPHATOM_REPO_DIR/.worktrees/
    rail-item-<N>`, le pendant git du workspace `data/item-<N>`. Pas de
    dépôt dans l'environnement, ou pas de worktree sur le disque : None —
    le progrès se mesure alors sur les deux autres signaux, et un prompt de
    reprise le dit au lieu d'inventer un diff.
    """
    path = item_path(item_id)
    return path if path is not None and path.is_dir() else None


def candidate_path(item_id: int, candidate: int) -> Path | None:
    """Où l'atelier du candidat k se trouve — qu'il existe ou non."""
    path = item_path(item_id)
    return None if path is None else path.with_name(
        f"{path.name}{CANDIDATE_SUFFIX}{candidate}")


def candidate_branch(branch: str, candidate: int) -> str:
    """La branche du candidat k : celle de l'item, suffixée."""
    return f"{branch}{CANDIDATE_SUFFIX}{candidate}"


def run_worktree(item_id: int, run: dict) -> Path | None:
    """L'atelier d'un run : celui de son candidat, ou celui de l'item.

    Un run sans numéro de candidat n'a rien de neuf — c'est l'atelier de
    l'item d'aujourd'hui, ni créé ni nettoyé autrement.
    """
    candidate = run.get("candidate")
    if candidate is None:
        return item_worktree(item_id)
    return open_run(item_id, candidate)


def open_run(item_id: int, candidate: int) -> Path | None:
    """Ouvre l'atelier d'un candidat, s'il n'est pas déjà là. Rend son chemin.

    La branche part de celle de l'item : les K candidats ont donc le même
    commit de départ, ce qui rend la promotion du gagnant sans conflit
    possible. Une tentative relancée retrouve l'atelier et la branche de la
    précédente — une reprise, pas une répétition.

    Pas de dépôt, pas d'atelier d'item, ou un git qui refuse : None. Le
    candidat travaille alors comme avant, dans son seul workspace, et sa
    plainte part sur la sortie du rail plutôt que dans le silence.
    """
    root, path = repo(), candidate_path(item_id, candidate)
    item = item_worktree(item_id)
    if root is None or path is None or item is None:
        return None
    if path.is_dir():
        return path

    branch = _registered(root).get(item)
    if branch is None:
        print(f"atelier c{candidate} : {item} n'est pas un worktree de {root}",
              flush=True)
        return None
    mine = candidate_branch(branch, candidate)
    if _known_branch(root, mine):  # la tentative précédente l'a laissée
        args = ["worktree", "add", str(path), mine]
    else:
        args = ["worktree", "add", str(path), "-b", mine, branch]
    _git(root, "worktree", "prune")
    if _retry(root, *args) != 0:
        return None
    return path if path.is_dir() else None


def promote(item_id: int, candidate: int) -> str | None:
    """Le travail du gagnant devient celui de l'item. Rend la branche promue.

    `merge --ff-only` sur la branche de l'item : tous les candidats sont
    partis du même commit et l'item n'a pas bougé depuis, donc l'avance est
    droite ou il n'y a rien à avancer. Refuser le merge non-ff est le
    garde-fou : mieux vaut un item resté en arrière, et dit à voix haute,
    qu'une fusion inventée.

    Après quoi l'item n'a toujours qu'une branche — la sienne, celle qu'il
    avait déjà.
    """
    root, item = repo(), item_worktree(item_id)
    path = candidate_path(item_id, candidate)
    if root is None or item is None or path is None:
        return None

    registered = _registered(root)
    branch = registered.get(item)
    if branch is None:
        return None
    mine = candidate_branch(branch, candidate)
    if not _mine(root, path) or registered.get(path) != mine:
        print(f"promotion refusée : {path} n'est pas l'atelier de c{candidate} "
              f"sur {mine}", flush=True)
        return None

    code, out = _git(item, "merge", "--ff-only", mine)
    if code != 0:
        print(f"promotion de {mine} sur {branch} refusée : {out}", flush=True)
        return None
    return mine


def candidate_work(item_id: int, candidate: int, limit: int) -> str | None:
    """Le travail d'un candidat, lu depuis le dépôt : ses commits, son diff.

    C'est ce qu'un juge doit lire pour départager des finalistes, et c'est
    exactement ce qu'une promotion emporterait — la branche du candidat face
    à celle de l'item. Ce qu'il aurait laissé non commité n'y est pas, et
    c'est juste : un `merge --ff-only` ne l'emporterait pas non plus.

    Tout se lit dans le dépôt, par les noms de branches : l'atelier du
    candidat n'est pas touché, pas même en lecture. Un juge n'a rien à y
    faire, et une commande git dans son worktree y écrirait au moins un
    cache d'index.

    Rien à lire — pas de dépôt, pas de branche, un git qui rate : None. Le
    bloc dit alors « diff illisible » plutôt que de faire passer un candidat
    vide pour un candidat sans changement.
    """
    root, item = repo(), item_path(item_id)
    if root is None or item is None:
        return None
    branch = _registered(root).get(item)
    if branch is None:
        return None
    mine = candidate_branch(branch, candidate)
    if not _known_branch(root, mine):
        return None

    # les titres ne citent pas les refs : un nom de branche porte le numéro
    # du candidat, et le juge n'a rien à savoir de qui a produit quoi
    parts = []
    for titre, args in (("commits", ["log", "--oneline", f"{branch}..{mine}"]),
                        ("diff", ["diff", f"{branch}...{mine}"])):
        code, out = _git(root, *args)
        if code != 0:
            return None
        parts.append(f"{titre} :\n\n```\n{out or '(rien)'}\n```")
    work = "\n\n".join(parts)
    return work if len(work) <= limit else work[:limit] + "\n… (tronqué)"


def discard(item_id: int) -> list[str]:
    """Détruit les ateliers des candidats de l'item. Rend les branches retirées.

    Worktrees et branches locales, tous les candidats, gagnant compris : une
    fois le gagnant promu, son atelier n'a plus rien d'unique. Appelé après
    la réduction, et sur tout chemin terminal atteint pendant la course.

    Chaque cible passe le garde-fou, une par une : enregistrée par ce dépôt,
    sous `.worktrees/`, et sur la branche attendue pour ce candidat-là. Ce
    qui ne le passe pas est nommé et laissé en place. Une branche encore
    tenue par un worktree qui, lui, ne nous appartient pas, survit avec lui.
    """
    root, item = repo(), item_path(item_id)
    if root is None or item is None:
        return []
    registered = _registered(root)
    branch = registered.get(item)
    if branch is None:  # pas d'atelier d'item : aucune branche attendue à opposer
        return []

    retirees = []
    for candidate, path in _candidates(item, registered):
        mine = candidate_branch(branch, candidate)
        if not _mine(root, path) or registered.get(path) != mine:
            print(f"atelier étranger laissé en place : {path} "
                  f"(branche {registered.get(path) or 'aucune'} ≠ {mine})", flush=True)
            continue
        if _git(root, "worktree", "remove", "--force", str(path))[0] == 0:
            retirees.append(mine)

    # les branches qu'aucun worktree ne tient plus : celles qu'on vient de
    # libérer, et celles qu'un atelier disparu autrement aurait laissées
    tenues = set(_registered(root).values())
    for mine in _branches(root, branch):
        if mine not in tenues and _git(root, "branch", "-D", mine)[0] == 0:
            retirees.append(mine)
    return sorted(set(retirees))


# ------------------------------------------------------------------- outillage


def _candidates(item: Path, registered: dict[Path, str]) -> list[tuple[int, Path]]:
    """Les ateliers de candidats de cet item, tels que le dépôt les enregistre."""
    motif = re.compile(re.escape(item.name + CANDIDATE_SUFFIX) + r"(\d+)$")
    trouves = []
    for path in registered:
        found = motif.fullmatch(path.name)
        if found and path.parent == item.parent:
            trouves.append((int(found.group(1)), path))
    return sorted(trouves)


def _branches(root: Path, branch: str) -> list[str]:
    """Les branches locales de candidats de cet item, dans l'ordre."""
    motif = re.compile(re.escape(branch + CANDIDATE_SUFFIX) + r"\d+$")
    out = _git(root, "branch", "--list", "--format=%(refname:short)",
               f"{branch}{CANDIDATE_SUFFIX}*")[1]
    return [line for line in out.splitlines() if motif.fullmatch(line)]


def _known_branch(root: Path, branch: str) -> bool:
    return _git(root, "show-ref", "--quiet", "--verify", f"refs/heads/{branch}")[0] == 0


def _mine(root: Path, path: Path) -> bool:
    """La cible est-elle un atelier de ce dépôt, et pas d'un autre ?

    Le dépôt doit être nommé par un chemin absolu, et la cible tomber
    directement sous son `.worktrees/`. C'est le garde-fou du cleanup du
    graph, mot pour mot : un chemin ailleurs n'est jamais détruit, même
    s'il porte le bon nom et la bonne branche.
    """
    return root.is_absolute() and path.parent == root / WORKTREES


def _registered(root: Path) -> dict[Path, str]:
    """Les worktrees que ce dépôt enregistre, et la branche de chacun.

    C'est `git worktree list --porcelain` qui fait foi, pas le disque : un
    répertoire qui ressemble à un atelier sans être enregistré ici
    appartient à quelqu'un d'autre. Un worktree en tête détachée n'a pas de
    branche, il n'entre donc pas dans la table.
    """
    table: dict[Path, str] = {}
    path = None
    for line in _git(root, "worktree", "list", "--porcelain")[1].splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line.startswith("branch refs/heads/") and path is not None:
            table[path] = line[len("branch refs/heads/"):]
    return table


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    """Un git, sa sortie mêlée. Un git qui ne part pas est un git qui rate."""
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"[git injouable : {exc}]"
    return done.returncode, (done.stdout + done.stderr).strip()


def _retry(root: Path, *args: str) -> int:
    """Un git du dépôt, réessayé : K candidats se disputent ses verrous.

    Trois ateliers ouverts à la même seconde, c'est trois `worktree add` sur
    le même dépôt — l'un d'eux peut trouver un `.lock` tenu par son voisin.
    L'échec qui reste est dit à voix haute, il ne passe pas en silence.
    """
    for essai in range(LOCK_RETRIES):
        code, out = _git(root, *args)
        if code == 0:
            return 0
        if essai + 1 < LOCK_RETRIES:
            time.sleep(LOCK_WAIT_S)
    print(f"git {' '.join(args)} : {out}", flush=True)
    return code
