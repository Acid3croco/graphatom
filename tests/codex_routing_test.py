"""Le routage explicite des modèles Codex et la voie rapide de release.

Ce test reste sans réseau et sans base : une fausse CLI capture les arguments
que l'adaptateur lui donne, et de faux scripts de release tranchent les deux
voies du nœud.

Usage : uv run python tests/codex_routing_test.py
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from graphatom import executors, graph  # noqa: E402

BUNDLE = json.loads((ROOT / "examples" / "code-task.json").read_text())
CODEX = ROOT / "scripts" / "agent-codex.sh"
DECLARED = ROOT / "scripts" / "agent-declared.sh"
RELEASE_NODE = ROOT / "scripts" / "release-node.sh"


def cmd(node: str) -> str:
    """La commande effective d'un wagon réel."""
    return executors.command(executors.resolve(BUNDLE, BUNDLE["nodes"][node]))


def porte(node: str, model: str, effort: str) -> None:
    """Un nœud résout le modèle et l'effort attendus."""
    resolved = executors.resolve(BUNDLE, BUNDLE["nodes"][node])
    assert (resolved.cli, resolved.model, resolved.effort) == (
        "codex", model, effort), (node, resolved)


def declaration() -> None:
    """1. Le bundle porte la table décidée et une course de trois candidats."""
    porte("scope", "gpt-5.6-sol", "high")
    porte("judge", "gpt-5.6-sol", "high")
    porte("validate", "gpt-5.6-luna", "low")
    porte("release", "gpt-5.6-luna", "low")
    assert "release-node.sh" in cmd("release"), cmd("release")
    for name in ("implement", "release"):
        resolved = executors.resolve(BUNDLE, BUNDLE["nodes"][name])
        assert resolved.cmd is not None and resolved.kind == "composed", (
            name, resolved
        )
    for name in ("test_backend", "test_frontend"):
        resolved = executors.resolve(BUNDLE, BUNDLE["nodes"][name])
        assert resolved.cmd is not None and resolved.kind == "command"
        assert "test_harness.py" in resolved.cmd, (name, resolved)
        assert (resolved.cli, resolved.model, resolved.effort) == (None, None, None)

    variants = BUNDLE["nodes"]["implement"]["config"]["fanout"]["variants"]
    assert [v["label"] for v in variants] == [
        "minimal Luna", "minimal Sol", "gratuit libre"]
    assert len(variants) == 3
    spec = BUNDLE["nodes"]["implement"]
    luna, sol, gratuit = [executors.resolve(BUNDLE, graph.candidate_node(spec, i))
                          for i in range(3)]
    assert (luna.cli, luna.model, luna.effort) == (
        "codex", "gpt-5.6-luna", "medium")
    assert (sol.cli, sol.model, sol.effort) == (
        "codex", "gpt-5.6-sol", "high")
    assert (gratuit.cli, gratuit.model, gratuit.effort) == (
        "opencode", "opencode/deepseek-v4-flash-free", None)
    minimal = "le diff minimal"
    assert minimal in variants[0]["strategy"]
    assert minimal in variants[1]["strategy"]
    assert "aucune méthode de construction ne t'est imposée" in variants[2]["strategy"]
    implement = executors.resolve(BUNDLE, BUNDLE["nodes"]["implement"])
    assert (implement.cli, implement.model, implement.effort) == (
        "codex", "gpt-5.6-sol", "high")
    assert variants[2]["execution"]["silence_s"] == 300
    assert "claude " not in json.dumps(BUNDLE)
    print("1. scope, judge et implement seul Sol high ; course minimale "
          "Luna medium + Sol high + DeepSeek gratuit libre ; tests sans modèle ✓")


def prompts_synchrones() -> None:
    """2. Seuls les wagons agent portent un prompt de modèle."""
    nodes = ("scope", "implement", "validate", "release", "judge")
    for node in nodes:
        prompt = BUNDLE["nodes"][node]["config"]["agent"]["prompt"]
        assert prompt.strip(), node
        assert "\\n" not in prompt, (node, prompt[:200])
        assert BUNDLE["nodes"][node]["config"]["execution"]["kind"] == "agent"
    for node in ("test_backend", "test_frontend"):
        config = BUNDLE["nodes"][node]["config"]
        assert config["execution"]["kind"] == "command", node
        assert "agent" not in config, node
    print("2. cinq agents avec prompt ; deux portes command sans faux modèle ✓")


def executable(path: Path, content: str) -> None:
    """Écrit un petit exécutable jetable."""
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def fake_codex(path: Path) -> None:
    """Une CLI qui capture ses arguments et rend une issue lisible."""
    executable(path, """#!/usr/bin/env bash
printf '%s\n' "$@" > "$CODEX_CAPTURE"
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}'
if [ -n "${FAKE_OUTCOME:-}" ]; then
  printf '%s\n' "$FAKE_OUTCOME" > outcome.json
else
  printf '%s\n' '{"outcome":"done","summary":"ok"}' > outcome.json
fi
""")


def execute(script: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Lance un adaptateur réel avec un environnement explicite."""
    return subprocess.run(["bash", str(script)], cwd=cwd, env=os.environ | env,
                          text=True, capture_output=True, timeout=15)


def arguments_codex(tmp: Path) -> None:
    """3. L'adaptateur passe le modèle et l'effort à la CLI."""
    workspace = tmp / "adapter"
    workspace.mkdir()
    (workspace / "prompt.md").write_text("test")
    capture = workspace / "args.txt"
    cli = workspace / "fake-codex"
    fake_codex(cli)
    result = execute(CODEX, workspace, {
        "CODEX_BIN": str(cli),
        "CODEX_CAPTURE": str(capture),
        "CODEX_DIR": str(workspace),
        "CODEX_MODEL": "gpt-5.6-luna",
        "CODEX_REASONING_EFFORT": "medium",
        "CODEX_TIMEOUT_S": "5",
    })
    assert result.returncode == 0, result.stderr
    args = capture.read_text().splitlines()
    assert args[args.index("-m") + 1] == "gpt-5.6-luna", args
    assert args[args.index("-c") + 1] == 'model_reasoning_effort="medium"', args
    print("3. agent-codex transmet gpt-5.6-luna et effort medium à la CLI ✓")


def release_rapide(tmp: Path) -> None:
    """4. Le nominal reste shell ; seule la panne appelle Luna low."""
    worktree = tmp / "worktree"
    scripts = worktree / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(CODEX, scripts / "agent-codex.sh")
    shutil.copy2(DECLARED, scripts / "agent-declared.sh")
    workspace = tmp / "release"
    workspace.mkdir()
    release = scripts / "release.sh"
    executable(release, "#!/usr/bin/env bash\nprintf nominal > \"$GRAPHATOM_WORKSPACE/release.md\"\n")
    absent = tmp / "codex-ne-doit-pas-tourner"
    result = execute(RELEASE_NODE, workspace, {
        "GRAPHATOM_WORKTREE": str(worktree),
        "GRAPHATOM_WORKSPACE": str(workspace),
        "CODEX_BIN": str(absent),
        "GRAPHATOM_AGENT_CLI": "codex",
        "CODEX_MODEL": "gpt-5.6-luna",
        "CODEX_REASONING_EFFORT": "low",
        "CODEX_TIMEOUT_S": "540",
    })
    assert result.returncode == 0, result.stderr
    assert json.loads((workspace / "outcome.json").read_text())["outcome"] == "done"

    executable(release, "#!/usr/bin/env bash\nprintf panne > \"$GRAPHATOM_WORKSPACE/release.md\"\nexit 9\n")
    (workspace / "outcome.json").unlink()
    (workspace / "prompt.md").write_text("répare")
    capture = workspace / "release-args.txt"
    cli = tmp / "fake-codex-release"
    fake_codex(cli)
    result = execute(RELEASE_NODE, workspace, {
        "GRAPHATOM_WORKTREE": str(worktree),
        "GRAPHATOM_WORKSPACE": str(workspace),
        "CODEX_BIN": str(cli),
        "CODEX_CAPTURE": str(capture),
        "FAKE_OUTCOME": '{"outcome":"rebased","summary":"réparé"}',
        "GRAPHATOM_AGENT_CLI": "codex",
        "CODEX_MODEL": "gpt-5.6-luna",
        "CODEX_REASONING_EFFORT": "low",
        "CODEX_TIMEOUT_S": "540",
    })
    assert result.returncode == 0, result.stderr
    assert json.loads((workspace / "outcome.json").read_text())["outcome"] == "rebased"
    args = capture.read_text().splitlines()
    assert args[args.index("-m") + 1] == "gpt-5.6-luna", args
    assert args[args.index("-c") + 1] == 'model_reasoning_effort="low"', args
    print("4. release nominale sans modèle ; panne confiée à Luna low ✓")


def git(cwd: Path, *args: str) -> str:
    """Lance git dans le petit dépôt du test et rend sa sortie."""
    done = subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          text=True, capture_output=True)
    return done.stdout.strip()


def selection_diff(tmp: Path) -> None:
    """4. Les tests voient la branche et le worktree, pas le retard sur main."""
    repo = tmp / "diff-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "diff@test.invalid")
    git(repo, "config", "user.name", "diff")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    worktree = repo / ".worktrees" / "rail-item-17"
    git(repo, "worktree", "add", "-q", "-b", "issue", str(worktree), "main")
    (worktree / "README.md").write_text("changement de l'issue\n")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-qm", "issue")

    (repo / "front").mkdir()
    (repo / "tests").mkdir()
    (repo / "front" / "page.tsx").write_text("nouveau sur main\n")
    (repo / "tests" / "main_test.py").write_text("# nouveau sur main\n")
    git(repo, "add", "front/page.tsx", "tests/main_test.py")
    git(repo, "commit", "-qm", "main avance")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    scripts = worktree / "scripts"
    scripts.mkdir()
    agent = scripts / "agent-codex.sh"
    executable(agent, """#!/usr/bin/env bash
printf appelé > "$GRAPHATOM_WORKSPACE/agent-called"
printf '%s\n' '{"outcome":"pass","summary":"agent appelé"}' > outcome.json
""")
    shutil.copy2(DECLARED, scripts / "agent-declared.sh")
    workspace = tmp / "item-17"
    workspace.mkdir()
    env = os.environ | {
        "GRAPHATOM_REPO_DIR": str(repo),
        "GRAPHATOM_WORKTREE": str(worktree),
        "GRAPHATOM_WORKSPACE": str(workspace),
        "GRAPHATOM_AGENT_CLI": "codex",
    }

    portes = (ROOT / "scripts" / "portes.sh").read_text()
    for fragment in ("git diff --name-only origin/main...HEAD",
                     "git diff --name-only HEAD",
                     "git ls-files --others --exclude-standard"):
        assert fragment in portes, fragment
    assert "git diff --name-only origin/main;" not in portes

    for node in ("test_backend", "test_frontend"):
        (workspace / "outcome.json").unlink(missing_ok=True)
        (workspace / "agent-called").unlink(missing_ok=True)
        done = subprocess.run(cmd(node), shell=True, cwd=workspace, env=env,
                              text=True, capture_output=True, timeout=10)
        assert done.returncode == 0, (node, done.stdout, done.stderr)
        outcome = json.loads((workspace / "outcome.json").read_text())
        assert outcome["outcome"] == "pass", (node, outcome)
        assert not (workspace / "agent-called").exists(), node

    def agents_appeles() -> None:
        """Les deux tests doivent appeler leur agent pour ce diff pertinent."""
        for node in ("test_backend", "test_frontend"):
            (workspace / "outcome.json").unlink(missing_ok=True)
            (workspace / "agent-called").unlink(missing_ok=True)
            done = subprocess.run(cmd(node), shell=True, cwd=workspace, env=env,
                                  text=True, capture_output=True, timeout=10)
            assert done.returncode == 0, (node, done.stdout, done.stderr)
            assert (workspace / "agent-called").read_text() == "appelé", node

    source = worktree / "src" / "graphatom"
    source.mkdir(parents=True)
    (source / "web.py").write_text("changement non commité\n")
    agents_appeles()  # fichier neuf non suivi
    git(worktree, "add", "src/graphatom/web.py")
    agents_appeles()  # fichier neuf indexé
    git(worktree, "commit", "-qm", "branche pertinente")
    agents_appeles()  # changement pertinent commité sur la branche
    (source / "web.py").write_text("changement suivi non commité\n")
    agents_appeles()  # fichier suivi modifié

    print("4. un main plus récent ne déclenche aucun test ; un fichier src/ "
          "non suivi, indexé, commité ou modifié déclenche les deux tests ; "
          "les portes utilisent la même sélection ✓")


def main() -> None:
    declaration()
    prompts_synchrones()
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-codex-routing-"))
    try:
        arguments_codex(tmp)
        release_rapide(tmp)
        # La sélection des tests est maintenant le contrat versionné de
        # tests/test_harness_test.py, sans agent factice.
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nroutage codex : OK — modèle et effort explicites, release script-first")


if __name__ == "__main__":
    main()
