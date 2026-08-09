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
un agent qui travaille dix minutes ne bloque pas le faucheur. Un item
complet tient la voie jusqu'à son terminal ; les K candidats d'un fan-out
restent concurrents à l'intérieur de cet item, puis sont réduits à une seule
issue avant qu'il n'avance.

Tuer ce processus n'importe quand est un cas nominal, pas une panne :
c'est le contrat que le crash-test vérifie. Perdre la base l'est aussi :
la boucle se reconnecte avec un backoff borné et reprend où elle en est.
Une migration jouée sous ses plans cachés compte comme une perte de
connexion — le rail se déploie lui-même, donc il migre son propre schéma
sous son propre worker : voir `run_forever`.
"""

import os
from pathlib import Path
import subprocess
import threading
import time
import datetime as dt

import psycopg

from . import heartbeat, kernel
from .blocks import (BLOCKS, Context, DEPLOYED_SERVICES,
                     deployed_service_shas, item_workspace)
from .graph import FANOUT_MAX_CANDIDATES, candidate_node, fanout_variants, load_bundle

RECONNECT_MAX_S = 30.0  # plafond du backoff : une base absente n'est jamais abandonnée

# Le plafond de runs en vol, tous items confondus. Le fan-out multiplie la
# charge : tant qu'un item n'avait qu'un run par nœud, le nombre d'items la
# bornait tout seul, et le fan-out a supprimé cette borne implicite. Un
# candidat ne coûte pas un agent qui écrit du texte, il coûte un agent *plus*
# ses portes — une construction et une suite de tests : la moitié des cœurs
# laisse la machine à Postgres et au reste. Aucun chiffre magique, et la
# configuration surcharge.
#
# Les deux plafonds ne descendent jamais sous la plus large course
# publiable : une course se réserve entière (voir `_dispatch`), donc un
# plafond plus serré que `FANOUT_MAX_CANDIDATES` ne la différerait pas — il
# l'empêcherait pour toujours. Le plafond borne le nombre de courses
# simultanées, jamais la largeur d'une seule.
MAX_RUNS = int(os.environ.get("GRAPHATOM_MAX_RUNS")
               or max(FANOUT_MAX_CANDIDATES, (os.cpu_count() or 4) // 2))
# …et un plafond par item, sinon un item en fan-out large prend toute la
# capacité et affame six items sur des nœuds bon marché. Même plancher, et
# pour la même raison.
MAX_RUNS_PER_ITEM = int(os.environ.get("GRAPHATOM_MAX_RUNS_PER_ITEM")
                        or max(FANOUT_MAX_CANDIDATES, MAX_RUNS // 2))
# Une instance complète à la fois. Ce n'est pas une surcharge d'environnement :
# la production ne doit pas rouvrir le flot par une variable oubliée. Les
# bancs d'essai des plafonds peuvent modifier la constante dans leur process.
MAX_ACTIVE_ITEMS = 1
WORKER_STARTED_AT = dt.datetime.now(dt.timezone.utc)


def _worker_sha() -> str:
    """Rend le SHA chargé par ce processus, avant tout déploiement."""
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "inconnu"


WORKER_SHA = os.environ.get("GRAPHATOM_WORKER_SHA") or _worker_sha()
os.environ.setdefault("GRAPHATOM_WORKER_SHA", WORKER_SHA)
os.environ.setdefault("GRAPHATOM_WORKER_STARTED_AT", WORKER_STARTED_AT.isoformat())


def _activation_request(conn: psycopg.Connection) -> dict | None:
    """Rend la dernière release appliquée qui demande un SHA de worker."""
    return conn.execute(
        "SELECT id, item_id, result FROM node_run "
        "WHERE node = 'deploy' AND status = 'applied' AND outcome = 'done' "
        "AND result->>'deploy_sha' IS NOT NULL "
        "ORDER BY finished_at DESC, id DESC LIMIT 1"
    ).fetchone()


def _activation_state(conn: psycopg.Connection, request: dict, state: dict) -> None:
    conn.execute(
        "UPDATE node_run SET result = jsonb_set(result, "
        "'{worker_activation}', %s::jsonb) WHERE id = %s",
        (psycopg.types.json.Jsonb(state), request["id"]),
    )


def _activation_report(item_id: int, *lines: str) -> None:
    """Ajoute un état au rapport encore présent, sans casser le worker nettoyé."""
    report = item_workspace(item_id) / "deploy.md"
    try:
        with report.open("a") as out:
            for line in lines:
                out.write(line + "\n")
    except OSError as exc:
        print(f"rapport d'activation indisponible pour l'item {item_id} : {exc}",
              flush=True)


def _activation_worker(conn: psycopg.Connection) -> bool:
    """Réconcilie la dernière demande durable et dit si elle reste en attente."""
    request = _activation_request(conn)
    if request is None:
        return False
    wanted = request["result"]["deploy_sha"]
    previous = request["result"].get("worker_activation") or {}
    if wanted == WORKER_SHA:
        state = {"status": "active", "worker_sha": WORKER_SHA}
        if previous != state:
            _activation_state(conn, request, state)
            _activation_report(request["item_id"],
                               f"worker actif sur le SHA voulu {wanted}")
        return False

    repo = Path(os.environ.get("GRAPHATOM_REPO_DIR", Path(__file__).resolve().parents[2]))
    checkout = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if checkout != wanted:
        state = {"status": "error", "worker_sha": WORKER_SHA,
                 "wanted_sha": wanted,
                 "error": f"checkout {checkout or 'inconnu'} différent"}
        if previous != state:
            _activation_state(conn, request, state)
            _activation_report(request["item_id"],
                               f"worker porte {WORKER_SHA} - voulu {wanted}",
                               f"activation en attente - {state['error']}")
        return False  # verify_deploy doit router cette discordance

    checksum = subprocess.run(
        ["cksum"], input=str(repo), capture_output=True, text=True, check=False,
    )
    try:
        lock = int(checksum.stdout.split()[0]) if checksum.returncode == 0 else None
    except (IndexError, ValueError):
        lock = None
    if lock is None:
        state = {"status": "error", "worker_sha": WORKER_SHA,
                 "wanted_sha": wanted, "error": "cksum indisponible"}
        if previous != state:
            _activation_state(conn, request, state)
            _activation_report(request["item_id"],
                               "activation du worker échouée - cksum indisponible")
        print("activation du worker échouée - cksum indisponible", flush=True)
        return True
    if not conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS pris", (lock,)).fetchone()["pris"]:
        return True
    try:
        current = _activation_request(conn)
        if current is None or current["id"] != request["id"]:
            return True
        current_checkout = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if current_checkout != wanted:
            return False

        attempted = previous.get("attempted_at")
        if attempted:
            try:
                age = (kernel.now() - dt.datetime.fromisoformat(attempted)).total_seconds()
            except (TypeError, ValueError):
                age = float("inf")
            retry_s = float(os.environ.get("GRAPHATOM_ACTIVATION_RETRY_S", "5"))
            if age < retry_s:
                return True

        services = deployed_service_shas(repo)
        wrong = [name for name in DEPLOYED_SERVICES if services[name] != wanted]
        if wrong:
            state = {"status": "error", "worker_sha": WORKER_SHA,
                     "wanted_sha": wanted,
                     "service_shas": services,
                     "error": "services discordants : " + ", ".join(wrong)}
            if previous != state:
                _activation_state(conn, request, state)
                _activation_report(request["item_id"], *(
                    f"{name} porte finalement {services[name] or 'aucun SHA'} - voulu {wanted}"
                    for name in DEPLOYED_SERVICES
                ), f"activation en attente - {state['error']}")
            return False

        _activation_report(request["item_id"], *(
            f"{name} porte finalement {services[name]} - voulu {wanted}"
            for name in DEPLOYED_SERVICES
        ))

        state = {"status": "pending", "worker_sha": WORKER_SHA,
                 "wanted_sha": wanted, "attempted_at": kernel.now().isoformat()}
        _activation_state(conn, request, state)
        _activation_report(request["item_id"],
                           f"worker porte {WORKER_SHA} - voulu {wanted}",
                           "résultat appliqué - activation du worker demandée")
        try:
            restarted = subprocess.run(
                [os.environ.get("GRAPHATOM_SYSTEMCTL", "systemctl"), "--user", "restart",
                 "graphatom-worker.service"], check=False,
            )
        except OSError as exc:
            restarted = subprocess.CompletedProcess([], 127)
            state["error"] = str(exc)
        if restarted.returncode:
            state["status"] = "error"
            state.setdefault("error", f"systemctl code {restarted.returncode}")
            _activation_state(conn, request, state)
            _activation_report(request["item_id"],
                               f"activation du worker échouée - {state['error']}")
        return True
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (lock,))


def tick(conn: psycopg.Connection) -> int:
    heartbeat.beat(conn, heartbeat.RAIL, WORKER_SHA, WORKER_STARTED_AT)
    activation_pending = _activation_worker(conn)
    did = kernel.reap(conn)
    did += _settle_waits(conn)
    if activation_pending:
        return did
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
    from .db import connect, incarnation

    wait_s = 1.0
    previous_incarnation = None
    while True:
        try:
            with connect() as conn:
                current_incarnation = incarnation(conn)
                if (previous_incarnation is not None
                        and current_incarnation != previous_incarnation):
                    print(
                        "base reprise après un arrêt PostgreSQL "
                        f"— serveur démarré à {current_incarnation[1].isoformat()}",
                        flush=True,
                    )
                previous_incarnation = current_incarnation
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
    head = kernel.active_item(conn)
    if head is None:
        return 0
    item_id = head["id"]
    n = 0
    answered = conn.execute(
        "SELECT q.*, w.state AS item_state FROM question q "
        "JOIN work_item w ON w.id = q.item_id "
        "WHERE q.item_id = %s AND q.state = 'answered' "
        "AND w.terminal_at IS NULL AND w.state = q.node",
        (item_id,),
    ).fetchall()
    for q in answered:
        with conn.transaction():
            conn.execute("UPDATE question SET state = 'closed' WHERE id = %s", (q["id"],))
            kernel.apply_item(conn, q["item_id"], q["answer"], kind="answer")
        n += 1

    expired = conn.execute(
        "SELECT q.* FROM question q JOIN work_item w ON w.id = q.item_id "
        "WHERE q.item_id = %s AND q.state = 'open' AND q.deadline < now() "
        "AND w.terminal_at IS NULL AND w.state = q.node",
        (item_id,),
    ).fetchall()
    for q in expired:
        with conn.transaction():
            conn.execute("UPDATE question SET state = 'expired' WHERE id = %s", (q["id"],))
            kernel.apply_item(conn, q["item_id"], "expired", kind="deadline")
        n += 1

    walled = conn.execute(
        "SELECT id FROM work_item WHERE id = %s AND terminal_at IS NULL "
        "AND wall_deadline < now()",
        (item_id,),
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
        ctx = Context(conn, run, item, node, bundle)
        try:
            result = BLOCKS[node["block"]](ctx)
        except Exception as exc:  # le bloc a le droit d'échouer, pas de router
            result = {"outcome": "crashed", "error": str(exc)}
        status = kernel.apply(conn, run_id, result)
        if (status == "applied" and run["node"] == "deploy"
                and result.get("outcome") == "done"):
            _activation_worker(conn)


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


def _largeur(conn: psycopg.Connection, item_id: int) -> int:
    """Combien de runs le prochain nœud de cet item demande — 1 hors fan-out.

    Lue sur la révision épinglée de l'item, donc sur le graph qu'il exécute
    et pas sur celui du moment. Un item terminal, ou posé sur un nœud qui ne
    se réserve pas, ne demande rien.
    """
    item = conn.execute(
        "SELECT state, revision, terminal_at FROM work_item WHERE id = %s", (item_id,)
    ).fetchone()
    if item is None or item["terminal_at"] is not None:
        return 0
    spec = load_bundle(conn, item["revision"])["nodes"].get(item["state"])
    if spec is None:
        return 0
    return max(1, len(fanout_variants(spec)))


def _solo(conn: psycopg.Connection, item_id: int) -> bool:
    """Vrai si l'état courant de l'item demande la voie entière."""
    item = conn.execute(
        "SELECT state, revision, terminal_at FROM work_item WHERE id = %s", (item_id,)
    ).fetchone()
    if item is None or item["terminal_at"] is not None:
        return False
    spec = load_bundle(conn, item["revision"])["nodes"].get(item["state"]) or {}
    return (spec.get("config") or {}).get("solo") is True


def etat_solo(conn: psycopg.Connection) -> dict[str, int]:
    """Les nœuds solo actifs, séparés entre le vol et l'attente.

    Le compte porte sur les items, pas sur les candidats d'un fan-out : un
    nœud reste une seule course, même quand plusieurs runs la composent.
    """
    items = conn.execute(
        "SELECT id, state, revision FROM work_item "
        "WHERE terminal_at IS NULL ORDER BY id"
    ).fetchall()
    running = {
        row["item_id"] for row in conn.execute(
            "SELECT DISTINCT item_id FROM node_run WHERE status = 'running'"
        ).fetchall()
    }
    solo = [row["id"] for row in items
            if (load_bundle(conn, row["revision"])["nodes"].get(row["state"], {})
                .get("config") or {}).get("solo") is True]
    return {"running": sum(item_id in running for item_id in solo),
            "waiting": sum(item_id not in running for item_id in solo)}


def _dispatch(conn: psycopg.Connection) -> int:
    """Réserve la tête de voie dans ce que les deux plafonds laissent passer.

    Un run que le plafond retient n'est pas réservé du tout : aucun bail
    n'est posé, aucune tentative n'est comptée, rien n'échoue — le tick
    suivant le prendra, exactement comme la file du déploiement.

    **Une course se réserve entière, ou pas du tout.** Un fan-out coupé en
    deux par un plafond serait pire qu'un fan-out différé : une réduction ne
    voit que le lot qui *existe en base*, pas celui qui *devrait* exister.
    Deux candidats réservés sur quatre pourraient donc suffire à `keep_n`,
    puis faire avancer l'item avant que les deux autres ne naissent. On ne
    réserve les K candidats que si la place les accueille tous.
    """
    # Le verrou est symétrique. Un solo en vol ferme immédiatement le rail ;
    # un solo qui arrive ne passe que sur un rail vide. Dans les deux cas,
    # `claim` n'est jamais appelé pour ce qui attend.
    if etat_solo(conn)["running"]:
        return 0

    libre = MAX_RUNS - en_vol(conn)
    items = conn.execute(
        "SELECT id FROM work_item WHERE terminal_at IS NULL "
        "ORDER BY id LIMIT %s", (MAX_ACTIVE_ITEMS,)
    ).fetchall()
    n = 0
    trop_larges = []
    for row in items:
        if libre <= 0:
            break
        place = min(libre, MAX_RUNS_PER_ITEM - _en_vol_item(conn, row["id"]))
        largeur = _largeur(conn, row["id"])
        solo = _solo(conn, row["id"])
        if solo and en_vol(conn) > 0:
            continue
        if place < largeur:
            trop_larges.append((row["id"], largeur))
            continue  # la course ne tient pas : elle attend, entière
        n += _reserve(conn, row["id"], place)
        if solo and n:
            return n  # rien d'autre ne naît sous cette course
        libre = MAX_RUNS - en_vol(conn)

    # Dernier recours : une course plus large que le plafond lui-même. La
    # différer ne servirait à rien — elle serait interdite pour toujours —,
    # donc elle passe, mais seulement quand rien d'autre ne peut avancer et
    # que le rail est vide. Le plafond borne le nombre de courses
    # simultanées, jamais la largeur d'une seule.
    if trop_larges and n == 0 and en_vol(conn) == 0:
        item_id, largeur = trop_larges[0]
        n += _reserve(conn, item_id, largeur)
    return n


def _reserve(conn: psycopg.Connection, item_id: int, place: int) -> int:
    """Réserve jusqu'à `place` runs de cet item, et lance leurs blocs."""
    n = 0
    while place > 0 and (run := kernel.claim(conn, item_id)) is not None:
        n += 1
        place -= 1
        threading.Thread(
            target=_execute, args=(run["id"], item_id), daemon=True
        ).start()
    return n
