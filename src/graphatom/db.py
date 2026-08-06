"""Connexion et initialisation de la base."""

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get(
    "GRAPHATOM_DSN",
    "postgresql://graphatom:graphatom@127.0.0.1:54321/graphatom",
)

def _schema() -> Path:
    # depuis le repo (src/graphatom/ → racine) ou depuis une installation (cwd)
    for p in (Path(__file__).resolve().parents[2] / "schema.sql",
              Path.cwd() / "schema.sql"):
        if p.exists():
            return p
    raise FileNotFoundError("schema.sql introuvable")


def connect() -> psycopg.Connection:
    # autocommit : chaque conn.transaction() est une vraie transaction,
    # commitée à la sortie du bloc — jamais de travail durable en suspens
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


def init_db(drop: bool = False) -> None:
    with connect() as conn:
        if drop:
            conn.execute(
                "DROP TABLE IF EXISTS question, effect, event, node_run, "
                "work_item, subject, graph_revision CASCADE"
            )
        conn.execute(_schema().read_text())
