"""Publication et lecture d'un graph — la porte publique du bundle.

Un graph est une donnée : un bundle JSON canonique, adressé par contenu.
Sa forme est dans `bundle`, sa validation dans `validation` ; ce module
publie et relit, et rassemble les noms que le reste du rail et les tests
appellent par `graph.`.
"""

import psycopg

from .bundle import (BLOCK_KINDS, FANOUT_KEEP_MAX, FANOUT_KEEP_MIN,
                     FANOUT_MAX_CANDIDATES, FANOUT_REDUCERS, JUDGE_OUTCOMES,
                     JUDGE_SOURCE, KERNEL_OUTCOMES, GraphError, candidate_node,
                     canonical, content_hash, fanout_variants, judge_source)
from .validation import validate

__all__ = [
    "BLOCK_KINDS", "FANOUT_KEEP_MAX", "FANOUT_KEEP_MIN", "FANOUT_MAX_CANDIDATES",
    "FANOUT_REDUCERS", "JUDGE_OUTCOMES", "JUDGE_SOURCE", "KERNEL_OUTCOMES",
    "GraphError", "candidate_node", "canonical", "content_hash",
    "fanout_variants", "judge_source", "load_bundle", "publish", "validate",
]


def publish(conn: psycopg.Connection, bundle: dict) -> str:
    validate(bundle)
    rev = content_hash(bundle)
    conn.execute(
        "INSERT INTO graph_revision (id, name, bundle) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (rev, bundle["name"], canonical(bundle)),
    )
    return rev


def load_bundle(conn: psycopg.Connection, revision: str) -> dict:
    row = conn.execute(
        "SELECT bundle FROM graph_revision WHERE id = %s", (revision,)
    ).fetchone()
    if row is None:
        raise GraphError(f"révision inconnue : {revision}")
    return row["bundle"]
