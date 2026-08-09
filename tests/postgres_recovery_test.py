"""Le test du marqueur Postgres et du journal de reprise.

Il ne demande pas de base. Deux incarnations simulées suffisent pour vérifier
que le worker distingue une reprise de Postgres d'une simple reconnexion.

Usage : uv run python tests/postgres_recovery_test.py
"""

import contextlib
import datetime as dt
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from graphatom import db, scheduler  # noqa: E402

STARTED = dt.datetime(2026, 8, 8, 6, 0, tzinfo=dt.UTC)
REPRISE = "base reprise après un arrêt PostgreSQL"


class Termine(Exception):
    """Arrête la boucle après les opérations utiles à la preuve."""


class FakeConnection:
    """Connexion minimale pour les deux blocs `with` de la boucle."""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def journal(first: tuple, second: tuple) -> str:
    """Joue une coupure, une reconnexion, puis plusieurs opérations."""
    identities = iter((first, second))
    operations = iter((
        psycopg.OperationalError("coupure simulée"),
        1,
        0,
        2,
        Termine(),
    ))
    true_connect = db.connect
    true_incarnation = db.incarnation
    true_tick = scheduler.tick
    true_sleep = scheduler.time.sleep
    db.connect = lambda: FakeConnection()
    db.incarnation = lambda _conn: next(identities)

    def tick(_conn):
        result = next(operations)
        if isinstance(result, Exception):
            raise result
        return result

    scheduler.tick = tick
    scheduler.time.sleep = lambda _seconds: None
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            scheduler.run_forever()
    except Termine:
        pass
    finally:
        db.connect = true_connect
        db.incarnation = true_incarnation
        scheduler.tick = true_tick
        scheduler.time.sleep = true_sleep
    return output.getvalue()


def main() -> None:
    same = journal(("token-a", STARTED), ("token-a", STARTED))
    assert "base injoignable" in same and "reconnexion" in same, same
    assert REPRISE not in same, same
    print("1. marqueur stable : reconnexion sans signal de reprise ✓")

    recovered = journal(("token-a", STARTED), ("token-b", STARTED))
    lines = recovered.splitlines()
    recovery_lines = [line for line in lines if REPRISE in line]
    connection_lines = [line for line in lines if "reconnexion" in line]
    assert len(recovery_lines) == 1, recovered
    assert len(connection_lines) == 1, recovered
    assert recovery_lines[0] != connection_lines[0], recovered
    assert STARTED.isoformat() in recovery_lines[0], recovery_lines[0]
    print("2. jeton changé : une ligne de reprise distincte ✓")
    print("3. plusieurs opérations après la reconnexion : une seule ligne ✓")

    restarted = journal(
        ("token-a", STARTED),
        ("token-a", STARTED + dt.timedelta(minutes=2)),
    )
    assert restarted.count(REPRISE) == 1, restarted
    print("4. postmaster redémarré proprement : reprise signalée ✓")

    print("\nreprise Postgres : OK — reprise et reconnexion restent distinctes")


if __name__ == "__main__":
    main()
