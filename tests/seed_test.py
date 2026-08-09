"""Le seed attend que la voie soit libre avant sa seconde admission.

Usage : uv run python tests/seed_test.py
"""

import json
from contextlib import contextmanager

import seed


class Result:
    def __init__(self, row: dict | None):
        self.row = row

    def fetchone(self) -> dict | None:
        return self.row


def main() -> None:
    calls = []
    state = {"terminal": False, "question": False}

    def sh(*args: str) -> str:
        if args[0] == "publish":
            return "revision-question" if len(args) > 1 and "raise" in args[1] \
                else "revision-nominale"
        if args[0] == "admit":
            if "question" in args[2]:
                assert state["terminal"], "seconde admission avant la fin du nominal"
                state["question"] = True
                calls.append("admit-question")
                return "2"
            calls.append("admit-nominal")
            return "1"
        return ""

    class Conn:
        def execute(self, query: str, params: tuple) -> Result:
            if "terminal_at" in query:
                return Result({"terminal_at": state["terminal"] or None})
            if "state, version" in query:
                return Result({"state": "close", "version": 9})
            if "id FROM question" in query:
                return Result({"id": 7} if state["question"] else None)
            return Result({"id": 7, "node": "escalate", "text": "Continuer ?"})

    @contextmanager
    def connect():
        yield Conn()

    def scheduler():
        calls.append("scheduler")
        state["terminal"] = True
        return object()

    seed.sh = sh
    seed.db.connect = connect
    seed.scheduler = scheduler
    seed.kill_group = lambda proc, sig: None
    seed.main()

    assert calls == ["admit-nominal", "scheduler", "admit-question"], calls
    bundle = json.loads((seed.ROOT / "examples" / "code-task.json").read_text())
    prompt = bundle["nodes"]["test_frontend"]["config"]["agent"]["prompt"]
    assert prompt.index("drop-agent-db") < prompt.index("tests/seed.py"), prompt
    print("seed : base préparée, nominal terminé, puis question admise ✓")


if __name__ == "__main__":
    main()
