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

from .gates import DEPLOYED_SERVICES, deployed_service_shas
from .policy import now
from .workspace import item_workspace

WORKER_STARTED_AT = dt.datetime.now(dt.timezone.utc)
SERVICE = "graphatom-worker.service"


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


def _note(conn, request: dict, previous: dict, state: dict, *lines: str) -> None:
    """Un état nouveau est écrit en base et dans le rapport ; un état déjà
    connu ne l'est pas — un tick ne redit pas ce que le précédent a dit."""
    if previous != state:
        _state(conn, request, state)
        _report(request["item_id"], request["node"], *lines)


def _checkout(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()


def _lock_key(repo: Path) -> int | None:
    """Une clé d'advisory lock stable par dépôt — `cksum` de son chemin.

    Un `cksum` absent, en échec, muet ou illisible rend None : l'appelant
    le dit et attend, il ne tombe pas.
    """
    try:
        checksum = subprocess.run(
            ["cksum"], input=str(repo), capture_output=True, text=True, check=False,
        )
        return int(checksum.stdout.split()[0]) if checksum.returncode == 0 else None
    except (OSError, IndexError, ValueError):
        return None


def _too_soon(previous: dict) -> bool:
    """Une demande déjà tentée il y a moins de `GRAPHATOM_ACTIVATION_RETRY_S`."""
    attempted = previous.get("attempted_at")
    if not attempted:
        return False
    try:
        age = (now() - dt.datetime.fromisoformat(attempted)).total_seconds()
    except (TypeError, ValueError):
        age = float("inf")
    return age < float(os.environ.get("GRAPHATOM_ACTIVATION_RETRY_S", "5"))


def _restart(conn, request: dict, state: dict) -> None:
    """Demande le redémarrage à systemd ; un refus est noté, pas caché."""
    try:
        restarted = subprocess.run(
            [os.environ.get("GRAPHATOM_SYSTEMCTL", "systemctl"), "--user", "restart",
             SERVICE], check=False,
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


def _activate(conn, request: dict, previous: dict, wanted: str, repo: Path) -> bool:
    """Sous le verrou : vérifie les services, note la demande, redémarre.

    Rend True tant que la demande reste en attente d'un redémarrage.
    """
    current = _request(conn)
    if current is None or current["id"] != request["id"]:
        return True
    if _checkout(repo) != wanted:
        return False
    if _too_soon(previous):
        return True
    services = deployed_service_shas(repo)
    wrong = [name for name in DEPLOYED_SERVICES if services[name] != wanted]
    if wrong:
        state = {"status": "error", "worker_sha": WORKER_SHA, "wanted_sha": wanted,
                 "service_shas": services,
                 "error": "services discordants : " + ", ".join(wrong)}
        _note(conn, request, previous, state, *(
            f"{name} porte finalement {services[name] or 'aucun SHA'} - voulu {wanted}"
            for name in DEPLOYED_SERVICES
        ), f"activation en attente - {state['error']}")
        return False
    _report(request["item_id"], request["node"], *(
        f"{name} porte finalement {services[name]} - voulu {wanted}"
        for name in DEPLOYED_SERVICES
    ))
    state = {"status": "pending", "worker_sha": WORKER_SHA,
             "wanted_sha": wanted, "attempted_at": now().isoformat()}
    _state(conn, request, state)
    _report(request["item_id"], request["node"],
            f"worker porte {WORKER_SHA} - voulu {wanted}",
            "résultat appliqué - activation du worker demandée")
    _restart(conn, request, state)
    return True


def reconcile(conn: psycopg.Connection) -> bool:
    """Réconcilie la dernière demande durable et dit si elle reste en attente."""
    request = _request(conn)
    if request is None:
        return False
    wanted = request["result"]["deploy_sha"]
    previous = request["result"].get("worker_activation") or {}
    if wanted == WORKER_SHA:
        _note(conn, request, previous, {"status": "active", "worker_sha": WORKER_SHA},
              f"worker actif sur le SHA voulu {wanted}")
        return False

    repo = Path(os.environ.get("GRAPHATOM_REPO_DIR", Path(__file__).resolve().parents[2]))
    checkout = _checkout(repo)
    if checkout != wanted:
        state = {"status": "error", "worker_sha": WORKER_SHA, "wanted_sha": wanted,
                 "error": f"checkout {checkout or 'inconnu'} différent"}
        _note(conn, request, previous, state,
              f"worker porte {WORKER_SHA} - voulu {wanted}",
              f"activation en attente - {state['error']}")
        return False  # verify_deploy doit router cette discordance

    lock = _lock_key(repo)
    if lock is None:
        _note(conn, request, previous,
              {"status": "error", "worker_sha": WORKER_SHA, "wanted_sha": wanted,
               "error": "cksum indisponible"},
              "activation du worker échouée - cksum indisponible")
        print("activation du worker échouée - cksum indisponible", flush=True)
        return True
    if not conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS pris", (lock,)).fetchone()["pris"]:
        return True
    try:
        return _activate(conn, request, previous, wanted, repo)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (lock,))
