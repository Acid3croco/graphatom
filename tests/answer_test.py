"""Le test de `/answer` : la première ligne décide, et le raté parle.

Scénario, sans base ni réseau — GitHub et la base sont des doublures :

  1. `/answer <id> <option>` valide en première ligne, suivi d'un
     paragraphe de prose : la réponse est prise, le reçu est posté
  2. `/answer <id-de-cette-question> <option-inconnue>` : le canal
     répond en listant les options valides, la question reste ouverte
  3. `/answer <id-d-une-autre-question> <option>` : rien du tout —
     la boucle balaie toutes les questions, une commande n'en vise qu'une
  4. `/answer plop merger` ne vise aucun id lisible : une seule réponse
     du canal, qui donne la forme attendue
  5. deux tours de sync sur le même commentaire mal formé ne laissent
     qu'un seul commentaire — la clé logique `reply-<commentaire>`
  6. les garde-fous tiennent : auteur non autorisé, fenêtre `armed_at`,
     et la première réponse valide gagne
  7. quatre ratés devant la bonne réponse — auteur non autorisé, autre
     question, deux mots, option hors contrat — chacun jugé seul : la bonne
     est prise, trois reproches, aucun pour l'autre question
  8. la fenêtre d'armement à la seconde : posté à `armed_at` même, pris ;
     une seconde avant, ignoré

Usage : uv run python tests/answer_test.py
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import github_questions  # noqa: E402

ITEM_ID = 56
NUMBER = 87
QUESTION_ID = 9
OPTIONS = ["merger", "reprendre"]
ALLOWED = {"Acid3croco"}
ARMED = dt.datetime(2026, 8, 8, 8, 0, tzinfo=dt.timezone.utc)


def question() -> dict:
    """Une question ouverte du rail, armée, sur l'issue #87."""
    return {
        "id": QUESTION_ID, "item_id": ITEM_ID, "subject_key": f"gh:o/r#{NUMBER}",
        "node": "review", "owner": "Acid3croco", "state": "open",
        "options": list(OPTIONS), "answer": None, "answered_by": None,
        "text": "On garde ?", "armed_at": ARMED,
        "deadline": dt.datetime(2026, 8, 8, 9, 30),
    }


def comment(body: str, cid: int = 1001, author: str = "Acid3croco",
            at: str = "2026-08-08T08:30:00Z") -> dict:
    return {"id": cid, "body": body, "user": {"login": author}, "created_at": at}


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
    """La base : une question, et la table des effets."""

    def __init__(self, q: dict):
        self.question = q
        self.effects: dict[tuple[str, str], str] = {}

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql: str, params: tuple = ()):
        if "FROM question q JOIN work_item" in sql:  # les questions ouvertes
            return FakeCursor([self.question]
                              if self.question["state"] == "open" else [])
        if sql.startswith("SELECT * FROM question WHERE id"):
            return FakeCursor([self.question]
                              if params[0] == self.question["id"] else [])
        if sql.startswith("UPDATE question SET state"):
            self.question.update(state="answered", answer=params[0],
                                 answered_by=params[1])
            return FakeCursor([])
        if sql.startswith("INSERT INTO effect"):
            # les params d'`effects.intend` : item, run, clé logique, cible, intention
            self.effects.setdefault((params[3], params[2]), "not_attempted")
            return FakeCursor([])
        if sql.startswith("SELECT * FROM effect"):
            return FakeCursor([{"observation": self.effects.get((params[0], params[1]))}])
        if sql.startswith("UPDATE effect SET observation"):
            self.effects[(params[0], params[1])] = "applied"
            return FakeCursor([])
        return FakeCursor([])


class FakeGitHub:
    """GitHub : les commentaires humains posés d'avance, ceux du rail à la suite."""

    repo = "o/r"

    def __init__(self, humains: list[dict]):
        self.humains = humains
        self.posted: list[tuple[int, str]] = []

    def comments(self, number: int) -> list[dict]:
        return self.humains + [{"body": body} for n, body in self.posted
                               if n == number]

    def post_comment(self, number: int, body: str) -> None:
        self.posted.append((number, body))


def sync(humains: list[dict], q: dict | None = None,
         tours: int = 1) -> tuple[FakeGitHub, FakeConn, dict]:
    """Un ou plusieurs tours de sync sur les mêmes commentaires."""
    q = q or question()
    gh, conn = FakeGitHub(humains), FakeConn(q)
    for _ in range(tours):
        github_questions.collect_answers(conn, gh, ALLOWED)
    return gh, conn, q


PROSE = ("/answer 9 merger\n"
         "\n"
         "Je merge : les critères sont tenus et la CI est verte.")


def premiere_ligne_valide() -> None:
    """1. La commande sur la première ligne, l'explication en dessous."""
    gh, _, q = sync([comment(PROSE)])
    assert q["state"] == "answered", q
    assert q["answer"] == "merger", q
    assert q["answered_by"] == "Acid3croco", q
    assert len(gh.posted) == 1, gh.posted
    number, body = gh.posted[0]
    assert number == NUMBER, number
    assert f"<!-- graphatom:q{QUESTION_ID}-receipt -->" in body, body
    assert "Réponse `merger` enregistrée (par @Acid3croco) — le rail reprend." in body
    print("1. première ligne valide + prose : réponse prise, reçu posté ✓")


def option_inconnue() -> None:
    """2. Une option inconnue, sur cette question : les options valides, et
    la question reste ouverte."""
    gh, _, q = sync([comment("/answer 9 plop")])
    assert q["state"] == "open", q
    assert len(gh.posted) == 1, gh.posted
    body = gh.posted[0][1]
    assert "<!-- graphatom:reply-1001 -->" in body, body
    for option in OPTIONS:
        assert option in body, body
    print("2. option inconnue : les options valides dites, question ouverte ✓")


def autre_question() -> None:
    """3. Une commande pour une autre question : silence complet, même mal formée."""
    for corps in ("/answer 77 merger", "/answer 77"):
        gh, _, q = sync([comment(corps)])
        assert gh.posted == [], (corps, gh.posted)
        assert q["state"] == "open", q
    print("3. commande pour une autre question : aucun bruit croisé ✓")


def aucun_id_lisible() -> None:
    """4. Aucun id lisible : une seule réponse, qui donne la forme."""
    gh, _, q = sync([comment("/answer plop merger")])
    assert len(gh.posted) == 1, gh.posted
    body = gh.posted[0][1]
    assert f"`/answer {QUESTION_ID} <option>`" in body, body
    assert "commande non prise" in body, body
    assert q["state"] == "open", q
    print("4. `/answer plop merger` : une réponse, la forme attendue ✓")

    # un mot de trop sur la ligne, ou `/answer` seul sans id : une
    # malformation, dite aussi — jamais une exception
    for corps in ("/answer 9 merger tout de suite", "/answer"):
        gh, _, q = sync([comment(corps)])
        assert len(gh.posted) == 1, (corps, gh.posted)
        assert "commande non prise" in gh.posted[0][1], (corps, gh.posted)
        assert q["state"] == "open", q


def un_seul_reproche() -> None:
    """5. Le reproche n'est posté qu'une fois, quel que soit le nombre de tours."""
    gh, conn, _ = sync([comment("/answer plop merger")], tours=3)
    assert len(gh.posted) == 1, gh.posted
    assert ("gh:o/r#87", "reply-1001") in conn.effects, conn.effects
    print("5. trois tours de sync, un seul reproche ✓")


def garde_fous() -> None:
    """6. Les garde-fous : autorisation, fenêtre d'armement, première réponse."""
    gh, _, q = sync([comment(PROSE, author="passant")])
    assert q["state"] == "open", q
    assert len(gh.posted) == 1, gh.posted
    assert "seuls Acid3croco" in gh.posted[0][1], gh.posted

    gh, _, q = sync([comment(PROSE, at="2026-08-08T07:00:00Z")])
    assert q["state"] == "open", "une commande d'une vie antérieure ne rejoue pas"
    assert gh.posted == [], "et elle ne fait aucun bruit"

    gh, _, q = sync([comment("/answer 9 merger", cid=1001),
                     comment("/answer 9 reprendre", cid=1002)])
    assert q["answer"] == "merger", "la première réponse valide gagne"
    assert len(gh.posted) == 1, gh.posted
    print("6. autorisation, fenêtre d'armement, première réponse : inchangés ✓")


def rates_devant_la_bonne() -> None:
    """7. Chaque raté est jugé seul : la bonne réponse qui suit est prise."""
    rates = [comment(PROSE, cid=1, author="passant"),      # (a) non autorisé
             comment("/answer 77 merger", cid=2),         # (b) une autre question
             comment("/answer 9", cid=3),                 # (c) deux mots
             comment("/answer 9 plop", cid=4)]            # (d) option hors contrat
    gh, _, q = sync(rates + [comment("/answer 9 reprendre", cid=5)])
    assert q["state"] == "answered" and q["answer"] == "reprendre", q
    corps = [body for _, body in gh.posted]
    for cid in (1, 3, 4):
        assert sum(f"<!-- graphatom:reply-{cid} -->" in b for b in corps) == 1, (cid, corps)
    assert not any("reply-2 " in b for b in corps), "l'autre question : aucun reproche"
    assert len(corps) == 4, "trois reproches et le reçu"
    print("7. quatre ratés devant la bonne : chacun jugé seul, la bonne prise ✓")


def fenetre_a_la_seconde() -> None:
    """8. Posté à `armed_at` même : pris ; une seconde avant : ignoré."""
    forme = "%Y-%m-%dT%H:%M:%SZ"
    gh, _, q = sync([comment(PROSE, at=ARMED.strftime(forme))])
    assert q["state"] == "answered", "à l'instant de l'armement, la commande vaut"
    assert len(gh.posted) == 1, gh.posted
    avant = (ARMED - dt.timedelta(seconds=1)).strftime(forme)
    gh, _, q = sync([comment(PROSE, at=avant)])
    assert q["state"] == "open" and gh.posted == [], (q, gh.posted)
    print("8. fenêtre d'armement à la seconde : prise à l'instant, ignorée avant ✓")


def main() -> None:
    premiere_ligne_valide()
    option_inconnue()
    autre_question()
    aucun_id_lisible()
    un_seul_reproche()
    garde_fous()
    rates_devant_la_bonne()
    fenetre_a_la_seconde()
    print("\n/answer : OK — la prose passe, le raté parle, le reste se tait")


if __name__ == "__main__":
    main()
