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
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = json.loads((ROOT / "examples" / "code-task.json").read_text())
CODEX = ROOT / "scripts" / "agent-codex.sh"
RELEASE_NODE = ROOT / "scripts" / "release-node.sh"


def cmd(node: str) -> str:
    """La commande d'agent d'un nœud réel."""
    return BUNDLE["nodes"][node]["config"]["agent"]["cmd"]


def porte(node: str, model: str, effort: str) -> None:
    """Un nœud nomme son modèle et son effort, sans dépendre du défaut local."""
    commande = cmd(node)
    assert f"CODEX_MODEL={model}" in commande, (node, commande)
    assert f"CODEX_REASONING_EFFORT={effort}" in commande, (node, commande)


def declaration() -> None:
    """1. Le bundle porte la table décidée et une course de trois candidats."""
    porte("scope", "gpt-5.6-sol", "high")
    porte("judge", "gpt-5.6-sol", "high")
    porte("test_backend", "gpt-5.6-luna", "low")
    porte("test_frontend", "gpt-5.6-luna", "medium")
    porte("validate", "gpt-5.6-luna", "low")
    porte("release", "gpt-5.6-luna", "low")
    assert "release-node.sh" in cmd("release"), cmd("release")

    variants = BUNDLE["nodes"]["implement"]["config"]["fanout"]["variants"]
    assert [v["label"] for v in variants] == ["minimal", "test d'abord", "gratuit"]
    assert len(variants) == 3
    luna, sol, gratuit = [v["agent"]["cmd"] for v in variants]
    assert "CODEX_MODEL=gpt-5.6-luna" in luna
    assert "CODEX_REASONING_EFFORT=medium" in luna
    assert "CODEX_MODEL=gpt-5.6-sol" in sol
    assert "CODEX_REASONING_EFFORT=high" in sol
    assert "agent-opencode.sh" in gratuit
    assert "opencode/deepseek-v4-flash-free" in gratuit
    assert variants[2]["agent"]["silence_s"] == 300
    assert "claude " not in json.dumps(BUNDLE)
    print("1. scope et judge Sol high ; course Luna medium + Sol high + "
          "DeepSeek gratuit ; portes légères sur Luna ✓")


def prompts_synchrones() -> None:
    """2. Les règles d'exécution sont cinq vrais paragraphes, pas du JSON cité."""
    nodes = ("scope", "implement", "test_backend", "test_frontend", "judge")
    for node in nodes:
        prompt = BUNDLE["nodes"][node]["config"]["agent"]["prompt"]
        assert "Tout se fait en synchrone" in prompt, node
        assert "outcome.json" in prompt, node
        assert "\\n" not in prompt, (node, prompt[:200])
    print("2. cinq prompts synchrones rendus avec de vrais retours de ligne ✓")


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
    workspace = tmp / "release"
    workspace.mkdir()
    release = scripts / "release.sh"
    executable(release, "#!/usr/bin/env bash\nprintf nominal > \"$GRAPHATOM_WORKSPACE/release.md\"\n")
    absent = tmp / "codex-ne-doit-pas-tourner"
    result = execute(RELEASE_NODE, workspace, {
        "GRAPHATOM_WORKTREE": str(worktree),
        "GRAPHATOM_WORKSPACE": str(workspace),
        "CODEX_BIN": str(absent),
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nroutage codex : OK — modèle et effort explicites, release script-first")


if __name__ == "__main__":
    main()
