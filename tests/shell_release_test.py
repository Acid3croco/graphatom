"""`scripts/release.sh` sur un dépôt jetable : du déterminisme, pas du prompt.

Le script est lancé comme le ferait l'agent, sur le worktree qu'a préparé
le nœud `worktree` de `code-task` — aucun modèle, aucun réseau, aucune
base. Sans `gh`, la release va toujours jusqu'au pas de la PR (code 6) :
tout ce qui passe avant a donc laissé passer, et aucun appel n'atteint
GitHub. Les autres preuves shell vivent dans `shell_worktree_test.py`,
`shell_deploy_test.py` et `shell_activation_test.py`.

  8. `scripts/release.sh` nomme le pas qui a lâché et sort sur son code
  9. son pas de rapprochement traverse les quatre cas : déjà à jour, merge
     automatique, conflit (code 9, worktree laissé propre), fetch en échec,
     et deux fois de plus sur le worktree sale que le rail a vraiment
     ; puis sa porte de construction traverse les cinq siens : absorption
     vide sans aucune porte, contenu sain avec et sans `front/`, et les
     deux ruptures — python, puis build front — en code 11 sans PR

Usage : uv run python tests/shell_release_test.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import git  # noqa: E402

from shell_outils import ROOT, SUJET, VERROU_DSN, dans_le_path, depot, joue, sans_gh  # noqa: E402


def faux_npm(tmp: Path, nom: str, code: int, sortie: str) -> str:
    """Un PATH sans `gh`, où `npm` joue le build du front et rend `code`.

    Le vrai `npm run build` demande `node_modules` et coûte des minutes :
    ce qui se vérifie ici, c'est que la porte le lance et lit son code, pas
    que Next sait construire.
    """
    binaires = tmp / nom
    binaires.mkdir()
    faux = binaires / "npm"
    faux.write_text(f"#!/bin/sh\necho '{sortie}'\nexit {code}\n")
    faux.chmod(0o755)
    refus = binaires / "gh"
    refus.write_text("#!/bin/sh\nexit 1\n")
    refus.chmod(0o755)
    return dans_le_path(binaires)


def portes_muettes(tmp: Path, nom: str) -> str:
    """Un PATH sans `gh`, où `python3` et `npm` échouent d'entrée.

    Rien d'autre que la porte de construction ne les lance : une release
    qui traverse ce PATH n'a donc exécuté aucune porte, et le prouve mieux
    qu'une absence de ligne dans le compte rendu.
    """
    binaires = tmp / nom
    binaires.mkdir()
    for outil in ("python3", "npm", "gh"):
        refus = binaires / outil
        refus.write_text(f"#!/bin/sh\necho '{outil} ne devait pas être lancé' >&2\nexit 1\n")
        refus.chmod(0o755)
    return dans_le_path(binaires)


def release(repo: Path, workspace: Path, subject: str,
            plus: dict | None = None) -> int:
    """`scripts/release.sh` du worktree, lancé comme le ferait l'agent."""
    out = subprocess.run(
        [str(ROOT / "scripts" / "release.sh")], capture_output=True, text=True,
        env=os.environ | {"GRAPHATOM_REPO_DIR": str(repo),
                          "GRAPHATOM_WORKSPACE": str(workspace),
                          "GRAPHATOM_SUBJECT_KEY": subject,
                          "GRAPHATOM_QUOTA_DSN": VERROU_DSN} | (plus or {}),
    )
    return out.returncode


def cas_release(tmp: Path, nom: str) -> tuple[Path, Path, Path]:
    """Un dépôt jetable neuf, son workspace, et le worktree de l'item dedans.

    Chaque cas de rapprochement fait avancer `main` à sa façon : ils ne
    peuvent pas se partager un `origin`.
    """
    base = tmp / nom
    base.mkdir()
    repo = depot(base)
    git(repo, "config", "user.email", "shell@test.invalid")
    git(repo, "config", "user.name", "shell")
    workspace = base / "item-42"
    workspace.mkdir()
    assert joue("worktree", repo, workspace)["outcome"] == "done"
    return repo, workspace, repo / ".worktrees" / "rail-item-42"


def avance_main(repo: Path, fichier: str, contenu: str) -> str:
    """Un commit de plus sur `origin/main`, comme un item voisin qui merge."""
    source = repo.parent / "source"
    (source / fichier).parent.mkdir(parents=True, exist_ok=True)
    (source / fichier).write_text(contenu)
    git(source, "add", "-A")
    git(source, "commit", "-qm", f"main avance sur {fichier}")
    git(source, "push", "-q", "origin", "main")
    return git(source, "rev-parse", "--short", "HEAD")


def travaille(worktree: Path, fichier: str, contenu: str) -> None:
    """Un commit sur la branche de l'item, pour qu'elle diverge vraiment.

    Sans lui le rapprochement serait un simple avance-rapide, et le commit
    de merge que le cas 3 attend n'existerait pas.
    """
    (worktree / fichier).write_text(contenu)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", f"l'item touche {fichier}")


def merge_en_cours(worktree: Path) -> bool:
    """Vrai si un merge est resté ouvert dans le worktree."""
    vu = subprocess.run(["git", "-C", str(worktree), "rev-parse", "-q",
                         "--verify", "MERGE_HEAD"], capture_output=True)
    return vu.returncode == 0


def est_un_merge(worktree: Path) -> bool:
    """Vrai si la tête du worktree est un commit de merge (deux parents)."""
    return len(git(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3


def preuve_8(tmp: Path) -> None:
    """8. le script de release sort sur le code du pas qui a lâché."""
    workspace = tmp / "item-42"  # le nom du workspace fait celui du worktree
    workspace.mkdir()
    repo = depot(tmp)
    assert joue("worktree", repo, workspace)["outcome"] == "done"
    assert release(repo, workspace, "pipeline-x:oom") == 2  # sujet illisible
    assert release(repo, workspace, "gh:Acid3croco/graphatom#999") == 3  # autre branche
    assert "code 3" in (workspace / "release.md").read_text()
    print("8. release.sh : code 2 sur le sujet, code 3 sur le worktree ✓")


def preuve_9(tmp: Path, aveugle: dict) -> None:
    """9. le pas de rapprochement, ses quatre cas."""
    repo, workspace, worktree = cas_release(tmp, "a-jour")
    tete = git(worktree, "rev-parse", "HEAD")
    assert release(repo, workspace, SUJET, aveugle) == 6
    rapport = (workspace / "release.md").read_text()
    assert "déjà à jour" in rapport, rapport
    assert git(worktree, "rev-parse", "HEAD") == tete, "le worktree a bougé"

    repo, workspace, worktree = cas_release(tmp, "disjoint")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    amont = avance_main(repo, "voisin.txt", "un autre item est passé par là\n")
    assert release(repo, workspace, SUJET, aveugle) == 6
    rapport = (workspace / "release.md").read_text()
    assert amont in rapport, rapport  # le SHA d'origin/main absorbé
    assert est_un_merge(worktree), "HEAD n'est pas un commit de merge"
    git(worktree, "merge-base", "--is-ancestor", "origin/main", "HEAD")

    repo, workspace, worktree = cas_release(tmp, "conflit")
    travaille(worktree, "socle.txt", "la version de l'item\n")
    avance_main(repo, "socle.txt", "la version de main\n")
    assert release(repo, workspace, SUJET, aveugle) == 9
    rapport = (workspace / "release.md").read_text()
    assert "socle.txt" in rapport, rapport
    assert not merge_en_cours(worktree), "le merge est resté ouvert"
    assert git(worktree, "status", "--porcelain") == "", "worktree laissé sale"
    assert "<<<<<<<" not in (worktree / "socle.txt").read_text()

    repo, workspace, worktree = cas_release(tmp, "sans-reseau")
    git(repo, "remote", "set-url", "origin", str(tmp / "origin-qui-n-existe-pas.git"))
    assert release(repo, workspace, SUJET, aveugle) == 10
    rapport = (workspace / "release.md").read_text()
    assert "fetch" in rapport, rapport
    assert "gh pr" not in rapport, "une PR tentée sur une vision périmée de main"
    print("9. release.sh : déjà à jour, merge automatique, conflit en code 9 "
          "worktree propre, fetch en échec en code 10 ✓")


def preuve_9_bis(tmp: Path, aveugle: dict) -> None:
    """9 bis. le worktree sale, la forme que le rail a vraiment.

    Le rapprochement passe avant le commit, donc l'implémentation est encore
    à nu. Main ailleurs, le merge passe quand même ; main sur un fichier que
    l'item a ouvert, git refuse d'entrée — code 9, worktree intact.
    """
    repo, workspace, worktree = cas_release(tmp, "sale-ailleurs")
    (worktree / "socle.txt").write_text("le travail de l'item, pas encore commité\n")
    avance_main(repo, "voisin.txt", "main a bougé ailleurs\n")
    assert release(repo, workspace, SUJET, aveugle) == 4  # le pas du commit, sans `gh`
    rapport = (workspace / "release.md").read_text()
    assert "merge automatique" in rapport, rapport
    assert git(worktree, "status", "--porcelain") == "M socle.txt", "le travail a bougé"

    repo, workspace, worktree = cas_release(tmp, "sale-meme-fichier")
    (worktree / "socle.txt").write_text("le travail de l'item, pas encore commité\n")
    avance_main(repo, "socle.txt", "main a bougé au même endroit\n")
    assert release(repo, workspace, SUJET, aveugle) == 9
    rapport = (workspace / "release.md").read_text()
    assert "refusé d'entrée" in rapport, rapport
    assert not merge_en_cours(worktree), "le merge est resté ouvert"
    assert git(worktree, "status", "--porcelain") == "M socle.txt", "le travail a bougé"
    print("9 bis. worktree sale : merge quand même si main est ailleurs, "
          "code 9 sans rien perdre sinon ✓")


def porte_sur_absorption_saine(tmp: Path, aveugle: dict) -> None:
    """9 ter, cas 1 à 3 : la porte de construction sur une absorption saine.

    Deux branches se compilaient chacune de son côté, leur merge n'a pas un
    marqueur de conflit, et `main` sort cassée : git a raison sur le texte
    et tort sur le sens. La porte tranche ce cas au compilateur, en
    secondes, plutôt qu'à un cycle d'agents de test.
    """
    # 1. absorption vide : aucune porte n'est exécutée. Le PATH le prouve —
    #    `python3` et `npm` y échouent, et la release passe quand même
    repo, workspace, worktree = cas_release(tmp, "construction-a-jour")
    assert release(repo, workspace, SUJET, {"PATH": portes_muettes(tmp, "muettes")}) == 6
    rapport = (workspace / "release.md").read_text()
    assert "rien d'absorbé" in rapport, rapport
    assert "porte" not in rapport, rapport

    # 2. absorption non vide et saine, hors `front/` : la porte python passe
    #    et se nomme — et la porte front, elle, n'est pas exécutée
    repo, workspace, worktree = cas_release(tmp, "construction-saine")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    avance_main(repo, "src/voisin.py", "def voisin():\n    return 1\n")
    assert release(repo, workspace, SUJET, aveugle) == 6
    rapport = (workspace / "release.md").read_text()
    assert "porte compilation python : passée" in rapport, rapport
    assert "front" not in rapport, rapport  # rien d'absorbé sous front/

    # 3. absorption non vide touchant `front/` : les deux portes s'exécutent
    repo, workspace, worktree = cas_release(tmp, "construction-front")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    avance_main(repo, "front/graph-view.tsx", "export const vue = () => null\n")
    assert release(repo, workspace, SUJET, {"PATH": faux_npm(tmp, "npm-passe", 0, "construit")}) == 6
    rapport = (workspace / "release.md").read_text()
    assert "porte compilation python : passée" in rapport, rapport
    assert "porte construction front : passée" in rapport, rapport


def porte_sur_rupture(tmp: Path, aveugle: dict) -> None:
    """9 ter, cas 4 à 6 : chaque rupture sort en code 11, et le README le dit."""
    # 4. l'absorption casse la compilation python : code 11, la sortie du
    #    compilateur dans `release.md`, aucune PR tentée, et le worktree
    #    laissé sur sa fusion — c'est un état à réparer, pas à effacer
    repo, workspace, worktree = cas_release(tmp, "construction-python-cassee")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    avance_main(repo, "src/casse.py", "def casse(:\n    return 1\n")
    assert release(repo, workspace, SUJET, aveugle) == 11
    rapport = (workspace / "release.md").read_text()
    assert "casse.py" in rapport and "SyntaxError" in rapport, rapport
    assert "gh pr" not in rapport, "une PR tentée sur un contenu qui ne compile pas"
    assert est_un_merge(worktree), "la fusion à réparer a été effacée"

    # 5. l'absorption casse le build du front : même code, même compte rendu
    repo, workspace, worktree = cas_release(tmp, "construction-front-cassee")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    avance_main(repo, "front/graph-view.tsx", "export const vue = () => null\n")
    casse = faux_npm(tmp, "npm-casse", 1, "graph-view.tsx(25,8): error TS2741")
    assert release(repo, workspace, SUJET, {"PATH": casse}) == 11
    rapport = (workspace / "release.md").read_text()
    assert "TS2741" in rapport, rapport
    assert "gh pr" not in rapport, "une PR tentée sur un front qui ne construit pas"

    # 6. le README dit la release telle qu'elle est : un merge, jamais un
    #    rebase, et la porte que ce merge traverse
    readme = (ROOT / "README.md").read_text()
    assert "rebase quand le rebase passe tout seul" not in readme, \
        "le README décrit encore la release comme rebasant"
    assert "porte de construction" in readme, "le README ne documente pas la porte"


def preuve_9_ter(tmp: Path, aveugle: dict) -> None:
    """9 ter. la porte de construction, après une absorption non vide."""
    porte_sur_absorption_saine(tmp, aveugle)
    porte_sur_rupture(tmp, aveugle)
    print("9 ter. porte de construction : rien sur une absorption vide, python "
          "et front sur une absorption saine, code 11 sur chaque rupture ✓")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-shell-"))
    preuve_8(tmp)
    aveugle = {"PATH": sans_gh(tmp)}
    preuve_9(tmp, aveugle)
    preuve_9_bis(tmp, aveugle)
    preuve_9_ter(tmp, aveugle)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nnœuds shell, release : OK — déterministes, et jamais sans outcome")


if __name__ == "__main__":
    main()
