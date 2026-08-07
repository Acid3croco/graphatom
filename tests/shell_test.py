"""Le test des nœuds shell de `code-task` : du déterminisme, pas du prompt.

Les `cmd` du graph sont joués tels quels sur un dépôt jetable — aucun
modèle, aucun réseau, aucun docker. Ce qu'on vérifie, c'est la promesse
qui autorise un nœud sans agent : il écrit toujours son `outcome.json`.

  1. `worktree` crée le worktree de l'item sur `rail/issue-<num>` depuis
     `origin/main`, et le dit dans `worktree.md`
  2. rejoué, il reprend celui du cycle en cours au lieu d'en refaire un
  3. la branche restée d'un cycle passé est reprise telle quelle
  4. un sujet sans numéro échoue — proprement, sans toucher au dépôt
  5. `deploy` et `verify_deploy` sans environnement : `failed` / `fail`,
     jamais un nœud coincé
  6. `scripts/release.sh` nomme le pas qui a lâché et sort sur son code

Usage : uv run python tests/shell_test.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = json.loads((ROOT / "examples" / "code-task.json").read_text())
SUJET = "gh:Acid3croco/graphatom#77"


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def depot(tmp: Path) -> Path:
    """Un clone de référence et son `origin`, comme en a le worker du rail."""
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    source = tmp / "source"
    # cloner un dépôt vide avertit sur stderr : le bruit n'est pas une panne
    subprocess.run(["git", "clone", "-q", str(origin), str(source)],
                   check=True, capture_output=True)
    git(source, "config", "user.email", "shell@test.invalid")
    git(source, "config", "user.name", "shell")
    (source / "socle.txt").write_text("le commit de départ\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "socle")
    git(source, "push", "-q", "origin", "main")
    repo = tmp / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    return repo


def joue(node: str, repo: Path, workspace: Path, subject: str = SUJET) -> dict:
    """Le `cmd` d'un nœud du graph, joué tel quel. Rend son outcome.json."""
    (workspace / "outcome.json").unlink(missing_ok=True)
    subprocess.run(
        BUNDLE["nodes"][node]["config"]["agent"]["cmd"],
        shell=True, cwd=workspace, check=False, capture_output=True,
        env=os.environ | {"GRAPHATOM_REPO_DIR": str(repo),
                          "GRAPHATOM_WORKSPACE": str(workspace),
                          "GRAPHATOM_SUBJECT_KEY": subject,
                          "GRAPHATOM_WEB_URL": "http://127.0.0.1:9"},
    )
    return json.loads((workspace / "outcome.json").read_text())


def release(repo: Path, workspace: Path, subject: str) -> int:
    """`scripts/release.sh` du worktree, lancé comme le ferait l'agent."""
    out = subprocess.run(
        [str(ROOT / "scripts" / "release.sh")], capture_output=True, text=True,
        env=os.environ | {"GRAPHATOM_REPO_DIR": str(repo),
                          "GRAPHATOM_WORKSPACE": str(workspace),
                          "GRAPHATOM_SUBJECT_KEY": subject},
    )
    return out.returncode


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-shell-"))
    workspace = tmp / "item-42"  # le nom du workspace fait celui du worktree
    workspace.mkdir()
    repo = depot(tmp)
    worktree = repo / ".worktrees" / "rail-item-42"

    # 1. le worktree part d'origin/main, sur la branche du sujet
    outcome = joue("worktree", repo, workspace)
    assert outcome["outcome"] == "done", outcome
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "rail/issue-77"
    assert git(worktree, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")
    assert "créé depuis origin/main" in (workspace / "worktree.md").read_text()
    print(f"1. {outcome['summary']} ✓")

    # 2. rejoué, il reprend : le travail du cycle en cours ne se perd pas
    (worktree / "travail.txt").write_text("l'implémentation en cours\n")
    outcome = joue("worktree", repo, workspace)
    assert outcome["outcome"] == "done", outcome
    assert "repris" in outcome["summary"], outcome
    assert (worktree / "travail.txt").exists(), "le worktree a été recréé sous le travail"
    print(f"2. {outcome['summary']} ✓")

    # 3. le worktree est parti, la branche est restée : on la reprend
    git(repo, "worktree", "remove", "--force", str(worktree))
    outcome = joue("worktree", repo, workspace)
    assert outcome["outcome"] == "done", outcome
    assert "déjà là" in outcome["summary"], outcome
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "rail/issue-77"
    print(f"3. {outcome['summary']} ✓")

    # 4. un sujet dont on ne tire pas de numéro : échec nommé, dépôt intact
    outcome = joue("worktree", repo, workspace, "pipeline-x:oom")
    assert outcome["outcome"] == "failed", outcome
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
    print(f"4. {outcome['summary']} ✓")

    # 5. les nœuds de déploiement sans rien à quoi se raccrocher : ils
    #    rendent leur issue quand même — un nœud shell ne coince jamais
    absent = tmp / "pas-un-clone"
    assert joue("deploy", absent, workspace)["outcome"] == "failed"
    assert joue("verify_deploy", absent, workspace)["outcome"] == "fail"
    for rapport in ("deploy.md", "verify_deploy.md"):
        assert (workspace / rapport).read_text().strip(), rapport
    print("5. deploy et verify_deploy sans environnement : outcome écrit ✓")

    # 6. le script de release sort sur le code du pas qui a lâché
    assert release(repo, workspace, "pipeline-x:oom") == 2  # sujet illisible
    assert release(repo, workspace, "gh:Acid3croco/graphatom#999") == 3  # autre branche
    assert "code 3" in (workspace / "release.md").read_text()
    print("6. release.sh : code 2 sur le sujet, code 3 sur le worktree ✓")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nnœuds shell : OK — déterministes, et jamais sans outcome")


if __name__ == "__main__":
    main()
