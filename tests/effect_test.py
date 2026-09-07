"""Le bloc EFFECT : l'intention commise avant l'acte, l'acte jamais rejoué.

Sur un Postgres jetable, un graph de deux nœuds EFFECT admis et réservé
comme le ferait l'ordonnanceur ; la cible du stub est l'outbox du bureau,
sous `workspace.DATA_DIR` — la seule racine que le test déplace :

  1. au moment où le bloc touche le monde, son intention est déjà commise —
     lisible d'une autre session, pas encore appliquée
  2. l'acte laisse une ligne à l'outbox : la clé logique, une tabulation,
     l'intention ; le résultat porte l'op_id, et la ligne est dite appliquée
  3. rejouer un effet appliqué ne touche plus le monde : même op_id, une
     seule ligne, aucun travail simulé
  4. l'outbox déjà écrite mais l'intention pas encore marquée — la panne
     entre les deux — : rien n'est réécrit, et le marquage se fait
  5. la panne AU marquage — l'acte fait, le marquage qui lève — : la reprise
     par une connexion neuve ne rejoue pas l'acte, l'outbox garde une seule
     ligne, l'intention finit `applied`
  6. deux clés logiques dont l'une préfixe l'autre (`…:publie`, `…:publie2`) :
     les deux sont écrites, la seconde n'est pas prise pour déjà appliquée —
     l'outbox se relit par clé entière, avant la tabulation

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

from outils import data_dir_jetable, etat, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-effect")

from graphatom import blocks, context, db, graph, kernel  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

from bords_outils import patche  # noqa: E402

GRAPHE = f"effet-{uuid.uuid4().hex[:8]}"
BUNDLE = {
    "name": GRAPHE,
    "entry": "publie",
    "budgets": {"escalations": 1, "wall_deadline_hours": 1},
    "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
    "nodes": {
        "publie": {"block": "EFFECT", "config": {"intent": "annonce"},
                   "edges": {"applied": "publie2"}},
        "publie2": {"block": "EFFECT", "config": {"intent": "suite"},
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


def admettre(conn, revision: str) -> tuple[int, str]:
    sujet = f"effet:{uuid.uuid4().hex[:8]}"
    return kernel.admit(conn, revision, sujet, _allow_parallel_for_test=True), sujet


def contexte(conn, item_id: int, sujet: str, run: dict | None = None):
    """Le contexte du run sur le nœud courant — réservé ici, ou repris tel quel —,
    la sonde à la place de l'acte, et la clé logique que le bloc va former."""
    run = run or kernel.claim(conn, item_id)
    item = etat(conn, item_id)
    ctx = context.Context(conn, run, item, BUNDLE["nodes"][item["state"]], BUNDLE)
    cle = f"{GRAPHE}:{sujet}:{item['state']}"
    ctx.simulate_work = Sonde(cle)
    return ctx, ctx.simulate_work, cle


def ecrites(outbox: Path, cle: str) -> int:
    """Les lignes de l'outbox dont la clé — avant la tabulation — est exactement `cle`."""
    return sum(ligne.split("\t", 1)[0] == cle for ligne in outbox.read_text().splitlines())


def observation(conn, op_id: int) -> str:
    return conn.execute("SELECT observation FROM effect WHERE op_id = %s",
                        (op_id,)).fetchone()["observation"]


def acte(conn, outbox: Path, ctx, sonde: Sonde, cle: str) -> dict:
    """1 et 2. l'intention commise avant l'acte ; l'acte laisse sa ligne et son op_id."""
    rendu = blocks.effect(ctx)
    assert sonde.vues == ["not_attempted"], sonde.vues
    print("1. à l'instant de l'acte, l'intention est commise et pas encore "
          "appliquée — vue d'une autre session ✓")

    assert rendu["outcome"] == "applied", rendu
    assert outbox.read_text() == f"{cle}\t{{'intent': 'annonce'}}\n"
    ligne = conn.execute("SELECT * FROM effect WHERE op_id = %s", (rendu["op_id"],)).fetchone()
    assert ligne["observation"] == "applied", ligne
    assert ligne["run_id"] == ctx.run["id"] and ligne["item_id"] == ctx.item["id"]
    print("2. une ligne à l'outbox « clé\\tintention », op_id rendu, "
          "intention marquée appliquée ✓")
    return rendu


def rejeu(conn, outbox: Path, ctx, sonde: Sonde, cle: str, rendu: dict) -> None:
    """3 et 4. rejouer un effet appliqué, puis un effet écrit mais pas marqué."""
    assert blocks.effect(ctx) == rendu
    assert len(sonde.vues) == 1, "le monde a été touché une seconde fois"
    assert ecrites(outbox, cle) == 1
    print("3. rejeu d'un effet appliqué : même op_id, une seule ligne, aucun acte ✓")

    conn.execute("UPDATE effect SET observation = 'not_attempted' WHERE op_id = %s",
                 (rendu["op_id"],))
    assert blocks.effect(ctx) == rendu
    assert len(sonde.vues) == 1, "l'acte a été rejoué sur une cible déjà écrite"
    assert ecrites(outbox, cle) == 1
    assert observation(conn, rendu["op_id"]) == "applied"
    print("4. panne entre l'acte et le marquage : la cible relue, rien réécrit, "
          "marquage rattrapé ✓")


def panne_au_marquage(revision: str, outbox: Path) -> None:
    """5. le marquage lève après l'acte ; une connexion neuve reprend sans rejouer."""
    def leve(*_args, **_kwargs):
        raise RuntimeError("panne au marquage")

    with db.connect() as conn:
        item_id, sujet = admettre(conn, revision)
        ctx, sonde, cle = contexte(conn, item_id, sujet)
        with patche(blocks.effects, "mark_applied", leve):
            try:
                blocks.effect(ctx)
            except RuntimeError as exc:
                assert "panne au marquage" in str(exc), exc
            else:
                raise AssertionError("le marquage n'a pas levé")
        assert sonde.vues == ["not_attempted"] and ecrites(outbox, cle) == 1
        op_id = conn.execute("SELECT op_id FROM effect WHERE logical_key = %s",
                             (cle,)).fetchone()["op_id"]
        assert observation(conn, op_id) == "not_attempted", "marqué malgré la panne"
        run = ctx.run
    with db.connect() as neuve:
        ctx, sonde, _ = contexte(neuve, item_id, sujet, run)
        assert blocks.effect(ctx) == {"outcome": "applied", "op_id": op_id}
        assert sonde.vues == [], "l'acte a été rejoué à la reprise"
        assert ecrites(outbox, cle) == 1
        assert observation(neuve, op_id) == "applied"
    print("5. panne au marquage : reprise par une connexion neuve, une seule ligne, "
          "aucun acte de plus, intention appliquée ✓")


def cles_prefixes(conn, revision: str, outbox: Path) -> None:
    """6. `…:publie` puis `…:publie2` : la seconde clé n'est pas prise pour la première."""
    item_id, sujet = admettre(conn, revision)
    ctx, _, cle = contexte(conn, item_id, sujet)
    kernel.apply(conn, ctx.run["id"], blocks.effect(ctx))
    assert etat(conn, item_id)["state"] == "publie2"
    suite, sonde, cle2 = contexte(conn, item_id, sujet)
    assert cle2.startswith(cle) and cle2 != cle, (cle, cle2)
    rendu = blocks.effect(suite)
    assert sonde.vues == ["not_attempted"], "la clé préfixée a été prise pour appliquée"
    assert ecrites(outbox, cle) == 1 and ecrites(outbox, cle2) == 1
    assert f"{cle2}\t{{'intent': 'suite'}}\n" in outbox.read_text()
    assert observation(conn, rendu["op_id"]) == "applied"
    kernel.apply(conn, suite.run["id"], rendu)
    assert etat(conn, item_id)["terminal_at"] is not None
    print("6. clés préfixe l'une de l'autre : deux lignes, deux actes, deux "
          "intentions appliquées ✓")


def main() -> None:
    workdir = data_dir_jetable("graphatom-effect-")
    outbox = workdir / bureau.OUTBOX_NAME
    db.init_db()
    try:
        with db.connect() as conn:
            revision = graph.publish(conn, BUNDLE)
            item_id, sujet = admettre(conn, revision)
            ctx, sonde, cle = contexte(conn, item_id, sujet)
            rendu = acte(conn, outbox, ctx, sonde, cle)
            rejeu(conn, outbox, ctx, sonde, cle, rendu)
            panne_au_marquage(revision, outbox)
            cles_prefixes(conn, revision, outbox)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("\nbloc EFFECT : OK")


if __name__ == "__main__":
    main()
