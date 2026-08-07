"""Le test du passage : un retry d'escalade redonne pleine marge aux nœuds.

Scénario, sur la base mais sans ordonnanceur — seuls claim/apply/apply_item
sont en jeu. Un graph minuscule dont le nœud de travail crashe toujours :

  1. le passage 1 brûle MAX_ATTEMPTS tentatives, puis escalade
  2. la réponse humaine `retry` ouvre le passage 2
  3. le passage 2 repart à la tentative 1, avec MAX_ATTEMPTS de marge
  4. les tentatives du passage 1 sont toujours là — l'histoire n'est pas réécrite
  5. le budget d'escalades, lui, ne se régénère jamais : il a bien débité deux fois

Le test ne détruit rien : il crée son sujet, ses items, et laisse la base
en place — il peut tourner à côté d'un rail vivant.

Usage : uv run python tests/passage_test.py
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, graph, kernel  # noqa: E402
from graphatom.kernel import MAX_ATTEMPTS  # noqa: E402

BUNDLE = {
    "name": "passage-test",
    "entry": "travail",
    "budgets": {"escalations": 3, "wall_deadline_hours": 1},
    "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
    "nodes": {
        "travail": {"block": "ACT", "config": {}, "edges": {"ok": "fini"}},
        "escalate": {
            "block": "WAIT",
            "escalade": True,
            "config": {"question": "On retente ?", "options": ["retry"],
                       "owner": "test", "deadline_minutes": 60},
            "edges": {"retry": "travail", "expired": "abandon"},
        },
        "fini": {"terminal": True},
        "abandon": {"terminal": True},
    },
}


def brule_le_passage(conn, item_id: int, cycle: int) -> None:
    """Fait crasher le nœud de travail jusqu'à l'escalade, et compte."""
    for attendu in range(1, MAX_ATTEMPTS + 1):
        run = kernel.claim(conn, item_id)
        assert run is not None, f"passage {cycle} : plus rien à réserver"
        assert run["cycle"] == cycle, run["cycle"]
        assert run["attempt"] == attendu, (run["attempt"], attendu)
        kernel.apply(conn, run["id"], {"outcome": "crashed"})


def etat(conn, item_id: int) -> dict:
    return conn.execute(
        "SELECT * FROM work_item WHERE id = %s", (item_id,)
    ).fetchone()


def main() -> None:
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    with db.connect() as conn:
        rev = graph.publish(conn, BUNDLE)
        item_id = kernel.admit(conn, rev, f"passage:{uuid.uuid4().hex[:8]}")
        print(f"révision {rev[:12]}…, item {item_id}")

        # 1. le premier passage brûle sa marge, puis escalade
        brule_le_passage(conn, item_id, cycle=1)
        item = etat(conn, item_id)
        assert item["state"] == "escalate", item["state"]
        assert item["cycle"] == 1, item["cycle"]
        assert item["escalations"] == 2, item["escalations"]
        print(f"1. passage 1 : {MAX_ATTEMPTS} tentatives brûlées → escalate, "
              f"escalades restantes {item['escalations']} ✓")

        # 2. la réponse humaine d'escalade ouvre un passage
        kernel.apply_item(conn, item_id, "retry", kind="answer")
        item = etat(conn, item_id)
        assert item["state"] == "travail", item["state"]
        assert item["cycle"] == 2, item["cycle"]
        assert item["escalations"] == 2, "le retry ne rend pas d'escalade"
        print(f"2. retry → passage {item['cycle']}, budget d'escalades intact ✓")

        # 3. le nouveau passage repart à 1 et dispose de toute la marge
        brule_le_passage(conn, item_id, cycle=2)
        item = etat(conn, item_id)
        assert item["state"] == "escalate", item["state"]
        print(f"3. passage 2 : {MAX_ATTEMPTS} tentatives de nouveau, "
              "puis escalade ✓")

        # 4. les tentatives des deux passages cohabitent en base
        runs = conn.execute(
            "SELECT cycle, attempt FROM node_run WHERE item_id = %s AND node = 'travail' "
            "ORDER BY cycle, attempt", (item_id,)
        ).fetchall()
        attendu = [(c, a) for c in (1, 2) for a in range(1, MAX_ATTEMPTS + 1)]
        assert [(r["cycle"], r["attempt"]) for r in runs] == attendu, runs
        print(f"4. {len(runs)} tentatives en base, passages 1 et 2 intacts ✓")

        # 5. le budget d'escalades ne se régénère pas : deux escalades, deux débits
        assert item["escalations"] == 1, item["escalations"]
        print(f"5. escalades restantes {item['escalations']} — "
              "jamais régénérées ✓")

    print("\npassage : OK — un retry rend la marge des nœuds, pas le budget")


if __name__ == "__main__":
    main()
