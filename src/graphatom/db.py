"""Connexion et initialisation de la base — celle du rail, et les jetables."""

import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

DSN = os.environ.get(
    "GRAPHATOM_DSN",
    "postgresql://graphatom:graphatom@127.0.0.1:54321/graphatom",
)
AGENT_DB_PREFIX = "graphatom_test_item_"  # une base jetable par item, jamais la production

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


def agent_dsn(item_id: int) -> str | None:
    """La base jetable de l'item, créée à la volée. None sans instance jetable.

    `GRAPHATOM_AGENT_DSN` désigne une instance, pas une base : chaque item y
    reçoit la sienne, `graphatom_test_item_<id>`. Deux items qui testent en
    même temps ne se marchent donc plus dessus — un `init-db --drop` ne
    détruit que le bac à sable de celui qui le lance —, et les numéros
    d'items d'un rail de test ne se promènent plus chez le voisin. Le
    cleanup du graph drop la base en même temps que le worktree.
    """
    instance = os.environ.get("GRAPHATOM_AGENT_DSN")
    if not instance:
        return None
    name = f"{AGENT_DB_PREFIX}{item_id}"
    with psycopg.connect(instance, autocommit=True) as conn:  # CREATE hors transaction
        try:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase:  # déjà là — c'est l'idempotence voulue
            pass
    return make_conninfo(instance, dbname=name)


def drop_agent_db() -> str:
    """Drop la base jetable que désigne GRAPHATOM_DSN. Rend son nom.

    Le garde-fou est celui du cleanup qui vérifie son worktree : on ne drop
    que ce qui porte le préfixe des bases jetables. Une DSN de production
    passée par mégarde ne détruit rien du tout.
    """
    name = conninfo_to_dict(DSN).get("dbname") or ""
    if not name.startswith(AGENT_DB_PREFIX):
        raise ValueError(f"« {name} » n'est pas une base jetable — rien de droppé")
    instance = os.environ.get("GRAPHATOM_AGENT_DSN")
    if not instance:
        raise ValueError("GRAPHATOM_AGENT_DSN absente — pas d'instance où dropper")
    with psycopg.connect(instance, autocommit=True) as conn:
        # FORCE : la connexion oubliée d'un ordonnanceur de test ne bloque pas
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
    return name


def init_db(drop: bool = False) -> list[str]:
    """Rejoue `schema.sql`. Rend les changements de forme des tables, s'il y en a.

    Le déploiement passe ici à chaque fois, et le passage est idempotent : la
    plupart du temps il ne change rien. Quand il change quelque chose, il
    faut le dire — une migration invisible périme les plans cachés des
    connexions ouvertes, et un worker qui se reconnecte sans qu'on sache
    pourquoi est deux fois plus dur à diagnostiquer (voir
    `scheduler.run_forever`).

    Ce qu'on relève est la forme des tables, colonne par colonne : c'est
    exactement ce qu'un plan caché voit changer. Une base neuve rend donc
    toutes ses colonnes — elle vient bien d'être créée.
    """
    with connect() as conn:
        if drop:
            conn.execute(
                "DROP TABLE IF EXISTS question, effect, event, node_run, "
                "work_item, subject, graph_revision, heartbeat CASCADE"
            )
        before = _shape(conn)
        conn.execute(_schema().read_text())
        after = _shape(conn)
    return ([f"+ {t}.{c} ({d})" for t, c, d in sorted(after - before)]
            + [f"- {t}.{c} ({d})" for t, c, d in sorted(before - after)])


def _shape(conn: psycopg.Connection) -> set[tuple[str, str, str]]:
    """La forme des tables du schéma courant : une ligne par colonne."""
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema()"
    ).fetchall()
    return {(r["table_name"], r["column_name"], r["data_type"]) for r in rows}
