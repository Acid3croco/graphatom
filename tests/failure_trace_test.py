"""La trace stable du dernier échec, commune aux agents et au shell.

Ce test est sans base. Il joue deux commandes de nœud par le vrai bloc ACT :

1. l'adaptateur Codex, devant une fausse CLI déterministe, rend `failed` ;
2. un script shell meurt sans `outcome.json` et devient `crashed` ;
3. les deux échecs se relisent dans le même `failure.json`, avec le nœud,
   l'issue, la queue du journal et le compte rendu bornés ;
4. le second échec remplace le premier ;
5. un item neuf et une réussite ne portent aucune trace d'échec.

Usage : uv run python tests/failure_trace_test.py
"""

import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, kernel  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
CODEX_ADAPTER = ROOT / "scripts" / "agent-codex.sh"
ITEM_ID = 172


class FakeConn:
    """Le bloc ne demande ici que le sujet et l'absence de tentative passée."""

    def execute(self, sql, *args):
        self.sql = sql
        return self

    def fetchone(self) -> dict | None:
        if "FROM subject" in self.sql:
            return {"subject_key": "gh:Acid3croco/graphatom#172"}
        return None


class RouteConn:
    """Le routage écrit ses deux mutations ; leur contenu ne sert pas ici."""

    def execute(self, sql, *args):
        return self


def context(node: str, attempt: int, cmd: str, outcome: str,
            item_id: int = ITEM_ID) -> blocks.Context:
    """Un nœud ACT réel, sans dépôt ni base, avec son workspace de candidat."""
    run = {"id": attempt, "node": node, "cycle": 1, "attempt": attempt,
           "candidate": 4}
    spec = {"block": "ACT", "edges": {outcome: "suite"},
            "config": {"agent": {"cmd": cmd, "prompt": "test déterministe",
                                 "timeout_s": 10, "silence_s": 10}}}
    return blocks.Context(FakeConn(), run, {"id": item_id, "subject_id": 1},
                          spec, {"name": "failure-trace"})


def route(ctx: blocks.Context, outcome: str) -> None:
    """Le vrai point de routage, qui écrit la trace avant de bouger l'item."""
    item = {"id": ctx.item["id"], "state": ctx.run["node"],
            "cycle": 1, "version": 1}
    bundle = {"nodes": {ctx.run["node"]: ctx.node,
                        "suite": {"terminal": True}},
              "on_kernel": {"escalate_to": "suite", "exhausted_to": "suite"}}
    kernel._route(RouteConn(), item, bundle, ctx.run, outcome, kind="result")


def fake_codex(path: Path) -> None:
    """Une CLI Codex qui rend une issue et un long compte rendu, sans réseau."""
    path.write_text("""#!/usr/bin/env bash
LONG=$(printf '%05000d' 0)
printf '{"type":"item.completed","item":{"type":"agent_message","text":"%s fin codex"}}\\n' "$LONG"
printf '%s\\n' '{"outcome":"failed","summary":"échec codex déterministe"}' > outcome.json
{
  printf '# Rapport Codex\\n'
  printf '%05000d\\n' 0
  printf 'FIN RAPPORT CODEX\\n'
} > codex.md
""")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def rendered_failure(workdir: Path) -> tuple[Path, dict]:
    """L'échec rendu par l'adaptateur Codex, puis sa trace écrite par le rail."""
    cli = workdir / "fake-codex"
    fake_codex(cli)
    cmd = (f"CODEX_BIN={shlex.quote(str(cli))} CODEX_DIR=\"$PWD\" "
           f"CODEX_TIMEOUT_S=5 bash {shlex.quote(str(CODEX_ADAPTER))}")
    ctx = context("codex", 1, cmd, "failed")
    result = blocks.act(ctx)
    assert result["outcome"] == "failed", result
    route(ctx, result["outcome"])
    path = blocks.failure_path(ITEM_ID)
    return path, json.loads(path.read_text())


def dead_failure() -> tuple[Path, dict]:
    """Le script shell écrit son rapport, puis meurt sans issue lisible."""
    cmd = "printf '# Rapport shell\\nFIN RAPPORT SHELL\\n' > shell.md; " \
          "printf 'queue shell\\n'; exit 17"
    ctx = context("deploy", 2, cmd, "failed")
    result = blocks.act(ctx)
    assert result["outcome"] == "crashed", result
    route(ctx, result["outcome"])
    path = blocks.failure_path(ITEM_ID)
    return path, json.loads(path.read_text())


def success_writes_nothing() -> None:
    """Un item neuf reste sans trace avant et après une issue de succès."""
    item_id = ITEM_ID + 1
    path = blocks.failure_path(item_id)
    assert not path.exists(), path
    cmd = "printf '{\"outcome\":\"done\",\"summary\":\"ok\"}' > outcome.json"
    ctx = context("release", 1, cmd, "done", item_id=item_id)
    result = blocks.act(ctx)
    assert result["outcome"] == "done", result
    route(ctx, result["outcome"])
    assert not path.exists(), path


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-failure-trace-"))
    blocks.DATA_DIR = workdir
    os.environ.pop("GRAPHATOM_REPO_DIR", None)

    path, codex = rendered_failure(workdir)
    assert set(codex) == {"node", "outcome", "log_tail", "report"}, codex
    assert (codex["node"], codex["outcome"]) == ("codex", "failed"), codex
    assert "fin codex" in codex["log_tail"], codex["log_tail"]
    assert len(codex["log_tail"]) <= blocks.TAIL_CHARS
    assert codex["report"]["name"] == "codex.md", codex["report"]
    assert codex["report"]["content"].endswith("FIN RAPPORT CODEX\n")
    assert len(codex["report"]["content"]) <= blocks.REPORT_CHARS
    print(f"1. agent Codex : échec rendu relu dans {path.name}, quatre champs bornés ✓")

    same_path, shell = dead_failure()
    assert same_path == path
    assert set(shell) == {"node", "outcome", "log_tail", "report"}, shell
    assert (shell["node"], shell["outcome"]) == ("deploy", "crashed"), shell
    assert "queue shell" in shell["log_tail"], shell["log_tail"]
    assert shell["report"]["name"] == "shell.md", shell["report"]
    assert "FIN RAPPORT SHELL" in shell["report"]["content"]
    assert "codex" not in path.read_text().lower(), "le premier échec s'est accumulé"
    print("2. shell sans issue : crashed au même chemin, qui ne porte que ce dernier échec ✓")

    assert blocks.is_failure_outcome("none")
    assert blocks.is_failure_outcome("timed_out")
    assert not blocks.is_failure_outcome("done")
    success_writes_nothing()
    print("3. règle explicite vérifiée ; item neuf et réussite sans trace inventée ✓")


if __name__ == "__main__":
    main()
