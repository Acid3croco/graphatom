"""Les battements des processus du rail : une ligne chacun, en base.

L'absence de signal doit être un signal. Un worker mort laisse les items
dans leurs états actifs : plus de faucheur, donc plus de classement des
agents expirés, donc plus d'escalade — aucune question, aucun signal, nulle
part. La stase est invisible, et elle dure. Le battement la rend visible.

Deux processus battent, et chacun peut mourir seul :

  - le worker (`rail`), qui tamponne dans son tick ;
  - le canal GitHub (`github-sync`), qui tamponne à chaque tour de sa
    boucle. Il est séparé du worker et survit à sa mort : c'est lui qui
    pose `rail:stalled` sur les issues des items actifs quand le worker se
    tait — mais son propre silence, lui, ne se voyait nulle part.

Le frontend affiche les deux dans l'en-tête commun — « rail vivant il y a
3 s · canal GitHub il y a 2 s » —, et l'alarme sonne dès qu'un seul des
deux dépasse `STALE_S`.

Une ligne par batteur, un UPSERT sur son identité : plusieurs workers
tamponnent le même battement, c'est « au moins un vivant » qu'on mesure,
pas qui est vivant. Lire coûte une ligne, et ne décide de rien : GitHub
reste une projection, la base reste l'autorité.
"""

import datetime as dt

import psycopg

from .kernel import now

STALE_S = 120.0  # au-delà, le batteur est déclaré à l'arrêt sur les deux surfaces

RAIL = "rail"                  # le worker : son tick tamponne
GITHUB_SYNC = "github-sync"    # le canal : son tour de boucle tamponne


def beat(conn: psycopg.Connection, who: str, worker_sha: str | None = None,
         worker_started_at: dt.datetime | None = None) -> None:
    """Tamponne le battement de `who` — dans la boucle, sans thread à part."""
    conn.execute(
        "INSERT INTO heartbeat (who, at, worker_sha, worker_started_at) "
        "VALUES (%s, now(), %s, %s) ON CONFLICT (who) DO UPDATE SET "
        "at = now(), worker_sha = EXCLUDED.worker_sha, "
        "worker_started_at = EXCLUDED.worker_started_at",
        (who, worker_sha, worker_started_at),
    )


def last(conn: psycopg.Connection, who: str) -> dt.datetime | None:
    """Le dernier battement de `who`. None : il n'a jamais tamponné ici."""
    row = conn.execute("SELECT at FROM heartbeat WHERE who = %s", (who,)).fetchone()
    return row["at"] if row else None


def worker(conn: psycopg.Connection) -> dict:
    """Rend l'identité du worker hôte portée par son dernier battement."""
    row = conn.execute(
        "SELECT at, worker_sha, worker_started_at FROM heartbeat WHERE who = %s",
        (RAIL,),
    ).fetchone()
    return row or {"at": None, "worker_sha": None, "worker_started_at": None}


def age_s(at: dt.datetime | None) -> float | None:
    """L'âge du battement, en secondes. None quand il n'y en a jamais eu."""
    return None if at is None else (now() - at).total_seconds()


def stalled(at: dt.datetime | None) -> bool:
    """Ce batteur est-il à l'arrêt ? Jamais battu compte comme à l'arrêt.

    Une base sans battement est le cas du processus qui n'a jamais démarré :
    rien ne tourne, et le dire est exactement ce qu'on veut.
    """
    age = age_s(at)
    return age is None or age > STALE_S
