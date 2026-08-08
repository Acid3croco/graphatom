"""Le test du nœud `scope` qui parle : les critères sur l'issue, et `unclear`.

Scénario, sans base ni réseau — le graph est une donnée, GitHub une doublure :

  1. le graph route `scope` vers `clarify` sur `unclear` : un WAIT
     `go` / `reformuler`, marqué `escalade` — la boucle de retour vers
     `scope` est bornée par le budget d'escalades de l'item
  2. sans `criteria.md`, aucun commentaire de critères, et la question
     publiée reste celle d'avant, mot pour mot
  3. avec `criteria.md`, le canal le publie en commentaire, sous une clé
     qui porte le graph et la génération
  4. deux ticks ne laissent qu'un commentaire — par l'effet appliqué, et
     par le marqueur si la base a oublié entre le POST et son marquage
  5. la question de `clarify` embarque le contenu de `criteria.md` :
     l'humain lit la lecture du rail avant de répondre

Usage : uv run python tests/criteria_test.py
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
ITEM_ID = 44
ITEM = {"id": ITEM_ID, "graph": "code-task", "generation": 1,
        "subject_key": "gh:o/r#87", "terminal_at": None}
CRITERIA = ("# Spécification proposée — gh:o/r#87\n"
            "\n"
            "## Compris\n"
            "\n"
            "Le nœud scope dit sur l'issue ce qu'il a compris.\n"
            "\n"
            "## Critères de succès\n"
            "\n"
            "1. Le commentaire de critères est posté — tests/criteria_test.py au vert\n")


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
    """La base : des items GitHub, une question ouverte, la table des effets."""

    def __init__(self, items: list, question: dict | None = None,
                 amnesique: bool = False):
        self.items, self.question = items, question
        self.amnesique = amnesique  # crash entre le POST et son marquage
        self.effects: dict[tuple[str, str], str] = {}

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple = ()):
        if "FROM question q JOIN work_item" in sql:
            return FakeCursor([self.question] if self.question else [])
        if "FROM work_item w" in sql:
            return FakeCursor(self.items)
        if sql.startswith("INSERT INTO effect"):
            self.effects.setdefault((params[3], params[2]), "not_attempted")
            return FakeCursor([])
        if sql.startswith("SELECT * FROM effect"):
            return FakeCursor([{"observation": self.effects.get((params[0], params[1]))}])
        if sql.startswith("UPDATE effect SET observation"):
            if not self.amnesique:
                self.effects[(params[0], params[1])] = "applied"
            return FakeCursor([])
        return FakeCursor([])


class FakeGitHub:
    repo = "o/r"

    def __init__(self):
        self.posted: list[tuple[int, str]] = []

    def comments(self, number: int) -> list[dict]:
        return [{"body": body} for n, body in self.posted if n == number]

    def post_comment(self, number: int, body: str) -> None:
        self.posted.append((number, body))


def question_body(gh: FakeGitHub | None = None) -> str:
    """Le commentaire que le canal publie pour la question de `clarify`."""
    question = {
        "id": 9, "item_id": ITEM_ID, "subject_key": "gh:o/r#87",
        "node": "clarify", "owner": "Acid3croco",
        "text": "L'issue admet plusieurs lectures incompatibles.",
        "options": ["go", "reformuler"],
        "deadline": dt.datetime(2026, 8, 8, 9, 30),
    }
    gh = gh or FakeGitHub()
    gs._publish_questions(FakeConn([ITEM], question), gh)
    assert len(gh.posted) == 1, gh.posted
    return gh.posted[0][1]


def main() -> None:
    # 1. le graph : scope sort par `unclear` sur un WAIT borné
    bundle = json.loads((ROOT / "examples" / "code-task.json").read_text())
    nodes = bundle["nodes"]
    assert nodes["scope"]["edges"]["unclear"] == "clarify", nodes["scope"]["edges"]
    clarify = nodes["clarify"]
    assert clarify["block"] == "WAIT", clarify["block"]
    assert clarify["escalade"] is True, clarify
    assert clarify["config"]["options"] == ["go", "reformuler"], clarify["config"]
    assert clarify["edges"] == {"go": "implement", "reformuler": "scope",
                                "expired": "escalate"}, clarify["edges"]
    assert clarify["config"]["owner"] and clarify["config"]["deadline_minutes"]
    print("1. scope → clarify : `go` / `reformuler`, retour borné par `escalade` ✓")

    # le prompt de scope porte les trois règles nouvelles
    prompt = nodes["scope"]["config"]["agent"]["prompt"]
    for regle in ("`unclear` est l'exception",
                  "**Le corps de l'issue n'est jamais édité**",
                  "**spécification proposée**"):
        assert regle in prompt, regle
    print("2. le prompt de scope : l'exception, le corps intouché, la spec ✓")

    with tempfile.TemporaryDirectory() as tmp:
        blocks.DATA_DIR = Path(tmp)
        workspace = blocks.item_workspace(ITEM_ID)

        # 3. pas de criteria.md : rien n'est dit, et la question ne bouge pas
        nue = question_body()
        assert "criteria.md" not in nue, nue
        gh = FakeGitHub()
        gs._publish_criteria(FakeConn([ITEM]), gh)
        assert gh.posted == [], gh.posted
        workspace.mkdir()
        (workspace / "criteria.md").write_text("")
        gs._publish_criteria(FakeConn([ITEM]), gh)
        assert gh.posted == [], "un criteria.md vide ne dit rien"
        assert question_body() == nue, "et la question reste mot pour mot la même"
        print("3. sans criteria.md : aucun commentaire, question inchangée ✓")

        # 4. les critères figés partent sur l'issue, sous leur clé
        (workspace / "criteria.md").write_text(CRITERIA)
        gh, conn = FakeGitHub(), FakeConn([ITEM])
        gs._publish_criteria(conn, gh)
        assert len(gh.posted) == 1, gh.posted
        number, body = gh.posted[0]
        assert number == 87, number
        for line in CRITERIA.splitlines():
            assert f"> {line}".rstrip() in body, line
        assert f"/item/{ITEM_ID}/file/criteria.md" in body, body
        key = next(k for _, k in conn.effects if "criteria" in k)
        assert key.startswith("code-task-g1-criteria-"), key
        print("4. les critères de `scope` publiés sur l'issue, clé graph+génération ✓")

        # 5. deux ticks, un seul commentaire — par l'effet, puis par le marqueur
        gs._publish_criteria(conn, gh)
        assert len(gh.posted) == 1, gh.posted
        oublieuse = FakeConn([ITEM], amnesique=True)
        gs._publish_criteria(oublieuse, gh)
        gs._publish_criteria(oublieuse, gh)
        assert len(gh.posted) == 1, gh.posted
        print("5. deux ticks, un seul commentaire de critères ✓")

        # 6. la question de clarification embarque ce que le rail a compris
        body = question_body()
        for line in CRITERIA.splitlines():
            assert f"> {line}".rstrip() in body, line
        assert "Options : `go` / `reformuler`" in body, body
        print("6. la question de `clarify` porte la lecture du rail ✓")

        # un fichier long est cité borné, et le dit
        long = "\n".join(f"{n}. critère — preuve" for n in range(200))
        (workspace / "criteria.md").write_text(long)
        gs._publish_criteria(FakeConn([ITEM]), gh := FakeGitHub())
        body = gh.posted[0][1]
        assert f"> {gs.CRITERIA_LINES - 1}. critère — preuve" in body, body
        assert f"… {200 - gs.CRITERIA_LINES} lignes de plus" in body, body

    print("\ncritères : OK — le rail dit sa lecture avant d'écrire une ligne")


if __name__ == "__main__":
    main()
