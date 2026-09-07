"""Le test de la base : le schéma, la migration sous verrou, et les gardes.

Sur un Postgres jetable — les deux premières épreuves n'y touchent pas :

  1. `schema.sql` se trouve depuis le dépôt, sinon depuis le répertoire
     courant ; absent des deux, l'erreur le nomme
  2. `drop_agent_db` refuse sans instance jetable, même sur un nom jetable
  3. une base neuve rend toute sa forme, colonne par colonne ; rejouer le
     schéma ne rend rien
  4. un verrou tenu par une autre session retarde la migration au lieu de la
     faire échouer : un essai de plus, puis le DDL passe
  5. sans battement, l'identité du worker est un triplet de None ; battu, il
     porte son SHA
  6. une révision inconnue lève, et nomme la révision

Usage : uv run python tests/db_schema_test.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from outils import ROOT, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-schema")
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

from graphatom import db, graph, heartbeat  # noqa: E402
from graphatom.bundle import GraphError  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="graphatom-schema-"))


def schema_depuis_le_cwd() -> None:
    installe, courant, vide = TMP / "site" / "lib" / "graphatom" / "db.py", TMP / "cwd", TMP / "vide"
    courant.mkdir()
    vide.mkdir()
    shutil.copy(ROOT / "schema.sql", courant / "schema.sql")
    garde, ici = db.__file__, os.getcwd()
    db.__file__ = str(installe)  # une installation : pas de dépôt au-dessus
    try:
        os.chdir(courant)
        assert db._schema() == courant / "schema.sql"
        os.chdir(vide)
        try:
            db._schema()
        except FileNotFoundError as exc:
            assert "schema.sql" in str(exc), exc
        else:
            raise AssertionError("un schéma absent a été trouvé")
    finally:
        db.__file__ = garde
        os.chdir(ici)
    assert db._schema() == ROOT / "schema.sql"
    print("1. schema.sql : depuis le dépôt, sinon le cwd, sinon nommé absent ✓")


def drop_sans_instance() -> None:
    garde = db.DSN
    db.DSN = "postgresql://graphatom@127.0.0.1/graphatom_test_item_1"
    try:
        db.drop_agent_db()
    except ValueError as exc:
        assert "GRAPHATOM_AGENT_DSN" in str(exc), exc
    else:
        raise AssertionError("droppé sans instance")
    finally:
        db.DSN = garde
    print("2. drop_agent_db sans instance jetable : refusé, nommément ✓")


def forme_rendue() -> None:
    changements = db.init_db(drop=True)
    assert changements and all(c.startswith("+ ") for c in changements), changements
    assert "+ work_item.id (bigint)" in changements, changements
    assert db.init_db() == [], "rejouer le schéma a changé la forme"
    print(f"3. base neuve : {len(changements)} colonnes rendues ; rejeu : rien ✓")


def migration_sous_verrou() -> None:
    pris, liberer = threading.Event(), threading.Event()

    def tenir() -> None:
        with psycopg.connect(db.DSN) as autre, autre.transaction():
            autre.execute("LOCK TABLE subject IN ACCESS EXCLUSIVE MODE")
            pris.set()
            liberer.wait(30)

    def attente(s: float) -> None:
        attentes.append(s)
        liberer.set()  # le bloqueur part pendant la reprise
        time.sleep(s)

    attentes: list[float] = []
    garde = db.LOCK_TIMEOUT, db.DDL_ATTENTE_S, db.time
    db.LOCK_TIMEOUT, db.DDL_ATTENTE_S = "200ms", 0.2
    db.time = SimpleNamespace(sleep=attente)
    tenant = threading.Thread(target=tenir, daemon=True)
    tenant.start()
    assert pris.wait(10), "le verrou n'a pas été pris"
    try:
        assert db.init_db() == []
    finally:
        db.LOCK_TIMEOUT, db.DDL_ATTENTE_S, db.time = garde
        liberer.set()
    tenant.join(10)
    assert attentes == [0.2], f"reprises : {attentes}"
    print("4. verrou tenu : une reprise, puis la migration passe ✓")


def identite_du_worker(conn) -> None:
    assert heartbeat.worker(conn) == {"at": None, "worker_sha": None,
                                      "worker_started_at": None}
    heartbeat.beat(conn, heartbeat.RAIL, "abc123", heartbeat.now())
    porte = heartbeat.worker(conn)
    assert porte["worker_sha"] == "abc123" and porte["at"] is not None, porte
    print("5. worker : triplet de None sans battement, son SHA après ✓")


def revision_inconnue(conn) -> None:
    try:
        graph.load_bundle(conn, "deadbeef")
    except GraphError as exc:
        assert "deadbeef" in str(exc), exc
    else:
        raise AssertionError("une révision inconnue a rendu un bundle")
    print("6. révision inconnue : levée, nommée ✓")


def main() -> None:
    try:
        schema_depuis_le_cwd()
        drop_sans_instance()
        forme_rendue()
        migration_sous_verrou()
        with db.connect() as conn:
            identite_du_worker(conn)
            revision_inconnue(conn)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\ndb_schema : OK")


if __name__ == "__main__":
    main()
