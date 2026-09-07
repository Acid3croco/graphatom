"""`graphatom serve` — le serveur du canal web : pages, API JSON, réponses.

Un http.server stdlib, zéro dépendance. Les pages vivent dans `pages`, les
routes JSON dans `api`, les données dans `views` ; ici il n'y a que le
serveur, et l'unique porte d'écriture : `POST /answer`, qui sert les deux
clients — du JSON à qui envoie `Accept: application/json`, la redirection
d'avant au formulaire.

Tout le reste est en lecture seule : cette interface montre le rail, elle
ne le pilote pas. Pas d'auth, pas d'exposition Internet, pas de mutation
d'items.
"""

import json
import secrets
import subprocess
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote

from . import api, channel, db, pages

NOTIFY_POLL_S = 2
NOTIFY_TIMEOUT_S = 30


class Handler(BaseHTTPRequestHandler):
    """Une requête : un fichier du workspace, une route JSON, ou une page."""

    def __init__(self, *args, token: str, by: str, **kwargs):
        self.token, self.by = token, by
        super().__init__(*args, **kwargs)

    def log_message(self, _format, *_args):  # pas de log d'accès
        pass

    def _respond(self, status: int, body: str = "", location: str | None = None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, default=api.jsonable, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _wants_json(self) -> bool:
        return "application/json" in self.headers.get("Accept", "")

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        path, _, query = self.path.partition("?")
        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "item" and parts[2].isdigit() and parts[3] == "file":
            return self._file(int(parts[2]), unquote(parts[4]))
        if path.startswith("/api/"):
            return self._api(path, query)
        self._page(path, query)

    def _file(self, item_id: int, name: str) -> None:
        found = pages.file_response(item_id, name)
        if found is None:
            return self._respond(404, "introuvable")
        data, ctype = found
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(data)

    def _api(self, path: str, query: str) -> None:
        """Les routes JSON — une erreur ici est un objet à `error`, jamais une page."""
        try:
            with db.connect() as conn:
                status, payload = api.dispatch(conn, path, query, self.token)
        except Exception as exc:  # une API qui tombe le dit, en JSON
            return self._json(500, {"error": str(exc)})
        self._json(status, payload)

    def _page(self, path: str, query: str) -> None:
        try:
            with db.connect() as conn:
                page = pages.render(conn, path, query, self.by, self.token)
        except Exception as exc:  # une vitrine qui tombe le dit, elle ne coupe pas
            return self._respond(500, f"erreur : {pages.e(exc)}")
        if page is None:
            return self._respond(404, "introuvable")
        self._respond(200, page)

    # ----------------------------------------------------------------- POST

    def _fields(self, body: str) -> dict:
        """Les champs postés : formulaire urlencodé, ou JSON du même nom."""
        if "application/json" in self.headers.get("Content-Type", ""):
            return json.loads(body)
        return {k: v[0] for k, v in parse_qs(body).items()}

    def _refuse(self, status: int, message: str) -> None:
        """Un refus, dans la langue du client : JSON ou page."""
        if self._wants_json():
            return self._json(status, {"ok": False, "message": message})
        self._respond(status, pages.e(message))

    def do_POST(self):
        if self.path != "/answer":
            return self._refuse(404, "introuvable")
        length = int(self.headers.get("Content-Length", 0))
        try:
            fields = self._fields(self.rfile.read(length).decode())
            qid, option = int(fields["question_id"]), str(fields["option"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return self._refuse(400, f"requête illisible : {exc}")
        if fields.get("token") != self.token:
            return self._refuse(403, "jeton invalide — rechargez la page")
        with db.connect() as conn:
            err = channel.record_answer(conn, qid, option, self.by)
        msg = err or f"réponse « {option} » enregistrée — le rail reprend"
        if self._wants_json():  # le client JSON lit l'issue, pas une redirection
            return self._json(200, {"ok": err is None, "message": msg})
        self._respond(303, location=f"/?m={quote(msg)}")


# --------------------------------------------------------------------- notify


def notify_once(cmd: str, base_url: str, seen: set[int]) -> None:
    """Une passe : chaque question ouverte pas encore dite part sur `cmd`.

    Une commande qui échoue n'a pas notifié : la question reste due, et la
    passe suivante la redit. Le canal ne doit jamais tomber pour une notif.
    """
    with db.connect() as conn:
        pending = [q for q in channel.open_questions(conn) if q["id"] not in seen]
    for q in pending:
        payload = json.dumps({
            "question_id": q["id"], "owner": q["owner"],
            "text": q["text"], "options": list(q["options"]),
            "deadline": q["deadline"].isoformat(),
            "subject": q["subject_key"], "url": base_url,
        })
        done = subprocess.run(cmd, shell=True, input=payload, text=True,
                              timeout=NOTIFY_TIMEOUT_S)
        if done.returncode:
            print(f"notify: question {q['id']} — commande en code {done.returncode}, "
                  "on réessaie", flush=True)
            continue
        seen.add(q["id"])


def notify_loop(cmd: str, base_url: str) -> None:
    seen: set[int] = set()
    while True:
        try:
            notify_once(cmd, base_url, seen)
        except Exception as exc:  # le canal ne doit jamais tomber pour une notif
            print(f"notify: {exc}", flush=True)
        time.sleep(NOTIFY_POLL_S)


# ---------------------------------------------------------------------- serve


def serve(port: int = 8848, by: str = "web", notify_cmd: str | None = None,
          host: str = "127.0.0.1") -> None:
    token = secrets.token_hex(16)
    base_url = f"http://127.0.0.1:{port}/"
    if notify_cmd:
        threading.Thread(target=notify_loop, args=(notify_cmd, base_url),
                         daemon=True).start()
    print(f"canal humain sur {base_url} (réponses signées « {by} »)", flush=True)
    build_server(host, port, token, by).serve_forever()


def build_server(host: str, port: int, token: str, by: str) -> ThreadingHTTPServer:
    """Le serveur, un fil par requête — et ces fils sont des démons.

    Un client qui garde sa connexion ouverte n'a pas à retenir le canal à
    sa mort : un SIGINT arrête le processus même si un navigateur traîne.
    """
    server = ThreadingHTTPServer((host, port), partial(Handler, token=token, by=by))
    server.daemon_threads = True
    return server
