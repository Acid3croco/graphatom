"""Le test de la base : le schéma, la migration sous verrou, et les gardes.

Sur un Postgres jetable — les deux premières épreuves n'y touchent pas :

  1. `schema.sql` se trouve depuis le dépôt — même depuis un répertoire
     courant étranger, avec ou sans copie — sinon depuis le répertoire
     courant ; absent des deux, l'erreur le nomme
  2. `drop_agent_db` refuse sans instance jetable, même sur un nom jetable
  3. une base neuve rend toute sa forme, colonne par colonne ; rejouer le
     schéma ne rend rien
  4. un verrou tenu par une autre session retarde la migration au lieu de la
     faire échouer : un essai de plus, puis le DDL passe
  5. un verrou tenu jusqu'au bout : `DDL_ESSAIS - 1` reprises exactement,
     puis l'erreur nomme le bloqueur et sa requête
  6. sans battement, l'identité du worker est un triplet de None ; battu, il
     porte son SHA
  7. une révision inconnue lève, et nomme la révision

Usage : uv run python tests/db_schema_test.py
"""

import contextlib
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
REPRISE_S = 0.2   # la reprise raccourcie des épreuves sous verrou


class Bloqueur:
    """Une autre session qui tient `subject` en ACCESS EXCLUSIVE jusqu'à `liberer`."""

    def __init__(self):
        self.pris, self.liberer = threading.Event(), threading.Event()
        self.fil = threading.Thread(target=self._tenir, daemon=True)

    def _tenir(self) -> None:
        with psycopg.connect(db.DSN) as autre, autre.transaction():
            autre.execute("LOCK TABLE subject IN ACCESS EXCLUSIVE MODE")
            self.pris.set()
            self.liberer.wait(30)

    def __enter__(self) -> "Bloqueur":
        self.fil.start()
        assert self.pris.wait(10), "le verrou n'a pas été pris"
        return self

    def __exit__(self, *_exc) -> bool:
        self.liberer.set()
        self.fil.join(10)
        return False


@contextlib.contextmanager
def migration_pressee(attente):
    """Un `lock_timeout` court, la reprise à `REPRISE_S`, `attente` à la place de `sleep`."""
    garde = db.LOCK_TIMEOUT, db.DDL_ATTENTE_S, db.time
    db.LOCK_TIMEOUT, db.DDL_ATTENTE_S = "200ms", REPRISE_S
    db.time = SimpleNamespace(sleep=attente)
    try:
        yield
    finally:
        db.LOCK_TIMEOUT, db.DDL_ATTENTE_S, db.time = garde


def schema_depuis_le_cwd() -> None:
    installe, courant, vide = TMP / "site" / "lib" / "graphatom" / "db.py", TMP / "cwd", TMP / "vide"
    courant.mkdir()
    vide.mkdir()
    shutil.copy(ROOT / "schema.sql", courant / "schema.sql")
    garde, ici = db.__file__, os.getcwd()
    try:
        # un cwd étranger, muni d'une copie ou vide : le dépôt du module gagne
        for etranger in (courant, vide):
            os.chdir(etranger)
            assert db._schema() == ROOT / "schema.sql", os.getcwd()
        db.__file__ = str(installe)  # une installation : pas de dépôt au-dessus
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
    print("1. schema.sql : depuis le dépôt même d'un cwd étranger, sinon le cwd, sinon nommé absent ✓")


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
    attentes: list[float] = []
    with Bloqueur() as bloqueur:
        def attente(s: float) -> None:
            attentes.append(s)
            bloqueur.liberer.set()  # le bloqueur part pendant la reprise
            time.sleep(s)

        with migration_pressee(attente):
            assert db.init_db() == []
    assert attentes == [REPRISE_S], f"reprises : {attentes}"
    print("4. verrou tenu : une reprise, puis la migration passe ✓")


def migration_abandonnee() -> None:
    """Le verrou tenu jusqu'au bout : chaque reprise attend, le dernier essai lève."""
    attentes: list[float] = []
    with Bloqueur(), migration_pressee(attentes.append):
        try:
            db.init_db()
        except RuntimeError as exc:
            assert f"abandonnée après {db.DDL_ESSAIS} essais" in str(exc), exc
            assert "pid " in str(exc) and "LOCK TABLE subject" in str(exc), exc
        else:
            raise AssertionError("la migration est passée sous un verrou tenu")
    assert attentes == [REPRISE_S] * (db.DDL_ESSAIS - 1), f"reprises : {attentes}"
    print(f"5. verrou tenu jusqu'au bout : {db.DDL_ESSAIS - 1} reprises, "
          "puis l'erreur nomme le bloqueur ✓")


def identite_du_worker(conn) -> None:
    assert heartbeat.worker(conn) == {"at": None, "worker_sha": None,
                                      "worker_started_at": None}
    heartbeat.beat(conn, heartbeat.RAIL, "abc123", heartbeat.now())
    porte = heartbeat.worker(conn)
    assert porte["worker_sha"] == "abc123" and porte["at"] is not None, porte
    print("6. worker : triplet de None sans battement, son SHA après ✓")


def revision_inconnue(conn) -> None:
    try:
        graph.load_bundle(conn, "deadbeef")
    except GraphError as exc:
        assert "deadbeef" in str(exc), exc
    else:
        raise AssertionError("une révision inconnue a rendu un bundle")
    print("7. révision inconnue : levée, nommée ✓")


def main() -> None:
    try:
        schema_depuis_le_cwd()
        drop_sans_instance()
        forme_rendue()
        migration_sous_verrou()
        migration_abandonnee()
        with db.connect() as conn:
            identite_du_worker(conn)
            revision_inconnue(conn)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\ndb_schema : OK")


if __name__ == "__main__":
    main()
