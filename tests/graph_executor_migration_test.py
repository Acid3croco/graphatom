"""La migration des graphes vers les exécuteurs structurés, sans base.

Usage : uv run python tests/graph_executor_migration_test.py
"""

from copy import deepcopy
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from graphatom import executors, graph  # noqa: E402


# Référence sémantique des commandes avant migration. Les scripts des
# adaptateurs gardent l'invocation, le prompt et l'extraction d'usage.
LEGACY = {
    ("code-task", "scope"): ("codex", "gpt-5.6-sol", "high"),
    ("code-task", "implement"): ("codex", "gpt-5.6-luna", "medium"),
    ("code-task", "implement[0]"): ("codex", "gpt-5.6-luna", "medium"),
    ("code-task", "implement[1]"): ("codex", "gpt-5.6-sol", "high"),
    ("code-task", "implement[2]"): (
        "opencode", "opencode/deepseek-v4-flash-free", None),
    ("code-task", "test_backend"): ("codex", "gpt-5.6-luna", "low"),
    ("code-task", "test_frontend"): ("codex", "gpt-5.6-luna", "medium"),
    ("code-task", "validate"): ("codex", "gpt-5.6-luna", "low"),
    ("code-task", "release"): ("codex", "gpt-5.6-luna", "low"),
    ("code-task", "judge"): ("codex", "gpt-5.6-sol", "high"),
    ("gauntlet", "plan"): ("claude", None, None),
    ("gauntlet", "build"): ("claude", None, None),
    ("gauntlet", "critique"): ("claude", None, None),
    ("gauntlet", "integrate"): ("claude", None, None),
}

PROMPTS = {
    ("code-task", "scope"): "c02cb2ab7b2f",
    ("code-task", "implement"): "1fdfc7929b26",
    ("code-task", "test_backend"): "9a3125818e52",
    ("code-task", "test_frontend"): "8029121aab4a",
    ("code-task", "validate"): "bc622f34ac7b",
    ("code-task", "release"): "dda519e4cff3",
    ("code-task", "judge"): "56f3b53169d6",
    ("gauntlet", "plan"): "2f0011a894f4",
    ("gauntlet", "build"): "82c08b7d4b44",
    ("gauntlet", "critique"): "1c06cc3a0d2e",
    ("gauntlet", "integrate"): "9cc8f38e36a7",
}


def bundles() -> dict[str, dict]:
    """Charge tous les graphes d'exemple publiables."""
    return {path.stem: json.loads(path.read_text())
            for path in sorted((ROOT / "examples").glob("*.json"))}


def inventaire(paquets: dict[str, dict]) -> None:
    """1 et 4. Les défauts sont partout; les commandes restantes sont motivées."""
    for name, paquet in paquets.items():
        assert (paquet.get("agent") or {}).get("cli"), \
            f"{name} ne déclare pas d'exécuteur par défaut"
        for node, spec in paquet["nodes"].items():
            local = (spec.get("config") or {}).get("agent") or {}
            if "cmd" in local:
                assert local.get("cmd_reason"), f"{name}.{node}.agent.cmd sans raison"
                assert "agent-codex.sh" not in local["cmd"]
                assert "agent-opencode.sh" not in local["cmd"]
                assert "claude --dangerously" not in local["cmd"]
    print("1 et 4. inventaire des défauts, surcharges et exceptions ✓")


def resolution(paquets: dict[str, dict]) -> None:
    """2 et 3. Héritage et référence antérieure restent identiques."""
    for (bundle_name, location), expected in LEGACY.items():
        node_name, _, index = location.partition("[")
        paquet = paquets[bundle_name]
        spec = paquet["nodes"][node_name]
        if index:
            spec = graph.candidate_node(spec, int(index[:-1]))
        resolved = executors.resolve(paquet, spec)
        actual = (resolved.cli, resolved.model, resolved.effort)
        assert actual == expected, f"{bundle_name}.{location}: {actual} != {expected}"
        command = executors.command(resolved)
        if resolved.cmd is None:
            assert executors.ADAPTERS[resolved.cli].script in command

    for (bundle_name, node_name), expected_hash in PROMPTS.items():
        prompt = paquets[bundle_name]["nodes"][node_name]["config"]["agent"]["prompt"]
        assert hashlib.sha256(prompt.encode()).hexdigest().startswith(expected_hash)

    code = paquets["code-task"]
    changed = deepcopy(code)
    changed["agent"]["model"] = "modèle-remplacé"
    for name, spec in code["nodes"].items():
        before = executors.resolve(code, spec)
        after = executors.resolve(changed, spec)
        local = (spec.get("config") or {}).get("agent") or {}
        if before.cli == "codex" and "model" not in local:
            assert after.model == "modèle-remplacé", name
        else:
            assert after.model == before.model, name
    print("2 et 3. héritage du modèle et équivalence de migration ✓")


def main() -> None:
    """Exécute les preuves sans base ni réseau."""
    paquets = bundles()
    inventaire(paquets)
    resolution(paquets)
    print("\nmigration des graphes : OK")


if __name__ == "__main__":
    main()
