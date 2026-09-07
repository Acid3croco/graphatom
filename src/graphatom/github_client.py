"""Le client GitHub du rail : le seul appel sortant, et le vocabulaire des labels.

Sans `timeout`, un serveur qui accepte la connexion et ne répond jamais
gèle le canal pour toujours : pas d'erreur, pas de tick suivant, rien à
voir nulle part. Avec, l'attente a une fin, et cette fin est un
`TimeoutError` — l'incident réseau déjà géré par `github_sync.tick`.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
LABEL = "graphatom"
RAIL = "rail:"        # préfixe des labels d'état — l'espace de noms du rail
RAIL_COLOR = "1f6feb"  # couleur unie : un label d'état se reconnaît d'un coup d'œil
BLOCKED = f"{RAIL}blocked"  # pas un état d'item : une admission différée se voit
QUEUED = f"{RAIL}queued"    # la voie unique est tenue par l'item précédent
STALLED = f"{RAIL}stalled"  # pas un état d'item non plus : le worker ne bat plus
STALLED_COLOR = "d73a4a"    # le rouge de l'alarme, seule exception à la couleur unie
TIMEOUT_S = 30.0            # tout appel sortant est borné : au-delà, GitHub est injoignable


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def _call(self, method: str, path: str, body: dict | None = None):
        """Le seul appel sortant du module — donc le seul endroit à borner."""
        req = urllib.request.Request(
            f"{API}{path}", method=method,
            data=json.dumps(body).encode() if body else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read() or "null")

    def _tolerate(self, code: int, method: str, path: str, body: dict | None = None):
        """Un appel dont un code HTTP précis n'est pas une erreur."""
        try:
            return self._call(method, path, body)
        except urllib.error.HTTPError as exc:
            if exc.code != code:
                raise
            return None

    def labeled_issues(self) -> list[dict]:
        # GRAPHATOM_TAKE_ALL : le déploiement prend toute issue ouverte en charge,
        # sans attendre le label — pour un repo où le rail est le seul mainteneur
        label = "" if os.environ.get("GRAPHATOM_TAKE_ALL") else f"labels={LABEL}&"
        rows = self._call(
            "GET", f"/repos/{self.repo}/issues?{label}state=open&per_page=100")
        return [r for r in rows if "pull_request" not in r]  # les PR sont des issues, pas pour nous

    def issue(self, number: int) -> dict | None:
        # vise l'issue par son numéro : ouverte, fermée — ou None, elle n'existe pas
        return self._tolerate(404, "GET", f"/repos/{self.repo}/issues/{number}")

    def comments(self, number: int) -> list[dict]:
        return self._call(
            "GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100")

    def post_comment(self, number: int, body: str) -> None:
        self._call("POST", f"/repos/{self.repo}/issues/{number}/comments",
                   {"body": body})

    def edit_comment(self, comment_id: int, body: str) -> None:
        # éditer ne notifie personne : c'est ce qui rend la trajectoire vivante gratuite
        self._call("PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}",
                   {"body": body})

    def edit_issue(self, number: int, body: str) -> None:
        # la seule écriture du rail dans un corps d'issue : la ligne
        # `Depends-on:` que la découpe reporte — le reste est à l'humain
        self._call("PATCH", f"/repos/{self.repo}/issues/{number}", {"body": body})

    def close_issue(self, number: int) -> None:
        # fermer une issue déjà fermée est sans effet : le rejeu est gratuit
        self._call("PATCH", f"/repos/{self.repo}/issues/{number}",
                   {"state": "closed"})

    def issue_labels(self, number: int) -> list[str]:
        # vise l'issue par son numéro : ouverte ou fermée, l'API répond pareil
        rows = self._call(
            "GET", f"/repos/{self.repo}/issues/{number}/labels?per_page=100")
        return [r["name"] for r in rows]

    def create_label(self, name: str) -> None:
        # les labels du rail naissent à la volée ; 422 = il existe déjà, tant mieux
        color = STALLED_COLOR if name == STALLED else RAIL_COLOR
        self._tolerate(422, "POST", f"/repos/{self.repo}/labels",
                       {"name": name, "color": color})

    def add_label(self, number: int, name: str) -> None:
        self._call("POST", f"/repos/{self.repo}/issues/{number}/labels",
                   {"labels": [name]})

    def remove_label(self, number: int, name: str) -> None:
        # 404 : quelqu'un l'a retiré entre la lecture et l'écriture — c'était le but
        self._tolerate(
            404, "DELETE",
            f"/repos/{self.repo}/issues/{number}/labels/{urllib.parse.quote(name)}")


def from_env(repo: str) -> GitHub:
    """Le client du jeton d'environnement — le seul endroit qui le lit."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN manquant")
    return GitHub(repo, token)
