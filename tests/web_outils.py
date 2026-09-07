"""L'outillage des tests du canal web et de la CLI : base jetable, vitrine, appels.

Ce module n'est pas un test. Il DOIT être importé avant `graphatom` : c'est
ici que GRAPHATOM_DSN est épinglé sur un Postgres jetable, et `db.DSN` se
fige à l'import. Le workspace va dans un répertoire temporaire.

Ce qu'il rassemble :
  - le graph vitrine : FETCH → ACT → JUDGE → WAIT, plus l'escalade commune
    des épreuves — de quoi montrer un journal, des runs, une question ;
  - les gestes qui y amènent un item sans ordonnanceur, réservation par
    réservation ;
  - le serveur `graphatom serve` en fil de fond sur un port libre, et
    l'appel HTTP brut qui ne suit aucune redirection.
"""

import atexit
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from outils import bundle_epreuve, etat, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-web")

from graphatom import graph, kernel, web  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="graphatom-web-"))
bureau.DATA_DIR = TMP  # noqa — les workspaces du test, jamais ceux du repo
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

OPTIONS = ["go", "stop"]

# la vitrine : quatre blocs en stub, une question à l'humain avant la fin
BUNDLE_VITRINE = bundle_epreuve("web-vitrine", {
    "ingest": {"block": "FETCH", "edges": {"ok": "travail"}},
    "travail": {"block": "ACT", "edges": {"ok": "verif"}},
    "verif": {"block": "JUDGE", "escalade": True, "config": {"stub_outcome": "pass"},
              "edges": {"pass": "attend", "fail": "travail"}},
    "attend": {
        "block": "WAIT",
        "config": {"question": "On livre ?", "options": OPTIONS,
                   "owner": "test", "deadline_minutes": 60},
        "edges": {"go": "fini", "stop": "abandon", "expired": "abandon"},
    },
}, entry="ingest")

# ce que le nœud de travail rapporte : un usage chiffré et une queue de log
USAGE = {"input_tokens": 1200, "output_tokens": 30, "total_cost_usd": 0.0123}
RESULTAT_TRAVAIL = {"outcome": "ok", "usage": USAGE, "log_tail": "dernières lignes"}


# ----------------------------------------------------------------- la base


def sujet(prefixe: str = "web") -> str:
    return f"{prefixe}:{uuid.uuid4().hex[:8]}"


def avancer(conn, item_id: int, submitted: dict) -> dict:
    """Une réservation, un résultat appliqué — ce que fait un tick, sans fil."""
    run = kernel.claim(conn, item_id)
    assert run is not None, "plus rien à réserver"
    kernel.apply(conn, run["id"], submitted)
    return run


def vitrine(conn, subject_key: str | None = None, title: str | None = None,
            travail: dict = RESULTAT_TRAVAIL) -> int:
    """Un item de la vitrine amené jusqu'à sa question ouverte, en trois runs."""
    rev = graph.publish(conn, BUNDLE_VITRINE)
    item_id = kernel.admit(conn, rev, subject_key or sujet(), title)
    avancer(conn, item_id, {"outcome": "ok"})
    avancer(conn, item_id, travail)
    avancer(conn, item_id, {"outcome": "pass"})
    assert etat(conn, item_id)["state"] == "attend"
    return item_id


def question_de(conn, item_id: int) -> dict:
    """La dernière question de l'item, telle que la table la porte."""
    return conn.execute(
        "SELECT * FROM question WHERE item_id = %s ORDER BY id DESC", (item_id,)
    ).fetchone()


def run_de(conn, item_id: int, node: str) -> dict:
    return conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s AND node = %s ORDER BY id",
        (item_id, node)).fetchone()


# -------------------------------------------------------------- le serveur


def port_libre() -> int:
    with socket.socket() as libre:  # un port éphémère : le rail peut tourner à côté
        libre.bind(("127.0.0.1", 0))
        return libre.getsockname()[1]


def appel(base: str, method: str, path: str, body: bytes | None = None,
          headers: dict | None = None) -> tuple[int, dict, bytes]:
    """Un appel HTTP brut : statut, en-têtes, corps — aucune redirection suivie."""
    host, port = base.split("/")[2].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        reponse = conn.getresponse()
        return reponse.status, dict(reponse.getheaders()), reponse.read()
    finally:
        conn.close()


def get_json(base: str, path: str) -> tuple[int, object]:
    status, en_tetes, body = appel(base, "GET", path)
    assert en_tetes["Content-Type"] == "application/json", en_tetes
    return status, json.loads(body)


def serveur(by: str, notify_cmd: str | None = None) -> str:
    """`web.serve` dans un fil démon sur un port libre ; rend l'URL de base."""
    port = port_libre()
    threading.Thread(target=web.serve, args=(port, by, notify_cmd, "127.0.0.1"),
                     daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    limite = time.monotonic() + 20
    while time.monotonic() < limite:
        try:
            appel(base, "GET", "/api/load")
            return base
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"{base} n'a jamais répondu")


def jeton(base: str) -> str:
    """Le jeton de `POST /answer`, tel que `/api/questions` le livre."""
    status, payload = get_json(base, "/api/questions")
    assert status == 200, payload
    return payload["token"]
