"""Le test des dépendances : `Depends-on: #N` diffère l'admission.

Scénario, sans base ni réseau — GitHub et le kernel sont des doublures :

  1. la grammaire est fermée : une ligne `Depends-on: #N`, rien d'autre lu
  2. une déclaration invalide — illisible, soi-même, issue inexistante —
     est ignorée, mais dite une fois
  3. une dépendance ouverte diffère l'admission — aucun item, un commentaire
     à clé logique, le label `rail:blocked` ; le titre de l'issue part avec
     l'admission, et se range sur le sujet à chaque tick
  4. le tick suivant ne redit rien ; la dépendance fermée, l'admission part
     par le chemin normal et le label s'en va

Usage : uv run python tests/depends_test.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, github_sync as gs  # noqa: E402


class FakeCursor:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    """La base : seulement ce que le canal lui demande — l'issue est-elle
    connue, et le titre du sujet qu'on lui range."""

    def __init__(self, terminals: dict[str, int] | None = None,
                 items: list[dict] | None = None,
                 events: list[dict] | None = None):
        self.known: set[str] = set()
        self.updated: list[tuple] = []
        self.terminals = terminals or {}
        self.items = items or []
        self.events = events or []

    def execute(self, sql: str, params: tuple = ()):
        if "FROM subject s JOIN work_item" in sql:
            return FakeCursor([{"?column?": 1}] if params[1] in self.known else [])
        if "UPDATE subject SET title" in sql:
            self.updated.append((params[1], params[2], params[0]))
            return FakeCursor([])
        if "w.terminal_at IS NOT NULL" in sql:
            item_id = self.terminals.get(params[1])
            return FakeCursor([{"id": item_id}] if item_id else [])
        if "SELECT * FROM event" in sql:
            return FakeCursor(self.events)
        if "FROM work_item w" in sql:
            return FakeCursor(self.items)
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
        return [{"id": index, "body": body}
                for index, (n, body) in enumerate(self.posted, start=1)
                if n == number]

    def post_comment(self, number: int, body: str) -> None:
        self.posted.append((number, body))

    def edit_comment(self, comment_id: int, body: str) -> None:
        number, _ = self.posted[comment_id - 1]
        self.posted[comment_id - 1] = (number, body)

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
        self.titles: list[str | None] = []

    def admit(self, conn, revision: str, subject_key: str,
              title: str | None = None, prepare=None) -> int:
        self.admitted.append(subject_key)
        self.titles.append(title)
        item_id = len(self.admitted)
        if prepare:
            prepare(item_id)
        return item_id


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
    assert gs._retry_of("Retry-of: #29") == ["#29"]
    assert gs._retry_of("- [ ] #29\nretry-of: #30\nvoir #31") == []
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
    issues = [{"number": 41, "body": "Depends-on: #29", "labels": [],
               "title": "Le titre de #41"},
              {"number": 42, "body": "aucune dépendance", "labels": [],
               "title": "Le titre de #42"}]
    gh = FakeGitHub(issues, {29: "open"})
    conn, said = FakeConn(), set()

    # 3. tick 1 : #41 attend, #42 part — un commentaire, un label
    blocked, queued = gs._admit_labeled(conn, gh, "rev", said)
    assert blocked == {41} and queued == set(), (blocked, queued)
    assert kernel.admitted == ["gh:o/r#42"], kernel.admitted
    assert "en attente de #29" in gh.posted[0][1], gh.posted
    # le titre part avec l'admission : le canal l'a sous la main, le web
    # ne rappellera jamais GitHub pour l'obtenir
    assert kernel.titles == ["Le titre de #42"], kernel.titles
    assert conn.updated == [("code-task", "gh:o/r#41", "Le titre de #41"),
                            ("code-task", "gh:o/r#42", "Le titre de #42")], conn.updated
    gs._paint_states(conn, gh, blocked, queued, stalled=False)
    assert gh.labels == [("+", 41, gs.BLOCKED)], gh.labels
    print("3. dépendance ouverte : pas d'item, `rail:blocked`, un commentaire ✓")

    # 4. tick 2 : rien de neuf, rien de dit
    conn.known.add("gh:o/r#42")
    issues[0]["labels"] = [{"name": gs.BLOCKED}]
    gh.labels.clear()
    assert gs._admit_labeled(conn, gh, "rev", said) == ({41}, set())
    assert len(gh.posted) == 1 and kernel.admitted == ["gh:o/r#42"], gh.posted
    gs._paint_states(conn, gh, {41}, set(), stalled=False)
    assert gh.labels == [], gh.labels

    # 4. tick 3 : la dépendance ferme → admission normale, le label s'en va
    gh.states[29] = "closed"
    blocked, queued = gs._admit_labeled(conn, gh, "rev", said)
    assert blocked == queued == set(), (blocked, queued)
    assert kernel.admitted == ["gh:o/r#42", "gh:o/r#41"], kernel.admitted
    gs._paint_states(conn, gh, blocked, queued, stalled=False)
    assert gh.labels == [("-", 41, gs.BLOCKED)], gh.labels
    print("4. dépendance fermée : admission au tick suivant, label retiré ✓")

    # 5. reprise valide : dernier item terminal, trois noms fermés, accusé lisible
    temporary = tempfile.TemporaryDirectory(prefix="graphatom-retry-")
    workspace = Path(temporary.name)
    blocks.DATA_DIR = workspace
    old = blocks.item_workspace(7)
    old.mkdir(parents=True)
    (old / "echec.md").write_text("trace de #40\n")
    (old / "criteria.md").write_text("critères de #40\n")
    (old / "validate.md").write_text("validation de #40\n")
    (old / "hors-scope.md").write_text("ne doit pas suivre\n")
    gs.kernel = FakeKernel()
    kernel = gs.kernel
    conn = FakeConn({"gh:o/r#40": 7})
    gh = FakeGitHub(
        [{"number": 50, "body": "Contexte\nRetry-of: #40", "labels": [],
          "title": "Reprise de #40"}],
        {40: "closed"},
    )
    retries: dict[int, tuple[int, int]] = {}
    assert gs._admit_labeled(conn, gh, "rev", set(), retries) == (set(), set())
    assert kernel.admitted == ["gh:o/r#50"], kernel.admitted
    assert retries == {1: (40, 7)}, retries
    new = blocks.item_workspace(1)
    assert (new / "echec.md").read_text() == "trace de #40\n"
    assert (new / "criteria.md").read_text() == "critères de #40\n"
    assert (new / "validate.md").read_text() == "validation de #40\n"
    assert not (new / "hors-scope.md").exists()
    ack = gs._ack_body(
        {"id": 1, "graph": "code-task", "generation": 1}, retries[1])
    assert "reprise de #40 (item 7)" in ack, ack

    # La peinture du journal garde la filiation, dans le même tick puis après
    # un redémarrage où la table temporaire `retries` n'existe plus.
    item = {"id": 1, "graph": "code-task", "generation": 1, "version": 1,
            "subject_key": "gh:o/r#50"}
    event = {"item_version": 1, "at": gs.dt.datetime(2026, 1, 1, 12, 0),
             "kind": "admitted", "from_state": None, "to_state": "ingest",
             "outcome": None}
    marker = f"<!-- graphatom:{gs._ack_key(item)} -->"
    gh.post_comment(50, f"{marker}\n{ack}")
    paint_conn = FakeConn(items=[item], events=[event])
    gs._paint_trajectories(paint_conn, gh, {})
    assert "reprise de #40 (item 7)" in gh.posted[-1][1], gh.posted[-1]
    gs._paint_trajectories(paint_conn, gh, {})
    assert "reprise de #40 (item 7)" in gh.posted[-1][1], gh.posted[-1]
    print("5. reprise valide : pièces existantes copiées, filiation dans l'accusé ✓")

    # 6. cible absente, sans terminal ou soi-même : commentaire et admission normale
    for body, states, terminals, word in (
        ("Retry-of: #99", {}, {}, "n'existe pas"),
        ("Retry-of: #40", {40: "closed"}, {}, "aucun item terminal"),
        ("Retry-of: #60", {60: "open"}, {}, "elle-même"),
        ("Retry-of: #40\nRetry-of: #41", {40: "closed", 41: "closed"}, {},
         "une seule ligne"),
    ):
        gs.kernel = FakeKernel()
        gh = FakeGitHub(
            [{"number": 60, "body": body, "labels": [], "title": "invalide"}],
            states,
        )
        retries = {}
        assert gs._admit_labeled(
            FakeConn(terminals), gh, "rev", set(), retries
        ) == (set(), set())
        assert gs.kernel.admitted == ["gh:o/r#60"], (body, gs.kernel.admitted)
        assert retries == {} and len(gh.posted) == 1, (body, retries, gh.posted)
        assert word in gh.posted[0][1], (body, gh.posted)
        assert "graphatom:code-task-retry-invalid" in gh.posted[0][1]
    print("6. reprise invalide : commentaire à clé logique, admission normale ✓")
    temporary.cleanup()

    print("\ndépendances : OK — l'admission attend, puis part toute seule")


if __name__ == "__main__":
    main()
