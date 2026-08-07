"""Le test du rafraîchissement en douceur : le marqueur, et sa stabilité.

Scénario, sans base ni serveur — le rendu est pur :

  1. chaque page porte son marqueur en meta, son conteneur `#live` et le
     script de rafraîchissement ; le rechargement complet d'avant reste,
     dans un `noscript`
  2. le marqueur vaut la version affichée : celle de l'item pour sa page,
     la plus haute des items listés ailleurs
  3. deux rendus des mêmes données donnent le même marqueur — le client
     ne réécrit alors rien ; une version qui bouge, ou le battement qui
     s'arrête, le font changer
  4. le script tient dans la page : pas de fetch d'onglet caché, pas de
     remplacement sous le curseur, le conteneur remplacé et non la page

Le comportement navigateur (polling, scroll, focus) se vérifie en headless
côté rail ; ici on vérifie ce que le serveur envoie.

Usage : uv run python tests/live_test.py
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import heartbeat, web  # noqa: E402
from graphatom.kernel import now  # noqa: E402

FRESH = now() - dt.timedelta(seconds=3)
OLD = now() - dt.timedelta(seconds=heartbeat.STALE_S + 1)


class FakeCursor:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConn:
    """La base : elle rend les items qu'on lui donne, et rien d'autre."""

    def __init__(self, items: list[dict]):
        self.items = items

    def execute(self, sql: str, params: tuple = ()):
        return FakeCursor(self.items)


def item_row(item_id: int, version: int) -> dict:
    return {"id": item_id, "graph": "code-task", "subject_key": f"sujet-{item_id}",
            "title": None, "generation": 1, "state": "implement", "version": version,
            "escalations": 3, "terminal_at": None}


def question(version: int) -> dict:
    return {"id": 5, "item_id": 14, "subject_key": "gh:o/r#27", "item_title": None,
            "item_state": "review",
            "item_version": version, "owner": "Acid3croco", "escalations": 3,
            "deadline": dt.datetime(2026, 8, 8, 9, 30), "text": "On garde ?",
            "options": ["merger", "abandonner"]}


def questions_page(questions: list[dict], beat=FRESH) -> str:
    return web._questions_page(questions, by="web", token="t", flash=None, beat=beat)


def marker(page: str) -> str:
    """Le marqueur tel qu'un client le lit : le content de la meta."""
    tag = f"<meta name='{web.MARKER}' content='"
    start = page.index(tag) + len(tag)
    return page[start:page.index("'", start)]


def main() -> None:
    # 1. le marqueur, le conteneur, le script — et le repli sans JavaScript
    page = questions_page([question(12)])
    assert f"<meta name='{web.MARKER}' content='" in page, page[:400]
    assert "<main id='live'>" in page and "</main>" in page, page[:400]
    assert "<script>" in page and "DOMParser" in page, page[-800:]
    assert f"<noscript>{web.REFRESH}</noscript>" in page, page[:400]
    assert page.count(web.REFRESH) == 1, "le rechargement complet vit dans le noscript"
    print("1. meta marqueur + conteneur #live + script, refresh en noscript ✓")

    # 2. le marqueur porte la version affichée — la plus haute quand il y en a
    assert marker(questions_page([question(12)])) == "v12-live"
    assert marker(questions_page([question(4), question(9)])) == "v9-live"
    assert marker(questions_page([])) == "v0-live", "aucune question : rien à suivre"
    items = web._items_page(FakeConn([item_row(2, 7), item_row(1, 30)]), FRESH)
    assert marker(items) == "v30-live", marker(items)
    assert marker(web._items_page(FakeConn([]), FRESH)) == "v0-live"
    print("2. version de l'item, ou la plus haute des items listés ✓")

    # 3. la stabilité : deux rendus identiques, un seul marqueur — le client
    #    compare et ne réécrit rien. C'est tout ce qui distingue ce
    #    rafraîchissement d'un rechargement de page toutes les 5 s.
    assert marker(questions_page([question(12)])) == marker(questions_page([question(12)]))
    assert marker(questions_page([question(12)])) != marker(questions_page([question(13)]))
    # le battement qui s'arrête ne change aucune version, mais change l'en-tête :
    # sans lui dans le marqueur, le bandeau rouge n'arriverait jamais sur une
    # page déjà ouverte
    fige = questions_page([question(12)], beat=OLD)
    assert marker(fige) == "v12-stalled", marker(fige)
    assert "class='stalled'" in fige, "l'en-tête figé est dans le conteneur repeint"
    print("3. marqueur stable à données égales, changé par la version ou l'arrêt ✓")

    # 4. ce que le script promet, ligne à ligne
    for promesse in ("document.hidden", "live.contains(document.activeElement)",
                     "live.innerHTML", "cache: 'no-store'", "5000"):
        assert promesse in web.LIVE, promesse
    assert "WebSocket" not in web.LIVE and "EventSource" not in web.LIVE
    assert "location.reload" not in web.LIVE, "on remplace le conteneur, pas la page"
    assert web.LIVE.count("\n") < 25, "quinze lignes de JS, pas une bibliothèque"
    print("4. onglet caché et focus respectés, conteneur remplacé, aucun canal ✓")

    print("\nrafraîchissement : OK — la page se suit sans se recharger")


if __name__ == "__main__":
    main()
