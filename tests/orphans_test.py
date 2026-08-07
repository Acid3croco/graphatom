"""Le test des orphelins : le bail révoque aussi les descendants de l'agent.

Scénario, sans base ni ordonnanceur — seul le bloc agent est en jeu :

  1. un agent factice qui laisse un sous-processus derrière lui est fauché
     au timeout, et la tentative est classée crashed avec son autopsie
  2. plus aucun processus vivant de son groupe après la tentative
  3. le journal `agent-<tentative>.log` est écrit comme d'habitude
  4. un agent qui répond normalement passe toujours
  5. un agent qui sort en erreur sans `outcome.json` rend son autopsie :
     code de sortie, queue de log, et pas de flag timeout

Usage : uv run python tests/orphans_test.py
"""

import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks  # noqa: E402

# le shell note le pgid de son groupe, puis laisse un sous-processus derrière lui
FACTICE = "ps -o pgid= -p $$ > pgid.txt; sleep 300 & sleep 300"
REPOND = """printf '{"outcome": "ok", "summary": "fait"}' > outcome.json"""
ECHOUE = "echo 'Execution error' >&2; exit 42"  # sort mal, sans outcome.json
TIMEOUT_S = 2


class FakeConn:
    """Le bloc n'interroge la base que pour la clé du sujet."""

    def execute(self, *args):
        return self

    def fetchone(self) -> dict:
        return {"subject_key": "orphelins:test"}


def context(workdir: Path, cmd: str, attempt: int) -> blocks.Context:
    blocks.DATA_DIR = workdir
    node = {"block": "ACT", "edges": {"ok": "done"},
            "config": {"agent": {"cmd": cmd, "prompt": "ne fais rien",
                                 "timeout_s": TIMEOUT_S}}}
    return blocks.Context(
        FakeConn(),
        {"id": 1, "node": "travail", "attempt": attempt},
        {"id": 7, "subject_id": 1},
        node,
        {"name": "orphelins"},
    )


def group_alive(pgid: int) -> list[int]:
    """Les pids vivants du groupe — zombies exclus, ils ne travaillent plus."""
    alive = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:  # une course avec /proc n'est pas un échec du test
            fields = stat.read_text().rsplit(") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) == pgid and fields[0] != "Z":
            alive.append(int(stat.parent.name))
    return alive


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-orphans-"))

    # 1. l'agent factice est fauché au timeout, et rend son autopsie
    ctx = context(workdir, FACTICE, attempt=1)
    started = time.time()
    result = blocks.act(ctx)
    assert result["outcome"] == "crashed", result
    assert result["timeout"] is True, result
    # négatif = signal : SIGTERM s'il part à la grâce, SIGKILL sinon
    assert result["exit_code"] in (-signal.SIGTERM, -signal.SIGKILL), result
    assert "TimeoutExpired" in result["error"], result
    print(f"1. agent fauché au timeout après {time.time() - started:.1f}s, "
          f"autopsie {result['exit_code']} ✓")

    # 2. le groupe entier est révoqué, descendants compris
    pgid = int((ctx.workspace / "pgid.txt").read_text())
    deadline = time.time() + 2  # laisser init récolter les tués
    while time.time() < deadline and group_alive(pgid):
        time.sleep(0.1)
    survivants = group_alive(pgid)
    if survivants:
        for pid in survivants:
            os.kill(pid, 9)
        sys.exit(f"ÉCHEC : survivants du groupe {pgid} : {survivants}")
    print(f"2. groupe {pgid} entièrement révoqué ✓")

    # 3. le journal de la tentative reste écrit
    log = ctx.workspace / "agent-1.log"
    assert log.exists(), log
    print(f"3. journal {log.name} écrit ✓")

    # 4. non-régression : un agent qui répond passe toujours
    result = blocks.act(context(workdir, REPOND, attempt=2))
    assert result == {"outcome": "ok", "summary": "fait"}, result
    print(f"4. agent nominal appliqué : {result} ✓")

    # 5. l'agent qui sort en erreur rend son autopsie, sans flag timeout
    result = blocks.act(context(workdir, ECHOUE, attempt=3))
    assert result["outcome"] == "crashed", result
    assert result["exit_code"] == 42, result
    assert result["timeout"] is False, result
    assert "Execution error" in result["log_tail"], result
    print(f"5. autopsie du crash : sortie {result['exit_code']}, "
          f"queue « {result['log_tail'].strip()} » ✓")

    shutil.rmtree(workdir, ignore_errors=True)
    print("\norphelins : OK — un descendant ne survit pas au bail de l'agent")


if __name__ == "__main__":
    main()
