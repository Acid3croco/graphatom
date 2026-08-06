"""Le canal GitHub — module hors noyau, par polling.

GitHub est l'interface humaine et la cible des effets ; Postgres reste
l'unique autorité d'exécution. Ce module fait quatre choses, et refuse
tout le reste :

  1. admission  — une issue ouverte portant le label `graphatom` devient
                  un sujet (une seule admission automatique par issue)
  2. questions  — chaque WAIT ouvert est publié en commentaire d'issue ;
                  la publication est un effet réconciliable par marqueur
  3. réponses   — un commentaire `/answer <id> <option>` d'un auteur
                  autorisé, postérieur à l'armement de la question,
                  enregistre la réponse ; l'ordonnanceur route
  4. rapports   — un item terminal reçoit son commentaire de clôture

Aucun parsing de langage naturel. Aucune lecture de GitHub comme état
d'item. Chaque prise de parole du rail est un effet : clé logique,
intention commise avant, réconciliation en relisant les commentaires.
"""

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

from psycopg import Connection

from . import channel, db, graph, kernel

API = "https://api.github.com"
LABEL = "graphatom"


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def _call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"{API}{path}", method=method,
            data=json.dumps(body).encode() if body else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or "null")

    def labeled_issues(self) -> list[dict]:
        rows = self._call(
            "GET", f"/repos/{self.repo}/issues?labels={LABEL}&state=open&per_page=100")
        return [r for r in rows if "pull_request" not in r]  # les PR sont des issues, pas pour nous

    def comments(self, number: int) -> list[dict]:
        return self._call(
            "GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100")

    def post_comment(self, number: int, body: str) -> None:
        self._call("POST", f"/repos/{self.repo}/issues/{number}/comments",
                   {"body": body})


def _issue_number(subject_key: str) -> int:
    return int(subject_key.rsplit("#", 1)[1])


def _speak(conn: Connection, gh: GitHub, number: int, item_id: int,
           key: str, body: str) -> None:
    """Une prise de parole du rail = un effet : commis avant, réconcilié par marqueur."""
    marker = f"<!-- graphatom:{key} -->"
    target = f"gh:{gh.repo}#{number}"
    with conn.transaction():
        conn.execute(
            "INSERT INTO effect (item_id, run_id, logical_key, target_uri, intent) "
            "VALUES (%s, NULL, %s, %s, %s) ON CONFLICT (target_uri, logical_key) DO NOTHING",
            (item_id, key, target, json.dumps({"comment": body})),
        )
    row = conn.execute(
        "SELECT observation FROM effect WHERE target_uri = %s AND logical_key = %s",
        (target, key),
    ).fetchone()
    if row["observation"] == "applied":
        return
    # réconciliation : la cible sait-elle déjà ? (crash possible entre POST et marquage)
    if not any(marker in c["body"] for c in gh.comments(number)):
        gh.post_comment(number, f"{marker}\n{body}")
        print(f"#{number} ← {key}", flush=True)
    conn.execute(
        "UPDATE effect SET observation = 'applied' "
        "WHERE target_uri = %s AND logical_key = %s", (target, key),
    )


def _admit_labeled(conn: Connection, gh: GitHub, revision: str) -> None:
    name = graph.load_bundle(conn, revision)["name"]
    for issue in gh.labeled_issues():
        subject_key = f"gh:{gh.repo}#{issue['number']}"
        known = conn.execute(
            "SELECT 1 FROM subject s JOIN work_item w ON w.subject_id = s.id "
            "WHERE s.graph = %s AND s.subject_key = %s LIMIT 1", (name, subject_key),
        ).fetchone()
        if known:  # une seule admission automatique — ré-admettre est un geste explicite
            continue
        try:
            item_id = kernel.admit(conn, revision, subject_key)
            print(f"#{issue['number']} admis → item {item_id}", flush=True)
        except RuntimeError as exc:
            print(f"#{issue['number']} refusé : {exc}", flush=True)


def _gh_questions(conn: Connection, gh: GitHub) -> list[dict]:
    return list(conn.execute(
        "SELECT q.*, s.subject_key, w.generation, "
        "  (SELECT max(at) FROM event e WHERE e.item_id = q.item_id "
        "   AND e.to_state = q.node) AS armed_at "
        "FROM question q JOIN work_item w ON w.id = q.item_id "
        "JOIN subject s ON s.id = w.subject_id "
        "WHERE q.state = 'open' AND s.subject_key LIKE %s ORDER BY q.id",
        (f"gh:{gh.repo}#%",),
    ))


def _publish_questions(conn: Connection, gh: GitHub) -> None:
    for q in _gh_questions(conn, gh):
        number = _issue_number(q["subject_key"])
        options = " / ".join(f"`{o}`" for o in q["options"])
        body = (f"**Question du rail** — pour @{q['owner']}, "
                f"avant le {q['deadline']:%d/%m %H:%M} UTC\n\n{q['text']}\n\n"
                f"Options : {options}\n"
                f"Répondre par un commentaire : `/answer {q['id']} <option>`")
        _speak(conn, gh, number, q["item_id"], f"q{q['id']}", body)


def _collect_answers(conn: Connection, gh: GitHub, allowed: set[str]) -> None:
    for q in _gh_questions(conn, gh):
        number = _issue_number(q["subject_key"])
        for c in gh.comments(number):
            body = c["body"].strip()
            if not body.startswith("/answer"):
                continue
            posted = dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
            if q["armed_at"] and posted < q["armed_at"]:
                continue  # commande d'une vie antérieure — jamais rejouée
            author = c["user"]["login"]
            parts = body.split()
            if author not in allowed:
                _speak(conn, gh, number, q["item_id"], f"reply-{c['id']}",
                       f"@{author} : seuls {', '.join(sorted(allowed))} "
                       "peuvent répondre aux questions du rail.")
                continue
            if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) != q["id"]:
                continue  # commande pour une autre question, ou malformée : ignorer
            err = channel.record_answer(conn, q["id"], parts[2], by=author)
            if err:
                _speak(conn, gh, number, q["item_id"], f"reply-{c['id']}",
                       f"@{author} : {err}")
            else:
                _speak(conn, gh, number, q["item_id"], f"q{q['id']}-receipt",
                       f"Réponse `{parts[2]}` enregistrée (par @{author}) — le rail reprend.")
                break  # la première réponse valide gagne


def _report_terminals(conn: Connection, gh: GitHub) -> None:
    rows = conn.execute(
        "SELECT w.*, s.subject_key FROM work_item w "
        "JOIN subject s ON s.id = w.subject_id "
        "WHERE w.terminal_at IS NOT NULL AND s.subject_key LIKE %s",
        (f"gh:{gh.repo}#%",),
    ).fetchall()
    for item in rows:
        number = _issue_number(item["subject_key"])
        events = conn.execute(
            "SELECT * FROM event WHERE item_id = %s ORDER BY item_version",
            (item["id"],),
        ).fetchall()
        lines = "\n".join(
            f"| v{e['item_version']} | {e['kind']} | "
            f"{(e['from_state'] + ' → ') if e['from_state'] else ''}{e['to_state']} | "
            f"{e['outcome'] or ''} |" for e in events)
        body = (f"**Item terminé : `{item['state']}`** "
                f"(génération {item['generation']}, {len(events)} transitions)\n\n"
                f"| v | événement | transition | issue |\n|---|---|---|---|\n{lines}")
        _speak(conn, gh, number, item["id"], f"g{item['generation']}-terminal", body)


def sync_forever(repo: str, bundle_path: str, poll_s: float = 15.0) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN manquant")
    allowed = set(filter(None, os.environ.get(
        "GRAPHATOM_ANSWERERS", repo.split("/")[0]).split(",")))
    gh = GitHub(repo, token)

    with db.connect() as conn:
        revision = graph.publish(conn, json.loads(open(bundle_path).read()))
        print(f"canal github sur {repo} — révision {revision[:12]}…, "
              f"répondants : {', '.join(sorted(allowed))}", flush=True)
        while True:
            try:
                _admit_labeled(conn, gh, revision)
                _publish_questions(conn, gh)
                _collect_answers(conn, gh, allowed)
                _report_terminals(conn, gh)
            except (urllib.error.URLError, OSError) as exc:
                print(f"github injoignable : {exc} — on réessaie", flush=True)
            time.sleep(poll_s)
