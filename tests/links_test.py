"""Le test des liens du frontend : la boucle vers GitHub, dans les deux sens.

Scénario, sans base ni serveur — le rendu est pur :

  1. un sujet `gh:<owner>/<repo>#<num>` devient un lien vers l'issue,
     href complet, dans la page des questions comme ailleurs
  2. tout autre sujet reste du texte brut, échappé
  3. `release.json` du workspace donne le lien de la PR de l'item ;
     pas de fichier, ou pas d'URL dedans : pas de lien

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


def question_page() -> str:
    question = {
        "id": 5, "item_id": 14, "subject_key": "gh:Acid3croco/graphatom#27",
        "item_state": "review", "owner": "Acid3croco", "escalations": 3,
        "deadline": dt.datetime(2026, 8, 8, 9, 30), "text": "On garde ?",
        "options": ["merger", "abandonner"],
    }
    return web._questions_page([question], by="web", token="t", flash=None)


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

    print("\nliens : OK — l'issue et la PR sont atteignables depuis le frontend")


if __name__ == "__main__":
    main()
