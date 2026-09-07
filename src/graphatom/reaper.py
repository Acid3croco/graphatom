"""Le faucheur : les runs au bail expiré, ou certainement orphelins.

La révocation a deux moitiés : l'autorité en base, et le processus. Un
agent lancé par un worker mort ne peut plus rien appliquer, mais il
travaille encore — le pgid laissé dans le workspace le tue.

Ce même pgid tranche l'issue, mécaniquement et sans modèle : un groupe
encore vivant au bout du bail, c'est un agent qui déborde de son budget —
`timed_out`, escalade directe ; un groupe déjà mort, c'est une panne —
`crashed`, retry sur place comme avant.

`CLAIMED` tranche, lui, le post-mortem : un run que ce worker n'a pas
réservé vient d'un worker qui n'est plus là. L'issue ne change pas, la
cause probable si. Avant le bail, seul un run étranger dont l'identité est
certainement morte passe. La sonde est refaite sous les verrous : une
observation périmée ne classe aucun run.
"""

import json

import psycopg

from . import kernel, reduction
from .failure import lease_autopsy
from .graph import load_bundle
from .policy import CLAIMED, now
from .process import AGENT_ALIVE, AGENT_DEAD, agent_state


def reap(conn: psycopg.Connection) -> int:
    """Runs au bail expiré ou certainement orphelins : classe et route."""
    running = conn.execute(
        "SELECT id, item_id, lease_expires_at FROM node_run WHERE status = 'running'"
    ).fetchall()
    suspects = [row for row in running
                if row["lease_expires_at"] < now()
                or (row["id"] not in CLAIMED
                    and agent_state(row["item_id"], row["id"]) == AGENT_DEAD)]
    reaped = 0
    for row in suspects:
        with conn.transaction():
            # l'item d'abord, son run ensuite : l'ordre des verrous d'`apply`,
            # sinon le faucheur et un candidat qui rend s'interbloquent
            item, run = kernel._locked(conn, row["id"])
            if run["status"] != "running" or not _doomed(item, run):
                continue
            revoques, ranger = _fault(conn, item, run)
        reduction.menage(item["id"], [run["id"], *revoques], ranger)
        reaped += 1
    return reaped


def _doomed(item: dict, run: dict) -> bool:
    """Sous les verrous : le bail a-t-il vraiment expiré, ou l'agent est-il
    certainement mort sous un worker qui n'est plus là ?"""
    if run["lease_expires_at"] < now():
        return True
    return run["id"] not in CLAIMED and agent_state(item["id"], run["id"]) == AGENT_DEAD


def _fault(conn, item: dict, run: dict) -> tuple[list[int], bool]:
    """Révoque l'autorité, écrit le post-mortem, et règle le nœud."""
    if run["candidate"] is None:
        # révocation d'autorité : un zombie ne peut plus appliquer. En
        # fan-out, la barrière est celle de la tentative entière : un
        # candidat fauché ne révoque pas ses frères — seule la réduction
        # pousse la barrière.
        conn.execute(
            "UPDATE work_item SET fence = fence + 1 WHERE id = %s", (item["id"],)
        )
    # Un run qui n'est pas dans CLAIMED n'a pas été réservé par ce worker-ci :
    # il a survécu à celui d'avant, qui l'a emporté.
    orphaned = run["id"] not in CLAIMED
    CLAIMED.discard(run["id"])
    alive = agent_state(item["id"], run["id"]) == AGENT_ALIVE
    post = lease_autopsy(item["id"], run, alive, orphaned)
    post = kernel.signed(item["id"], {**run, "outcome": post["outcome"]},
                         post["outcome"], post)
    run = {**run, "outcome": post["outcome"], "result": post}
    conn.execute(
        "UPDATE node_run SET status = 'faulted', outcome = %s, result = %s, "
        "finished_at = %s WHERE id = %s",
        (post["outcome"], json.dumps(post), now(), run["id"]),
    )
    bundle = load_bundle(conn, item["revision"])
    return kernel._settle(conn, item, bundle, run, post["outcome"], kind="reaped")
