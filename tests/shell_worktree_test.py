"""Les nœuds shell de `code-task`, côté worktree : du déterminisme, pas du prompt.

Les `cmd` du graph sont joués tels quels sur un dépôt jetable — aucun
modèle, aucun réseau. Ce qu'on vérifie, c'est la promesse qui autorise un
nœud sans agent : il écrit toujours son `outcome.json`. Un Postgres jetable
est provisionné pour que `deploy` y pose le verrou de sa file, comme en
prod ; les autres preuves shell vivent dans `shell_release_test.py`,
`shell_deploy_test.py` et `shell_activation_test.py`.

  1. `worktree` crée le worktree de l'item sur `rail/issue-<num>` depuis
     `origin/main`, et le dit dans `worktree.md`
  2. rejoué, il reprend celui du cycle en cours au lieu d'en refaire un
  3. la branche restée d'un cycle passé est reprise telle quelle
  4. un sujet sans numéro échoue — proprement, sans toucher au dépôt
  5. `deploy` et `verify_deploy` sans environnement : `failed` / `fail`,
     jamais un nœud coincé
  6. `deploy` accepte un clone de référence qui est un worktree, où `.git`
     est un fichier de renvoi et non un répertoire
  7. `deploy` passe aussi quand ce worktree n'a pas `main` : la branche est
     tenue par le checkout principal, et la tête détachée ne la dispute pas
 10. la frontière tient dans le bundle : un nœud mécanique ne lance aucun
     modèle, un nœud à modèle rend son `usage.json`, les trois nœuds de
     retrait sont le même shell, et aucun ne teste `.git` comme un chemin

Usage : uv run python tests/shell_worktree_test.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import git, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-shell-worktree")

from graphatom import executors, graph  # noqa: E402

from shell_outils import BUNDLE, ROOT, depot, joue, sans_gh  # noqa: E402


def reference_worktree(tmp: Path) -> Path:
    """Un clone de référence qui est lui-même un worktree git.

    C'est la forme du clone du worker : son `.git` est un fichier de renvoi,
    pas un répertoire. L'hôte garde `main` libre pour que le worktree l'ait.
    """
    hote = tmp / "hote"
    subprocess.run(["git", "clone", "-q", str(tmp / "origin.git"), str(hote)], check=True)
    git(hote, "switch", "-q", "-c", "garage")
    reference = tmp / "reference"
    git(hote, "worktree", "add", "-q", str(reference), "main")
    return reference


def reference_worktree_main_prise(tmp: Path) -> Path:
    """Le même worktree, mais `main` est tenue par le checkout principal.

    C'est la forme observée en prod : l'hôte reste sur `main`, donc aucun
    worktree ne peut la reprendre. Seule une tête détachée passe.
    """
    hote = tmp / "hote-sur-main"
    subprocess.run(["git", "clone", "-q", str(tmp / "origin.git"), str(hote)], check=True)
    reference = tmp / "reference-main-prise"
    git(hote, "worktree", "add", "-q", "--detach", str(reference), "main")
    return reference


def sha_deploye(workspace: Path) -> str:
    """Le SHA sur lequel `deploy.md` dit s'être détaché."""
    marque = "HEAD détachée sur origin/main - "
    for ligne in (workspace / "deploy.md").read_text().splitlines():
        if ligne.startswith(marque):
            return ligne[len(marque):]
    raise AssertionError("deploy.md ne dit pas sur quel SHA il s'est détaché")


def preuve_1(repo: Path, workspace: Path, worktree: Path) -> None:
    """1. le worktree part d'origin/main, sur la branche du sujet."""
    outcome = joue("worktree", repo, workspace)
    assert outcome["outcome"] == "done", outcome
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "rail/issue-77"
    assert git(worktree, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")
    assert "créé depuis origin/main" in (workspace / "worktree.md").read_text()
    print(f"1. {outcome['summary']} ✓")


def preuve_2(repo: Path, workspace: Path, worktree: Path) -> None:
    """2. rejoué, il reprend : le travail du cycle en cours ne se perd pas."""
    (worktree / "travail.txt").write_text("l'implémentation en cours\n")
    outcome = joue("worktree", repo, workspace)
    assert outcome["outcome"] == "done", outcome
    assert "repris" in outcome["summary"], outcome
    assert (worktree / "travail.txt").exists(), "le worktree a été recréé sous le travail"
    print(f"2. {outcome['summary']} ✓")


def preuve_3(repo: Path, workspace: Path, worktree: Path) -> None:
    """3. le worktree est parti, la branche est restée : on la reprend."""
    git(repo, "worktree", "remove", "--force", str(worktree))
    outcome = joue("worktree", repo, workspace)
    assert outcome["outcome"] == "done", outcome
    assert "déjà là" in outcome["summary"], outcome
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "rail/issue-77"
    print(f"3. {outcome['summary']} ✓")


def preuve_4(repo: Path, workspace: Path) -> None:
    """4. un sujet dont on ne tire pas de numéro : échec nommé, dépôt intact."""
    outcome = joue("worktree", repo, workspace, "pipeline-x:oom")
    assert outcome["outcome"] == "failed", outcome
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
    print(f"4. {outcome['summary']} ✓")


def preuve_5(tmp: Path, workspace: Path) -> None:
    """5. les nœuds de déploiement sans rien à quoi se raccrocher.

    Ils rendent leur issue quand même — un nœud shell ne coince jamais.
    """
    absent = tmp / "pas-un-clone"
    assert joue("deploy", absent, workspace)["outcome"] == "failed"
    assert joue("verify_deploy", absent, workspace)["outcome"] == "fail"
    for rapport in ("deploy.md", "verify_deploy.md"):
        assert (workspace / rapport).read_text().strip(), rapport
    print("5. deploy et verify_deploy sans environnement : outcome écrit ✓")


def preuve_6(tmp: Path, workspace: Path) -> None:
    """6. le clone de référence est parfois un worktree.

    Son `.git` est alors un fichier, et le garde-fou du deploy doit quand
    même le reconnaître. Sans `gh`, les deux pas git se jouent puis le nœud
    s'arrête au jeton : le test ne construit jamais d'image ni ne lance de
    service.
    """
    reference = reference_worktree(tmp)
    assert (reference / ".git").is_file(), "le clone de référence n'est pas un worktree"
    outcome = joue("deploy", reference, workspace, plus={"PATH": sans_gh(tmp)})
    assert outcome["outcome"] == "failed", outcome
    assert "pas 2" in outcome["summary"], outcome  # le jeton, pas le garde-fou
    assert "$ git switch --detach origin/main" in (workspace / "deploy.md").read_text()
    assert sha_deploye(workspace) == git(reference, "rev-parse", "origin/main")
    print(f"6. {outcome['summary']} ✓")


def preuve_7(tmp: Path, workspace: Path) -> None:
    """7. le même worktree, mais `main` est tenue par le checkout principal.

    `switch -f main` sortait là en code 128 ; la tête détachée passe.
    """
    prise = reference_worktree_main_prise(tmp)
    assert git(prise, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", "pas détaché"
    outcome = joue("deploy", prise, workspace, plus={"PATH": sans_gh(tmp)})
    assert outcome["outcome"] == "failed", outcome
    assert "pas 2" in outcome["summary"], outcome  # les pas git ont tous passé
    assert sha_deploye(workspace) == git(prise, "rev-parse", "origin/main")
    print(f"7. {outcome['summary']} ✓")


def modeles_du_bundle() -> None:
    """Les nœuds mécaniques n'appellent aucun modèle, les jugements chers
    restent sur Sol high, les deux harnais de test sont du shell sans
    modèle, et la release prend sa voie shell avant Luna."""
    for nom in ("worktree", "deploy", "verify_deploy",
                "cleanup", "cleanup_unresolved", "cleanup_split"):
        cmd = BUNDLE["nodes"][nom]["config"]["execution"]["cmd"]
        assert "agent-codex.sh" not in cmd and "agent-opencode.sh" not in cmd, \
            f"{nom} lance encore un modèle"
    attendus = {
        "scope": ("gpt-5.6-sol", "high"),
        "implement": ("gpt-5.6-sol", "high"),
        "validate": ("gpt-5.6-luna", "low"),
        "judge": ("gpt-5.6-sol", "high"),
    }
    for nom, attendu in attendus.items():
        resolu = executors.resolve(BUNDLE, BUNDLE["nodes"][nom])
        assert resolu.cli == "codex", f"{nom} n'est pas sur codex"
        assert (resolu.model, resolu.effort) == attendu, \
            f"{nom} : modèle ou effort inattendu {resolu}"
    for nom in ("test_backend", "test_frontend"):
        resolu = executors.resolve(BUNDLE, BUNDLE["nodes"][nom])
        assert (resolu.cli, resolu.model, resolu.effort) == (None, None, None)
        assert resolu.cmd is not None and "test_harness.py" in resolu.cmd
    assert "release-node.sh" in BUNDLE["nodes"]["release"]["config"]["execution"]["cmd"], \
        "release ne prend pas sa voie shell avant Luna"
    assert "claude " not in json.dumps(BUNDLE), "le bundle dépend encore de claude"


def preuve_10() -> None:
    """10. la frontière du bundle, relue à chaque tour.

    La course d'implémentation tient en trois runs, les trois retraits sont
    le même shell, et aucun shell du rail ne teste `.git` comme un chemin.
    """
    modeles_du_bundle()
    variants = BUNDLE["nodes"]["implement"]["config"]["fanout"]["variants"]
    resolus = [executors.resolve(
        BUNDLE, graph.candidate_node(BUNDLE["nodes"]["implement"], rang))
        for rang in range(len(variants))]
    opencode = [r for r in resolus if r.cli == "opencode"]
    codex = [r for r in resolus if r.cli == "codex"]
    assert len(opencode) == 1 and len(codex) == 2, \
        f"course inattendue : {len(opencode)} opencode, {len(codex)} codex"
    # les trois retraits sont le même shell : seul leur prompt les distingue,
    # et une correction sur l'un doit se voir sur les trois
    retraits = {BUNDLE["nodes"][nom]["config"]["execution"]["cmd"]
                for nom in ("cleanup", "cleanup_unresolved", "cleanup_split")}
    assert len(retraits) == 1, "les nœuds de retrait ont divergé"
    # `.git` n'est un répertoire que dans un clone ordinaire : aucun shell du
    # rail ne doit s'en servir pour reconnaître un dépôt
    shells = {nom: node.get("config", {}).get("execution", {}).get("cmd", "")
              for nom, node in BUNDLE["nodes"].items()}
    shells["scripts/release.sh"] = (ROOT / "scripts" / "release.sh").read_text()
    for nom, shell in shells.items():
        assert "/.git" not in shell, f"{nom} teste .git comme un chemin"
    print("10. six nœuds sans modèle, Sol/Luna et effort explicites, "
          "fan-out deux codex + un opencode, aucun test sur .git ✓")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-shell-"))
    workspace = tmp / "item-42"  # le nom du workspace fait celui du worktree
    workspace.mkdir()
    repo = depot(tmp)
    worktree = repo / ".worktrees" / "rail-item-42"

    preuve_1(repo, workspace, worktree)
    preuve_2(repo, workspace, worktree)
    preuve_3(repo, workspace, worktree)
    preuve_4(repo, workspace)
    preuve_5(tmp, workspace)
    preuve_6(tmp, workspace)
    preuve_7(tmp, workspace)
    preuve_10()

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nnœuds shell, worktree : OK — déterministes, et jamais sans outcome")


if __name__ == "__main__":
    main()
