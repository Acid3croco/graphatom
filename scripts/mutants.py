#!/usr/bin/env python3
"""Les mutants : chaque petite altération d'un module doit faire tomber une preuve.

    uv run python scripts/mutants.py src/graphatom/validation.py tests/validate_test.py
    uv run python scripts/mutants.py src/graphatom/routing.py tests/train_test.py --workers 3
    uv run python scripts/mutants.py src/graphatom/bundle.py --list

Les outils du marché (mutmut, cosmic-ray) mutent en mémoire sous pytest ;
les preuves d'ici sont des scripts qui lancent la CLI et des agents en
sous-processus, et rien ne leur passerait un mutant. Ce runner fait la
seule chose honnête : il écrit chaque mutant dans une copie du dépôt, joue
les preuves nommées, et classe. Les opérateurs sont explicites, et rien
d'autre n'est muté :

    ==  ↔  !=      <  ↔  <=      >  ↔  >=      in ↔ not in      is ↔ is not
    and ↔ or       not x → x     True ↔ False    n → n + 1 (entiers)
    +  ↔  -        += ↔ -=       chaque maillon d'une comparaison chaînée

Quatre verdicts, jamais confondus :

    killed      une preuve tombe — le mutant est vu
    survived    toutes passent — une preuve manque, ou le mutant est équivalent
    timeout     les preuves dépassent trois fois leur durée : à examiner, pas
                compté tué — une boucle infinie est détectée, une lenteur non
    equivalent  inscrit, avec sa raison, dans scripts/mutants_equivalents.txt

Un mutant est nommé par son module, sa ligne, sa colonne et son opérateur :
deux mutants d'une même ligne ne se confondent pas. Une erreur du runner
lui-même — un worker qui lève — arrête la campagne : un mutant sans
verdict n'est pas un mutant tué.

Ce qui n'est pas muté, et pourquoi, tient en une liste courte : les
arguments nommés de pure forme (`sort_keys`, `indent`, `exist_ok`…), dont
le retournement ne change aucune garantie. Tout le reste l'est — réglages
de module et valeurs par défaut compris — et ce qui survit se prouve, ou
s'inscrit avec sa raison.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EQUIVALENTS = ROOT / "scripts" / "mutants_equivalents.txt"
# `.git` voyage avec la copie : un module qui lit sa propre tête git — le
# worker qui se déploie — doit la trouver, sinon ses mutants restent invisibles
EXCLUDED_DIRS = {".venv", "node_modules", ".next", "data", "__pycache__", ".worktrees",
                 ".coverage-rules"}
# les arguments nommés de pure forme : retournés, ils changent la mise en
# page d'un texte ou l'idempotence d'un mkdir, jamais une garantie du rail
COSMETIC_KEYWORDS = {"sort_keys", "ensure_ascii", "indent", "separators", "flush",
                     "errors", "exist_ok", "parents", "missing_ok", "frozen"}
TIMEOUT_FACTOR = 3
TIMEOUT_FLOOR_S = 30.0

COMPARE = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt,
           ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In,
           ast.Is: ast.IsNot, ast.IsNot: ast.Is}
BOOL = {ast.And: ast.Or, ast.Or: ast.And}
ARITH = {ast.Add: ast.Sub, ast.Sub: ast.Add}


class Mutant:
    def __init__(self, line: int, col: int, operator: str, tree: ast.Module):
        self.line, self.col, self.operator, self.tree = line, col, operator, tree

    @property
    def name(self) -> str:
        return f"{self.line}:{self.col}:{self.operator}"


# ------------------------------------------------------------------ opérateurs


def _cosmetic(tree: ast.Module) -> set[int]:
    """Les nœuds sous un argument nommé de pure forme : non mutés."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in COSMETIC_KEYWORDS:
                    skip.update(id(child) for child in ast.walk(keyword.value))
    return skip


def _sites(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """Chaque nœud mutable et l'opérateur qui s'y applique, dans l'ordre du source."""
    cosmetic = _cosmetic(tree)
    found = []
    for node in ast.walk(tree):
        if id(node) in cosmetic or not hasattr(node, "lineno"):
            continue
        if isinstance(node, ast.Compare):
            found.extend((node, f"compare:{i}:{type(op).__name__}")
                         for i, op in enumerate(node.ops) if type(op) in COMPARE)
        elif isinstance(node, ast.BoolOp):
            found.append((node, f"boolop:{type(node.op).__name__}"))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            found.append((node, "not"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            found.append((node, "bool"))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            found.append((node, "int"))
        elif isinstance(node, ast.BinOp) and type(node.op) in ARITH:
            found.append((node, f"binop:{type(node.op).__name__}"))
        elif isinstance(node, ast.AugAssign) and type(node.op) in ARITH:
            found.append((node, f"augassign:{type(node.op).__name__}"))
    return found


def _mutate(tree: ast.Module, node: ast.AST, operator: str) -> None:
    """Applique l'opérateur au nœud, en place dans l'arbre."""
    kind = operator.split(":")[0]
    if kind == "compare":
        index = int(operator.split(":")[1])
        node.ops[index] = COMPARE[type(node.ops[index])]()
    elif kind == "boolop":
        node.op = BOOL[type(node.op)]()
    elif kind == "not":
        _replace(tree, node, node.operand)
    elif kind == "bool":
        node.value = not node.value
    elif kind == "int":
        node.value = node.value + 1
    else:  # binop, augassign
        node.op = ARITH[type(node.op)]()


def _replace(tree: ast.Module, old: ast.AST, new: ast.AST) -> None:
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            if value is old:
                setattr(parent, field, new)
                return
            if isinstance(value, list):
                for i, item in enumerate(value):
                    if item is old:
                        value[i] = new
                        return


def mutants(source: str) -> list[Mutant]:
    """Tous les mutants du module, un arbre chacun."""
    base = ast.parse(source)
    found = []
    for index, (site, operator) in enumerate(_sites(base)):
        tree = copy.deepcopy(base)
        node = _sites(tree)[index][0]
        _mutate(tree, node, operator)
        found.append(Mutant(site.lineno, site.col_offset, operator, tree))
    return found


# --------------------------------------------------------------- équivalents


def equivalents() -> dict[str, str]:
    """`module:ligne:colonne:opérateur  # raison` — la raison est obligatoire."""
    known: dict[str, str] = {}
    if not EQUIVALENTS.exists():
        return known
    for raw in EQUIVALENTS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, reason = line.partition("#")
        if not reason.strip():
            raise SystemExit(f"équivalent sans raison : {raw}")
        known[key.strip()] = reason.strip()
    return known


# --------------------------------------------------------------------- course


def _copy_repo(destination: Path) -> None:
    """Une copie du dépôt sans son venv : chaque worker a la sienne."""
    shutil.copytree(ROOT, destination, ignore=lambda _d, names: [
        n for n in names if n in EXCLUDED_DIRS])


def _run_tests(repo: Path, tests: list[str], timeout: float) -> str:
    """`killed` dès qu'une preuve tombe, `timeout` si elle s'éternise, sinon `survived`.

    Chaque preuve part dans sa propre session : au dépassement, le groupe
    entier est tué, descendants compris, avant que le worker ne réutilise
    sa copie du dépôt.
    """
    for test in tests:
        proc = subprocess.Popen(["uv", "run", "python", test], cwd=repo,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return "timeout"
        if code:
            return "killed"
    return "survived"


def _baseline(repo: Path, tests: list[str]) -> float:
    start = time.monotonic()
    if _run_tests(repo, tests, timeout=1800) != "survived":
        raise SystemExit("les preuves tombent déjà sans mutant : rien à mesurer")
    return time.monotonic() - start


def _judge(repo: Path, module: Path, mutant: Mutant, tests: list[str],
           timeout: float) -> str:
    target = repo / module
    original = target.read_text()
    target.write_text(ast.unparse(mutant.tree) + "\n")
    try:
        return _run_tests(repo, tests, timeout)
    finally:
        target.write_text(original)


MARKS = {"killed": "✓", "survived": "✗", "timeout": "⏱", "equivalent": "="}


def campaign(module: Path, tests: list[str], workers: int) -> dict:
    source = (ROOT / module).read_text()
    found = mutants(source)
    known = equivalents()
    scratch = Path(tempfile.mkdtemp(prefix="graphatom-mutants-"))
    repos = [scratch / f"w{i}" for i in range(workers)]
    for repo in repos:
        _copy_repo(repo)
    baseline = _baseline(repos[0], tests)
    timeout = max(TIMEOUT_FLOOR_S, baseline * TIMEOUT_FACTOR)
    print(f"{module} : {len(found)} mutants, preuves en {baseline:.0f} s, "
          f"{workers} worker(s)", flush=True)
    lines = source.splitlines()
    verdicts: dict[str, str] = {}

    def work(repo: Path, jobs: list[Mutant]) -> None:
        for mutant in jobs:
            key = f"{module.stem}:{mutant.name}"
            verdict = "equivalent" if key in known else _judge(repo, module, mutant,
                                                                 tests, timeout)
            verdicts[mutant.name] = verdict
            print(f"  {MARKS[verdict]} {mutant.name:<30} "
                  f"{lines[mutant.line - 1].strip()[:66]}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, repos[i], found[i::workers]) for i in range(workers)]
        for future in futures:
            future.result()  # une erreur du runner remonte : pas de verdict inventé
    shutil.rmtree(scratch, ignore_errors=True)
    missing = [m.name for m in found if m.name not in verdicts]
    if missing:
        raise SystemExit(f"mutants sans verdict : {missing}")
    return {"module": str(module), "tests": tests, "baseline_s": baseline,
            "mutants": len(found), "verdicts": verdicts,
            "survived": [n for n, v in verdicts.items() if v == "survived"],
            "timeout": [n for n, v in verdicts.items() if v == "timeout"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="mutants d'un module, jugés par des preuves")
    parser.add_argument("module", type=Path)
    parser.add_argument("tests", nargs="*")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--list", action="store_true", help="compter sans jouer")
    parser.add_argument("--json", type=Path, help="où écrire le rapport")
    args = parser.parse_args()
    if args.list:
        found = mutants((ROOT / args.module).read_text())
        print(f"{args.module} : {len(found)} mutants")
        for mutant in found:
            print(f"  {mutant.name}")
        return
    if not args.tests:
        parser.error("au moins une preuve à jouer")
    report = campaign(args.module, args.tests, args.workers)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n{args.module} : {report['mutants']} mutants, "
          f"{len(report['survived'])} survivant(s), {len(report['timeout'])} en dépassement",
          flush=True)
    if report["survived"] or report["timeout"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
