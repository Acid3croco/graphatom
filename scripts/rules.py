#!/usr/bin/env python3
"""La porte des règles de qualité : chaque seuil mesuré, aucun deviné.

    uv run python scripts/rules.py static     # complexité, taille, code mort, doublons
    uv run python scripts/rules.py coverage   # les tests sous coverage, puis 100 % et CRAP
    uv run python scripts/rules.py            # les deux

Les seuils (voir ~/rules.md du mainteneur, repris ici tels quels) :

    complexité cyclomatique < 22      par fonction, radon
    complexité cognitive < 22         par fonction, complexipy
    difficulté de Halstead < 80       par fonction et par fichier, radon
    lignes par fichier < 500          lignes physiques
    couverture des tests : 100 %      lignes et branches, sous-processus compris
    CRAP < 25                         CC² × (1 − couverture)³ + CC, par fonction
    code mort : 0                     vulture, avec la liste blanche des rappels HTTP
    code redondant : 0                pylint duplicate-code, 6 lignes ou plus

Les mutants survivants (0) sont mesurés à part par `scripts/mutants.py`,
parce que la mesure coûte des heures et non des minutes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("src/graphatom", "scripts", "tests")

MAX_CC = 22
MAX_COGNITIVE = 22
MAX_HALSTEAD = 80
MAX_LINES = 500
MAX_CRAP = 25
MIN_COVERAGE = 100.0
DUPLICATE_LINES = 6
DEAD_CONFIDENCE = 60


def _run(*command: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=check)


def _tool(name: str, *args: str) -> subprocess.CompletedProcess:
    return _run("uv", "run", "--", name, *args)


# ------------------------------------------------------------------ statique


def _functions(payload: dict) -> list[tuple[str, str, int, int, int]]:
    """(fichier, nom, ligne de début, ligne de fin, complexité) pour chaque fonction."""
    found = []
    for path, items in payload.items():
        for item in items:
            if "complexity" not in item:
                continue
            found.append((path, item["name"], item["lineno"], item["endline"],
                          item["complexity"]))
            for method in item.get("methods", []):
                found.append((path, f"{item['name']}.{method['name']}",
                              method["lineno"], method["endline"], method["complexity"]))
    return found


def cyclomatic() -> tuple[list[str], list[tuple[str, str, int, int, int]]]:
    done = _tool("radon", "cc", "-j", *SOURCES)
    functions = _functions(json.loads(done.stdout))
    return ([f"{path}:{name} complexité cyclomatique {cc} ≥ {MAX_CC}"
             for path, name, _, _, cc in functions if cc >= MAX_CC], functions)


def cognitive() -> list[str]:
    done = _tool("complexipy", *SOURCES, "--max-complexity-allowed",
                 str(MAX_COGNITIVE - 1), "-f", "--plain", "-C", "no", "--quiet")
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    return [f"{line} : complexité cognitive ≥ {MAX_COGNITIVE}" for line in lines
            if line.split()[-1].isdigit() and int(line.split()[-1]) >= MAX_COGNITIVE]


def halstead() -> list[str]:
    done = _tool("radon", "hal", "-j", "-f", *SOURCES)
    problems = []
    for path, report in json.loads(done.stdout).items():
        if report["total"]["difficulty"] >= MAX_HALSTEAD:
            problems.append(f"{path} : difficulté de Halstead "
                            f"{report['total']['difficulty']:.1f} ≥ {MAX_HALSTEAD}")
        for name, metrics in report["functions"].items():
            if metrics["difficulty"] >= MAX_HALSTEAD:
                problems.append(f"{path}:{name} : difficulté de Halstead "
                                f"{metrics['difficulty']:.1f} ≥ {MAX_HALSTEAD}")
    return problems


def file_lengths() -> list[str]:
    problems = []
    for source in SOURCES:
        for path in sorted((ROOT / source).rglob("*.py")):
            lines = sum(1 for _ in path.open())
            if lines >= MAX_LINES:
                problems.append(f"{path.relative_to(ROOT)} : {lines} lignes ≥ {MAX_LINES}")
    return problems


def dead_code() -> list[str]:
    done = _tool("vulture", "--min-confidence", str(DEAD_CONFIDENCE), *SOURCES,
                 "scripts/vulture_whitelist.py")
    return [f"code mort : {line}" for line in done.stdout.splitlines() if line.strip()]


def duplicates() -> list[str]:
    files = [str(path.relative_to(ROOT)) for source in SOURCES
             for path in sorted((ROOT / source).rglob("*.py"))]
    done = _tool("pylint", "--disable=all", "--enable=duplicate-code",
                 f"--min-similarity-lines={DUPLICATE_LINES}", "--score=n", *files)
    heads = [line for line in done.stdout.splitlines() if "R0801" in line]
    pairs = [line for line in done.stdout.splitlines() if line.startswith("==")]
    return [f"code redondant : {len(heads)} bloc(s) — {' '.join(pairs)}"] if heads else []


def static() -> list[str]:
    problems, _ = cyclomatic()
    for check in (cognitive, halstead, file_lengths, dead_code, duplicates):
        problems.extend(check())
    return problems


# ---------------------------------------------------------------- couverture


def _coverage_hook() -> Path:
    """Le `.pth` qui fait démarrer coverage dans chaque sous-processus.

    Les tests lancent la CLI et des agents en sous-processus : sans ce
    crochet, leurs lignes ne compteraient pas. Idempotent, dans le venv du
    projet.
    """
    site = _run("uv", "run", "python", "-c",
                "import site; print(site.getsitepackages()[0])", check=True).stdout.strip()
    hook = Path(site) / "graphatom_cov.pth"
    hook.write_text("import coverage; coverage.process_startup()\n")
    return hook


def _rc(data_dir: Path) -> Path:
    rc = data_dir / "rc"
    rc.write_text(
        "[run]\nparallel = True\nbranch = True\nsource = graphatom\n"
        f"data_file = {data_dir / '.coverage'}\n"
    )
    return rc


def _measure(tests: list[Path], data_dir: Path) -> None:
    """Chaque preuve sous coverage, dans l'ordre, sans s'arrêter au premier échec.

    Une preuve rouge est rapportée par sa commande exacte : la porte du
    dépôt (`check.py`) reste celle qui s'arrête au premier échec, ici on
    veut la couverture entière.
    """
    rc = _rc(data_dir)
    env = os.environ | {"COVERAGE_PROCESS_START": str(rc)}
    failed = []
    for test in tests:
        print(f"$ uv run coverage run {test.relative_to(ROOT)}", flush=True)
        done = subprocess.run(
            ["uv", "run", "coverage", "run", f"--rcfile={rc}", str(test)],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        if done.returncode:
            failed.append(str(test.relative_to(ROOT)))
            print(done.stdout[-2000:], done.stderr[-2000:], flush=True)
    _run("uv", "run", "coverage", "combine", f"--rcfile={rc}", str(data_dir))
    if failed:
        raise SystemExit("ÉCHEC des preuves : " + ", ".join(failed))


def _lines_report(data_dir: Path) -> dict:
    rc = data_dir / "rc"
    _run("uv", "run", "coverage", "json", "-i", f"--rcfile={rc}",
         "-o", str(data_dir / "coverage.json"))
    return json.loads((data_dir / "coverage.json").read_text())


def _crap(functions, report: dict) -> list[str]:
    """CRAP = CC² × (1 − couverture)³ + CC, par fonction, sur ses lignes."""
    problems = []
    for path, name, start, end, cc in functions:
        if not path.startswith("src/graphatom"):
            continue
        entry = report["files"].get(path)
        if entry is None:
            continue
        executed = {n for n in entry["executed_lines"] if start <= n <= end}
        missing = {n for n in entry["missing_lines"] if start <= n <= end}
        total = len(executed) + len(missing)
        ratio = len(executed) / total if total else 1.0
        crap = cc ** 2 * (1 - ratio) ** 3 + cc
        if crap >= MAX_CRAP:
            problems.append(f"{path}:{name} : CRAP {crap:.1f} ≥ {MAX_CRAP} "
                            f"(CC {cc}, couverture {ratio:.0%})")
    return problems


def _classified_tests() -> list[Path]:
    """Les preuves des paliers train, core et full — jamais le palier live.

    L'inventaire fermé de `check.py` fait foi : `live` dépend d'un
    fournisseur réel et du réseau, la couverture ne l'attend pas.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import check  # noqa: PLC0415 — le module voisin, lu pour son inventaire
    names = (*check.TRAIN_PY, *check.CORE_PY, *check.FULL_PY)
    return [ROOT / "tests" / name for name in names]


def coverage(tests: list[Path] | None = None) -> list[str]:
    data_dir = ROOT / ".coverage-rules"
    data_dir.mkdir(exist_ok=True)
    for stale in data_dir.glob(".coverage*"):
        stale.unlink()
    _coverage_hook()
    _measure(tests or _classified_tests(), data_dir)
    report = _lines_report(data_dir)
    problems = []
    for path, entry in sorted(report["files"].items()):
        summary = entry["summary"]
        percent = summary["percent_covered"]
        if percent < MIN_COVERAGE:
            missing = ", ".join(str(n) for n in entry["missing_lines"][:20])
            branches = summary.get("missing_branches", 0)
            problems.append(f"{path} : couverture {percent:.1f} % < {MIN_COVERAGE:.0f} % "
                            f"(lignes {missing}{'…' if len(entry['missing_lines']) > 20 else ''}"
                            f", {branches} branche(s) manquante(s))")
    _, functions = cyclomatic()
    problems.extend(_crap(functions, report))
    total = report["totals"]["percent_covered"]
    print(f"couverture totale : {total:.2f} %", flush=True)
    return problems


# ---------------------------------------------------------------------- main


def main() -> None:
    wanted = sys.argv[1:] or ["static", "coverage"]
    problems: list[str] = []
    if "static" in wanted:
        problems.extend(static())
        print(f"statique : {len(problems)} écart(s)", flush=True)
    if "coverage" in wanted:
        tests = [ROOT / name for name in sys.argv[2:] if name.endswith(".py")]
        problems.extend(coverage(tests or None))
    if problems:
        raise SystemExit("règles non tenues\n- " + "\n- ".join(problems))
    print("RÈGLES OK — tous les seuils tenus")


if __name__ == "__main__":
    main()
