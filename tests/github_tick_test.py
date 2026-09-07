"""Le test du tick GitHub : chaque geste du canal, sur une vraie base.

Scénario — un Postgres jetable, le vrai noyau, et GitHub tenu par un
greffier qui note chaque geste :

  1. l'accusé de prise en charge : un par occurrence, jamais deux ; la
     reprise se lit dans celui de la génération suivante
  2. la filiation se relit dans la seule forme fermée de l'accusé — jamais
     dans une prose qui lui ressemble
  3. les labels d'un item terminal partent une fois, et jamais tant qu'une
     génération plus récente tourne sur la même issue
  4. l'accusé porte le journal ; il n'est réécrit que si la version a
     bougé ; un PATCH raté se retente au tick suivant
  5. le rapport de clôture : une table, une fois par génération
  6. sans accusé posté, la trajectoire attend le tick suivant
  7. les labels d'état : voulus posés, périmés retirés, l'actif gagne sur
     la file, l'alarme à côté de l'état
  8. un tour complet : la file reste sur GitHub, l'alarme suit le
     battement, la panne réseau se dit et ne casse rien
  9. un tour porte les critères de `scope` et la question ouverte sur
     l'issue, chacun une fois, et ramène la réponse humaine dans la base
 10. les répondants : la variable d'environnement, sinon le propriétaire
 11. `sync_forever` bat, publie, tourne une fois et dort du délai demandé —
     contre le vrai client, sur un serveur local

Usage : uv run python tests/github_tick_test.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import types
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from github_outils import REPO, Greffier, Serveur  # noqa: E402
from outils import bundle_epreuve, data_dir_jetable, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-github")

from graphatom import db, github_client, graph, heartbeat, kernel, workspace  # noqa: E402
from graphatom import github_speech, github_sync as gs  # noqa: E402
from graphatom.github_client import BLOCKED, QUEUED, STALLED  # noqa: E402

GRAPH = "gh-tick"
BUNDLE = bundle_epreuve(GRAPH, {
    "travail": {"block": "ACT", "config": {"duration_s": 0}, "edges": {"ok": "fini"}},
})
ACK_G1 = f"{GRAPH}-g1{gs.ADMITTED}"


class Reveil(Exception):
    """Le sommeil de la boucle, remplacé par un réveil qui la fait sortir."""


def admettre(conn, rev: str, number: int) -> int:
    return kernel.admit(conn, rev, f"gh:{REPO}#{number}", f"Issue {number}")


def terminer(conn, item_id: int) -> None:
    conn.execute("UPDATE work_item SET terminal_at = now() WHERE id = %s", (item_id,))


def effet(conn, number: int, key: str) -> str | None:
    row = conn.execute(
        "SELECT observation FROM effect WHERE target_uri = %s AND logical_key = %s",
        (f"gh:{REPO}#{number}", key)).fetchone()
    return row["observation"] if row else None


def capture(fn, *args) -> str:
    sortie = io.StringIO()
    with contextlib.redirect_stdout(sortie):
        fn(*args)
    return sortie.getvalue()


def accuse_par_generation(conn, rev: str, gh: Greffier) -> tuple[int, int]:
    """1. Un accusé par occurrence, jamais deux ; la reprise dans le second."""
    gh.issue_ouverte(7)
    i1 = admettre(conn, rev, 7)
    gs.acknowledge(conn, gh)
    gs.acknowledge(conn, gh)
    corps = gh.corps(7)
    assert len(corps) == 1, corps
    assert gs.marker(ACK_G1) in corps[0], corps[0]
    assert f"item {i1}, graph `{GRAPH}` (génération 1)." in corps[0], corps[0]
    assert f"{gs.web_url()}/item/{i1}" in corps[0], corps[0]
    assert effet(conn, 7, ACK_G1) == "applied"

    terminer(conn, i1)
    i2 = admettre(conn, rev, 7)
    gs.acknowledge(conn, gh, {i2: (5, i1)})
    gs.acknowledge(conn, gh, {i2: (5, i1)})
    corps = gh.corps(7)
    assert len(corps) == 2, corps
    assert f"(génération 2), reprise de #5 (item {i1})." in corps[1], corps[1]
    print("1. un accusé par génération, jamais deux ; la reprise dans le second ✓")
    return i1, i2


def filiation_fermee(gh: Greffier, i1: int, i2: int) -> None:
    """2. La filiation se relit dans la seule forme fermée de l'accusé."""
    item = {"id": i2, "graph": GRAPH, "generation": 2}
    assert gs._ack_retry(item, gh.corps(7)[1]) == (5, i1)
    entete = gs.ack_body(item).splitlines()[0].removesuffix(".")
    # l'accusé d'un autre item au numéro aussi long : même longueur d'en-tête
    assert len(str(i1)) == len(str(i2)), (i1, i2)
    voisin = gs.ack_body({"id": i1, "graph": GRAPH, "generation": 2}, (5, i1))
    for prose in (f"On parle de la reprise de #5 (item {i1}).",  # pas l'en-tête
                  voisin.splitlines()[0],                         # pas cet item
                  f"{entete}, reprise de #cinq (item {i1}).",    # pas un numéro
                  f"{entete}, reprise de #5 (item {i1})",        # pas fermée
                  f"{entete}, reprise de #5 (item {i1}))",       # mal fermée
                  f"{entete}, reprise de #5.",                    # sans item
                  gs.ack_body(item)):                             # sans reprise
        assert gs._ack_retry(item, prose) is None, prose
    print("2. la filiation ne se lit que dans la forme fermée de l'accusé ✓")


def labels_au_terminal(conn, gh: Greffier, i2: int) -> None:
    """3. Les labels d'un item terminal : jamais tant qu'une génération tourne, une fois après."""
    gh.labels_de[7] = ["graphatom", "rail:travail", STALLED]
    gs.clear_terminal_labels(conn, gh)   # génération 1 terminale, 2 active
    assert gh.gestes("lire", 7).count("labels") == 0, gh.journal
    assert effet(conn, 7, f"{GRAPH}-g1-unlabeled") is None
    assert gh.labels_de[7] == ["graphatom", "rail:travail", STALLED]

    terminer(conn, i2)
    gs.clear_terminal_labels(conn, gh)
    assert gh.labels_de[7] == ["graphatom"], gh.labels_de
    assert gh.gestes("-", 7) == ["rail:travail", STALLED], gh.journal
    assert gh.gestes("lire", 7).count("labels") == 2, "une lecture par génération"
    for generation in (1, 2):
        assert effet(conn, 7, f"{GRAPH}-g{generation}-unlabeled") == "applied"
    gs.clear_terminal_labels(conn, gh)
    assert gh.gestes("lire", 7).count("labels") == 2, "l'effet retient que c'est fait"
    print("3. labels retirés une fois au terminal, jamais sous une génération active ✓")


def trajectoire_vivante(conn, rev: str, gh: Greffier) -> int:
    """4. L'accusé porte le journal, réécrit à chaque version ; un PATCH raté se retente."""
    gh.issue_ouverte(8)
    i3 = admettre(conn, rev, 8)
    # un processus d'avant a posté l'accusé et s'est éteint avant de le marquer
    gh.post_comment(8, f"{gs.marker(ACK_G1)}\ndéjà dit")
    gs.acknowledge(conn, gh)
    assert len(gh.corps(8)) == 1 and effet(conn, 8, ACK_G1) == "applied"

    drawn: dict[int, int] = {}
    gs.paint_trajectories(conn, gh, drawn)
    corps = gh.corps(8)[0]
    assert "**Trajectoire** (v1, 1 transitions)" in corps, corps
    assert " admitted  travail" in corps, corps
    assert drawn[i3] == 1 and len(drawn) == 3, drawn
    edite = gh.gestes("editer", 8)
    assert len(edite) == 1, edite
    gs.paint_trajectories(conn, gh, drawn)
    del drawn[i3]                                  # même version : corps égal
    gs.paint_trajectories(conn, gh, drawn)
    assert gh.gestes("editer", 8) == edite and drawn[i3] == 1

    run = kernel.claim(conn, i3)
    kernel.apply(conn, run["id"], {"outcome": "ok"})   # v2, terminal
    gh.casse = set(edite)
    sortie = capture(gs.paint_trajectories, conn, gh, drawn)
    assert "#8 trajectoire non éditée" in sortie and "on réessaie" in sortie, sortie
    assert drawn[i3] == 1, "un PATCH raté n'entre pas dans le peint"
    gh.casse = set()
    sortie = capture(gs.paint_trajectories, conn, gh, drawn)
    assert "#8 ✎ trajectoire v2" in sortie, sortie
    corps = gh.corps(8)[0]
    assert "(v2, 2 transitions)" in corps and " result    travail → fini [ok]" in corps
    assert drawn[i3] == 2, drawn
    print("4. journal dans l'accusé, réécrit par version, PATCH raté retenté ✓")
    return i3


def rapport_terminal(conn, gh: Greffier) -> None:
    """5. Le rapport de clôture : une table, une fois par génération."""
    gs.report_terminals(conn, gh)
    gs.report_terminals(conn, gh)
    rapports = [c for c in gh.corps(8) if "-terminal -->" in c]
    assert len(rapports) == 1, rapports
    assert "**Item terminé : `fini`** (génération 1, 2 transitions)" in rapports[0]
    assert "| v1 | admitted | travail |  |" in rapports[0], rapports[0]
    assert "| v2 | result | travail → fini | ok |" in rapports[0], rapports[0]
    assert sum("-terminal -->" in c for c in gh.corps(7)) == 2, "un par génération"
    print("5. rapport de clôture : la table, une fois par génération ✓")


def accuse_absent(conn, rev: str, gh: Greffier) -> int:
    """6. Pas d'accusé posté : rien n'est écrit, et rien n'est retenu.

    La version n'entre dans `drawn` qu'une fois le journal réellement posé :
    dès que l'accusé apparaît, le tick suivant peint la trajectoire, sans
    attendre une version de plus ni un redémarrage du canal.
    """
    gh.issue_ouverte(9)
    i4 = admettre(conn, rev, 9)
    drawn: dict[int, int] = {}
    gs.paint_trajectories(conn, gh, drawn)
    assert gh.corps(9) == [] and gh.gestes("editer", 9) == [], gh.journal
    assert i4 not in drawn, "rien n'est peint : rien n'est retenu"
    gs.acknowledge(conn, gh)
    gs.paint_trajectories(conn, gh, drawn)
    assert "(v1, 1 transitions)" in gh.corps(9)[0], gh.corps(9)
    assert drawn[i4] == 1, drawn
    print("6. sans accusé, rien n'est peint ni retenu ; l'accusé posé, le tick suivant peint ✓")
    return i4


def peinture_des_etats(conn, gh: Greffier) -> None:
    """7. Voulus posés, périmés retirés, l'actif gagne sur la file, l'alarme à côté."""
    gh.labels_de[9] = [QUEUED, "graphatom"]      # la vue d'un sync perdant
    gh.issue(9)["labels"] = [{"name": QUEUED}, {"name": "graphatom"}]
    gh.issue_ouverte(10)
    gh.issue_ouverte(11, labels=[{"name": BLOCKED}])
    gh.journal.clear()
    gs.paint_states(conn, gh, blocked={10}, queued={9, 11}, stalled=True)
    assert gh.gestes("+", 9) == [STALLED, "rail:travail"], gh.journal
    assert gh.gestes("-", 9) == [QUEUED], gh.journal
    assert gh.gestes("+", 10) == [BLOCKED] and gh.gestes("-", 10) == []
    assert gh.gestes("+", 11) == [QUEUED] and gh.gestes("-", 11) == [BLOCKED]
    assert gh.gestes("creer") == [STALLED, "rail:travail", BLOCKED, QUEUED]
    assert gh.gestes("+", 7) == [] and gh.gestes("+", 8) == [], "terminaux : rien"
    print("7. labels d'état : posés, retirés, l'actif gagne, l'alarme à côté ✓")


def tour_complet(conn, rev: str, gh: Greffier) -> None:
    """8. Un tour : la file reste sur GitHub, l'alarme suit le battement, la panne se dit."""
    drawn: dict[int, int] = {}
    said: set[tuple[int, str]] = set()
    sortie = capture(gs.tick, conn, gh, rev, {"o"}, drawn, said)
    assert "#10 ⌫ rail:blocked" in sortie and "#10 ← rail:queued" in sortie, sortie
    assert gh.labels_de[10] == [QUEUED] and gh.labels_de[11] == [QUEUED]
    assert gh.issue(10)["labels"] == [{"name": QUEUED}], "la vue GitHub suit"
    assert STALLED in gh.labels_de[9], "le worker n'a jamais battu"

    heartbeat.beat(conn, heartbeat.RAIL)
    gh.journal.clear()
    gs.tick(conn, gh, rev, {"o"}, drawn, said)
    assert gh.gestes("-", 9) == [STALLED], gh.journal

    gh.panne = urllib.error.URLError("dns en panne")
    gh.journal.clear()
    sortie = capture(gs.tick, conn, gh, rev, {"o"}, drawn, said)
    assert "github injoignable : <urlopen error dns en panne> — on réessaie" in sortie
    assert gh.journal == [], "le tour s'arrête à l'incident"
    gh.panne = None
    print("8. tour complet : file, alarme, panne réseau dite et absorbée ✓")


def pieces_du_workspace(i4: int) -> None:
    """Les pièces que `scope` et `validate` laissent : citées bornées, ou pas du tout."""
    atelier = workspace.item_workspace(i4)
    atelier.mkdir(parents=True, exist_ok=True)
    (atelier / "criteria.md").write_text(
        "\n".join(f"- critère {n}" for n in range(1, 83)) + "\n")
    (atelier / "validate.md").write_text("")
    assert github_speech.cite(i4, "validate.md", 40, "T", "http://w") == "", "vide : rien"
    (atelier / "validate.md").write_text("- [x] un\n- [ ] deux\n")
    courte = github_speech.cite(i4, "validate.md", 40, "T", "http://w")
    assert courte.startswith(f"T — [validate.md](http://w/item/{i4}/file/validate.md)")
    assert "lignes de plus" not in courte and "> - [ ] deux\n" in courte, courte


def question_et_reponse(conn, rev: str, gh: Greffier, i4: int) -> None:
    """9. Le tour porte critères et question sur l'issue, et ramène la réponse."""
    pieces_du_workspace(i4)
    qid = conn.execute(
        "INSERT INTO question (item_id, node, text, options, owner, deadline) "
        "VALUES (%s, 'escalate', 'On retente ?', '[\"retry\"]', 'o', "
        "now() + interval '1 hour') RETURNING id", (i4,)).fetchone()["id"]
    drawn: dict[int, int] = {}
    said: set[tuple[int, str]] = set()
    for _ in range(2):                       # deux tours : chacun dit une fois
        gs.tick(conn, gh, rev, {"o"}, drawn, said)
    criteres = [c for c in gh.corps(9) if "-criteria-" in c]
    assert len(criteres) == 1, criteres
    assert "> - critère 80" in criteres[0] and "critère 81" not in criteres[0]
    assert "> *(… 2 lignes de plus)*" in criteres[0], criteres[0]
    questions = [c for c in gh.corps(9) if gs.marker(f"q{qid}") in c]
    assert len(questions) == 1, questions
    assert "**Question du rail** — pour @o" in questions[0], questions[0]
    assert f"`/answer {qid} <option>`" in questions[0], questions[0]
    assert "- critère 1" in questions[0], "la question cite les critères"
    assert "> - [x] un" in questions[0], "et la checklist, entière"

    gh.humain(9, "o", f"/answer {qid} retry\n\nOn y retourne.")
    gs.tick(conn, gh, rev, {"o"}, drawn, said)
    reponse = conn.execute(
        "SELECT state, answer, answered_by FROM question WHERE id = %s", (qid,),
    ).fetchone()
    assert reponse == {"state": "answered", "answer": "retry", "answered_by": "o"}
    assert any(gs.marker(f"q{qid}-receipt") in c for c in gh.corps(9)), gh.corps(9)
    print("9. critères et question dits une fois, la réponse revient en base ✓")


def repondants() -> None:
    """10. Les répondants : la variable, sinon le propriétaire du dépôt."""
    os.environ.pop("GRAPHATOM_ANSWERERS", None)
    assert gs.answerers("o/r") == {"o"}
    os.environ["GRAPHATOM_ANSWERERS"] = "alice,,bob,"
    assert gs.answerers("o/r") == {"alice", "bob"}
    del os.environ["GRAPHATOM_ANSWERERS"]
    print("10. répondants : la variable épurée, sinon le propriétaire ✓")


def boucle_du_canal(conn) -> None:
    """11. `sync_forever` : bat, publie, tourne une fois, dort du délai demandé."""
    serveur = Serveur()
    github_client.API = serveur.url
    chemin = "/repos/o/vide/issues?labels=graphatom&state=open&per_page=100"
    serveur.programme("GET", chemin, 200, [])
    os.environ["GITHUB_TOKEN"] = "ghp_boucle"
    bundle = Path(tempfile.mkdtemp(prefix="graphatom-github-")) / "bundle.json"
    bundle.write_text(json.dumps(BUNDLE))
    delais: list[float] = []

    def dormir(seconds: float) -> None:
        delais.append(seconds)
        raise Reveil

    gs.time = types.SimpleNamespace(sleep=dormir)
    sortie = io.StringIO()
    try:
        with contextlib.redirect_stdout(sortie):
            gs.sync_forever("o/vide", str(bundle), poll_s=0.5)
        raise AssertionError("la boucle ne rend jamais la main")
    except Reveil:
        pass
    finally:
        gs.time = time
        serveur.fermer()
    assert delais == [0.5], delais
    assert heartbeat.last(conn, heartbeat.GITHUB_SYNC) is not None, "le canal a battu"
    texte = sortie.getvalue()
    assert "canal github sur o/vide — révision" in texte and "répondants : o" in texte
    assert [r["path"] for r in serveur.journal] == [chemin, chemin], serveur.journal
    assert serveur.journal[0]["headers"]["authorization"] == "Bearer ghp_boucle"
    print("11. sync_forever : battement, publication, un tour, sommeil du délai ✓")


def main() -> None:
    data_dir_jetable("graphatom-github-")
    db.init_db(drop=True)
    with db.connect() as conn:
        rev = graph.publish(conn, BUNDLE)
        gh = Greffier()
        i1, i2 = accuse_par_generation(conn, rev, gh)
        filiation_fermee(gh, i1, i2)
        labels_au_terminal(conn, gh, i2)
        trajectoire_vivante(conn, rev, gh)
        rapport_terminal(conn, gh)
        i4 = accuse_absent(conn, rev, gh)
        peinture_des_etats(conn, gh)
        tour_complet(conn, rev, gh)
        question_et_reponse(conn, rev, gh, i4)
        repondants()
        boucle_du_canal(conn)
    print("\ntick github : OK — chaque geste est un effet, dit une fois")


if __name__ == "__main__":
    main()
