"""Connexion et initialisation de la base — celle du rail, et les jetables."""

import os
import time
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

# Les deux délais qui empêchent un client tiers de mettre le site à terre.
# Une transaction laissée ouverte et inactive tient un AccessShareLock sur nos
# tables ; le DDL du déploiement demande un AccessExclusiveLock derrière elle,
# et la file d'attente des verrous étant FIFO, toutes les lectures s'empilent
# derrière ce DDL — le site meurt sans que la base ni le worker n'aient rien.
IDLE_TX_TIMEOUT = os.environ.get("GRAPHATOM_IDLE_TX_TIMEOUT", "5min")
LOCK_TIMEOUT = os.environ.get("GRAPHATOM_LOCK_TIMEOUT", "3s")
DDL_ESSAIS = 3          # reprises de la migration quand le verrou est tenu
DDL_ATTENTE_S = 2.0     # entre deux reprises

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

    Le passage pose aussi les deux délais de sûreté : le rôle applicatif
    perd ses transactions inactives, et le DDL d'ici n'attend jamais sans
    limite. Bloqué après ses reprises, il lève — un déploiement qui échoue
    vite en nommant le bloqueur laisse le site debout, là où un DDL qui
    attend fait s'empiler toutes les lectures derrière lui.
    """
    with connect() as conn:
        # protection 1 : une transaction inactive meurt d'elle-même. Vaut
        # pour les sessions ouvertes après ce passage, les nôtres comme
        # celles d'un client tiers — c'est le rôle qui la porte. Sur la base
        # de ce passage seulement : l'instance des bases jetables porte celle
        # de chaque item, et un item n'impose rien à ses voisins.
        base = conn.execute("SELECT current_database() AS n").fetchone()["n"]
        conn.execute(sql.SQL("ALTER ROLE CURRENT_USER IN DATABASE {} SET "
                             "idle_in_transaction_session_timeout = {}")
                     .format(sql.Identifier(base), sql.Literal(IDLE_TX_TIMEOUT)))
        # protection 2 : le DDL qui suit attend au plus LOCK_TIMEOUT
        conn.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(LOCK_TIMEOUT)))
        if drop:
            conn.execute(
                "DROP TABLE IF EXISTS question, effect, event, node_run, "
                "work_item, subject, graph_revision, heartbeat CASCADE"
            )
        for essai in range(1, DDL_ESSAIS + 1):
            try:
                before = _shape(conn)
                conn.execute(_schema().read_text())
                after = _shape(conn)
                break
            except psycopg.errors.LockNotAvailable:
                if essai == DDL_ESSAIS:
                    raise RuntimeError(
                        f"migration abandonnée après {DDL_ESSAIS} essais — "
                        f"verrou tenu par {_bloqueurs(conn)}"
                    ) from None
                time.sleep(DDL_ATTENTE_S)
    return ([f"+ {t}.{c} ({d})" for t, c, d in sorted(after - before)]
            + [f"- {t}.{c} ({d})" for t, c, d in sorted(before - after)])


def _bloqueurs(conn: psycopg.Connection) -> str:
    """Les sessions qui tiennent un verrou sur nos tables : pid, état, requête.

    « Bloqué » sans dire par qui n'aide personne à trancher entre attendre et
    intervenir : c'est ce qui manquait au journal le jour de la panne.
    """
    rows = conn.execute(
        "SELECT DISTINCT a.pid, a.state, a.query FROM pg_locks l "
        "JOIN pg_stat_activity a ON a.pid = l.pid "
        "JOIN pg_class c ON c.oid = l.relation "
        "WHERE c.relkind = 'r' AND c.relnamespace = to_regnamespace(current_schema()) "
        "AND a.pid <> pg_backend_pid()"
    ).fetchall()
    # la requête sur une ligne et bornée : un `schema.sql` entier en journal
    # d'erreur noierait le pid, qui est ce qu'on vient y chercher
    return " ; ".join(
        f"pid {r['pid']} ({r['state']}) : {' '.join(r['query'].split())[:120]}"
        for r in rows
    ) or "aucune session — le bloqueur est parti entre-temps"


def _shape(conn: psycopg.Connection) -> set[tuple[str, str, str]]:
    """La forme des tables du schéma courant : une ligne par colonne."""
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema()"
    ).fetchall()
    return {(r["table_name"], r["column_name"], r["data_type"]) for r in rows}
