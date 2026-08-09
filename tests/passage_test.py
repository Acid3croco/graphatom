"""Le test du passage : un retry d'escalade redonne pleine marge aux nœuds.

Scénario, sur la base mais sans ordonnanceur — seuls claim/apply/apply_item
sont en jeu. Un graph minuscule dont le nœud de travail crashe toujours :

  1. le passage 1 brûle MAX_ATTEMPTS tentatives, puis escalade
  2. la réponse humaine `retry` ouvre le passage 2
  3. le passage 2 repart à la tentative 1, avec MAX_ATTEMPTS de marge
  4. les tentatives du passage 1 sont toujours là — l'histoire n'est pas réécrite
  5. le budget d'escalades, lui, ne se régénère jamais : il a bien débité deux fois

Puis un second graph, où un nœud d'escalade est sur le chemin nominal :

  6. budget zéro, première visite du nœud d'escalade : l'item entre quand même
  7. re-entrée dans le même nœud, même passage : `budget_exhausted`
  8. avec du budget, c'est bien la re-entrée qui débite, pas la traversée

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

from outils import etat  # noqa: E402

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


def bundle_nominal(escalations: int) -> dict:
    """Un graph où un nœud d'escalade non-`WAIT` est sur le chemin nominal.

    `verif` porte `escalade` parce que son arête `fail` referme le cycle
    `verif → travail → verif` — exactement la forme du `validate` de
    `code-task`. Le traverser une fois n'est pas boucler.
    """
    return {
        "name": "passage-test-nominal",
        "entry": "travail",
        "budgets": {"escalations": escalations, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {"block": "ACT", "config": {}, "edges": {"ok": "verif"}},
            "verif": {"block": "JUDGE", "escalade": True, "config": {},
                      "edges": {"pass": "fini", "fail": "travail"}},
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


def avance(conn, item_id: int, outcome: str) -> dict:
    """Réserve le nœud courant, y applique l'issue, rend l'état de l'item."""
    run = kernel.claim(conn, item_id)
    assert run is not None, "plus rien à réserver"
    kernel.apply(conn, run["id"], {"outcome": outcome})
    return etat(conn, item_id)


def derniere_issue(conn, item_id: int) -> str:
    return conn.execute(
        "SELECT outcome FROM event WHERE item_id = %s "
        "ORDER BY item_version DESC LIMIT 1", (item_id,)
    ).fetchone()["outcome"]


def chemin_nominal(conn) -> None:
    """Le budget paie les tours de boucle, pas la traversée du chemin."""
    # 6-7. budget zéro : la traversée passe, la boucle non
    rev = graph.publish(conn, bundle_nominal(escalations=0))
    item_id = kernel.admit(
        conn, rev, f"nominal-0:{uuid.uuid4().hex[:8]}", _allow_parallel_for_test=True
    )
    item = avance(conn, item_id, "ok")
    assert item["state"] == "verif", item["state"]
    assert item["escalations"] == 0, item["escalations"]
    print(f"6. item {item_id}, budget 0 : il entre quand même dans le nœud "
          "d'escalade nominal ✓")

    avance(conn, item_id, "fail")  # verif → travail, même passage
    item = avance(conn, item_id, "ok")  # travail → verif : cette fois, c'est un tour
    assert item["state"] == "abandon", item["state"]
    assert derniere_issue(conn, item_id) == "budget_exhausted"
    print("7. re-entrée dans le même nœud, même passage → "
          "budget_exhausted → abandon ✓")

    # 8. avec du budget, c'est bien la re-entrée qui débite
    rev = graph.publish(conn, bundle_nominal(escalations=1))
    item_id = kernel.admit(
        conn, rev, f"nominal-1:{uuid.uuid4().hex[:8]}", _allow_parallel_for_test=True
    )
    item = avance(conn, item_id, "ok")
    assert item["escalations"] == 1, "la première visite est gratuite"
    avance(conn, item_id, "fail")
    item = avance(conn, item_id, "ok")
    assert item["state"] == "verif", item["state"]
    assert item["escalations"] == 0, "la re-entrée débite"
    print(f"8. item {item_id} : traversée gratuite, re-entrée débitée, "
          "1 → 0 escalade ✓")


def main() -> None:
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    with db.connect() as conn:
        rev = graph.publish(conn, BUNDLE)
        item_id = kernel.admit(
            conn, rev, f"passage:{uuid.uuid4().hex[:8]}", _allow_parallel_for_test=True
        )
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

        chemin_nominal(conn)

    print("\npassage : OK — un retry rend la marge des nœuds, pas le budget ;"
          "\n          et le budget paie les boucles, pas le chemin nominal")


if __name__ == "__main__":
    main()
