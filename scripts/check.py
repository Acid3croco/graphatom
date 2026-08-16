#!/usr/bin/env python3
"""Porte unique du dépôt : inventaire fermé, puis preuves ordonnées.

Usage :

    uv run python scripts/check.py          # sans service DB, Docker ni LLM réel
    uv run python scripts/check.py --full   # toutes les preuves hermétiques
    uv run python scripts/check.py --live   # ajoute le vrai fournisseur gratuit

La liste est volontairement explicite. L'inventaire refuse tout nouveau test
qui n'est pas classé : aucune preuve ne disparaît parce qu'un fichier neuf a
été oublié dans un autre script.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# La porte du train : la métrique unique — noyau + pannes + train entier,
# sur un Postgres jetable qu'elle provisionne elle-même (docker requis).
TRAIN_PY = ("train_test.py",)

CORE_PY = (
    "agent_executor_test.py", "answer_test.py", "api_test.py",
    "checklist_test.py", "codex_routing_test.py", "criteria_test.py",
    "depends_test.py", "failure_trace_test.py", "fanout_config_test.py",
    "graph_executor_migration_test.py", "heartbeat_test.py", "links_test.py",
    "live_test.py", "orphans_test.py", "passation_test.py",
    "postgres_recovery_test.py", "pricing_test.py", "seed_test.py",
    "split_deps_test.py", "starved_test.py", "test_harness_test.py",
    "timeout_marge_test.py", "timeout_test.py", "validate_test.py",
)

FULL_PY = (
    "crash_test.py", "cycle_test.py", "deterministic_retry_test.py",
    "escalade_timeout_test.py", "fanout_opencode_test.py", "fanout_test.py",
    "fanout_worktree_test.py", "hermetic_test.py", "item_lane_test.py",
    "judge_test.py", "migration_test.py", "passage_test.py",
    "plafond_test.py", "portes_test.py", "pricing_db_test.py", "quota_test.py",
    "reconnect_test.py", "shell_test.py", "silence_test.py", "solo_test.py",
    "verrou_test.py",
)

LIVE_PY = ("opencode_test.py",)

CORE_SH = ("front_graph_dom_test.sh", "front_run_trace_test.sh")
FULL_SH = ("front_test.sh",)

# Ces deux programmes Node sont appelés par les portes shell ci-dessus.
NODE_ENTRY = {
    "front_agent_model_test.mjs", "front_graph_dom_test.cjs",
    "front_run_trace_test.cjs",
}


def _inventory() -> None:
    """Refuse un test exécutable absent du classement fermé."""
    tests = ROOT / "tests"
    python = {path.name for path in tests.glob("*_test.py")}
    shell = {path.name for path in tests.glob("*_test.sh")}
    node = {path.name for suffix in ("*.mjs", "*.cjs")
            for path in tests.glob(suffix) if path.stem.endswith("_test")}
    expected_python = set(TRAIN_PY) | set(CORE_PY) | set(FULL_PY) | set(LIVE_PY)
    expected_shell = set(CORE_SH) | set(FULL_SH)
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
    parser.add_argument("--full", action="store_true",
                        help="ajouter base, crash, concurrence et image Docker")
    parser.add_argument("--live", action="store_true",
                        help="ajouter le test réel du fournisseur gratuit")
    args = parser.parse_args()

    _inventory()
    print(f"inventaire fermé : "
          f"{len(TRAIN_PY) + len(CORE_PY) + len(FULL_PY) + len(LIVE_PY)} "
          f"tests Python, {len(CORE_SH) + len(FULL_SH)} portes shell ✓")
    _python(TRAIN_PY)
    _run(["uv", "run", "python", "-c",
          "import graphatom.blocks, graphatom.graph, graphatom.kernel, "
          "graphatom.scheduler, graphatom.web"])
    _python(CORE_PY)
    _run(["npm", "ci", "--prefer-offline"], ROOT / "front")
    _run(["npm", "run", "build"], ROOT / "front")
    _shell(CORE_SH)

    if args.full:
        _python(FULL_PY)
        _shell(FULL_SH)
    if args.live:
        _python(LIVE_PY)

    level = "+".join(part for part, active in (
        ("full", args.full), ("live", args.live)
    ) if active) or "core"
    print(f"\nCHECK {level.upper()} OK — toutes les preuves classées ont passé")


if __name__ == "__main__":
    main()
