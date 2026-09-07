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
