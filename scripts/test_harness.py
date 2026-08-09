#!/usr/bin/env python3
"""Harnais fermé des nœuds de test backend et frontend.

Le modèle ne choisit ni les commandes, ni leurs processus, ni le verdict.
Le diff sélectionne une liste versionnée ; chaque commande rend une preuve
structurée, et le premier échec ferme le nœud proprement.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


TOTAL_S = float(os.environ.get("GRAPHATOM_TEST_HARNESS_S", "85"))
TAIL_CHARS = 4000
BACKEND_PREFIXES = ("src/", "schema.sql", "tests/", "examples/", "scripts/")
FRONT_PREFIXES = (
    "front/", "src/graphatom/web.py", "tests/front", "scripts/front",
    "scripts/test_harness.py", "tests/test_harness_test.py",
)


def _git(repo: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        check=False, timeout=10,
    )
    return result.stdout.splitlines() if result.returncode == 0 else []


def changed(repo: Path) -> list[str]:
    """Rend le diff complet une fois, sans doublon et dans un ordre stable."""
    paths = (
        _git(repo, "diff", "--name-only", "origin/main...HEAD")
        + _git(repo, "diff", "--name-only", "HEAD")
        + _git(repo, "ls-files", "--others", "--exclude-standard")
    )
    return list(dict.fromkeys(path for path in paths if path))


def backend_gates(paths: list[str]) -> list[tuple[str, list[str]]]:
    """Sélection mécanique, ordonnée et bornée des tests backend."""
    if not any(path.startswith(BACKEND_PREFIXES) for path in paths):
        return []
    gates = [("import", [
        "uv", "run", "python", "-c",
        "import graphatom.blocks, graphatom.kernel, graphatom.scheduler, graphatom.web",
    ])]
    names: list[str] = []

    def add(*tests: str) -> None:
        for test in tests:
            if test not in names:
                names.append(test)

    for path in paths:
        if path == "scripts/test_harness.py" or path == "tests/test_harness_test.py":
            add("tests/test_harness_test.py")
        if path.startswith("src/graphatom/blocks.py"):
            add("tests/passation_test.py", "tests/orphans_test.py")
        if path.startswith(("src/graphatom/scheduler.py", "src/graphatom/kernel.py",
                            "src/graphatom/db.py", "schema.sql")):
            add("tests/crash_test.py", "tests/reconnect_test.py")
        if path.startswith("src/graphatom/web.py"):
            add("tests/api_test.py")
        if path == "examples/code-task.json":
            add("tests/validate_test.py", "tests/checklist_test.py",
                "tests/shell_test.py")
        if (path.startswith("tests/")
                and (path.endswith("_test.py") or path.endswith("_test.sh"))):
            add(path)
    return gates + [
        (Path(test).name, (["bash", test] if test.endswith(".sh")
                           else ["uv", "run", "python", test]))
        for test in names
    ]


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ready(url: str, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    return False


def _descendants(pid: int) -> list[int]:
    """Rend les descendants actuels, les plus profonds en premier."""
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True,
        check=False, timeout=3,
    )
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        try:
            child, parent = (int(value) for value in line.split())
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)
    found = []

    def walk(parent: int) -> None:
        for child in children.get(parent, []):
            walk(child)
            found.append(child)

    walk(pid)
    return found


def _signal_tree(process: subprocess.Popen, sig: signal.Signals) -> list[int]:
    descendants = _descendants(process.pid)
    try:
        if os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, sig)
            return descendants
    except ProcessLookupError:
        return descendants
    for pid in descendants:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
    try:
        process.send_signal(sig)
    except ProcessLookupError:
        pass
    return descendants


def _stop(processes: list[subprocess.Popen]) -> None:
    descendants = []
    groups = []
    for process in reversed(processes):
        try:
            os.killpg(process.pid, signal.SIGTERM)
            groups.append(process.pid)
        except ProcessLookupError:
            pass
        if process.poll() is None and process.pid not in groups:
            descendants.extend(_signal_tree(process, signal.SIGTERM))
    deadline = time.monotonic() + 3
    for process in reversed(processes):
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _signal_tree(process, signal.SIGKILL)
            process.wait()
    alive = descendants
    active_groups = groups
    while time.monotonic() < deadline:
        alive = [pid for pid in descendants if Path(f"/proc/{pid}").exists()]
        active_groups = []
        for pgid in groups:
            try:
                os.killpg(pgid, 0)
                active_groups.append(pgid)
            except ProcessLookupError:
                pass
        if not alive and not active_groups:
            return
        time.sleep(0.05)
    for pgid in active_groups:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def front_gate(repo: Path, workspace: Path, deadline: float) -> tuple[int, str]:
    """Possède API et Next, attend leurs ports, capture, puis les arrête."""
    processes: list[subprocess.Popen] = []
    api_log = (workspace / "frontend-api.log").open("w")
    next_log = (workspace / "frontend-next.log").open("w")
    try:
        for command in (["uv", "run", "graphatom", "init-db", "--drop"],
                        ["uv", "run", "python", "tests/seed.py"],
                        [str(repo / "scripts" / "front-env.sh")]):
            left = max(1, int(deadline - time.monotonic()))
            result = subprocess.run(
                command, cwd=repo, capture_output=True, text=True,
                check=False, timeout=left,
            )
            if result.returncode:
                return result.returncode, (result.stdout + result.stderr)[-TAIL_CHARS:]

        api_port, front_port = _port(), _port()
        api = subprocess.Popen(
            ["uv", "run", "graphatom", "serve", "--port", str(api_port)],
            cwd=repo, stdout=api_log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(api)
        if not _ready(f"http://127.0.0.1:{api_port}/api/items",
                      min(deadline, time.monotonic() + 12)):
            return 1, "api readiness failed (HTTP 000)"

        env = os.environ | {
            "GRAPHATOM_API_URL": f"http://127.0.0.1:{api_port}",
            "PORT": str(front_port),
        }
        front = subprocess.Popen(
            ["npm", "run", "start"], cwd=repo / "front", env=env,
            stdout=next_log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(front)
        if not _ready(f"http://127.0.0.1:{front_port}/items",
                      min(deadline, time.monotonic() + 15)):
            return 1, "next readiness failed (HTTP 000)"

        evidence = workspace / "frontend-evidence"
        evidence.mkdir(exist_ok=True)
        for name, route in (("home", "/"), ("items", "/items"),
                            ("item", "/item/1")):
            url = f"http://127.0.0.1:{front_port}{route}"
            if not _ready(url, min(deadline, time.monotonic() + 5)):
                return 1, f"route {route}: HTTP 000"
            result = subprocess.run(
                ["chromium", "--headless=new", "--no-sandbox",
                 "--disable-gpu", "--dump-dom", url],
                capture_output=True, text=True, check=False,
                timeout=max(1, deadline - time.monotonic()),
            )
            (evidence / f"{name}.dom.html").write_text(result.stdout)
            if result.returncode:
                return result.returncode, f"route {route}: chromium code {result.returncode}"
            if route == "/item/1" and "<svg" not in result.stdout:
                return 1, "route /item/1: SVG absent"
            screenshot = evidence / f"{name}.png"
            capture = subprocess.run(
                ["chromium", "--headless=new", "--no-sandbox",
                 "--disable-gpu", "--window-size=1440,900",
                 f"--screenshot={screenshot}", url],
                capture_output=True, text=True, check=False,
                timeout=max(1, deadline - time.monotonic()),
            )
            if capture.returncode or not screenshot.is_file() or not screenshot.stat().st_size:
                return capture.returncode or 1, (
                    f"route {route}: screenshot absent "
                    f"(chromium code {capture.returncode})"
                )
        return 0, ("API et Next prêts; routes /, /items et /item/1 "
                   "capturées en DOM et PNG")
    except subprocess.TimeoutExpired:
        return 124, "frontend harness timeout"
    finally:
        _stop(processes)
        api_log.close()
        next_log.close()


def _write(workspace: Path, mode: str, paths: list[str], evidence: list[dict],
           outcome: str, summary: str, started: float) -> None:
    duration = round(time.monotonic() - started, 3)
    document = {
        "version": 1, "mode": mode, "diff": paths,
        "duration_s": duration, "commands": evidence,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    (workspace / "test-evidence.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )
    report = workspace / f"test_{mode}.md"
    lines = [f"# Harnais {mode}", "", f"Durée totale : {duration} s", ""]
    for item in evidence:
        lines.append(
            f"- `{item['name']}` — code {item['exit_code']}, {item['duration_s']} s, "
            f"preuve `{item['output_sha256'][:12]}`"
        )
    lines.extend(("", summary))
    report.write_text("\n".join(lines) + "\n")
    (workspace / "usage.json").write_text(json.dumps(document["usage"]) + "\n")
    (workspace / "outcome.json").write_text(json.dumps({
        "outcome": outcome, "summary": summary,
    }, ensure_ascii=False) + "\n")


def execute_gates(repo: Path, workspace: Path, mode: str,
                  gates: list[tuple[str, list[str]]], deadline: float
                  ) -> tuple[list[dict], tuple[str, int] | None]:
    """Exécute le plan en série et s'arrête au premier échec."""
    evidence = []
    for name, command in gates:
        gate_started = time.monotonic()
        if gate_started >= deadline:
            code, output = 124, "budget total dépassé"
        elif mode == "frontend":
            code, output = front_gate(repo, workspace, deadline)
        else:
            try:
                result = subprocess.run(
                    command, cwd=repo, capture_output=True, text=True,
                    check=False, timeout=max(1, deadline - gate_started),
                )
                code, output = result.returncode, result.stdout + result.stderr
            except subprocess.TimeoutExpired as exc:
                code = 124
                output = exc.stdout if isinstance(exc.stdout, str) else "timeout"
        evidence.append({
            "name": name, "argv": command, "exit_code": code,
            "duration_s": round(time.monotonic() - gate_started, 3),
            "output_tail": output[-TAIL_CHARS:],
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        })
        if code:
            return evidence, (name, code)
    return evidence, None


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("backend", "frontend"):
        print("usage: test_harness.py backend|frontend", file=sys.stderr)
        return 2
    mode = sys.argv[1]
    repo = Path(os.environ.get("GRAPHATOM_WORKTREE", "")).resolve()
    workspace = Path(os.environ.get("GRAPHATOM_WORKSPACE", "")).resolve()
    if not repo.is_dir() or not workspace.is_dir():
        print("GRAPHATOM_WORKTREE et GRAPHATOM_WORKSPACE requis", file=sys.stderr)
        return 2
    started = time.monotonic()
    deadline = started + TOTAL_S
    paths = changed(repo)
    prefixes = BACKEND_PREFIXES if mode == "backend" else FRONT_PREFIXES
    if not paths:
        _write(workspace, mode, paths, [], "fail", "diff vide", started)
        return 0
    if not any(path.startswith(prefixes) for path in paths):
        _write(workspace, mode, paths, [], "pass", f"diff {mode} non concerné", started)
        return 0

    gates = backend_gates(paths) if mode == "backend" else [("frontend", [])]
    evidence, failure = execute_gates(repo, workspace, mode, gates, deadline)
    if failure:
        name, code = failure
        _write(workspace, mode, paths, evidence, "fail",
               f"{name} a échoué avec le code {code}", started)
        return 0
    _write(workspace, mode, paths, evidence, "pass",
           f"{len(evidence)} porte(s) déterministe(s) passée(s)", started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
