"""Le test de la boucle du worker : `run_forever`, connexion perdue et reprise.

Sans base : la connexion est une doublure scriptée, le tick aussi. Ce que
`reconnect_test` prouve avec un vrai Postgres coupé de force — le worker
survit —, ce test le prouve tour par tour, sur la boucle seule :

  1. base injoignable : reconnexion avec un délai doublé à chaque échec, et
     plafonné à `RECONNECT_MAX_S`
  2. un tick réussi remet le délai à 1 s — le prochain incident ne paie pas
     les échecs d'avant
  3. une migration sous les plans cachés (`FeatureNotSupported`) est
     rattrapée comme une perte de connexion, mais nommée à part
  4. une base reprise après un arrêt PostgreSQL — l'incarnation change — est
     annoncée, une seule fois, avec l'heure de démarrage du serveur ; la
     première connexion, elle, ne dit rien
  5. chaque connexion neuve passe le préflight, et la connexion perdue est
     refermée avant d'en rouvrir une
  6. un tick sans travail attend `poll_s` ; toute autre exception traverse
     la boucle au lieu d'être avalée

Usage : uv run python tests/scheduler_loop_test.py
"""

import datetime as dt
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from graphatom import db, scheduler  # noqa: E402

PLAFOND_S = 4.0  # un plafond bas : le doublement se prouve en quatre pas
POLL_S = 0.25
DEMARRE = dt.datetime(2026, 9, 7, 12, 0, tzinfo=dt.timezone.utc)
REPRIS = DEMARRE + dt.timedelta(minutes=5)


class Assez(Exception):
    """La sentinelle qui arrête la boucle : elle n'est pas une perte de base."""


class Base:
    """Une connexion scriptée : son incarnation, et l'issue de chaque tick."""

    def __init__(self, incarnation: tuple, ticks: list):
        self.incarnation, self.ticks, self.fermee = incarnation, ticks, False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.fermee = True
        return False


def jouer(connexions: list) -> tuple[list[float], str, list[Base]]:
    """Joue la boucle sur le script. Rend les attentes, la sortie, et les
    bases dans l'ordre où le préflight les a vues."""
    attentes, preflights, script = [], [], list(connexions)

    def connect():
        acte = script.pop(0)
        if isinstance(acte, BaseException):
            raise acte
        return acte

    def tick(conn: Base) -> int:
        issue = conn.ticks.pop(0)
        if isinstance(issue, BaseException):
            raise issue
        return issue

    garde = (db.connect, db.incarnation, scheduler.tick, scheduler._preflight,
             scheduler.time, scheduler.RECONNECT_MAX_S)
    db.connect, db.incarnation = connect, lambda conn: conn.incarnation
    scheduler.tick, scheduler._preflight = tick, preflights.append
    scheduler.time = SimpleNamespace(sleep=attentes.append)
    scheduler.RECONNECT_MAX_S = PLAFOND_S
    sortie = io.StringIO()
    try:
        with redirect_stdout(sortie):
            scheduler.run_forever(poll_s=POLL_S)
    except Assez:
        pass
    finally:
        (db.connect, db.incarnation, scheduler.tick, scheduler._preflight,
         scheduler.time, scheduler.RECONNECT_MAX_S) = garde
    return attentes, sortie.getvalue(), preflights


def injoignable() -> psycopg.OperationalError:
    return psycopg.OperationalError("connection refused (simulé)")


def main() -> None:
    # quatre refus, une connexion qui travaille puis perd ses plans, une
    # connexion sur une base redémarrée qui ne fait rien puis rencontre un bug
    premiere = Base(("jeton-1", DEMARRE),
                    [1, psycopg.errors.FeatureNotSupported("cached plan (simulé)")])
    seconde = Base(("jeton-2", REPRIS), [0, Assez()])
    attentes, sortie, bases = jouer(
        [injoignable(), injoignable(), injoignable(), injoignable(),
         premiere, seconde])
    lignes = sortie.splitlines()

    assert attentes[:4] == [1, 2, 4, PLAFOND_S], attentes
    assert sortie.count("base injoignable") == 4, lignes
    print(f"1. base injoignable : reconnexion après {attentes[:4]} — "
          f"doublé, plafonné à {PLAFOND_S:.0f}s ✓")

    assert attentes[4] == 1.0, f"le backoff n'a pas été remis à 1 s : {attentes}"
    print("2. un tick réussi remet le délai à 1 s ✓")

    migration = [l for l in lignes if "plan caché invalidé par une migration" in l]
    assert len(migration) == 1 and "reconnexion dans 1s" in migration[0], lignes
    print("3. plans cachés périmés : rattrapé, et nommé à part ✓")

    reprises = [l for l in lignes if l.startswith("base reprise")]
    assert len(reprises) == 1, lignes
    assert REPRIS.isoformat() in reprises[0], reprises
    assert DEMARRE.isoformat() not in sortie, "la première connexion a parlé"
    print("4. base reprise après un arrêt : annoncée une fois, datée ✓")

    assert bases == [premiere, seconde], "une connexion a sauté le préflight"
    assert all(b.fermee for b in bases), "une connexion perdue reste ouverte"
    print("5. chaque connexion est préflightée, puis refermée quand elle est perdue ✓")

    assert attentes[5:] == [POLL_S], attentes
    assert seconde.ticks == [], "la sentinelle n'a pas été jouée"
    print("6. tick sans travail → poll_s ; une autre exception traverse ✓")
    print("\nscheduler_loop : OK")


if __name__ == "__main__":
    main()
