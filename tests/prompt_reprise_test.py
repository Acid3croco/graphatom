"""La reprise d'une tentative : ce qui compte comme travail, et comment on le dit.

Sans base — une base réduite à ses réponses — et un dépôt jetable pour
l'atelier. Les bords que le prompt de reprise et la demande de passation
tiennent :

  1. une exécution `command` ne reçoit aucune demande de passation ; un
     agent la reçoit, et elle nomme `passation-<nœud>.md`
  2. `work_files` ne compte ni les traces du rail, ni un sous-répertoire :
     seuls les fichiers qu'un agent a écrits restent, triés
  3. `_worktree_work` : un atelier propre sans avance ne porte rien ; sali,
     il porte ; commité en avance, il porte même quand `git status` rate
  4. l'en-tête de la reprise nomme le chemin de l'atelier ; sans atelier,
     il dit « (aucun) »

Usage : uv run python tests/prompt_reprise_test.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.pop("GRAPHATOM_REPO_DIR", None)

from graphatom import executors, prompt  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

from bords_outils import (NOEUD, SUJET, FauxConn, agent as noeud_agent,  # noqa: E402
                          commande, contexte, patche)
from outils import atelier_item, data_dir_jetable, depot_git, git  # noqa: E402

SANS_ATELIER = 70  # un item sans worktree : la reprise le dit
AVEC_ATELIER = 71  # l'item dont le dépôt jetable porte l'atelier
DERNIERE = {"cycle": 1, "attempt": 1, "outcome": "crashed", "result": {}}


def demande_de_passation() -> None:
    """1. une commande n'apprend rien : pas de passation ; un agent en doit une."""
    ctx = contexte(commande("true"))
    muette = prompt._demande_passation(ctx, ctx.workspace,
                                       executors.resolve(ctx.bundle, ctx.node))
    assert muette == "", muette
    ctx = contexte(noeud_agent("true", passation=True))
    demande = prompt._demande_passation(ctx, ctx.workspace,
                                        executors.resolve(ctx.bundle, ctx.node))
    assert "Écris aussi" in demande, demande
    assert str(ctx.workspace / f"passation-{NOEUD}.md") in demande, demande
    print("1. exécution command : aucune demande de passation ; agent : demandée, "
          f"et nommée passation-{NOEUD}.md ✓")


def fichiers_de_travail(workdir: Path) -> None:
    """2. traces du rail et sous-répertoire écartés ; le travail reste, trié."""
    espace = workdir / "bureau"
    espace.mkdir()
    (espace / "sous-repertoire").mkdir()
    traces = [bureau.PGID_FILE, bureau.OUTCOME_NAME, bureau.STARVED_NAME,
              bureau.PROMPT_NAME, bureau.USAGE_NAME]
    traces += [f"{prefixe}x.log" for prefixe in bureau.RAIL_PREFIXES]
    for nom in (*traces, "rapport.md", "notes.txt"):
        (espace / nom).write_text("x")
    assert bureau.work_files(espace) == ["notes.txt", "rapport.md"], bureau.work_files(espace)
    print(f"2. {len(traces)} traces du rail et un sous-répertoire écartés ; "
          "notes.txt et rapport.md rendus, triés ✓")


def travail_de_l_atelier(atelier: Path) -> None:
    """3. propre : rien ; sali : du travail ; commité en avance : du travail,
    même quand `git status` rate."""
    assert prompt._worktree_work(atelier) is False, "travail inventé"
    (atelier / "reprise.txt").write_text("pas encore commité\n")
    assert prompt._worktree_work(atelier) is True, "un fichier sali ignoré"
    git(atelier, "add", "-A")
    git(atelier, "commit", "-qm", "une avance sur origin/main")
    assert prompt._worktree_work(atelier) is True, "une avance commitée ignorée"
    vrai = prompt._git

    def statut_en_panne(tree: Path, *args: str) -> tuple[int, str]:
        return (1, "") if args[0] == "status" else vrai(tree, *args)

    with patche(prompt, "_git", statut_en_panne):
        assert prompt._worktree_work(atelier) is True, "git status en panne"
    print("3. atelier propre : rien ; sali : travail ; avance commitée : travail, "
          "git status en panne compris ✓")


def en_tete_de_reprise(atelier: Path) -> None:
    """4. la reprise nomme l'atelier par son chemin, ou dit « (aucun) »."""
    conn = FauxConn({"FROM node_run WHERE item_id": DERNIERE})
    avec = contexte(noeud_agent("true"), conn, item_id=AVEC_ATELIER, attempt=2)
    assert avec.worktree == atelier, avec.worktree
    texte = prompt._reprise(avec, avec.workspace)
    assert f"État du worktree {atelier} :" in texte, texte
    sans = contexte(noeud_agent("true"), conn, item_id=SANS_ATELIER, attempt=2)
    (sans.workspace / "brouillon.txt").write_text("un fichier d'agent\n")
    texte = prompt._reprise(sans, sans.workspace)
    assert "État du worktree (aucun) :" in texte, texte
    print("4. en-tête de reprise : le chemin de l'atelier ; sans atelier : (aucun) ✓")


def fichiers_communs() -> None:
    """5. Seul un candidat de fan-out se voit dire où sont les fichiers communs.

    Son bureau est sous celui de l'item : le prompt nomme le workspace de
    l'item, criteria.md et validate.md. Un run sans candidat a le même
    bureau que son item, et le paragraphe n'a rien à lui dire.
    """
    seul = contexte(noeud_agent("true", passation=True))
    assert prompt._commun(seul) == "", prompt._commun(seul)
    candidat = contexte(noeud_agent("true", passation=True), candidate=0)
    texte = prompt._commun(candidat)
    assert "criteria.md, validate.md" in texte, texte
    assert str(bureau.item_workspace(0).resolve()) in texte, texte
    assert str(candidat.workspace.resolve()) not in texte, "le bureau du candidat n'est pas le commun"
    print("5. fichiers communs : nommés au candidat seulement, dans le bureau de l'item ✓")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-prompt-reprise-"))
    workdir = data_dir_jetable("graphatom-prompt-reprise-data-")
    bureau.item_workspace(0).mkdir(parents=True)
    try:
        demande_de_passation()
        fichiers_de_travail(workdir)
        _, atelier = atelier_item(depot_git(tmp, "reprise"), AVEC_ATELIER)
        travail_de_l_atelier(atelier)
        en_tete_de_reprise(atelier)
        fichiers_communs()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)
    print("\nreprise d'une tentative : OK")


if __name__ == "__main__":
    main()
