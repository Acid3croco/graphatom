"""Le test du nœud `validate` : les critères cochés, puis cités à l'humain.

Scénario, sans base ni réseau — le graph est une donnée, GitHub une doublure :

  1. le graph route les tests vers `validate`, et `validate` vers la review
     ou le retour en `implement` ; le cycle qu'il referme est borné par
     `escalade`, donc par le budget d'escalades de l'item
  2. sans `validate.md`, la question publiée est celle d'avant, mot pour mot
  3. avec `validate.md`, la question l'embarque cité, et donne le lien de
     la preview du fichier entier
  4. un `validate.md` long est borné : les premières lignes, et le compte
     de ce qui reste

Usage : uv run python tests/checklist_test.py
"""

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks  # noqa: E402
from graphatom import github_sync as gs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ITEM_ID = 14
CHECKLIST = ("# Validation — gh:o/r#60\n"
             "\n"
             "- [x] 1. Le nœud validate existe — `jq .nodes.validate examples/code-task.json`\n"
             "- [x] 2. La question cite la checklist — tests/checklist_test.py au vert\n")


class FakeCursor:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class FakeConn:
    """La base : une question ouverte, et des effets jamais encore appliqués."""

    def __init__(self, question: dict):
        self.question = question

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple = ()):
        if "FROM question q JOIN work_item" in sql:
            return FakeCursor([self.question])
        if "SELECT * FROM effect" in sql:
            return FakeCursor([{"observation": None}])
        return FakeCursor([])


class FakeGitHub:
    repo = "o/r"

    def __init__(self):
        self.posted: list[tuple[int, str]] = []

    def comments(self, number: int) -> list[dict]:
        return [{"body": body} for n, body in self.posted if n == number]

    def post_comment(self, number: int, body: str) -> None:
        self.posted.append((number, body))


def question_body() -> str:
    """Le commentaire que le canal publie pour la review de l'item."""
    question = {
        "id": 5, "item_id": ITEM_ID, "subject_key": "gh:o/r#60",
        "node": "review", "owner": "Acid3croco", "text": "On garde ?",
        "options": ["merger", "abandonner"],
        "deadline": dt.datetime(2026, 8, 8, 9, 30),
    }
    gh = FakeGitHub()
    gs._publish_questions(FakeConn(question), gh)
    assert len(gh.posted) == 1, gh.posted
    return gh.posted[0][1]


def main() -> None:
    # 1. le graph : les tests passent par validate, qui coche puis route
    bundle = json.loads((ROOT / "examples" / "code-task.json").read_text())
    nodes = bundle["nodes"]
    assert nodes["test_frontend"]["edges"]["pass"] == "validate"
    assert nodes["validate"]["block"] == "JUDGE", nodes["validate"]["block"]
    assert nodes["validate"]["edges"] == {"pass": "review", "fail": "implement"}
    # l'arête `fail` referme un cycle : sans `escalade`, rien ne le bornerait
    assert nodes["validate"].get("escalade") is True
    assert bundle["budgets"]["escalations"] >= 5, bundle["budgets"]
    print("1. test_frontend → validate → review / implement, cycle borné ✓")

    with tempfile.TemporaryDirectory() as tmp:
        blocks.DATA_DIR = Path(tmp)
        workspace = blocks.item_workspace(ITEM_ID)

        # 2. pas de workspace, pas de validate.md : la question d'avant
        nue = question_body()
        assert "validate.md" not in nue, nue
        assert "On garde ?\n\nOptions : `merger` / `abandonner`" in nue, nue
        workspace.mkdir()
        assert question_body() == nue, "un workspace vide ne change rien"
        (workspace / "validate.md").write_text("")
        assert question_body() == nue, "un validate.md vide ne cite rien"
        print("2. sans validate.md : la question reste ce qu'elle était ✓")

        # 3. le validate.md du workspace part avec la question
        (workspace / "validate.md").write_text(CHECKLIST)
        body = question_body()
        for line in CHECKLIST.splitlines():
            assert f"> {line}".rstrip() in body, line
        assert f"/item/{ITEM_ID}/file/validate.md" in body, body
        assert "Options : `merger`" in body, body
        print("3. la question embarque les critères cochés et leurs preuves ✓")

        # 4. un fichier long est cité borné, et le dit
        long = "\n".join(f"- [x] {n}. critère — preuve" for n in range(100))
        (workspace / "validate.md").write_text(long)
        body = question_body()
        assert "> - [x] 39. critère — preuve" in body, body
        assert "40. critère" not in body, body
        assert f"… {100 - gs.CHECKLIST_LINES} lignes de plus" in body, body
        print(f"4. citation bornée à {gs.CHECKLIST_LINES} lignes, le reste compté ✓")

    print("\nvalidate : OK — les critères sont cochés, puis lus par l'humain")


if __name__ == "__main__":
    main()
