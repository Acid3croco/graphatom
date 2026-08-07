"""Le battement du worker : une ligne en base, tamponnée à chaque tick.

L'absence de signal doit être un signal. Un worker mort laisse les items
dans leurs états actifs : plus de faucheur, donc plus de classement des
agents expirés, donc plus d'escalade — aucune question, aucun signal, nulle
part. La stase est invisible, et elle dure. Le battement la rend visible.

Deux surfaces le lisent, et elles suffisent :

  - le frontend l'affiche dans l'en-tête commun — « rail vivant il y a 3 s »,
    ou le bandeau qui dit que les états montrés sont figés ;
  - le canal GitHub pose `rail:stalled` sur les issues des items actifs.
    C'est le point clé : le sync est un processus séparé du worker, il
    survit à sa mort — le problème se voit précisément quand le worker ne
    peut plus parler.

Une seule ligne (`id = 1`), un UPSERT : plusieurs workers tamponnent le même
battement, c'est « au moins un vivant » qu'on mesure. Lire coûte une ligne,
et ne décide de rien : GitHub reste une projection, la base reste l'autorité.
"""

import datetime as dt

import psycopg

from .kernel import now

STALE_S = 120.0  # au-delà, le rail est déclaré à l'arrêt sur les deux surfaces


def beat(conn: psycopg.Connection) -> None:
    """Tamponne le battement — dans le tick, sans thread ni timer à part."""
    conn.execute(
        "INSERT INTO heartbeat (id, at) VALUES (1, now()) "
        "ON CONFLICT (id) DO UPDATE SET at = now()"
    )


def last(conn: psycopg.Connection) -> dt.datetime | None:
    """Le dernier battement. None : aucun worker n'a jamais tamponné ici."""
    row = conn.execute("SELECT at FROM heartbeat WHERE id = 1").fetchone()
    return row["at"] if row else None


def age_s(at: dt.datetime | None) -> float | None:
    """L'âge du battement, en secondes. None quand il n'y en a jamais eu."""
    return None if at is None else (now() - at).total_seconds()


def stalled(at: dt.datetime | None) -> bool:
    """Le rail est-il à l'arrêt ? Jamais battu compte comme à l'arrêt.

    Une base sans battement est le cas du worker qui n'a jamais démarré :
    rien ne tourne, et le dire est exactement ce qu'on veut.
    """
    age = age_s(at)
    return age is None or age > STALE_S
