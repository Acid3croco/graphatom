"""Le test des dépendances : `Depends-on: #N` diffère l'admission.

Scénario, sans base ni réseau — GitHub et le kernel sont des doublures :

  1. la grammaire est fermée : une ligne `Depends-on: #N`, rien d'autre lu
  2. une déclaration invalide — illisible, soi-même, issue inexistante —
     est ignorée, mais dite une fois
  3. une dépendance ouverte diffère l'admission — aucun item, un commentaire
     à clé logique, le label `rail:blocked`
  4. le tick suivant ne redit rien ; la dépendance fermée, l'admission part
     par le chemin normal et le label s'en va

Usage : uv run python tests/depends_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import github_sync as gs  # noqa: E402


class FakeCursor:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    """La base : seulement ce que le canal lui demande — l'issue est-elle connue."""

    def __init__(self):
        self.known: set[str] = set()

    def execute(self, sql: str, params: tuple = ()):
        if "FROM subject s JOIN work_item" in sql:
            return FakeCursor([{"?column?": 1}] if params[1] in self.known else [])
        return FakeCursor([])   # _gh_items : aucun item actif


class FakeGitHub:
    """GitHub : des issues ouvertes, des états par numéro, et ce qu'on lui écrit."""

    repo = "o/r"

    def __init__(self, issues: list[dict], states: dict[int, str]):
        self.issues, self.states = issues, states
        self.posted: list[tuple[int, str]] = []
        self.labels: list[tuple[str, int, str]] = []
        self.reads = 0

    def labeled_issues(self) -> list[dict]:
        return self.issues

    def issue(self, number: int) -> dict | None:
        return {"state": self.states[number]} if number in self.states else None

    def comments(self, number: int) -> list[dict]:
        self.reads += 1
        return [{"body": body} for n, body in self.posted if n == number]

    def post_comment(self, number: int, body: str) -> None:
        self.posted.append((number, body))

    def create_label(self, name: str) -> None:
        pass

    def add_label(self, number: int, name: str) -> None:
        self.labels.append(("+", number, name))

    def remove_label(self, number: int, name: str) -> None:
        self.labels.append(("-", number, name))


class FakeGraph:
    def load_bundle(self, conn, revision: str) -> dict:
        return {"name": "code-task"}


class FakeKernel:
    def __init__(self):
        self.admitted: list[str] = []

    def admit(self, conn, revision: str, subject_key: str) -> int:
        self.admitted.append(subject_key)
        return len(self.admitted)


def deps(body, number=1, states=None, gh=None, said=None):
    """Les dépendances ouvertes d'une issue, telles que le canal les voit."""
    gh = gh or FakeGitHub([], states or {})
    issue = {"number": number, "body": body}
    said = set() if said is None else said
    return gh, gs._pending_deps(gh, issue, "code-task", {}, said)


def main() -> None:
    # 1. grammaire fermée : la ligne au mot, le reste du corps n'existe pas
    assert gs._depends_on("Depends-on: #29") == ["#29"]
    assert gs._depends_on("- [ ] #29\ndepends-on: #30\nvoir #31") == []
    assert gs._depends_on(None) == [] and gs._depends_on("") == []
    gh, waiting = deps("bla\nDepends-on: #29\n- [ ] #30\nDepends-on: #31\nfin",
                       states={29: "open", 31: "closed"})
    assert waiting == [29] and gh.posted == [], (waiting, gh.posted)
    _, waiting = deps("Depends-on: #29", states={29: "closed"})
    assert waiting == [], waiting
    print("1. `Depends-on: #N` lu, task list et prose ignorées ✓")

    # 2. les déclarations invalides : ignorées, dites une fois
    for body, said_word in (("Depends-on: #1", "elle-même"),
                            ("Depends-on: #99", "n'existe pas"),
                            ("Depends-on: bientôt", "attendu")):
        gh, waiting = deps(body, number=1, states={1: "open"})
        assert waiting == [] and said_word in gh.posted[0][1], (body, gh.posted)
    gh, waiting = deps("Depends-on: bientôt\nDepends-on: #29", states={29: "open"})
    assert waiting == [29] and len(gh.posted) == 1, (waiting, gh.posted)
    said, gh = set(), FakeGitHub([], {})
    for _ in range(3):
        deps("Depends-on: #99", gh=gh, said=said)
    assert len(gh.posted) == 1 and gh.reads == 1, (gh.posted, gh.reads)
    deps("Depends-on: #99", gh=gh, said=set())   # nouveau processus : le marqueur parle
    assert len(gh.posted) == 1, gh.posted
    print("2. dépendance invalide : ignorée, dite une fois ✓")

    # les doublures prennent la place du kernel et du graph dans le canal
    gs.graph, gs.kernel = FakeGraph(), FakeKernel()
    kernel = gs.kernel
    issues = [{"number": 41, "body": "Depends-on: #29", "labels": []},
              {"number": 42, "body": "aucune dépendance", "labels": []}]
    gh = FakeGitHub(issues, {29: "open"})
    conn, said = FakeConn(), set()

    # 3. tick 1 : #41 attend, #42 part — un commentaire, un label
    blocked = gs._admit_labeled(conn, gh, "rev", said)
    assert blocked == {41} and kernel.admitted == ["gh:o/r#42"], (blocked, kernel.admitted)
    assert "en attente de #29" in gh.posted[0][1], gh.posted
    gs._paint_states(conn, gh, blocked)
    assert gh.labels == [("+", 41, gs.BLOCKED)], gh.labels
    print("3. dépendance ouverte : pas d'item, `rail:blocked`, un commentaire ✓")

    # 4. tick 2 : rien de neuf, rien de dit
    conn.known.add("gh:o/r#42")
    issues[0]["labels"] = [{"name": gs.BLOCKED}]
    gh.labels.clear()
    assert gs._admit_labeled(conn, gh, "rev", said) == {41}
    assert len(gh.posted) == 1 and kernel.admitted == ["gh:o/r#42"], gh.posted
    gs._paint_states(conn, gh, {41})
    assert gh.labels == [], gh.labels

    # 4. tick 3 : la dépendance ferme → admission normale, le label s'en va
    gh.states[29] = "closed"
    blocked = gs._admit_labeled(conn, gh, "rev", said)
    assert blocked == set(), blocked
    assert kernel.admitted == ["gh:o/r#42", "gh:o/r#41"], kernel.admitted
    gs._paint_states(conn, gh, blocked)
    assert gh.labels == [("-", 41, gs.BLOCKED)], gh.labels
    print("4. dépendance fermée : admission au tick suivant, label retiré ✓")

    print("\ndépendances : OK — l'admission attend, puis part toute seule")


if __name__ == "__main__":
    main()
