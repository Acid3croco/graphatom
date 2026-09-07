"""Le test des ateliers git : les refus, les reprises et les garde-fous.

Sur un dépôt jetable, sans base. `fanout_isolation_test` prouve le chemin
nominal — K ateliers, une promotion, un ménage ; ce test prouve les bords,
là où le module doit refuser à voix haute plutôt que d'inventer :

  0. sans dépôt dans l'environnement, le rail tourne sans git : rien n'est
     ouvert, promu ni détruit, et l'atelier d'un run est None ; avec un
     dépôt, l'atelier d'un run sans candidat est celui de l'item
  1. un atelier d'item que le dépôt n'enregistre pas : rien n'est ouvert,
     rien n'est promu, rien n'est lu, rien n'est détruit — et le refus est dit
  2. la branche laissée par une tentative précédente est reprise, pas
     recréée : l'atelier rouvert porte ses commits ; rouvert encore, il est
     rendu tel quel
  3. un git qui échoue est réessayé `LOCK_RETRIES` fois, à `LOCK_WAIT_S`
     d'intervalle, puis l'échec est dit et l'atelier rendu None
  4. promouvoir un candidat sans branche ne promeut rien ; promouvoir un
     candidat dont l'atelier a disparu nomme l'empêchement
  5. un merge qui ne serait pas une avance droite est refusé, nommément, et
     la tête de l'item ne bouge pas ; l'item revenu au commit de départ, la
     même promotion passe et l'item porte le commit du candidat
  6. le travail d'un candidat : None sans dépôt, sans branche, ou quand git
     rate ; tronqué à la limite sinon
  7. le ménage laisse en place un atelier étranger, et le nomme ; un atelier
     verrouillé survit avec sa branche, jusqu'à ce qu'on le déverrouille
  8. un git qui ne peut pas partir rend (1, plainte) au lieu de faire tomber
     l'appelant ; un git qui part et refuse rend son propre code

Usage : uv run python tests/worktree_test.py
"""

import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import worktree  # noqa: E402

from outils import atelier_item, depot_git, git  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="graphatom-worktree-")).resolve()
REPO = depot_git(TMP, "atelier")


def capturer(action) -> tuple[object, str]:
    """Le résultat d'un geste, et ce qu'il a dit sur la sortie du rail."""
    sortie = io.StringIO()
    with redirect_stdout(sortie):
        resultat = action()
    return resultat, sortie.getvalue()


def commit_dans(atelier: Path, nom: str) -> str:
    """Un commit de plus dans cet atelier ; rend son SHA."""
    (atelier / f"{nom}.txt").write_text(f"{nom}\n")
    git(atelier, "add", "-A")
    git(atelier, "commit", "-qm", nom)
    return git(atelier, "rev-parse", "HEAD")


def enregistre(chemin: Path) -> bool:
    return chemin in worktree._registered(REPO)


def sans_depot() -> None:
    _, atelier = atelier_item(REPO, 8)
    depot = os.environ.pop("GRAPHATOM_REPO_DIR")
    try:
        assert worktree.open_run(8, 0) is None and worktree.promote(8, 0) is None
        assert worktree.discard(8) == []
        assert worktree.run_worktree(8, {"candidate": 0}) is None
    finally:
        os.environ["GRAPHATOM_REPO_DIR"] = depot
    assert worktree.run_worktree(8, {"candidate": None}) == atelier
    assert worktree.run_worktree(8, {"candidate": 0}) == worktree.candidate_path(8, 0)
    print("0. sans dépôt : rien n'est ouvert ni détruit ; avec : l'atelier du run ✓")


def atelier_non_enregistre() -> None:
    item = 1
    (REPO / ".worktrees" / f"rail-item-{item}").mkdir(parents=True)
    ouvert, dit = capturer(lambda: worktree.open_run(item, 0))
    assert ouvert is None and "n'est pas un worktree de" in dit, dit
    assert worktree.promote(item, 0) is None
    assert worktree.candidate_work(item, 0, 1000) is None
    assert worktree.discard(item) == []
    assert not (REPO / ".worktrees" / f"rail-item-{item}-c0").exists()
    print("1. atelier d'item non enregistré : refus dit, rien de touché ✓")


def branche_reprise() -> None:
    item = 2
    branche, _ = atelier_item(REPO, item)
    mienne = worktree.candidate_branch(branche, 0)
    # la tentative précédente a laissé sa branche, avec un commit dessus
    arbre = git(REPO, "rev-parse", "HEAD^{tree}")
    laisse = git(REPO, "commit-tree", arbre, "-p", "HEAD", "-m", "reprise")
    git(REPO, "branch", mienne, laisse)

    chemin = worktree.open_run(item, 0)
    assert chemin is not None and chemin.is_dir(), chemin
    assert git(chemin, "rev-parse", "HEAD") == laisse, "la branche a été recréée"
    assert worktree._registered(REPO)[chemin] == mienne
    assert worktree.open_run(item, 0) == chemin, "un atelier ouvert se rouvre tel quel"
    print("2. la branche d'une tentative précédente est reprise, pas recréée ✓")


def git_qui_echoue() -> None:
    item = 3
    atelier_item(REPO, item)
    # un fichier là où l'atelier devrait naître : `worktree add` refuse
    worktree.candidate_path(item, 0).write_text("obstacle\n")
    attentes, garde = [], worktree.time
    worktree.time = SimpleNamespace(sleep=attentes.append)
    try:
        ouvert, dit = capturer(lambda: worktree.open_run(item, 0))
    finally:
        worktree.time = garde
    assert ouvert is None
    assert attentes == [worktree.LOCK_WAIT_S] * (worktree.LOCK_RETRIES - 1), attentes
    assert "git worktree add" in dit and "obstacle" not in dit, dit
    print(f"3. git réessayé {worktree.LOCK_RETRIES} fois, puis l'échec est dit ✓")


def promotion_sans_atelier() -> None:
    item = 4
    atelier_item(REPO, item)
    assert worktree.promote(item, 0) is None, "un candidat sans branche a promu"
    chemin = worktree.open_run(item, 0)
    git(REPO, "worktree", "remove", "--force", str(chemin))
    empechement = worktree.promote(item, 0)
    assert empechement and str(chemin) in empechement, empechement
    assert "n'y est pas enregistré sur rail/issue-4-c0" in empechement, empechement
    print("4. sans branche : rien à promouvoir ; atelier disparu : nommé ✓")


def merge_non_ff() -> None:
    item = 5
    _, atelier = atelier_item(REPO, item)
    depart = git(atelier, "rev-parse", "HEAD")
    candidat = worktree.open_run(item, 0)
    travail = commit_dans(candidat, "travail-c0")
    tete = commit_dans(atelier, "item-a-bouge")  # l'item a divergé
    empechement = worktree.promote(item, 0)
    assert empechement and "merge --ff-only" in empechement, empechement
    assert "refusé" in empechement, empechement
    assert git(atelier, "rev-parse", "HEAD") == tete, "la tête de l'item a bougé"
    git(atelier, "reset", "-q", "--hard", depart)
    assert worktree.promote(item, 0) is None, "la promotion droite a été refusée"
    assert git(atelier, "rev-parse", "HEAD") == travail, "le travail n'est pas promu"
    print("5. merge non-ff refusé, tête intacte ; avance droite promue ✓")


def travail_du_candidat() -> None:
    item = 6
    atelier_item(REPO, item)
    commit_dans(worktree.open_run(item, 0), "travail-c0")
    depot = os.environ.pop("GRAPHATOM_REPO_DIR")
    try:
        assert worktree.candidate_work(item, 0, 1000) is None, "lu sans dépôt"
    finally:
        os.environ["GRAPHATOM_REPO_DIR"] = depot
    assert worktree.candidate_work(item, 1, 1000) is None, "lu sans branche"

    entier = worktree.candidate_work(item, 0, 100_000)
    assert entier and "travail-c0" in entier and "diff :" in entier, entier
    court = worktree.candidate_work(item, 0, 40)
    assert court.endswith("… (tronqué)") and court.startswith(entier[:40]), court

    vrai = worktree.run_git

    def git_qui_rate(cwd, *args, **kw):
        return (128, "fatal (simulé)") if args[0] == "diff" else vrai(cwd, *args, **kw)

    worktree.run_git = git_qui_rate
    try:
        assert worktree.candidate_work(item, 0, 1000) is None, "un diff inventé"
    finally:
        worktree.run_git = vrai
    print("6. travail d'un candidat : None sans dépôt/branche/git, tronqué sinon ✓")


def menage_prudent() -> None:
    item = 7
    branche, _ = atelier_item(REPO, item)
    etranger = worktree.candidate_path(item, 0)
    git(REPO, "worktree", "add", "-q", str(etranger), "-b", "autre-chose")
    retirees, dit = capturer(lambda: worktree.discard(item))
    assert retirees == [] and etranger.is_dir() and enregistre(etranger)
    assert "atelier étranger laissé en place" in dit and "autre-chose" in dit, dit

    verrouille = worktree.open_run(item, 1)
    mienne = worktree.candidate_branch(branche, 1)
    git(REPO, "worktree", "lock", str(verrouille))
    retirees, _ = capturer(lambda: worktree.discard(item))
    assert retirees == [] and verrouille.is_dir(), retirees
    assert worktree._known_branch(REPO, mienne), "la branche d'un atelier tenu"
    git(REPO, "worktree", "unlock", str(verrouille))
    retirees, _ = capturer(lambda: worktree.discard(item))
    assert retirees == [mienne], retirees
    assert not verrouille.exists() and not worktree._known_branch(REPO, mienne)
    print("7. ménage : l'étranger est nommé et laissé, le verrouillé attend ✓")


def git_injouable() -> None:
    chemin = os.environ["PATH"]
    os.environ["PATH"] = str(TMP / "vide")
    try:
        code, plainte = worktree.run_git(REPO, "status")
    finally:
        os.environ["PATH"] = chemin
    assert code == 1 and plainte.startswith("[git injouable :"), (code, plainte)
    code, plainte = worktree.run_git(TMP / "absent", "status")
    assert code not in (0, 1) and "absent" in plainte, (code, plainte)
    print("8. git qui ne part pas : (1, plainte) ; git qui refuse : son code ✓")


def main() -> None:
    try:
        sans_depot()
        atelier_non_enregistre()
        branche_reprise()
        git_qui_echoue()
        promotion_sans_atelier()
        merge_non_ff()
        travail_du_candidat()
        menage_prudent()
        git_injouable()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\nworktree : OK")


if __name__ == "__main__":
    main()
