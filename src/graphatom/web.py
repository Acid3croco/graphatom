"""`graphatom serve` — la surface locale de réponse aux WAIT.

Un http.server stdlib, lié à localhost, zéro dépendance. GET / liste les
questions ouvertes, un bouton par option ; POST /answer enregistre la
réponse via channel.record_answer. Rien d'autre : pas d'auth, pas de
comptes, pas d'exposition Internet, pas de mutation d'items.

Une page disponible n'est pas un oncall notifié : --notify-cmd lance une
commande shell (JSON de la question sur stdin) à chaque question ouverte
détectée. La détection est en mémoire — au redémarrage, on renotifie.
Au-moins-une-fois, comme le reste.
"""

import html
import json
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote

from . import channel, db

STYLE = """
body { font-family: system-ui, sans-serif; max-width: 44rem; margin: 2rem auto;
       padding: 0 1rem; color: #1a1a1a; }
h1 { font-size: 1.3rem; } h1 small { color: #888; font-weight: normal; }
.q { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
.meta { color: #666; font-size: .85rem; margin-bottom: .5rem; }
.text { margin: .5rem 0 1rem; }
button { font-size: 1rem; padding: .4rem 1rem; margin-right: .5rem;
         border: 1px solid #bbb; border-radius: 6px; background: #f6f6f6;
         cursor: pointer; }
button:hover { background: #e8e8e8; }
.empty { color: #888; margin-top: 3rem; text-align: center; }
.flash { background: #fff3cd; border: 1px solid #ffe08a; border-radius: 6px;
         padding: .5rem 1rem; }
"""


def _page(questions: list[dict], by: str, token: str, flash: str | None) -> str:
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<meta http-equiv='refresh' content='5'>",
        f"<title>graphatom — {len(questions)} question(s)</title>",
        f"<style>{STYLE}</style>",
        f"<h1>graphatom <small>· répondre en tant que {html.escape(by)}</small></h1>",
    ]
    if flash:
        parts.append(f"<p class='flash'>{html.escape(flash)}</p>")
    if not questions:
        parts.append("<p class='empty'>Aucune question ouverte. La page se rafraîchit toute seule.</p>")
    for q in questions:
        buttons = "".join(
            f"<button name='option' value='{html.escape(opt)}'>{html.escape(opt)}</button>"
            for opt in q["options"]
        )
        parts.append(
            f"<div class='q'><div class='meta'>"
            f"[{q['id']}] {html.escape(q['subject_key'])} · item {q['item_id']} "
            f"en <b>{html.escape(q['item_state'])}</b> · pour {html.escape(q['owner'])} "
            f"· avant {q['deadline']:%d/%m %H:%M} · escalades restantes {q['escalations']}"
            f"</div><div class='text'>{html.escape(q['text'])}</div>"
            f"<form method='post' action='/answer'>"
            f"<input type='hidden' name='question_id' value='{q['id']}'>"
            f"<input type='hidden' name='token' value='{token}'>"
            f"{buttons}</form></div>"
        )
    return "".join(parts)


def _notify_loop(cmd: str, base_url: str) -> None:
    seen: set[int] = set()
    while True:
        try:
            with db.connect() as conn:
                for q in channel.open_questions(conn):
                    if q["id"] in seen:
                        continue
                    payload = json.dumps({
                        "question_id": q["id"], "owner": q["owner"],
                        "text": q["text"], "options": list(q["options"]),
                        "deadline": q["deadline"].isoformat(),
                        "subject": q["subject_key"], "url": base_url,
                    })
                    subprocess.run(cmd, shell=True, input=payload,
                                   text=True, timeout=30)
                    seen.add(q["id"])
        except Exception as exc:  # le canal ne doit jamais tomber pour une notif
            print(f"notify: {exc}", flush=True)
        time.sleep(2)


def serve(port: int = 8848, by: str = "web", notify_cmd: str | None = None,
          host: str = "127.0.0.1") -> None:
    token = secrets.token_hex(16)
    base_url = f"http://127.0.0.1:{port}/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # pas de log d'accès
            pass

        def _respond(self, status: int, body: str = "", location: str | None = None):
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            if self.path.split("?")[0] != "/":
                return self._respond(404, "introuvable")
            flash = None
            if "?" in self.path:
                flash = parse_qs(self.path.split("?", 1)[1]).get("m", [None])[0]
            with db.connect() as conn:
                questions = channel.open_questions(conn)
            self._respond(200, _page(questions, by, token, flash))

        def do_POST(self):
            if self.path != "/answer":
                return self._respond(404, "introuvable")
            length = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(length).decode())
            if form.get("token", [""])[0] != token:
                return self._respond(403, "jeton invalide — rechargez la page")
            qid = int(form["question_id"][0])
            option = form["option"][0]
            with db.connect() as conn:
                err = channel.record_answer(conn, qid, option, by)
            msg = err or f"réponse « {option} » enregistrée — le rail reprend"
            self._respond(303, location=f"/?m={quote(msg)}")

    if notify_cmd:
        threading.Thread(target=_notify_loop, args=(notify_cmd, base_url),
                         daemon=True).start()
    print(f"canal humain sur {base_url} (réponses signées « {by} »)", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
