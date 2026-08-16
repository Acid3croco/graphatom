"""L'activation du worker : le rail se déploie lui-même, hors du tick.

Le nœud ``deploy`` écrit un ``deploy_sha`` durable dans son résultat. Ce
module réconcilie ce souhait avec le processus courant : si le worker porte
déjà le SHA voulu, rien ; sinon, checkout et services vérifiés, il demande
son propre redémarrage à systemd. Tout l'état d'avancement est journalisé
dans le résultat du run (``worker_activation``) et dans ``deploy.md``.

C'est de la mécanique de déploiement, pas de la mécanique d'items : le
noyau et l'ordonnanceur n'en dépendent que par deux appels — un par tick,
un après l'application d'un ``deploy``.
"""

import datetime as dt
import os
import subprocess
from pathlib import Path

import psycopg

from . import kernel
from .blocks import item_workspace
from .gates import DEPLOYED_SERVICES, deployed_service_shas

WORKER_STARTED_AT = dt.datetime.now(dt.timezone.utc)


def _worker_sha() -> str:
    """Rend le SHA chargé par ce processus, avant tout déploiement."""
    repo = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return "inconnu"
    return result.stdout.strip() if result.returncode == 0 else "inconnu"


WORKER_SHA = os.environ.get("GRAPHATOM_WORKER_SHA") or _worker_sha()
os.environ.setdefault("GRAPHATOM_WORKER_SHA", WORKER_SHA)
os.environ.setdefault("GRAPHATOM_WORKER_STARTED_AT", WORKER_STARTED_AT.isoformat())


def _request(conn: psycopg.Connection) -> dict | None:
    """Rend la dernière release appliquée qui demande un SHA de worker.

    La demande est le contrat, pas le nom du nœud : n'importe quel run
    appliqué dont le résultat porte un ``deploy_sha`` est une demande.
    """
    return conn.execute(
        "SELECT id, item_id, node, result FROM node_run "
        "WHERE status = 'applied' AND outcome = 'done' "
        "AND result->>'deploy_sha' IS NOT NULL "
        "ORDER BY finished_at DESC, id DESC LIMIT 1"
    ).fetchone()


def _state(conn: psycopg.Connection, request: dict, state: dict) -> None:
    conn.execute(
        "UPDATE node_run SET result = jsonb_set(result, "
        "'{worker_activation}', %s::jsonb) WHERE id = %s",
        (psycopg.types.json.Jsonb(state), request["id"]),
    )


def _report(item_id: int, node: str, *lines: str) -> None:
    """Ajoute un état au rapport encore présent, sans casser le worker nettoyé."""
    report = item_workspace(item_id) / f"{node}.md"
    try:
        with report.open("a") as out:
            for line in lines:
                out.write(line + "\n")
    except OSError as exc:
        print(f"rapport d'activation indisponible pour l'item {item_id} : {exc}",
              flush=True)


def reconcile(conn: psycopg.Connection) -> bool:
    """Réconcilie la dernière demande durable et dit si elle reste en attente."""
    request = _request(conn)
    if request is None:
        return False
    wanted = request["result"]["deploy_sha"]
    previous = request["result"].get("worker_activation") or {}
    if wanted == WORKER_SHA:
        state = {"status": "active", "worker_sha": WORKER_SHA}
        if previous != state:
            _state(conn, request, state)
            _report(request["item_id"], request["node"],
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
            _state(conn, request, state)
            _report(request["item_id"], request["node"],
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
            _state(conn, request, state)
            _report(request["item_id"], request["node"],
                    "activation du worker échouée - cksum indisponible")
        print("activation du worker échouée - cksum indisponible", flush=True)
        return True
    if not conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS pris", (lock,)).fetchone()["pris"]:
        return True
    try:
        current = _request(conn)
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
                _state(conn, request, state)
                _report(request["item_id"], request["node"], *(
                    f"{name} porte finalement {services[name] or 'aucun SHA'} - voulu {wanted}"
                    for name in DEPLOYED_SERVICES
                ), f"activation en attente - {state['error']}")
            return False

        _report(request["item_id"], request["node"], *(
            f"{name} porte finalement {services[name]} - voulu {wanted}"
            for name in DEPLOYED_SERVICES
        ))

        state = {"status": "pending", "worker_sha": WORKER_SHA,
                 "wanted_sha": wanted, "attempted_at": kernel.now().isoformat()}
        _state(conn, request, state)
        _report(request["item_id"], request["node"],
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
            _state(conn, request, state)
            _report(request["item_id"], request["node"],
                    f"activation du worker échouée - {state['error']}")
        return True
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (lock,))
