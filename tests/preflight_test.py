"""Le préflight et l'incarnation : les deux gardes du démarrage du worker.

  1. un item actif épinglé sur une révision devenue invalide fait refuser
     le démarrage — bruyamment, en nommant l'item et le remède
  2. une base saine ne déclenche rien, et le préflight ignore les items
     terminaux, même épinglés sur une révision historique
  3. le jeton d'incarnation est stable entre deux connexions — une simple
     reconnexion n'est pas une récupération
  4. une table d'incarnation vidée (ce que fait une récupération sur crash,
     la table étant UNLOGGED) donne un jeton neuf : le worker peut nommer
     l'incident dans son journal

Hermétique : Postgres jetable auto-provisionné, aucun réseau.

Usage : uv run python tests/preflight_test.py
"""

import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-preflight")

from graphatom import db, graph, kernel, scheduler  # noqa: E402

BUNDLE = {
    "name": "preflight-test",
    "entry": "travail",
    "budgets": {"escalations": 1, "wall_deadline_hours": 1},
    "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
    "nodes": {
        "travail": {"block": "ACT", "config": {}, "edges": {"ok": "fini"}},
        "fini": {"terminal": True},
        "abandon": {"terminal": True},
    },
}

# la forme d'avant la rupture : agent sans execution — refusée à la
# publication, donc introduite par SQL, comme le ferait une vraie base
# d'avant la mise à jour
LEGACY = json.loads(json.dumps(BUNDLE))
LEGACY["nodes"]["travail"]["config"] = {
    "agent": {"cmd": "true", "prompt": "ancien contrat"},
}


def main() -> None:
    db.init_db()
    with db.connect() as conn:
        rev = graph.publish(conn, BUNDLE)
        item_id = kernel.admit(conn, rev, f"preflight:{uuid.uuid4().hex[:8]}")

        # 2. base saine : le préflight laisse passer
        scheduler._preflight(conn)
        print("2. révision valide : le préflight laisse démarrer ✓")

        # 1. la révision épinglée devient invalide : refus bruyant et nommé
        conn.execute("UPDATE graph_revision SET bundle = %s WHERE id = %s",
                     (json.dumps(LEGACY), rev))
        try:
            scheduler._preflight(conn)
        except SystemExit as exc:
            message = str(exc)
            assert f"item {item_id}" in message, message
            assert "republier" in message, message
        else:
            raise AssertionError("un item actif sur une révision invalide "
                                 "a laissé le rail démarrer")
        print(f"1. item {item_id} sur révision invalide → refus qui le nomme ✓")

        # 2 bis. le même item, terminal : l'histoire ne bloque pas le rail
        conn.execute("UPDATE work_item SET terminal_at = now() WHERE id = %s",
                     (item_id,))
        scheduler._preflight(conn)
        print("2 bis. item terminal sur révision historique : ignoré ✓")

        # 3. le jeton d'incarnation est stable entre deux connexions
        premier = db.incarnation(conn)
    with db.connect() as conn:
        second = db.incarnation(conn)
        assert premier == second, (premier, second)
        print("3. jeton d'incarnation stable à travers une reconnexion ✓")

        # 4. la table vidée — l'effet d'une récupération sur crash sur une
        # table UNLOGGED — donne un jeton neuf
        conn.execute("TRUNCATE database_incarnation")
        troisieme = db.incarnation(conn)
        assert troisieme[0] != second[0], "le jeton n'a pas changé"
        print("4. table d'incarnation vidée → jeton neuf, incident nommable ✓")

    print("\npréflight : OK — refus bruyant, jamais de stub muet")


if __name__ == "__main__":
    main()
