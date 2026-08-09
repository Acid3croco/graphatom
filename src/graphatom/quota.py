"""Quota global des opérations qui consomment la machine.

Une place est un verrou consultatif de session PostgreSQL. La session reste
ouverte pendant toute la commande : sa mort rend donc la place, sans démon,
fichier de verrou ni nettoyage différé. Une seconde famille de verrous borne
un item à moins du quota global. Dès que le quota vaut plus de un, un item ne
peut ainsi pas prendre toutes les places.

L'attente précède la dépense. Elle ne compte pas dans le couperet du bloc et
elle repousse son bail ; voir ``blocks._wait`` et ``_renew_lease``. Après la
prise, le budget entier du bloc recommence et la commande s'exécute sous la
surveillance de la session qui porte la place.
"""

import os
import signal
import subprocess
import time
import zlib
from pathlib import Path
from typing import Sequence

import psycopg
from psycopg.rows import dict_row


# Deux constructions sur douze cœurs : une construction Next utilise déjà
# plusieurs cœurs. Un cœur ou une valeur de os.cpu_count() inconnue gardent
# une place, donc une petite machine avance toujours.
MAX_BUILDS = int(os.environ.get("GRAPHATOM_MAX_BUILDS")
                 or max(1, (os.cpu_count() or 4) // 6))
MAX_BUILDS_PER_ITEM = max(1, MAX_BUILDS - 1)

# La forme à deux entiers rend les places globales lisibles dans pg_locks :
# classid porte le domaine et objid le numéro de place. La seconde famille
# utilise la forme à un bigint et ne peut donc pas se confondre avec elle.
BUILD_LOCK_NAMESPACE = 1_196_579_301
ITEM_LOCK_PREFIX = "graphatom-build-item-v1"
WAIT_FILE = ".graphatom-quota-waiting"
POLL_S = 0.25
LEASE_REFRESH_S = 5.0
REPORT_S = 30.0


def _dsn() -> str:
    """La base commune du rail, jamais la base jetable d'un item."""
    dsn = (os.environ.get("GRAPHATOM_QUOTA_DSN")
           or os.environ.get("GRAPHATOM_AGENT_DSN")
           or os.environ.get("GRAPHATOM_DSN"))
    if not dsn:
        raise RuntimeError("GRAPHATOM_QUOTA_DSN absente : quota global impossible")
    return dsn


def en_vol(conn) -> int:
    """Le nombre de places globales prises sur cette instance PostgreSQL."""
    return conn.execute(
        "SELECT count(*) AS n FROM pg_locks "
        "WHERE locktype = 'advisory' AND granted "
        "AND classid = %s AND objsubid = 2",
        (BUILD_LOCK_NAMESPACE,),
    ).fetchone()["n"]


def _item_id() -> int:
    """L'item du bloc, ou une identité stable pour un appel manuel."""
    raw = os.environ.get("GRAPHATOM_ITEM_ID")
    if raw:
        return int(raw)
    workspace = Path(os.environ.get("GRAPHATOM_WORKSPACE", ""))
    for part in reversed(workspace.parts):
        if part.startswith("item-") and part[5:].isdigit():
            return int(part[5:])
    if workspace.parts:
        # Les tests de scripts et les appels manuels n'ont pas toujours un
        # workspace nommé item-N. Ils gardent quand même une limite locale,
        # stable pour ce workspace, sans inventer une identité partagée.
        return zlib.crc32(str(workspace).encode())
    raise RuntimeError("GRAPHATOM_ITEM_ID et GRAPHATOM_WORKSPACE absents")


def _waiting_path() -> Path | None:
    workspace = os.environ.get("GRAPHATOM_WORKSPACE")
    return Path(workspace) / WAIT_FILE if workspace else None


def _renew_lease(conn) -> None:
    """Rend au run son bail entier pendant qu'il attend une place."""
    run_id = os.environ.get("GRAPHATOM_RUN_ID")
    lease_s = os.environ.get("GRAPHATOM_LEASE_S")
    if not run_id or not lease_s:
        return  # appel manuel hors d'un bloc : aucun bail à préserver
    conn.execute(
        "UPDATE node_run SET lease_expires_at = now() + (%s * interval '1 second') "
        "WHERE id = %s AND status = 'running'",
        (float(lease_s), int(run_id)),
    )


def _try_item_place(conn, item_id: int) -> str | None:
    for place in range(MAX_BUILDS_PER_ITEM):
        key = f"{ITEM_LOCK_PREFIX}:{item_id}:{place}"
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS pris", (key,)
        ).fetchone()
        if row["pris"]:
            return key
    return None


def _try_global_place(conn, start: int) -> int | None:
    for offset in range(MAX_BUILDS):
        place = (start + offset) % MAX_BUILDS
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS pris",
            (BUILD_LOCK_NAMESPACE, place),
        ).fetchone()
        if row["pris"]:
            return place
    return None


def _acquire(conn, item_id: int) -> tuple[str, int, float]:
    """Attend une place d'item puis une place globale, sans borne aveugle."""
    marker = _waiting_path()
    if marker:
        marker.touch()
    start = time.monotonic()
    next_lease = start
    next_report = start + REPORT_S
    item_key = None
    global_place = None
    try:
        while item_key is None:
            item_key = _try_item_place(conn, item_id)
            now = time.monotonic()
            if now >= next_lease:
                _renew_lease(conn)
                if marker:
                    marker.touch()
                next_lease = now + LEASE_REFRESH_S
            if item_key is None:
                time.sleep(POLL_S)

        first = item_id % MAX_BUILDS
        while global_place is None:
            global_place = _try_global_place(conn, first)
            now = time.monotonic()
            if now >= next_lease:
                _renew_lease(conn)
                if marker:
                    marker.touch()
                next_lease = now + LEASE_REFRESH_S
            if now >= next_report:
                print(f"quota de constructions occupé — attente {now - start:.0f} s",
                      flush=True)
                next_report = now + REPORT_S
            if global_place is None:
                time.sleep(POLL_S)

        _renew_lease(conn)  # le travail démarre avec son bail entier
        waited = time.monotonic() - start
        print(f"quota de constructions : place {global_place + 1}/{MAX_BUILDS} "
              f"prise après {waited:.1f} s", flush=True)
        return item_key, global_place, waited
    finally:
        if marker:
            marker.unlink(missing_ok=True)


def _stop(proc: subprocess.Popen) -> None:
    """Arrête la commande et toute sa famille de processus."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run(command: Sequence[str]) -> int:
    """Exécute ``command`` sous quota et rend son code de sortie.

    La commande a sa propre session de processus. Le preneur de quota peut
    ainsi la révoquer si sa session PostgreSQL meurt. SIGTERM et SIGINT
    suivent le même chemin : aucun descendant ne survit au preneur.
    """
    if not command:
        raise ValueError("le quota demande une commande")
    item_id = _item_id()
    proc = None
    previous = {}

    def interrupted(signum, _frame):
        if proc is not None:
            _stop(proc)
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, interrupted)
    try:
        with psycopg.connect(_dsn(), autocommit=True, connect_timeout=10,
                             row_factory=dict_row) as conn:
            item_key, global_place, _ = _acquire(conn, item_id)
            try:
                proc = subprocess.Popen(list(command), start_new_session=True)
                while proc.poll() is None:
                    time.sleep(POLL_S)
                    conn.execute("SELECT 1")  # la session morte arrête la dépense
                return proc.returncode
            finally:
                if proc is not None and proc.poll() is None:
                    _stop(proc)
                conn.execute("SELECT pg_advisory_unlock(%s, %s)",
                             (BUILD_LOCK_NAMESPACE, global_place))
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (item_key,)
                )
    except psycopg.Error as exc:
        if proc is not None:
            _stop(proc)
        print(f"quota de constructions perdu : {str(exc).splitlines()[0]}", flush=True)
        return 75
    finally:
        marker = _waiting_path()
        if marker:
            marker.unlink(missing_ok=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
