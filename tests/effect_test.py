"""Le bloc EFFECT : l'intention commise avant l'acte, l'acte jamais rejoué.

Sur un Postgres jetable, un graph d'un seul nœud EFFECT admis et réservé
comme le ferait l'ordonnanceur ; la cible du stub est l'outbox du bureau :

  1. au moment où le bloc touche le monde, son intention est déjà commise —
     lisible d'une autre session, pas encore appliquée
  2. l'acte laisse une ligne à l'outbox : la clé logique, une tabulation,
     l'intention ; le résultat porte l'op_id, et la ligne est dite appliquée
  3. rejouer un effet appliqué ne touche plus le monde : même op_id, une
     seule ligne, aucun travail simulé
  4. l'outbox déjà écrite mais l'intention pas encore marquée — la panne
     entre les deux — : rien n'est réécrit, et le marquage se fait

Usage : uv run python tests/effect_test.py
"""

import os
import shutil
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import data_dir_jetable, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-effect")

from graphatom import blocks, context, db, graph, kernel  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

GRAPHE = f"effet-{uuid.uuid4().hex[:8]}"
BUNDLE = {
    "name": GRAPHE,
    "entry": "publie",
    "budgets": {"escalations": 1, "wall_deadline_hours": 1},
    "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
    "nodes": {
        "publie": {"block": "EFFECT", "config": {"intent": "annonce"},
                   "edges": {"applied": "fini"}},
        "fini": {"terminal": True},
        "abandon": {"terminal": True},
    },
}


class Sonde:
    """Ce qu'une autre session voit de l'intention au moment de l'acte."""

    def __init__(self, cle: str):
        self.cle = cle
        self.vues: list[str | None] = []

    def __call__(self) -> None:  # à la place de `simulate_work`, juste avant l'acte
        with psycopg.connect(db.DSN, row_factory=dict_row) as autre:
            ligne = autre.execute(
                "SELECT observation FROM effect WHERE logical_key = %s", (self.cle,)
            ).fetchone()
        self.vues.append(ligne["observation"] if ligne else None)


def main() -> None:
    workdir = data_dir_jetable("graphatom-effect-")
    blocks.DATA_DIR = bureau.DATA_DIR  # le bloc a figé la racine à l'import
    outbox = workdir / bureau.OUTBOX_NAME
    db.init_db()
    try:
        with db.connect() as conn:
            revision = graph.publish(conn, BUNDLE)
            sujet = f"effet:{uuid.uuid4().hex[:8]}"
            item_id = kernel.admit(conn, revision, sujet, _allow_parallel_for_test=True)
            run = kernel.claim(conn, item_id)
            item = conn.execute("SELECT * FROM work_item WHERE id = %s",
                                (item_id,)).fetchone()
            ctx = context.Context(conn, run, item, BUNDLE["nodes"]["publie"], BUNDLE)
            cle = f"{GRAPHE}:{sujet}:publie"
            sonde = Sonde(cle)
            ctx.simulate_work = sonde

            rendu = blocks.effect(ctx)
            assert sonde.vues == ["not_attempted"], sonde.vues
            print("1. à l'instant de l'acte, l'intention est commise et pas encore "
                  "appliquée — vue d'une autre session ✓")

            assert rendu["outcome"] == "applied", rendu
            assert outbox.read_text() == f"{cle}\t{{'intent': 'annonce'}}\n"
            ligne = conn.execute("SELECT * FROM effect WHERE op_id = %s",
                                 (rendu["op_id"],)).fetchone()
            assert ligne["observation"] == "applied", ligne
            assert ligne["run_id"] == run["id"] and ligne["item_id"] == item_id
            print("2. une ligne à l'outbox « clé\\tintention », op_id rendu, "
                  "intention marquée appliquée ✓")

            rejoue = blocks.effect(ctx)
            assert rejoue == rendu, (rejoue, rendu)
            assert len(sonde.vues) == 1, "le monde a été touché une seconde fois"
            assert outbox.read_text().count(cle) == 1
            print("3. rejeu d'un effet appliqué : même op_id, une seule ligne, "
                  "aucun acte ✓")

            conn.execute("UPDATE effect SET observation = 'not_attempted' "
                         "WHERE op_id = %s", (rendu["op_id"],))
            repris = blocks.effect(ctx)
            assert repris == rendu, repris
            assert len(sonde.vues) == 1, "l'acte a été rejoué sur une cible déjà écrite"
            assert outbox.read_text().count(cle) == 1
            assert conn.execute("SELECT observation FROM effect WHERE op_id = %s",
                                (rendu["op_id"],)).fetchone()["observation"] == "applied"
            print("4. panne entre l'acte et le marquage : la cible relue, rien "
                  "réécrit, marquage rattrapé ✓")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("\nbloc EFFECT : OK")


if __name__ == "__main__":
    main()
