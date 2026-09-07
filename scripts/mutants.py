#!/usr/bin/env python3
"""Les mutants : chaque petite altération d'un module doit faire tomber une preuve.

    uv run python scripts/mutants.py src/graphatom/validation.py tests/validate_test.py
    uv run python scripts/mutants.py src/graphatom/routing.py tests/train_test.py --workers 3
    uv run python scripts/mutants.py src/graphatom/bundle.py --list

Les outils du marché (mutmut, cosmic-ray) mutent en mémoire sous pytest ;
les preuves d'ici sont des scripts qui lancent la CLI et des agents en
sous-processus, et rien ne leur passerait un mutant. Ce runner fait la
seule chose honnête : il écrit chaque mutant dans une copie du dépôt, joue
les preuves nommées, et classe — tué quand une preuve tombe, survivant
quand toutes passent. Les opérateurs sont explicites, et rien d'autre
n'est muté :

    ==  ↔  !=      <  ↔  <=      >  ↔  >=      in ↔ not in      is ↔ is not
    and ↔ or       not x → x     True ↔ False    n → n + 1 (entiers)
    +  ↔  -        += ↔ -=

Les réglages ne sont pas mutés — arguments nommés d'un appel, décorateurs,
valeurs par défaut, constantes de module en majuscules : muter `TAIL_LINES
= 20` ne mesure qu'une preuve tautologique, et le contrat que porte une
telle constante est muté là où il s'applique, dans sa comparaison.

Un mutant survivant est nommé par sa ligne et son opérateur : c'est la
preuve qui manque, ou un mutant équivalent à documenter dans
`scripts/mutants_equivalents.txt` (un par ligne : `<module>:<ligne>:<opérateur>`).
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EQUIVALENTS = ROOT / "scripts" / "mutants_equivalents.txt"
EXCLUDED = {".venv", "node_modules", ".next", "data", "__pycache__", ".worktrees",
            ".coverage-rules", ".git"}

COMPARE = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt,
           ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In,
           ast.Is: ast.IsNot, ast.IsNot: ast.Is}
BOOL = {ast.And: ast.Or, ast.Or: ast.And}
ARITH = {ast.Add: ast.Sub, ast.Sub: ast.Add}


class Mutant:
    def __init__(self, line: int, operator: str, tree: ast.Module):
        self.line, self.operator, self.tree = line, operator, tree

    @property
    def name(self) -> str:
        return f"{self.line}:{self.operator}"


def _settings(tree: ast.Module) -> set[int]:
    """Les constantes qui sont des réglages, pas de la logique : non mutées.

    Un argument nommé d'un appel (`sort_keys=True`), un décorateur, la
    valeur par défaut d'un paramètre, la constante d'un réglage de module en
    majuscules (`TAIL_LINES = 20`) : les muter produit des mutants
    équivalents ou des preuves tautologiques. Les contrats qu'ils portent —
    « huit candidats au plus », « trois tentatives » — restent mutés là où
    ils se décident : dans la comparaison qui les applique.
    """
    skip: set[int] = set()

    def mark(node: ast.AST) -> None:
        skip.update(id(child) for child in ast.walk(node))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                mark(keyword.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                mark(decorator)
            if not isinstance(node, ast.ClassDef):
                for default in node.args.defaults + node.args.kw_defaults:
                    if default is not None:
                        mark(default)
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id.isupper() for t in statement.targets):
            mark(statement.value)
    return skip


def _sites(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """Chaque nœud mutable et l'opérateur qui s'y applique, dans l'ordre du source."""
    settings = _settings(tree)
    found = []
    for node in ast.walk(tree):
        if id(node) in settings:
            continue
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in COMPARE:
            found.append((node, f"compare:{type(node.ops[0]).__name__}"))
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
    return [(node, op) for node, op in found if hasattr(node, "lineno")]


def _mutate(node: ast.AST, operator: str) -> None:
    """Applique l'opérateur au nœud, en place."""
    if operator.startswith("compare:"):
        node.ops = [COMPARE[type(node.ops[0])]()]
    elif operator.startswith("boolop:"):
        node.op = BOOL[type(node.op)]()
    elif operator == "bool":
        node.value = not node.value
    elif operator == "int":
        node.value = node.value + 1
    elif operator.startswith("binop:") or operator.startswith("augassign:"):
        node.op = ARITH[type(node.op)]()


def mutants(source: str) -> list[Mutant]:
    """Tous les mutants du module, un arbre chacun — `not x → x` compris."""
    base = ast.parse(source)
    found = []
    for index, (site, operator) in enumerate(_sites(base)):
        tree = copy.deepcopy(base)
        node = _sites(tree)[index][0]
        if operator == "not":
            _replace(tree, node, node.operand)
        else:
            _mutate(node, operator)
        found.append(Mutant(site.lineno, operator, tree))
    return found


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


# --------------------------------------------------------------------- course


def _copy_repo(destination: Path) -> None:
    """Une copie du dépôt sans son venv : chaque worker a la sienne."""
    shutil.copytree(ROOT, destination, ignore=lambda _d, names: [
        n for n in names if n in EXCLUDED])


def _run_tests(repo: Path, tests: list[str], timeout: float) -> bool:
    """Vrai si une preuve tombe (mutant tué), faux si toutes passent."""
    for test in tests:
        try:
            done = subprocess.run(["uv", "run", "python", test], cwd=repo,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return True  # une boucle qui ne finit plus est un mutant tué
        if done.returncode:
            return True
    return False


def _baseline(repo: Path, tests: list[str]) -> float:
    start = time.monotonic()
    if _run_tests(repo, tests, timeout=1800):
        raise SystemExit("les preuves tombent déjà sans mutant : rien à mesurer")
    return time.monotonic() - start


def _judge(repo: Path, module: Path, mutant: Mutant, tests: list[str],
           timeout: float) -> str:
    target = repo / module
    original = target.read_text()
    target.write_text(ast.unparse(mutant.tree) + "\n")
    try:
        return "killed" if _run_tests(repo, tests, timeout) else "survived"
    finally:
        target.write_text(original)


def campaign(module: Path, tests: list[str], workers: int) -> dict:
    source = (ROOT / module).read_text()
    found = mutants(source)
    known = set(EQUIVALENTS.read_text().split()) if EQUIVALENTS.exists() else set()
    scratch = Path(tempfile.mkdtemp(prefix="graphatom-mutants-"))
    repos = [scratch / f"w{i}" for i in range(workers)]
    for repo in repos:
        _copy_repo(repo)
    baseline = _baseline(repos[0], tests)
    timeout = max(30.0, baseline * 3)
    print(f"{module} : {len(found)} mutants, preuves en {baseline:.0f} s, "
          f"{workers} worker(s)", flush=True)

    verdicts: dict[str, str] = {}
    lines = source.splitlines()

    def work(job: tuple[int, Mutant]) -> None:
        index, mutant = job
        repo = repos[index % workers]
        key = f"{module.stem}:{mutant.name}"
        verdict = "equivalent" if key in known else _judge(repo, module, mutant, tests, timeout)
        verdicts[mutant.name] = verdict
        mark = {"killed": "✓", "survived": "✗", "equivalent": "="}[verdict]
        print(f"  {mark} {mutant.name:<24} {lines[mutant.line - 1].strip()[:70]}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # un worker ne joue qu'un mutant à la fois : les jobs sont rangés par worker
        for offset in range(workers):
            pool.submit(lambda o=offset: [work(job) for job in
                                          list(enumerate(found))[o::workers]])
    shutil.rmtree(scratch, ignore_errors=True)
    return {"module": str(module), "tests": tests, "baseline_s": baseline,
            "mutants": len(found), "verdicts": verdicts,
            "survived": [name for name, v in verdicts.items() if v == "survived"]}


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
    survived = report["survived"]
    print(f"\n{args.module} : {report['mutants']} mutants, "
          f"{len(survived)} survivant(s)", flush=True)
    if survived:
        sys.exit(1)


if __name__ == "__main__":
    main()
