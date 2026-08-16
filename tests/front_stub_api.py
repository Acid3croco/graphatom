"""Une API de doublure pour le front : un item figé, sans base ni rail.

`tests/front_test.sh` fait tourner l'image du front contre cette doublure
pour lire le DOM qu'elle rend. Ce qu'on veut prouver — le prix du jugement
affiché à part de celui des candidats — se lit sur un item passé par un nœud
arbitre, et fabriquer un tel item pour de vrai coûterait une base, un rail et
plusieurs minutes. Ici, la réponse JSON est la même, écrite à la main.

La doublure ne sert que ce que la page d'un item demande : le détail de
l'item 1, le battement, et les questions. Tout le reste est un 404.

Usage : uv run python tests/front_stub_api.py <port>
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Trois nombres par part, tous distincts d'une part à l'autre, et des coûts
# qui ne tombent pas ronds : le front écrit un entier autrement qu'un
# décimal, et le test lit le texte rendu au caractère près.
JUGEMENT = {"input_tokens": 700000, "output_tokens": 9000,
            "total_cost_usd": 2.5}
CANDIDATS = {"input_tokens": 300000, "output_tokens": 21000,
             "total_cost_usd": 6.25}
TOTAL = {k: JUGEMENT[k] + CANDIDATS[k] for k in JUGEMENT}  # l'ordre des clés tient

ITEM = {
    "item": {
        "id": 1, "subject_key": "gh:exemple/depot#1", "title": "un item jugé",
        "graph": "code-task", "generation": 1, "state": "close",
        "status": "terminal", "version": 6, "cycle": 1, "escalations": 5,
        "total_cost_usd": 8.75, "reported_cost_usd": 8.75,
        "issue_url": None, "pr_url": None,
        "terminal_at": "2026-01-01T12:00:00+00:00",
        "updated_at": "2026-01-01T12:00:00+00:00",
        "revision": "0" * 64, "lineage_budget": 3,
        "wall_deadline": "2026-01-04T10:00:00+00:00", "duration_s": 7200.0,
        "usage": TOTAL,
        "usage_split": {"judgement": JUGEMENT, "candidates": CANDIDATS, "other": {}},
    },
    "graph": {
        "name": "code-task", "entry": "implement",
        "on_kernel": {"escalate_to": "judge", "exhausted_to": "close"},
        "current": "close",
        "nodes": [
            {"name": "implement", "block": "ACT", "terminal": False,
             "escalade": False,
             "fanout": {"reduce": "keep_n", "repeat": 1,
                        "candidates": [{"variant": {"label": "a"}, "cmd": None},
                                       {"variant": {"label": "b"}, "cmd": None}]}},
            {"name": "judge", "block": "JUDGE", "terminal": False, "escalade": True,
             "finalists_from": "implement"},
            {"name": "close", "block": None, "terminal": True, "escalade": False},
        ],
        "edges": [{"from": "implement", "outcome": "done", "to": "judge"},
                  {"from": "judge", "outcome": "chosen", "to": "close"},
                  {"from": "judge", "outcome": "sole", "to": "close"},
                  {"from": "judge", "outcome": "none", "to": "implement"}],
    },
    "journal": [
        {"version": 2, "at": "2026-01-01T11:00:00+00:00", "kind": "result",
         "from_state": "implement", "to_state": "judge", "outcome": "done",
         "run_id": 1, "duration_s": 1800.0},
        {"version": 3, "at": "2026-01-01T12:00:00+00:00", "kind": "result",
         "from_state": "judge", "to_state": "close", "outcome": "chosen",
         "run_id": 3, "duration_s": 600.0},
    ],
    "runs": [
        {"id": 1, "node": "implement", "cycle": 1, "attempt": 1, "candidate": 0,
         "status": "applied", "outcome": "done", "fence": 1,
         "lease_expires_at": None, "finished_at": "2026-01-01T11:00:00+00:00",
         "duration_s": 1800.0, "usage": CANDIDATS, "result": {"outcome": "done"}},
        {"id": 2, "node": "implement", "cycle": 1, "attempt": 1, "candidate": 1,
         "status": "applied", "outcome": "done", "fence": 1,
         "lease_expires_at": None, "finished_at": "2026-01-01T11:00:00+00:00",
         "duration_s": 1700.0, "usage": {}, "result": {"outcome": "done"}},
        {"id": 3, "node": "judge", "cycle": 1, "attempt": 1, "candidate": None,
         "status": "applied", "outcome": "chosen", "fence": 2,
         "lease_expires_at": None, "finished_at": "2026-01-01T12:00:00+00:00",
         "duration_s": 600.0, "usage": JUGEMENT, "result": {"outcome": "chosen"}},
    ],
    "effects": [], "questions": [], "criteria": None, "files": [],
    "decision": {
        "type": "chosen", "source": "implement",
        "finalists": [{"letter": "A", "candidate": 0},
                       {"letter": "B", "candidate": 1}],
        "selected": {"letter": "B", "candidate": 1},
        "summary": "B couvre les preuves demandées",
        "verdict": "Comparaison complète des finalistes A et B.",
        "judge": {"model": "gpt-5.6-sol", "cli": "codex", "effort": "high"},
    },
}

SOLE = json.loads(json.dumps(ITEM))
SOLE["item"]["id"] = 2
SOLE["decision"] = {
    "type": "sole", "source": "implement",
    "finalists": [{"letter": "A", "candidate": 0}],
    "selected": {"letter": "A", "candidate": 0},
    "summary": "finaliste unique — traversé sans appel de modèle",
    "verdict": "Un seul finaliste.", "judge": None,
}

NONE = json.loads(json.dumps(ITEM))
NONE["item"]["id"] = 3
NONE["decision"] = {
    "type": "none", "source": "implement", "finalists": [],
    "selected": None, "summary": "aucun finaliste", "verdict": None,
    "judge": None,
}

ROUTES = {
    "/api/item/1": ITEM,
    "/api/item/2": SOLE,
    "/api/item/3": NONE,
    "/api/questions": {"token": "doublure", "questions": []},
    "/api/heartbeat": {"rail": {"at": None, "ago_s": 0.0, "stale": True},
                       "github-sync": {"at": None, "ago_s": 0.0, "stale": True}},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — la signature est celle de la stdlib
        payload = ROUTES.get(self.path)
        body = json.dumps(payload if payload is not None else {"error": "inconnu"})
        self.send_response(200 if payload is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):  # le journal du test est déjà assez bavard
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
