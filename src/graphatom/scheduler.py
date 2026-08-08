"""L'ordonnanceur : un seul processus, un tick à trois passes.

    0. beat    — le battement du worker, tamponné avant le travail
    1. reap    — bails expirés → révocation, timed_out ou crashed, routage
    2. wait    — réponses arrivées et échéances de WAIT, wall_deadline
    3. dispatch— pour chaque item actif sans run : claim → bloc → apply

Le battement est écrit dans le tick, comme le reste : pas de thread dédié,
pas de timer. Il ne compte pas comme du travail — un rail au repos bat
quand même. Ce que le worker ne peut plus dire quand il meurt, son silence
le dit à sa place : voir `heartbeat`.

Deux plafonds bornent le dispatch, `MAX_RUNS` et `MAX_RUNS_PER_ITEM` : ce
qu'ils retiennent n'échoue pas, il attend le tick suivant — la charge est
une file, comme celle du déploiement. La charge en vol et les plafonds se
lisent hors de la base sur `/api/load`.

Chaque bloc s'exécute dans son propre thread avec sa propre connexion :
un agent qui travaille dix minutes ne bloque ni le faucheur ni les
autres items. claim() garantit qu'un item n'a qu'une tentative à la
fois — un seul run, ou les K candidats concurrents d'un nœud en fan-out,
lancés ensemble et réduits à une seule issue avant que l'item n'avance.

Tuer ce processus n'importe quand est un cas nominal, pas une panne :
c'est le contrat que le crash-test vérifie. Perdre la base l'est aussi :
la boucle se reconnecte avec un backoff borné et reprend où elle en est.
Une migration jouée sous ses plans cachés compte comme une perte de
connexion — le rail se déploie lui-même, donc il migre son propre schéma
sous son propre worker : voir `run_forever`.
"""

import os
import threading
import time

import psycopg

from . import heartbeat, kernel
from .blocks import BLOCKS, Context
from .graph import candidate_node, load_bundle

RECONNECT_MAX_S = 30.0  # plafond du backoff : une base absente n'est jamais abandonnée

# Le plafond de runs en vol, tous items confondus. Le fan-out multiplie la
# charge : tant qu'un item n'avait qu'un run par nœud, le nombre d'items la
# bornait tout seul, et le fan-out a supprimé cette borne implicite. Un
# candidat ne coûte pas un agent qui écrit du texte, il coûte un agent *plus*
# ses portes — une construction et une suite de tests : la moitié des cœurs
# laisse la machine à Postgres et au reste. Aucun chiffre magique, et la
# configuration surcharge.
MAX_RUNS = int(os.environ.get("GRAPHATOM_MAX_RUNS")
               or max(2, (os.cpu_count() or 4) // 2))
# …et un plafond par item, sinon un item en fan-out large prend toute la
# capacité et affame six items sur des nœuds bon marché. Toujours
# strictement sous le plafond global : une place reste donc toujours libre
# pour un autre item.
MAX_RUNS_PER_ITEM = int(os.environ.get("GRAPHATOM_MAX_RUNS_PER_ITEM")
                        or max(1, MAX_RUNS // 2))


def tick(conn: psycopg.Connection) -> int:
    heartbeat.beat(conn, heartbeat.RAIL)
    did = kernel.reap(conn)
    did += _settle_waits(conn)
    did += _dispatch(conn)
    return did


def run_forever(poll_s: float = 0.5) -> None:
    """Ticks à l'infini — perdre sa connexion est un incident nominal.

    Deux incidents la coûtent, et le remède est le même : fermer la
    connexion, attendre 1 s, 2 s, 4 s… plafonné à RECONNECT_MAX_S, en rouvrir
    une et reprendre les ticks. Rien n'est perdu : tout l'état est dans la
    base, et le faucheur rattrape au retour les runs restés orphelins.

      - Postgres qui disparaît — redémarrage, docker, réseau : une
        `OperationalError`, et la connexion n'existe plus.
      - Le rail qui se déploie lui-même : la migration change la forme d'une
        table sous une connexion dont psycopg a fait préparer les requêtes,
        et Postgres refuse un plan caché dont le résultat a changé de forme —
        `FeatureNotSupported`, SQLSTATE `0A000`. La connexion, elle, est
        vivante ; ce sont ses plans qui sont périmés, et une connexion neuve
        en prépare des neufs.

    L'autre voie pour le second cas — couper la préparation des requêtes
    (`prepare_threshold=None` à la connexion) — supprimerait la classe
    d'erreurs entièrement. On ne la prend pas : elle paie un aller-retour de
    plus par requête à chaque tick, pour toujours, contre une migration de
    loin en loin, et elle rendrait la transition muette. Rattraper laisse une
    ligne dans le journal du worker, en face de celle du déploiement.

    Ces deux-là seulement sont rattrapées. Toute autre exception fait crasher
    le processus, bruyamment : elle n'était pas attendue.
    """
    from .db import connect

    wait_s = 1.0
    while True:
        try:
            with connect() as conn:
                while True:
                    did = tick(conn)
                    wait_s = 1.0  # un tick passé : la base répond, on repart de 1 s
                    if did == 0:
                        time.sleep(poll_s)
        except (psycopg.OperationalError, psycopg.errors.FeatureNotSupported) as exc:
            print(f"{_cause(exc)} : {exc} — reconnexion dans {wait_s:.0f}s",
                  flush=True)
            time.sleep(wait_s)
            wait_s = min(wait_s * 2, RECONNECT_MAX_S)


def _cause(exc: Exception) -> str:
    """Ce que la boucle vient de perdre — la seule chose qui change des deux."""
    if isinstance(exc, psycopg.OperationalError):
        return "base injoignable"
    return "plan caché invalidé par une migration du schéma"


def _settle_waits(conn: psycopg.Connection) -> int:
    n = 0
    answered = conn.execute(
        "SELECT q.*, w.state AS item_state FROM question q "
        "JOIN work_item w ON w.id = q.item_id "
        "WHERE q.state = 'answered' AND w.terminal_at IS NULL AND w.state = q.node"
    ).fetchall()
    for q in answered:
        with conn.transaction():
            conn.execute("UPDATE question SET state = 'closed' WHERE id = %s", (q["id"],))
            kernel.apply_item(conn, q["item_id"], q["answer"], kind="answer")
        n += 1

    expired = conn.execute(
        "SELECT q.* FROM question q JOIN work_item w ON w.id = q.item_id "
        "WHERE q.state = 'open' AND q.deadline < now() "
        "AND w.terminal_at IS NULL AND w.state = q.node"
    ).fetchall()
    for q in expired:
        with conn.transaction():
            conn.execute("UPDATE question SET state = 'expired' WHERE id = %s", (q["id"],))
            kernel.apply_item(conn, q["item_id"], "expired", kind="deadline")
        n += 1

    walled = conn.execute(
        "SELECT id FROM work_item WHERE terminal_at IS NULL AND wall_deadline < now()"
    ).fetchall()
    for row in walled:
        kernel.apply_item(conn, row["id"], "wall_deadline", kind="wall")
        n += 1
    return n


def _execute(run_id: int, item_id: int) -> None:
    """Un bloc, un thread, une connexion. L'issue est appliquée à la fin."""
    from .db import connect

    with connect() as conn:
        run = conn.execute("SELECT * FROM node_run WHERE id = %s", (run_id,)).fetchone()
        item = conn.execute("SELECT * FROM work_item WHERE id = %s", (item_id,)).fetchone()
        bundle = load_bundle(conn, item["revision"])
        node = bundle["nodes"][run["node"]]
        if run["candidate"] is not None:  # un candidat joue sa variante du nœud
            node = candidate_node(node, run["candidate"])
        try:
            result = BLOCKS[node["block"]](Context(conn, run, item, node, bundle))
        except Exception as exc:  # le bloc a le droit d'échouer, pas de router
            result = {"outcome": "crashed", "error": str(exc)}
        kernel.apply(conn, run_id, result)


def en_vol(conn: psycopg.Connection) -> int:
    """Les runs qui volent, tous items confondus — la charge du rail."""
    return conn.execute(
        "SELECT count(*) AS n FROM node_run WHERE status = 'running'"
    ).fetchone()["n"]


def _en_vol_item(conn: psycopg.Connection, item_id: int) -> int:
    """Les runs en vol de cet item — ses candidats, quand il est en fan-out."""
    return conn.execute(
        "SELECT count(*) AS n FROM node_run WHERE item_id = %s AND status = 'running'",
        (item_id,),
    ).fetchone()["n"]


def _dispatch(conn: psycopg.Connection) -> int:
    """Réserve ce que les deux plafonds laissent passer. Le reste attend.

    Un run que le plafond retient n'est pas réservé du tout : aucun bail
    n'est posé, aucune tentative n'est comptée, rien n'échoue — le tick
    suivant le prendra, exactement comme la file du déploiement.
    """
    libre = MAX_RUNS - en_vol(conn)
    items = conn.execute(
        "SELECT id FROM work_item WHERE terminal_at IS NULL ORDER BY id"
    ).fetchall()
    n = 0
    for row in items:
        if libre <= 0:
            break
        place = min(libre, MAX_RUNS_PER_ITEM - _en_vol_item(conn, row["id"]))
        # un nœud en fan-out se réserve candidat par candidat : on rappelle
        # tant qu'il en reste et que la place le permet, et les blocs
        # réservés partent concurremment
        while place > 0 and (run := kernel.claim(conn, row["id"])) is not None:
            n += 1
            libre -= 1
            place -= 1
            threading.Thread(
                target=_execute, args=(run["id"], row["id"]), daemon=True
            ).start()
    return n
