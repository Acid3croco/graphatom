"""Le test du nœud solo : la voie entière, sans tentative d'attente.

Sur une base jetable, le dispatch réel et un exécuteur factice vérifient les
deux sens du verrou : un solo attend les runs déjà en vol, puis aucun autre
item ne part sous lui. L'attente ne crée aucune ligne `node_run` ; elle ne
pose donc ni bail ni numéro de tentative. Le tick rend ce qu'il a fait : 1
par run réservé, et 0 — le dispatch aussi — tant que le solo vole.

Usage : uv run python tests/solo_test.py
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, kernel, scheduler  # noqa: E402

from outils import admettre, bundle_epreuve, data_dir_jetable, runs_de  # noqa: E402

INSTANCE = os.environ.get("GRAPHATOM_AGENT_DSN") or db.DSN
os.environ["GRAPHATOM_AGENT_DSN"] = INSTANCE
ITEM_ID = 940000 + os.getpid() % 10000


def bundle(solo: bool) -> dict:
    """Un ACT qui garde son run en vol jusqu'à ce que le test le rende."""
    return bundle_epreuve(f"solo-{solo}", {
        "travail": {
            "block": "ACT",
            "config": {"duration_s": 0, "lease_s": 600, "solo": solo},
            "edges": {"ok": "fini"},
        },
    })


def seme(conn, solo: bool) -> int:
    """Un item de plus sur la voie, solo ou non."""
    return admettre(conn, bundle(solo), "solo")


def rendre(conn, item_id: int) -> None:
    """Applique tous les runs en vol d'un item, sans attendre un thread."""
    for run in conn.execute(
        "SELECT id FROM node_run WHERE item_id = %s AND status = 'running'",
        (item_id,),
    ).fetchall():
        kernel.apply(conn, run["id"], {"outcome": "ok"})


def main() -> None:
    workdir = data_dir_jetable("graphatom-solo-")
    os.environ["GRAPHATOM_REPO_DIR"] = str(workdir / "sans-depot")
    garde = (db.DSN, scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM,
             scheduler.MAX_ACTIVE_ITEMS, scheduler._execute)
    dsn = db.agent_dsn(ITEM_ID)
    assert dsn, "aucune instance jetable : impossible de tester le dispatch"
    db.DSN = dsn
    scheduler.MAX_RUNS = scheduler.MAX_RUNS_PER_ITEM = 8
    scheduler.MAX_ACTIVE_ITEMS = 8
    scheduler._execute = lambda run_id, item_id: None
    try:
        db.init_db()
        with db.connect() as conn:
            ordinaire = seme(conn, False)
            seul = seme(conn, True)

            assert scheduler.tick(conn) == 1, "un run réservé compte 1"
            assert len(runs_de(conn, ordinaire)) == 1
            assert runs_de(conn, seul) == [], \
                "le solo attendu ne doit avoir ni bail ni tentative"
            assert scheduler.etat_solo(conn) == {"running": 0, "waiting": 1}
            print("1. un run ordinaire vole : le solo attend sans node_run ✓")

            rendre(conn, ordinaire)
            assert scheduler.tick(conn) == 1, "le solo réservé compte 1"
            solo_runs = runs_de(conn, seul)
            assert len(solo_runs) == 1 and solo_runs[0]["attempt"] == 1
            assert scheduler.etat_solo(conn) == {"running": 1, "waiting": 0}
            print("2. voie vide : le solo part à sa première tentative ✓")

            voisin = seme(conn, False)
            assert scheduler._dispatch(conn) == 0, "un solo en vol, et le dispatch compte"
            assert scheduler.tick(conn) == 0, "un solo en vol, et le tick compte"
            assert runs_de(conn, voisin) == [], \
                "un voisin ne doit avoir ni bail ni tentative sous un solo"
            assert scheduler.en_vol(conn) == 1
            print("3. le solo vole : aucun voisin n'est réservé, tick et dispatch rendent 0 ✓")

            rendre(conn, seul)
            assert scheduler.tick(conn) == 1, "le voisin réservé compte 1"
            voisin_runs = runs_de(conn, voisin)
            assert len(voisin_runs) == 1 and voisin_runs[0]["attempt"] == 1
            print("4. le solo rendu : le voisin part à sa première tentative ✓")
    finally:
        db.drop_agent_db()
        (db.DSN, scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM,
         scheduler.MAX_ACTIVE_ITEMS, scheduler._execute) = garde
        shutil.rmtree(workdir, ignore_errors=True)

    print("\nsolo : OK — le nœud court seul, et l'attente ne coûte rien")


if __name__ == "__main__":
    main()
