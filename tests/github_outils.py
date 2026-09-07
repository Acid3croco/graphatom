"""Les doublures du canal GitHub, partagées entre ses scripts de tests/.

Deux façons de tenir GitHub sans réseau :

  - `Serveur` : un vrai serveur HTTP local qui note chaque requête reçue et
    rend la réponse qu'on lui a programmée — c'est contre lui que le client
    `github_client.GitHub` est éprouvé, en-têtes et corps compris ;
  - `Greffier` : la doublure du client, au niveau des méthodes — elle tient
    les issues, les commentaires, les labels, et un journal ordonné de
    chaque geste que le canal lui demande.

Ce module n'est pas un test : il se charge depuis les scripts du canal.
"""

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = "o/r"


# ------------------------------------------------------------------ serveur


class _Guichet(BaseHTTPRequestHandler):
    """Un guichet : la requête est notée, la réponse programmée est rendue."""

    def _servir(self) -> None:
        longueur = int(self.headers.get("Content-Length") or 0)
        corps = self.rfile.read(longueur) if longueur else b""
        self.server.journal.append({
            "method": self.command, "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": corps,
        })
        status, payload = self.server.reponses.get(
            (self.command, self.path), (200, {}))
        if self.server.attente_s and self.server.mute.wait(self.server.attente_s):
            return  # le test a relâché le guichet : le client est déjà parti
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if payload is not None:
            self.wfile.write(json.dumps(payload).encode())

    do_GET = do_POST = do_PATCH = do_DELETE = _servir

    def log_message(self, *args) -> None:  # le journal du test, pas celui d'HTTP
        pass


class Serveur:
    """Le faux api.github.com : à brancher sur `github_client.API`."""

    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Guichet)
        self.httpd.daemon_threads = True
        self.httpd.journal = []
        self.httpd.reponses = {}
        self.httpd.attente_s = 0.0
        self.httpd.mute = threading.Event()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def journal(self) -> list[dict]:
        return self.httpd.journal

    def programme(self, method: str, path: str, status: int, payload=None) -> None:
        self.httpd.reponses[(method, path)] = (status, payload)

    def derniere(self) -> dict:
        return self.httpd.journal[-1]

    def fermer(self) -> None:
        self.httpd.mute.set()
        self.httpd.shutdown()
        self.httpd.server_close()


# ----------------------------------------------------------------- greffier


class Greffier:
    """GitHub vu du canal : ses issues, ce qu'on y dit, et l'ordre des gestes.

    `journal` retient chaque geste `(verbe, numéro, détail)` ; `casse` fait
    échouer l'édition des commentaires qu'il nomme ; `panne` fait tomber la
    lecture des issues ; `avant_lecture` se joue juste avant cette lecture,
    la place exacte d'un autre processus de sync qui prend la voie.
    """

    def __init__(self, issues: list[dict] | None = None,
                 repo: str = REPO):
        self.repo = repo
        self.issues = issues or []
        self.commentaires: dict[int, list[dict]] = {}
        self.labels_de: dict[int, list[str]] = {}
        self.journal: list[tuple] = []
        self.casse: set[int] = set()
        self.panne: Exception | None = None
        self.avant_lecture = None
        self._prochain_id = 100

    def issue_ouverte(self, number: int, **champs) -> dict:
        row = {"number": number, "state": "open", "title": f"Issue {number}",
               "body": "", "labels": []} | champs
        self.issues.append(row)
        return row

    # -- lectures

    def labeled_issues(self) -> list[dict]:
        if self.avant_lecture is not None:
            self.avant_lecture()
        if self.panne is not None:
            raise self.panne
        return [i for i in self.issues if i["state"] == "open"]

    def issue(self, number: int) -> dict | None:
        return next((i for i in self.issues if i["number"] == number), None)

    def comments(self, number: int) -> list[dict]:
        self.journal.append(("lire", number, "comments"))
        return list(self.commentaires.get(number, []))

    def issue_labels(self, number: int) -> list[str]:
        self.journal.append(("lire", number, "labels"))
        return list(self.labels_de.get(number, []))

    def corps(self, number: int) -> list[str]:
        return [c["body"] for c in self.commentaires.get(number, [])]

    def humain(self, number: int, login: str, body: str) -> None:
        """Un commentaire posé par une personne, pas par le rail : sans journal."""
        self._prochain_id += 1
        self.commentaires.setdefault(number, []).append(
            {"id": self._prochain_id, "body": body, "user": {"login": login},
             "created_at": "2026-01-02T00:00:00Z"})

    # -- écritures

    def post_comment(self, number: int, body: str) -> None:
        self._prochain_id += 1
        self.commentaires.setdefault(number, []).append(
            {"id": self._prochain_id, "body": body,
             "user": {"login": "rail"}, "created_at": "2026-01-01T00:00:00Z"})
        self.journal.append(("poster", number, self._prochain_id))

    def edit_comment(self, comment_id: int, body: str) -> None:
        if comment_id in self.casse:
            raise urllib.error.URLError("PATCH refusé par le test")
        for number, rows in self.commentaires.items():
            for row in rows:
                if row["id"] == comment_id:
                    row["body"] = body
                    self.journal.append(("editer", number, comment_id))
                    return
        raise KeyError(comment_id)

    def edit_issue(self, number: int, body: str) -> None:
        self.issue(number)["body"] = body
        self.journal.append(("reecrire", number, body))

    def close_issue(self, number: int) -> None:
        self.issue(number)["state"] = "closed"
        self.journal.append(("fermer", number, None))

    def create_label(self, name: str) -> None:
        self.journal.append(("creer", None, name))

    def add_label(self, number: int, name: str) -> None:
        self.labels_de.setdefault(number, []).append(name)
        self.journal.append(("+", number, name))
        self._vue_labels(number)

    def remove_label(self, number: int, name: str) -> None:
        if name in self.labels_de.get(number, []):
            self.labels_de[number].remove(name)
        self.journal.append(("-", number, name))
        self._vue_labels(number)

    def _vue_labels(self, number: int) -> None:
        """La vue GitHub de l'issue suit ce qu'on y pose — comme sur le vrai site."""
        issue = self.issue(number)
        if issue is not None:
            issue["labels"] = [{"name": n} for n in self.labels_de.get(number, [])]

    # -- le journal, filtré

    def gestes(self, verbe: str, number: int | None = None) -> list:
        return [g[2] for g in self.journal
                if g[0] == verbe and (number is None or g[1] == number)]
