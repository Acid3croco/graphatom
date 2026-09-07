"""Le test du canal hors du tick : refus, course, découpe, post-mortem.

Scénario — un Postgres jetable, le vrai noyau, GitHub tenu par un greffier :

  1. un sujet au budget de lignée épuisé : le noyau refuse, le canal le dit
     sur sa sortie, l'issue reste sur GitHub sans item ni commentaire
  2. la course de deux syncs : le perdant reçoit « voie occupée », classe
     l'issue en file, n'insiste pas — et la file se voit dès le tick suivant
  3. une reprise dont l'item d'avant n'a laissé aucune pièce : admise, la
     filiation retenue, et aucun workspace créé pour rien
  4. la découpe ne réécrit que les dépendants encore en attente : une issue
     déjà prise en charge par le rail garde son corps
  5. le post-mortem d'un couperet : rien quand le run qui a armé la question
     n'existe plus ou quand c'est une réponse humaine qui l'a armée ; sinon
     chaque mort a son en-tête, et la queue du journal suit
  6. la citation d'une pièce est bornée juste : `CRITERIA_LINES` lignes se
     citent entières, sans queue ; une de plus, et la queue en compte une

Usage : uv run python tests/github_channel_test.py
"""

import contextlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from github_outils import REPO, Greffier  # noqa: E402
from outils import bundle_epreuve, data_dir_jetable, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-canal")

from graphatom import db, github_admission, github_questions, graph, kernel  # noqa: E402
from graphatom import workspace  # noqa: E402
from graphatom.github_speech import ADMITTED, cite, marker  # noqa: E402

GRAPH = "gh-canal"
BUNDLE = bundle_epreuve(GRAPH, {
    "travail": {"block": "ACT", "config": {"duration_s": 0}, "edges": {"ok": "fini"}},
})


def sujet(number: int) -> str:
    return f"gh:{REPO}#{number}"


def items_de(conn, number: int) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM work_item w JOIN subject s ON s.id = w.subject_id "
        "WHERE s.subject_key = %s", (sujet(number),)).fetchone()["n"]


def admission(conn, gh: Greffier, rev: str) -> tuple[tuple[set, set], str]:
    """Un tour d'admission, et ce qu'il a dit sur sa sortie."""
    sortie = io.StringIO()
    with contextlib.redirect_stdout(sortie):
        resultat = github_admission.admit_labeled(conn, gh, rev, set())
    return resultat, sortie.getvalue()


def refus_du_noyau(conn, rev: str) -> None:
    """1. Le budget de lignée épuisé : refusé par le noyau, dit par le canal."""
    gh = Greffier()
    gh.issue_ouverte(12, title="Le sujet usé")
    conn.execute(
        "INSERT INTO subject (graph, subject_key, lineage_budget) VALUES (%s, %s, 0)",
        (GRAPH, sujet(12)))
    (blocked, queued), sortie = admission(conn, gh, rev)
    assert "#12 refusé : budget de lignée épuisé" in sortie, sortie
    assert blocked == queued == set(), (blocked, queued)
    assert items_de(conn, 12) == 0, "aucune occurrence n'est née"
    assert gh.journal == [], "rien n'est dit sur l'issue, elle reste à prendre"
    titre = conn.execute("SELECT title FROM subject WHERE subject_key = %s",
                         (sujet(12),)).fetchone()["title"]
    assert titre == "Le sujet usé", "le titre est rangé avant le refus"
    print("1. lignée épuisée : refus du noyau dit sur la sortie, issue intacte ✓")


def course_de_deux_syncs(conn, rev: str) -> None:
    """2. La voie prise entre la lecture et l'admission : en file, sans insister."""
    gh = Greffier()
    gh.issue_ouverte(13)
    gh.avant_lecture = lambda: kernel.admit(conn, rev, sujet(14))   # l'autre sync
    (blocked, queued), sortie = admission(conn, gh, rev)
    assert queued == {13} and blocked == set(), (blocked, queued)
    assert "admis" not in sortie and "refusé" not in sortie, sortie
    assert items_de(conn, 13) == 0 and items_de(conn, 14) == 1
    gh.avant_lecture = None
    assert admission(conn, gh, rev)[0] == (set(), {13}), "la voie est vue occupée"
    assert items_de(conn, 13) == 0
    conn.execute("UPDATE work_item SET terminal_at = now() WHERE terminal_at IS NULL")
    (blocked, queued), sortie = admission(conn, gh, rev)     # la voie libérée
    assert blocked == queued == set() and items_de(conn, 13) == 1, sortie
    assert "#13 admis → item" in sortie, sortie
    titre = conn.execute("SELECT title FROM subject WHERE subject_key = %s",
                         (sujet(13),)).fetchone()["title"]
    assert titre == "Issue 13", "le titre part avec l'admission"
    print("2. course de deux syncs : en file sans insister, admise la voie libre ✓")
    return gh


def reprise_sans_pieces(conn, rev: str, gh: Greffier) -> None:
    """3. L'item d'avant n'a rien laissé : admise, filiation retenue, pas de workspace."""
    conn.execute("UPDATE work_item SET terminal_at = now() WHERE terminal_at IS NULL")
    precedent = conn.execute(
        "SELECT w.id FROM work_item w JOIN subject s ON s.id = w.subject_id "
        "WHERE s.subject_key = %s", (sujet(13),)).fetchone()["id"]
    assert not workspace.item_workspace(precedent).exists(), "rien n'a été écrit"
    gh.issue_ouverte(15, body="Contexte.\nRetry-of: #13\n")
    retries: dict[int, tuple[int, int]] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        resultat = github_admission.admit_labeled(conn, gh, rev, set(), retries)
    assert resultat == (set(), set()) and items_de(conn, 15) == 1, resultat
    (nouveau, filiation), = retries.items()
    assert filiation == (13, precedent), retries
    assert not workspace.item_workspace(nouveau).exists(), "aucun dossier vide"
    assert gh.corps(15) == [], "une reprise valide ne se plaint pas"
    print("3. reprise sans pièces : admise, filiation retenue, aucun workspace ✓")


def decoupe_selective() -> None:
    """4. La découpe réécrit les dépendants en attente, pas ceux déjà pris en charge."""
    gh = Greffier()
    gh.issue_ouverte(20, body="Mère\n")
    gh.issue_ouverte(21, body="Fille\n")
    gh.issue_ouverte(22, body="Pris en charge\nDepends-on: #20\n")
    gh.issue_ouverte(23, body="En attente\nDepends-on: #20\n")
    gh.issue_ouverte(24, body="Ailleurs\nDepends-on: #21\n")
    gh.post_comment(22, f"{marker(f'{GRAPH}-g1{ADMITTED}')}\nPrise en charge par le rail")
    with contextlib.redirect_stdout(io.StringIO()):
        github_admission.split_close(gh, 20, [21])
    assert gh.issue(22)["body"] == "Pris en charge\nDepends-on: #20\n", "déjà admise"
    assert gh.issue(23)["body"] == "En attente\nDepends-on: #21\n", gh.issue(23)
    assert gh.issue(24)["body"] == "Ailleurs\nDepends-on: #21\n", "pas un dépendant"
    assert gh.gestes("reecrire") == ["En attente\nDepends-on: #21\n"]
    assert gh.journal[-1] == ("fermer", 20, None), "la mère ferme en dernier"
    assert gh.issue(20)["state"] == "closed"
    assert [c for c in gh.corps(23) if marker("reparent-20") in c], gh.corps(23)
    assert len(gh.corps(22)) == 1, "rien de neuf sur l'issue déjà admise"
    lus = [n for verbe, n, _ in gh.journal if verbe == "lire"]
    assert 24 not in lus and 22 in lus, "seuls les dépendants sont relus"
    print("4. découpe : le dépendant déjà pris en charge garde son corps ✓")


def post_mortem(conn, rev: str) -> None:
    """5. Muet sans le run ou sans couperet ; sinon l'en-tête de chaque mort."""
    conn.execute("UPDATE work_item SET terminal_at = now() WHERE terminal_at IS NULL")
    item = kernel.admit(conn, rev, sujet(30))
    run = kernel.claim(conn, item)
    assert run is not None
    escalade = ("INSERT INTO event (item_id, item_version, kind, from_state, "
                "to_state, outcome, run_id) VALUES (%s, %s, %s, 'travail', "
                "'escalate', %s, %s)")
    conn.execute(escalade, (item, 2, "reaped", "timed_out", 424242))   # run purgé
    assert github_questions.death_report(conn, item, "escalate") == ""
    conn.execute(escalade, (item, 3, "reaped", "timed_out", run["id"]))
    rapport = github_questions.death_report(conn, item, "escalate")
    assert rapport.startswith(github_questions.TIMEOUT_TITLE), rapport
    assert "`travail` a dépassé son `timeout_s`" in rapport, rapport
    assert "Queue du journal" not in rapport, "un run sans résultat n'a pas de queue"

    conn.execute("UPDATE node_run SET result = %s WHERE id = %s", (json.dumps(
        {"provider": "codex", "reason": "quota épuisé", "log_tail": "ligne A\nligne B\n"}),
        run["id"]))
    conn.execute(escalade, (item, 4, "reaped", "starved", run["id"]))
    rapport = github_questions.death_report(conn, item, "escalate")
    assert rapport.startswith(github_questions.STARVED_TITLE), rapport
    assert "`codex`" in rapport and "Raison du fournisseur : quota épuisé" in rapport
    assert "Queue du journal :\n\n> ligne A\n> ligne B\n" in rapport, rapport
    conn.execute(escalade, (item, 5, "reaped", "stalled", run["id"]))
    rapport = github_questions.death_report(conn, item, "escalate")
    assert rapport.startswith(github_questions.HUNG_TITLE), rapport
    assert "1 tentatives relancées sur place" in rapport, rapport
    conn.execute(escalade, (item, 6, "answer", "retry", None))    # une réponse humaine
    assert github_questions.death_report(conn, item, "escalate") == ""
    print("5. post-mortem : muet sans run ni couperet, un en-tête par mort ✓")


def citation_bornee() -> None:
    """6. `limit` lignes : citées entières, sans queue ; une de plus : la queue le dit."""
    limite, piece = github_questions.CRITERIA_LINES, "criteria.md"
    atelier = workspace.item_workspace(404)
    atelier.mkdir(parents=True)
    lignes = [f"- critère {n}" for n in range(1, limite + 2)]
    (atelier / piece).write_text("\n".join(lignes[:limite]) + "\n")
    entiere = cite(404, piece, limite, "Critères", "http://w")
    assert entiere.count("\n> - critère ") == limite, entiere
    assert "lignes de plus" not in entiere, "une pièce de `limit` lignes n'a pas de queue"
    (atelier / piece).write_text("\n".join(lignes) + "\n")
    coupee = cite(404, piece, limite, "Critères", "http://w")
    assert coupee.count("\n> - critère ") == limite, coupee
    assert f"> - critère {limite}\n> *(… 1 lignes de plus)*\n" in coupee, coupee
    print(f"6. citation : {limite} lignes entières sans queue, {limite + 1} → une de plus ✓")


def main() -> None:
    data_dir_jetable("graphatom-canal-")
    db.init_db(drop=True)
    with db.connect() as conn:
        rev = graph.publish(conn, BUNDLE)
        refus_du_noyau(conn, rev)
        gh = course_de_deux_syncs(conn, rev)
        reprise_sans_pieces(conn, rev, gh)
        decoupe_selective()
        post_mortem(conn, rev)
    citation_bornee()
    print("\ncanal github : OK — le refus se dit, la course se classe, la découpe trie")


if __name__ == "__main__":
    main()
