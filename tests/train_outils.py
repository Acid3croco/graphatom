"""L'outillage de la porte du train : la base jetable, les graphs, les gestes.

Ce module n'est pas un test — c'est ce que `train_test.py` a sous la main
pour rester lisible : les graphs minimaux qu'il fait traverser, et les
quelques gestes qu'un test du noyau répète (admettre, réserver, appliquer,
lire le journal, attendre un fait).

Il DOIT être importé avant `graphatom` : c'est ici que GRAPHATOM_DSN est
épinglé sur la base jetable, et `db.DSN` se fige à l'import.
"""

import atexit
import os
import shutil
import sys
import tempfile
import time
import uuid

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from outils import bundle_epreuve, etat, provision_postgres  # noqa: E402

__all__ = [
    "BUNDLE_TRAIN", "admet", "attendre", "avance", "bail_expire",
    "bundle_commande", "bundle_kernel", "execute_bloc", "issue_du_run",
    "journal", "journal_contigu", "libere_la_voie", "sujet", "termine",
    "tick_jusqu_a",
]

# Jamais la base que GRAPHATOM_DSN désigne : la porte migre, termine les
# items et libère la voie — sur une production, ce serait un dégât. Seule
# une base explicitement donnée POUR la porte est réutilisée.
os.environ["GRAPHATOM_DSN"] = (os.environ.get("GRAPHATOM_TRAIN_DSN")
                               or provision_postgres("graphatom-train"))

from graphatom import blocks, graph, kernel, scheduler  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402
from graphatom.context import Context  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="graphatom-train-"))
bureau.DATA_DIR = TMP  # noqa — les workspaces du test, jamais ceux du repo
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

# --------------------------------------------------------------- les graphs
#
# Tous les graphs de la porte finissent par l'escalade commune des épreuves
# (`outils.bundle_epreuve`) : une question à l'humain dont `retry` renvoie au
# travail, et deux terminaux. Le budget d'escalades de la porte est de 3.

def _graph_travail(name: str, config: dict, escalations: int = 3) -> dict:
    """Un nœud de travail ACT, puis l'escalade et ses terminaux."""
    return bundle_epreuve(name, {
        "travail": {"block": "ACT", "config": config, "edges": {"ok": "fini"}},
    }, escalations=escalations, owner="train")


def bundle_kernel(escalations: int = 3) -> dict:
    """Le graph minimal des transitions : un nœud de travail, une escalade."""
    return _graph_travail("train-kernel", {}, escalations)


def bundle_commande(cmd: str, *, timeout_s: float = 30, silence_s: float | None = None,
                    lease_s: float | None = None) -> dict:
    """Un graph dont le nœud d'entrée exécute une vraie commande shell."""
    config: dict = {"execution": {"kind": "command", "cmd": cmd,
                                  "timeout_s": timeout_s}}
    if silence_s is not None:
        config["execution"]["silence_s"] = silence_s
    if lease_s is not None:
        config["lease_s"] = lease_s
    return _graph_travail("train-commande", config)


# le train entier : FETCH → ACT → JUDGE → WAIT, tous en stub
BUNDLE_TRAIN = bundle_epreuve("train-complet", {
    "ingest": {"block": "FETCH", "config": {"materialize": ["brief"]},
               "edges": {"ok": "travail"}},
    "travail": {"block": "ACT", "config": {"duration_s": 0},
                "edges": {"ok": "verif"}},
    "verif": {"block": "JUDGE", "escalade": True,
              "config": {"stub_outcome": "pass"},
              "edges": {"pass": "attend", "fail": "travail"}},
    "attend": {
        "block": "WAIT",
        "config": {"question": "On livre ?", "options": ["go", "stop"],
                   "owner": "train", "deadline_minutes": 60},
        "edges": {"go": "fini", "stop": "abandon", "expired": "abandon"},
    },
}, entry="ingest", escalations=3, owner="train")


# --------------------------------------------------------------- les gestes

def sujet() -> str:
    return f"train:{uuid.uuid4().hex[:10]}"


def journal(conn, item_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version",
        (item_id,)).fetchall()


def journal_contigu(conn, item_id: int) -> None:
    versions = [e["item_version"] for e in journal(conn, item_id)]
    assert versions == list(range(1, len(versions) + 1)), \
        f"journal troué : {versions}"


def issue_du_run(conn, run_id: int) -> dict:
    """La ligne `node_run` du run : son statut, son issue, son résultat."""
    return conn.execute(
        "SELECT status, outcome, result FROM node_run WHERE id = %s",
        (run_id,)).fetchone()


def bail_expire(conn, run_id: int) -> bool:
    return conn.execute(
        "SELECT lease_expires_at < now() AS mort FROM node_run WHERE id = %s",
        (run_id,)).fetchone()["mort"]


def avance(conn, item_id: int, submitted: dict) -> dict:
    run = kernel.claim(conn, item_id)
    assert run is not None, "plus rien à réserver"
    kernel.apply(conn, run["id"], submitted)
    return etat(conn, item_id)


def termine(conn, item_id: int) -> None:
    """Range l'item hors de la voie pour libérer l'admission suivante."""
    conn.execute(
        "UPDATE work_item SET terminal_at = now() WHERE id = %s", (item_id,))


def libere_la_voie(conn) -> None:
    """Termine tout item encore actif : la voie unique doit être libre."""
    conn.execute("UPDATE work_item SET terminal_at = now() "
                 "WHERE terminal_at IS NULL")


def admet(conn, bundle: dict) -> int:
    rev = graph.publish(conn, bundle)
    return kernel.admit(conn, rev, sujet())


def attendre(predicat, seconds: float = 30.0):
    limite = time.monotonic() + seconds
    while time.monotonic() < limite:
        valeur = predicat()
        if valeur:
            return valeur
        time.sleep(0.05)
    raise AssertionError("attente dépassée")


def execute_bloc(conn, item_id: int, run: dict) -> str:
    """Ce que fait l'ordonnanceur : le bloc, puis apply — sans thread."""
    item = etat(conn, item_id)
    bundle = graph.load_bundle(conn, item["revision"])
    node = bundle["nodes"][run["node"]]
    ctx = Context(conn, run, item, node, bundle)
    try:
        result = blocks.BLOCKS[node["block"]](ctx)
    except Exception as exc:  # le bloc peut échouer, jamais router
        result = {"outcome": "crashed", "error": str(exc)}
    return kernel.apply(conn, run["id"], result)


def tick_jusqu_a(conn, item_id: int, etat_vise: str, seconds: float = 30.0):
    """Fait battre l'ordonnanceur jusqu'à ce que l'item atteigne l'état visé."""
    def _pret():
        scheduler.tick(conn)
        item = etat(conn, item_id)
        return item if item["state"] == etat_vise else None
    return attendre(_pret, seconds)
