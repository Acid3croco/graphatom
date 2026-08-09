"""Le test des liens du frontend : la boucle vers GitHub, dans les deux sens.

Scénario, sans base ni serveur — le rendu est pur :

  1. un sujet `gh:<owner>/<repo>#<num>` devient un lien vers l'issue,
     href complet, dans la page des questions comme ailleurs
  2. tout autre sujet reste du texte brut, échappé
  3. `release.json` du workspace donne le lien de la PR de l'item ;
     pas de fichier, ou pas d'URL dedans : pas de lien
  4. la table `/items` porte le numéro d'issue en lien vers GitHub et le
     titre en lien vers `/item/<id>` — un sujet d'un autre canal n'a ni
     l'un ni l'autre, deux cellules vides et rien de cassé
  5. la question mentionne le titre de son item, pas seulement son numéro

Le titre vient de la base, jamais de GitHub : ces pages ne connaissent
que ce que le canal a rangé sur le sujet à l'admission.

Usage : uv run python tests/links_test.py
"""

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, web  # noqa: E402

ISSUE_URL = "https://github.com/Acid3croco/graphatom/issues/27"
PR_URL = "https://github.com/Acid3croco/graphatom/pull/31"
TITRE = "Le titre de l'issue partout <27>"   # avec de quoi vérifier l'échappement


class FakeCursor:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConn:
    """La base : elle rend les items qu'on lui donne, et rien d'autre."""

    def __init__(self, items: list[dict], runs: list[dict] | None = None):
        self.items = items
        self.runs = runs or []

    def execute(self, sql: str, params: tuple = ()):
        if "FROM node_run" in sql:
            return FakeCursor(self.runs)
        return FakeCursor(self.items)


def item_row(item_id: int, subject_key: str, title: str | None) -> dict:
    return {"id": item_id, "graph": "code-task", "subject_key": subject_key,
            "title": title, "generation": 1, "state": "implement", "version": 12,
            "escalations": 3, "terminal_at": None}


def question_page(title: str | None = None) -> str:
    question = {
        "id": 5, "item_id": 14, "subject_key": "gh:Acid3croco/graphatom#27",
        "item_title": title,
        "item_state": "review", "item_version": 12, "owner": "Acid3croco",
        "escalations": 3,
        "deadline": dt.datetime(2026, 8, 8, 9, 30), "text": "On garde ?",
        "options": ["merger", "abandonner"],
    }
    return web._questions_page([question], by="web", token="t", flash=None,
                               beat=dt.datetime.now(dt.timezone.utc))


def main() -> None:
    # 1. le sujet GitHub est un lien, href complet
    assert web._subject("gh:Acid3croco/graphatom#27") == (
        f"<a href='{ISSUE_URL}'>gh:Acid3croco/graphatom#27</a>")
    assert f"href='{ISSUE_URL}'" in question_page()
    print(f"1. sujet gh → {ISSUE_URL} ✓")

    # 2. ce qui n'est pas un sujet GitHub reste du texte brut
    for plain in ("pipeline-x:oom", "gh:Acid3croco/graphatom#abc",
                  "gh:graphatom#27", "voir gh:a/b#1 plus tard"):
        assert web._subject(plain) == plain, plain
    assert web._subject("<b>&") == "&lt;b&gt;&amp;"
    print("2. sujet quelconque : texte brut, échappé ✓")

    # 3. la PR vient de release.json, écrit par le nœud release
    #    Le répertoire déplacé est celui du résolveur, `blocks.DATA_DIR` — comme
    #    dans les autres tests. Poser un `web.DATA_DIR` que le module ne définit
    #    pas fabriquait la constante au lieu de la vérifier : le test passait et
    #    la page d'item rendait 500 (NameError).
    assert not hasattr(web, "DATA_DIR"), "web lit le workspace par item_workspace()"
    with tempfile.TemporaryDirectory() as tmp:
        blocks.DATA_DIR = Path(tmp)  # le seul répertoire de données, celui des blocs
        workspace = blocks.item_workspace(14)
        workspace.mkdir()
        assert web._pr(14) == ""  # pas de release : rien à dire

        release = {"pr_number": 31, "pr_url": PR_URL, "merge_sha": "a1b2c3d4e5f6"}
        (workspace / "release.json").write_text(json.dumps(release))
        assert web._pr(14) == f" · <a href='{PR_URL}'>PR #31</a> (mergée a1b2c3d)", web._pr(14)

        (workspace / "release.json").write_text(json.dumps({"pr_number": 31}))
        assert web._pr(14) == ""  # une release sans URL n'invente pas de lien

        (workspace / "release.json").write_text("{pas du json")
        assert "illisible" in web._pr(14)  # le dire, sans emporter la page
    print(f"3. release.json → {PR_URL} ✓")

    # 4. la table des items, dans le DOM rendu : le numéro vers GitHub, le
    #    titre vers la page de l'item
    page = web._items_page(
        FakeConn(
            [item_row(14, "gh:Acid3croco/graphatom#27", TITRE),
             item_row(9, "pipeline-x:oom", None)],
            [{"item_id": 14, "result": {"usage": {"total_cost_usd": 0.0123}}},
             {"item_id": 14, "result": {"usage": {"total_cost_usd": 0.0045}}},
             {"item_id": 9, "result": {"usage": {"input_tokens": 10}}}]),
        dt.datetime.now(dt.timezone.utc))
    assert "<th>issue</th><th>titre</th>" in page, page
    assert f"<a href='{ISSUE_URL}'>#27</a>" in page, page
    assert f"<a href='/item/14'>{web._e(TITRE)}</a>" in page, page
    assert TITRE not in page, "le titre est échappé, jamais rendu tel quel"
    # le sujet d'un autre canal : deux cellules vides, et la ligne tient
    assert "<td></td><td></td>" in page, page
    assert "pipeline-x:oom" not in page, "aucun titre inventé pour un autre canal"
    assert "<th>coût rapporté $</th><th>coût API estimé $</th>" in page, page
    assert "<td>0.0168</td>" in page, page
    assert "<td>g1</td><td>—</td><td>—</td>" in page, page
    print("4. /items : #27 → GitHub, titre → /item/14, autre canal → vide ✓")

    # 5. la question dit de quoi il s'agit — le titre, pas seulement le numéro
    page = question_page(TITRE)
    assert f"« {web._e(TITRE)} »" in page, page
    assert "item 14" in page and TITRE not in page, page
    assert "« " not in question_page(), "sans titre, la question n'invente rien"
    print("5. question : le titre de l'item à côté de son numéro ✓")

    print("\nliens : OK — l'issue, la PR et le titre sont lisibles depuis le frontend")


if __name__ == "__main__":
    main()
