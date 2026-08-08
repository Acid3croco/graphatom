"""Le test de la découpe : une mère découpée reporte ses dépendances.

Une découpe crée les filles puis ferme la mère. Sans report, toute issue
qui portait `Depends-on: <mère>` voit sa dépendance satisfaite par une
fermeture qui ne livre aucun travail, et part pour rien. Le report ferme
ce trou, et l'ordre est ce qui le rend sûr.

Scénario, sans base ni réseau — GitHub et le kernel sont des doublures :

  1. la réécriture ne touche que la ligne visée : le reste du corps est
     rendu au caractère près
  2. une mère à deux dépendants : chacun porte la dernière fille et reçoit
     son commentaire, qui nomme l'ancienne cible et la nouvelle ; une issue
     déjà admise, elle, n'est pas touchée
  3. l'ordre : la mère ne ferme qu'après les réécritures, et un tick joué
     avant cette fermeture n'admet aucun dépendant — celui d'après non plus
  4. une mère sans dépendant se ferme sans une écriture de plus
  5. une réécriture qui échoue laisse la mère ouverte, et le dit en nommant
     l'issue non réécrite

Usage : uv run python tests/split_deps_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import github_sync as gs  # noqa: E402

ADMIS = "<!-- graphatom:code-task-g1-admitted -->\nPrise en charge par le rail."


class FakeCursor:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    """La base : seulement ce que l'admission lui demande — l'issue est-elle
    connue —, et le titre du sujet qu'on lui range."""

    def __init__(self, known: set[str] | None = None):
        self.known = known or set()

    def execute(self, sql: str, params: tuple = ()):
        if "FROM subject s JOIN work_item" in sql:
            return FakeCursor([{"?column?": 1}] if params[1] in self.known else [])
        return FakeCursor([])


class FakeGitHub:
    """GitHub : des issues, leur état, et le journal de ce qu'on lui écrit.

    Le journal retient l'ordre des gestes — c'est lui qui tranche le
    critère 3 : la fermeture de la mère vient après les réécritures.
    """

    repo = "o/r"

    def __init__(self, issues: list[dict], casse: set[int] | None = None):
        self.issues = issues
        self.casse = casse or set()      # les numéros dont l'édition échoue
        self.posted: list[tuple[int, str]] = []
        self.journal: list[str] = []
        self.on_close = None             # joué à la fermeture, avant qu'elle prenne

    def _issue(self, number: int) -> dict | None:
        return next((i for i in self.issues if i["number"] == number), None)

    def labeled_issues(self) -> list[dict]:
        return [i for i in self.issues if i["state"] == "open"]

    def issue(self, number: int) -> dict | None:
        return self._issue(number)

    def comments(self, number: int) -> list[dict]:
        return [{"body": body} for n, body in self.posted if n == number]

    def post_comment(self, number: int, body: str) -> None:
        self.posted.append((number, body))
        self.journal.append(f"comment#{number}")

    def edit_issue(self, number: int, body: str) -> None:
        if number in self.casse:
            raise OSError("403 — droits insuffisants")
        self._issue(number)["body"] = body
        self.journal.append(f"edit#{number}")

    def close_issue(self, number: int) -> None:
        if self.on_close:
            self.on_close()
        self._issue(number)["state"] = "closed"
        self.journal.append(f"close#{number}")

    def create_label(self, name: str) -> None:
        pass

    def add_label(self, number: int, name: str) -> None:
        pass

    def remove_label(self, number: int, name: str) -> None:
        pass


class FakeGraph:
    def load_bundle(self, conn, revision: str) -> dict:
        return {"name": "code-task"}


class FakeKernel:
    def __init__(self):
        self.admitted: list[str] = []

    def admit(self, conn, revision: str, subject_key: str,
              title: str | None = None) -> int:
        self.admitted.append(subject_key)
        return len(self.admitted)


def issue(number: int, body: str = "", state: str = "open") -> dict:
    return {"number": number, "body": body, "state": state,
            "labels": [], "title": f"Le titre de #{number}"}


def reparents(gh: FakeGitHub, mother: int) -> list[tuple[int, str]]:
    """Les commentaires de reprise de dépendance, à leur marqueur."""
    return [(n, b) for n, b in gh.posted
            if f"graphatom:reparent-{mother}" in b]


def main() -> None:
    # 1. la réécriture ne touche que sa ligne
    corps = ("Ce que fait cette fille.\n\n## Critères de succès\n\n"
             "1. voir #10 — preuve : `Depends-on: #10` reste de la prose ici\n\n"
             "Lineage-budget: 2\nDepends-on: #10\n")
    assert gs._reparent(corps, 10, 13) == corps.replace(
        "\nDepends-on: #10\n", "\nDepends-on: #13\n"), gs._reparent(corps, 10, 13)
    assert gs._reparent("Depends-on: #100", 10, 13) == "Depends-on: #100"
    assert gs._reparent("Depends-on: #10\nDepends-on: #9", 10, 13) == \
        "Depends-on: #13\nDepends-on: #9"
    assert gs._reparent("aucune ligne", 10, 13) == "aucune ligne"
    print("1. seule la ligne `Depends-on: #<mère>` est réécrite ✓")

    # les doublures prennent la place du kernel et du graph dans le canal
    gs.graph, gs.kernel = FakeGraph(), FakeKernel()

    # 2. la mère #10, découpée en #11 → #12 → #13, et ses dépendants
    issues = [issue(10, "La primitive.\n\n## Découpée en\n\n- [ ] #11\n- [ ] #12\n- [ ] #13\n"),
              issue(11, "La première fille.\n"),
              issue(12, "La deuxième.\n\nDepends-on: #11\n"),
              issue(13, "La troisième.\n\nDepends-on: #12\n"),
              issue(20, "Le premier dépendant.\n\nDepends-on: #10\n"),
              issue(21, "Le second.\n\nLineage-budget: 1\nDepends-on: #10\n"),
              issue(22, "Déjà admis, on n'y touche pas.\n\nDepends-on: #10\n")]
    gh = FakeGitHub(issues)
    gh.posted.append((22, ADMIS))       # #22 est déjà pris en charge par le rail
    conn = FakeConn({"gh:o/r#10", "gh:o/r#22"})
    kernel, said = gs.kernel, set()

    # 3. le tick joué au moment de la fermeture : aucun dépendant admis
    pendant: list[str] = []
    gh.on_close = lambda: (gs._admit_labeled(conn, gh, "rev", said),
                           pendant.extend(kernel.admitted))

    gs.split_close(gh, 10, [11, 12, 13])

    assert gh._issue(20)["body"] == "Le premier dépendant.\n\nDepends-on: #13\n", \
        gh._issue(20)["body"]
    assert gh._issue(21)["body"] == \
        "Le second.\n\nLineage-budget: 1\nDepends-on: #13\n", gh._issue(21)["body"]
    assert gh._issue(22)["body"] == \
        "Déjà admis, on n'y touche pas.\n\nDepends-on: #10\n", gh._issue(22)["body"]
    dits = reparents(gh, 10)
    assert [n for n, _ in dits] == [20, 21], dits
    for _, body in dits:
        assert "#10" in body and "#13" in body, body
    print("2. les deux dépendants portent la dernière fille, chacun avec son "
          "commentaire ✓")

    # l'ordre : tout ce qui touche les dépendants est passé avant la fermeture
    assert gh.journal.index("close#10") == len(gh.journal) - 1, gh.journal
    assert gh.journal[:4] == ["edit#20", "comment#20", "edit#21", "comment#21"], \
        gh.journal
    assert "gh:o/r#20" not in pendant and "gh:o/r#21" not in pendant, pendant
    # et le tick d'après : les deux attendent maintenant #13, toujours ouverte
    gs._admit_labeled(conn, gh, "rev", said)
    assert "gh:o/r#20" not in kernel.admitted, kernel.admitted
    assert "gh:o/r#21" not in kernel.admitted, kernel.admitted
    assert "gh:o/r#11" in kernel.admitted, kernel.admitted   # la fille, elle, part
    assert gh._issue(10)["state"] == "closed"
    print("3. la mère ne ferme qu'après, et aucun dépendant n'est admis ✓")

    # 4. une mère sans dépendant : la découpe d'aujourd'hui, rien de plus
    issues = [issue(30, "Seule au monde.\n"), issue(31, "Sa fille.\n")]
    gh = FakeGitHub(issues)
    gs.split_close(gh, 30, [31])
    assert gh.journal == ["comment#30", "close#30"], gh.journal
    assert len(gh.posted) == 1 and "#31" in gh.posted[0][1], gh.posted
    assert gh._issue(30)["state"] == "closed"
    print("4. mère sans dépendant : une fermeture, aucune écriture de plus ✓")

    # 5. une réécriture qui échoue : la mère reste ouverte, et on sait laquelle
    issues = [issue(40, "La mère.\n"), issue(41, "Sa fille.\n"),
              issue(42, "Dépendant réécrit.\n\nDepends-on: #40\n"),
              issue(43, "Dépendant impossible.\n\nDepends-on: #40\n")]
    gh = FakeGitHub(issues, casse={43})
    try:
        gs.split_close(gh, 40, [41])
        raise AssertionError("la découpe aurait dû s'arrêter")
    except SystemExit as exc:
        assert "#43" in str(exc), exc
    assert gh._issue(40)["state"] == "open", gh._issue(40)
    assert "close#40" not in gh.journal, gh.journal
    print("5. réécriture impossible : mère ouverte, issue non réécrite nommée ✓")

    print("\ndécoupe : OK — les dépendances suivent la dernière fille, "
          "et la mère ferme en dernier")


if __name__ == "__main__":
    main()
