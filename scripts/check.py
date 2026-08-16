#!/usr/bin/env python3
"""Porte unique du dépôt : inventaire fermé, puis preuves ordonnées.

Usage :

    uv run python scripts/check.py               # train + core (défaut)
    uv run python scripts/check.py train         # la porte du train seule
    uv run python scripts/check.py ui            # build Next.js + portes DOM
    uv run python scripts/check.py full          # base, crash, concurrence
    uv run python scripts/check.py live          # le vrai fournisseur gratuit
    uv run python scripts/check.py train core ui full   # composables

La liste est volontairement explicite. L'inventaire refuse tout nouveau test
qui n'est pas classé : aucune preuve ne disparaît parce qu'un fichier neuf a
été oublié dans un autre script.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# La porte du train : la métrique unique — noyau + pannes + train entier,
# sur un Postgres jetable qu'elle provisionne elle-même (docker requis).
TRAIN_PY = ("train_test.py",)

# Hermétiques : ni service DB partagé, ni Docker, ni LLM réel.
CORE_PY = (
    "agent_executor_test.py", "answer_test.py", "api_test.py",
    "checklist_test.py", "codex_routing_test.py", "criteria_test.py",
    "depends_test.py", "failure_trace_test.py", "fanout_config_test.py",
    "heartbeat_test.py", "links_test.py", "live_test.py", "orphans_test.py",
    "passation_test.py", "split_deps_test.py", "starved_test.py",
    "test_harness_test.py", "timeout_test.py", "validate_test.py",
)

FULL_PY = (
    "crash_test.py", "cycle_test.py", "deterministic_retry_test.py",
    "escalade_timeout_test.py", "fanout_opencode_test.py", "fanout_test.py",
    "fanout_worktree_test.py", "hermetic_test.py", "item_lane_test.py",
    "judge_test.py", "migration_test.py", "passage_test.py",
    "plafond_test.py", "portes_test.py", "preflight_test.py", "quota_test.py",
    "reconnect_test.py", "shell_test.py", "silence_test.py", "solo_test.py",
    "verrou_test.py",
)

LIVE_PY = ("opencode_test.py",)

UI_SH = ("front_graph_dom_test.sh", "front_run_trace_test.sh")
FULL_SH = ("front_test.sh",)

# Ces trois programmes Node sont appelés par les portes shell ci-dessus.
NODE_ENTRY = {
    "front_agent_model_test.mjs", "front_graph_dom_test.cjs",
    "front_run_trace_test.cjs",
}

TIERS = ("train", "core", "ui", "full", "live")


def _inventory() -> None:
    """Refuse un test exécutable absent du classement fermé."""
    tests = ROOT / "tests"
    python = {path.name for path in tests.glob("*_test.py")}
    shell = {path.name for path in tests.glob("*_test.sh")}
    node = {path.name for suffix in ("*.mjs", "*.cjs")
            for path in tests.glob(suffix) if path.stem.endswith("_test")}
    expected_python = set(TRAIN_PY) | set(CORE_PY) | set(FULL_PY) | set(LIVE_PY)
    expected_shell = set(UI_SH) | set(FULL_SH)
    problems = []
    for label, seen, expected in (
        ("python", python, expected_python),
        ("shell", shell, expected_shell),
        ("node", node, NODE_ENTRY),
    ):
        missing = sorted(seen - expected)
        stale = sorted(expected - seen)
        if missing:
            problems.append(f"{label} non classés : {missing}")
        if stale:
            problems.append(f"{label} déclarés mais absents : {stale}")
    if problems:
        raise SystemExit("inventaire des tests invalide\n- " + "\n- ".join(problems))


def _run(command: list[str], cwd: Path = ROOT) -> None:
    """Exécute une preuve et s'arrête au premier résultat faux."""
    print("\n$ " + " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode:
        raise SystemExit(
            f"ÉCHEC : {' '.join(command)} → code {completed.returncode}"
        )


def _python(names: tuple[str, ...]) -> None:
    for name in names:
        _run(["uv", "run", "python", f"tests/{name}"])


def _shell(names: tuple[str, ...]) -> None:
    for name in names:
        _run(["bash", f"tests/{name}"])


def main() -> None:
    parser = argparse.ArgumentParser(description="porte complète de GraphAtom")
    parser.add_argument("tiers", nargs="*", choices=TIERS,
                        help="les paliers à jouer (défaut : train core)")
    args = parser.parse_args()
    tiers = args.tiers or ["train", "core"]

    _inventory()
    print(f"inventaire fermé : "
          f"{len(TRAIN_PY) + len(CORE_PY) + len(FULL_PY) + len(LIVE_PY)} "
          f"tests Python, {len(UI_SH) + len(FULL_SH)} portes shell ✓")

    for tier in TIERS:
        if tier not in tiers:
            continue
        if tier == "train":
            _python(TRAIN_PY)
        elif tier == "core":
            _run(["uv", "run", "python", "-c",
                  "import graphatom.activation, graphatom.blocks, graphatom.gates, "
                  "graphatom.graph, graphatom.kernel, graphatom.scheduler, "
                  "graphatom.web"])
            _python(CORE_PY)
        elif tier == "ui":
            _run(["npm", "ci", "--prefer-offline"], ROOT / "front")
            _run(["npm", "run", "build"], ROOT / "front")
            _shell(UI_SH)
        elif tier == "full":
            _python(FULL_PY)
            _shell(FULL_SH)
        elif tier == "live":
            _python(LIVE_PY)

    print(f"\nCHECK {'+'.join(t.upper() for t in TIERS if t in tiers)} OK "
          "— toutes les preuves classées ont passé")


if __name__ == "__main__":
    main()
