"""Le canal GitHub — module hors noyau, par polling.

GitHub est l'interface humaine et la cible des effets ; Postgres reste
l'unique autorité d'exécution. Ce module fait six choses, et refuse
tout le reste :

  1. admission  — une issue ouverte portant le label `graphatom` devient
                  un sujet (une seule admission automatique par issue) ;
                  une ligne `Depends-on: #N` dans le corps diffère
                  l'admission tant que l'issue visée est ouverte ; le titre
                  de l'issue est posé sur le sujet au passage — le canal l'a
                  sous la main, le frontend le lira en base et jamais ici
  2. accusé     — l'occurrence ouverte reçoit son commentaire de prise en
                  charge : item, graph, lien trajectoire — une seule fois ;
                  ce commentaire est ensuite réécrit à chaque transition
                  pour porter la trajectoire en direct (une édition ne
                  notifie personne : le tableau de bord ne coûte aucun spam)
  3. questions  — chaque WAIT ouvert est publié en commentaire d'issue ;
                  la publication est un effet réconciliable par marqueur ;
                  le `validate.md` du workspace, quand il est là, part avec
                  la question — l'humain voit les critères cochés et leurs
                  preuves, pas seulement « on garde ? »
  4. réponses   — un commentaire `/answer <id> <option>` d'un auteur
                  autorisé, postérieur à l'armement de la question,
                  enregistre la réponse ; l'ordonnanceur route
  5. état       — un label `rail:<état>` projette l'état de l'item actif,
                  repeint à chaque tick sur les issues ouvertes, retiré au
                  terminal sans condition — l'issue peut être déjà fermée ;
                  `rail:stalled` s'y ajoute quand le worker ne bat plus
  6. rapports   — un item terminal reçoit son commentaire de clôture

Aucun parsing de langage naturel. Aucune lecture de GitHub comme état
d'item. Chaque prise de parole du rail est un effet : clé logique,
intention commise avant, réconciliation en relisant les commentaires.
"""

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from psycopg import Connection

from . import channel, db, graph, heartbeat, kernel
from .blocks import item_workspace

API = "https://api.github.com"
LABEL = "graphatom"
RAIL = "rail:"        # préfixe des labels d'état — l'espace de noms du rail
RAIL_COLOR = "1f6feb"  # couleur unie : un label d'état se reconnaît d'un coup d'œil
BLOCKED = f"{RAIL}blocked"  # pas un état d'item : une admission différée se voit
STALLED = f"{RAIL}stalled"  # pas un état d'item non plus : le worker ne bat plus
STALLED_COLOR = "d73a4a"    # le rouge de l'alarme, seule exception à la couleur unie
DEPENDS = "Depends-on:"     # la grammaire fermée des dépendances, dans le corps
CHECKLIST = "validate.md"   # la checklist du nœud validate, dans le workspace de l'item
CHECKLIST_LINES = 40        # ce qu'on en cite : de quoi tenir sans noyer la question
TIMEOUT_S = 30.0            # tout appel sortant est borné : au-delà, GitHub est injoignable


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def _call(self, method: str, path: str, body: dict | None = None):
        """Le seul appel sortant du module — donc le seul endroit à borner.

        Sans `timeout`, un serveur qui accepte la connexion et ne répond
        jamais gèle le canal pour toujours : pas d'erreur, pas de tick
        suivant, rien à voir nulle part. Avec, l'attente a une fin, et cette
        fin est un `TimeoutError` — l'incident réseau déjà géré par `tick`.
        """
        req = urllib.request.Request(
            f"{API}{path}", method=method,
            data=json.dumps(body).encode() if body else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read() or "null")

    def labeled_issues(self) -> list[dict]:
        # GRAPHATOM_TAKE_ALL : le déploiement prend toute issue ouverte en charge,
        # sans attendre le label — pour un repo où le rail est le seul mainteneur
        label = "" if os.environ.get("GRAPHATOM_TAKE_ALL") else f"labels={LABEL}&"
        rows = self._call(
            "GET", f"/repos/{self.repo}/issues?{label}state=open&per_page=100")
        return [r for r in rows if "pull_request" not in r]  # les PR sont des issues, pas pour nous

    def issue(self, number: int) -> dict | None:
        # vise l'issue par son numéro : ouverte, fermée — ou None, elle n'existe pas
        try:
            return self._call("GET", f"/repos/{self.repo}/issues/{number}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            return None

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

    def issue_labels(self, number: int) -> list[str]:
        # vise l'issue par son numéro : ouverte ou fermée, l'API répond pareil
        rows = self._call(
            "GET", f"/repos/{self.repo}/issues/{number}/labels?per_page=100")
        return [r["name"] for r in rows]

    def create_label(self, name: str) -> None:
        # les labels du rail naissent à la volée ; 422 = il existe déjà, tant mieux
        color = STALLED_COLOR if name == STALLED else RAIL_COLOR
        try:
            self._call("POST", f"/repos/{self.repo}/labels",
                       {"name": name, "color": color})
        except urllib.error.HTTPError as exc:
            if exc.code != 422:
                raise

    def add_label(self, number: int, name: str) -> None:
        self._call("POST", f"/repos/{self.repo}/issues/{number}/labels",
                   {"labels": [name]})

    def remove_label(self, number: int, name: str) -> None:
        # 404 : quelqu'un l'a retiré entre la lecture et l'écriture — c'était le but
        try:
            self._call(
                "DELETE",
                f"/repos/{self.repo}/issues/{number}/labels/{urllib.parse.quote(name)}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise


def _issue_number(subject_key: str) -> int:
    return int(subject_key.rsplit("#", 1)[1])


def _web() -> str:
    return os.environ.get("GRAPHATOM_WEB_URL", "http://127.0.0.1:8850")


def _pending(conn: Connection, item_id: int, target: str, key: str,
             intent: dict) -> bool:
    """Commet l'intention d'un effet ; faux si cet effet est déjà appliqué."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO effect (item_id, run_id, logical_key, target_uri, intent) "
            "VALUES (%s, NULL, %s, %s, %s) ON CONFLICT (target_uri, logical_key) DO NOTHING",
            (item_id, key, target, json.dumps(intent)),
        )
    row = conn.execute(
        "SELECT observation FROM effect WHERE target_uri = %s AND logical_key = %s",
        (target, key),
    ).fetchone()
    return row["observation"] != "applied"


def _applied(conn: Connection, target: str, key: str) -> None:
    conn.execute(
        "UPDATE effect SET observation = 'applied' "
        "WHERE target_uri = %s AND logical_key = %s", (target, key),
    )


def _speak(conn: Connection, gh: GitHub, number: int, item_id: int,
           key: str, body: str) -> None:
    """Une prise de parole du rail = un effet : commis avant, réconcilié par marqueur."""
    marker = f"<!-- graphatom:{key} -->"
    target = f"gh:{gh.repo}#{number}"
    if not _pending(conn, item_id, target, key, {"comment": body}):
        return
    # réconciliation : la cible sait-elle déjà ? (crash possible entre POST et marquage)
    if not any(marker in c["body"] for c in gh.comments(number)):
        gh.post_comment(number, f"{marker}\n{body}")
        print(f"#{number} ← {key}", flush=True)
    _applied(conn, target, key)


def _say_once(gh: GitHub, number: int, key: str, body: str,
              said: set[tuple[int, str]]) -> None:
    """Une prise de parole d'avant l'admission : sans item, donc sans effet.

    Un effet est rattaché à un item ; ici il n'y en a pas encore. Le marqueur
    posé dans l'issue tient lieu de mémoire — c'est déjà la réconciliation de
    `_speak`, sans la ligne en base. `said` évite d'aller relire les
    commentaires à chaque tick ; au redémarrage, le marqueur reprend le relais.
    """
    if (number, key) in said:
        return
    marker = f"<!-- graphatom:{key} -->"
    if not any(marker in c["body"] for c in gh.comments(number)):
        gh.post_comment(number, f"{marker}\n{body}")
        print(f"#{number} ← {key}", flush=True)
    said.add((number, key))


def _depends_on(body: str) -> list[str]:
    """Le membre de droite de chaque ligne `Depends-on: …` du corps.

    Grammaire fermée, comme `/answer` : la ligne est prise au mot, et rien
    d'autre n'est lu. Une task list `- [ ] #29` est de la lisibilité GitHub,
    jamais une dépendance — la vérité du rail est ici.
    """
    return [line.strip()[len(DEPENDS):].strip()
            for line in (body or "").splitlines()
            if line.strip().startswith(DEPENDS)]


def _issue_state(gh: GitHub, number: int, states: dict[int, str | None]) -> str | None:
    """`open`, `closed`, ou None si l'issue n'existe pas — un cache par tick.

    Deux issues qui dépendent de la même troisième ne la lisent qu'une fois.
    """
    if number not in states:
        issue = gh.issue(number)
        states[number] = issue["state"] if issue else None
    return states[number]


def _pending_deps(gh: GitHub, issue: dict, name: str, states: dict[int, str | None],
                  said: set[tuple[int, str]]) -> list[int]:
    """Les dépendances encore ouvertes de l'issue — vide si elle peut être admise.

    Le corps est lu au moment de l'admission seulement, et rien n'en reste en
    base : la condition se réévalue à chaque tick, sur les issues non admises.
    Une déclaration invalide — numéro illisible, renvoi à soi-même, issue
    inexistante — est ignorée, mais dite une fois : rien ne passe en silence.
    """
    number, waiting, invalid = issue["number"], [], []
    for raw in _depends_on(issue["body"]):
        if not (raw.startswith("#") and raw[1:].isdigit()):
            invalid.append(f"`{DEPENDS} {raw}` — attendu `{DEPENDS} #<numéro>`")
            continue
        dep = int(raw[1:])
        if dep == number:
            invalid.append(f"`{DEPENDS} #{dep}` — une issue ne dépend pas d'elle-même")
            continue
        state = _issue_state(gh, dep, states)
        if state is None:
            invalid.append(f"`{DEPENDS} #{dep}` — cette issue n'existe pas")
        elif state == "open":
            waiting.append(dep)
    if invalid:
        _say_once(gh, number, f"{name}-depends-invalid",
                  "**Dépendances ignorées** — le rail ne lit que "
                  f"`{DEPENDS} #<numéro>`, vers une autre issue existante :\n\n"
                  + "\n".join(f"- {line}" for line in invalid), said)
    return waiting


def _remember_title(conn: Connection, name: str, subject_key: str,
                    title: str) -> None:
    """Range le titre de l'issue sur le sujet, quand il a bougé.

    L'admission le pose une première fois ; ce rafraîchissement ne coûte
    rien — le titre est déjà dans la liste des issues du tick — et rattrape
    les deux cas où l'admission ne suffit pas : un titre édité sur GitHub,
    et les sujets admis avant que la colonne existe. Un sujet inconnu n'a
    pas de ligne : la mise à jour ne touche rien, et c'est bien ainsi.
    """
    conn.execute(
        "UPDATE subject SET title = %s WHERE graph = %s AND subject_key = %s "
        "AND title IS DISTINCT FROM %s",
        (title, name, subject_key, title),
    )


def _admit_labeled(conn: Connection, gh: GitHub, revision: str,
                   said: set[tuple[int, str]]) -> set[int]:
    """L'admission automatique, et son seul frein : les dépendances déclarées.

    Renvoie les numéros d'issues dont l'admission est différée — le repeint
    des labels les montre `rail:blocked`. Tant qu'une dépendance est ouverte,
    aucun item n'est créé ; quand la dernière se ferme, l'admission part au
    tick suivant par le chemin normal.
    """
    name = graph.load_bundle(conn, revision)["name"]
    blocked: set[int] = set()
    states: dict[int, str | None] = {}
    for issue in gh.labeled_issues():
        subject_key = f"gh:{gh.repo}#{issue['number']}"
        _remember_title(conn, name, subject_key, issue["title"])
        known = conn.execute(
            "SELECT 1 FROM subject s JOIN work_item w ON w.subject_id = s.id "
            "WHERE s.graph = %s AND s.subject_key = %s LIMIT 1", (name, subject_key),
        ).fetchone()
        if known:  # une seule admission automatique — ré-admettre est un geste explicite
            continue
        waiting = _pending_deps(gh, issue, name, states, said)
        if waiting:
            blocked.add(issue["number"])
            refs = ", ".join(f"#{dep}" for dep in waiting)
            _say_once(gh, issue["number"], f"{name}-blocked",
                      f"**Admission différée** — en attente de {refs}.\n\n"
                      f"Le corps déclare `{DEPENDS} #<numéro>` : le rail admet "
                      "cette issue au tick qui suit la fermeture de la dernière "
                      "dépendance.", said)
            continue
        try:
            item_id = kernel.admit(conn, revision, subject_key, issue["title"])
            print(f"#{issue['number']} admis → item {item_id}", flush=True)
        except RuntimeError as exc:
            print(f"#{issue['number']} refusé : {exc}", flush=True)
    return blocked


def _gh_items(conn: Connection, gh: GitHub, where: str = "") -> list[dict]:
    return conn.execute(
        "SELECT w.*, s.subject_key, s.graph FROM work_item w "
        "JOIN subject s ON s.id = w.subject_id "
        f"WHERE s.subject_key LIKE %s {where} ORDER BY w.id",
        (f"gh:{gh.repo}#%",),
    ).fetchall()


def _ack_key(item: dict) -> str:
    # la clé porte le graph et la génération : chaque occurrence a droit à
    # son accusé, une ré-admission n'est pas un doublon
    return f"{item['graph']}-g{item['generation']}-admitted"


def _ack_body(item: dict) -> str:
    return (f"**Prise en charge par le rail** — item {item['id']}, "
            f"graph `{item['graph']}` (génération {item['generation']}).\n\n"
            f"Le label `{RAIL}<état>` suit la trajectoire ; les questions "
            f"arrivent ici en commentaire.\n"
            f"Trajectoire et artefacts : {_web()}/item/{item['id']}")


def _acknowledge(conn: Connection, gh: GitHub) -> None:
    """Accusé de prise en charge : entre l'admission et la première question,
    l'issue dit déjà qu'on travaille dessus.

    Un acte de parole comme les autres — une fois l'effet appliqué, le geste
    ne coûte plus qu'une lecture en base : jamais deux accusés par occurrence.
    """
    for item in _gh_items(conn, gh):
        _speak(conn, gh, _issue_number(item["subject_key"]), item["id"],
               _ack_key(item), _ack_body(item))


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


def _checklist(item_id: int, web: str) -> str:
    """Les critères cochés par `validate`, cités dans la question qui suit.

    Le nœud `validate` laisse `validate.md` dans le workspace de l'item :
    une ligne par critère, la case, la preuve constatée. Une question posée
    après lui l'embarque telle quelle — répondre « on garde ? » sans la
    checklist, c'était juger sur la seule bonne foi des agents de test.

    Le fichier est cité en bloc de citation, borné aux premières lignes :
    le rail ne réécrit pas ce qu'un agent a écrit, il le montre. Pas de
    fichier — cycle d'avant `validate`, question posée avant lui, workspace
    hors de portée du conteneur : rien, et la question reste ce qu'elle était.
    """
    try:
        lines = (item_workspace(item_id) / CHECKLIST).read_text().splitlines()
    except OSError:
        return ""
    if not lines:  # fichier vide : rien à citer, et surtout pas un bloc vide
        return ""
    quoted = [f"> {line}".rstrip() for line in lines[:CHECKLIST_LINES]]
    reste = len(lines) - CHECKLIST_LINES
    if reste > 0:
        quoted.append(f"> *(… {reste} lignes de plus)*")
    return ("**Critères de succès, cochés par `validate`** — "
            f"[{CHECKLIST}]({web}/item/{item_id}/file/{CHECKLIST})\n\n"
            + "\n".join(quoted) + "\n\n")


def _publish_questions(conn: Connection, gh: GitHub) -> None:
    for q in _gh_questions(conn, gh):
        number = _issue_number(q["subject_key"])
        options = " / ".join(f"`{o}`" for o in q["options"])
        web = _web()
        body = (f"**Question du rail** — pour @{q['owner']}, "
                f"avant le {q['deadline']:%d/%m %H:%M} UTC\n\n{q['text']}\n\n"
                f"{_checklist(q['item_id'], web)}"
                f"Options : {options}\n"
                f"Répondre par un commentaire : `/answer {q['id']} <option>`\n"
                f"Trajectoire et artefacts (previews) : {web}/item/{q['item_id']}")
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


def _paint_states(conn: Connection, gh: GitHub, blocked: set[int],
                  stalled: bool) -> None:
    """Les labels du rail : une projection possédée par le rail.

    Comme la colonne d'un board — jamais lue comme état d'item, seulement
    réécrite depuis la base : les anciens labels rail partent, ceux qu'on
    veut arrivent. Un item terminal n'en porte aucun (le rapport suffit) et
    un label bricolé à la main est simplement repeint au tick suivant.

    `rail:blocked` sort du même pinceau sans venir de la base : une issue
    dont l'admission est différée n'a pas d'item, donc pas d'état — le label
    part tout seul au tick où elle est enfin admise.

    `rail:stalled` non plus n'est pas un état : c'est le battement du worker
    qui est trop vieux. Il s'ajoute au label d'état des items actifs, et
    part au retour du battement. Ce sync est un processus séparé du worker :
    il survit à sa mort, et c'est tout l'intérêt — le problème se voit sur
    GitHub précisément quand le worker ne peut plus rien dire.
    """
    wanted = {
        _issue_number(r["subject_key"]): {f"{RAIL}{r['state']}"}
        | ({STALLED} if stalled else set())
        for r in _gh_items(conn, gh, "AND w.terminal_at IS NULL")
    }
    wanted.update({number: {BLOCKED} for number in blocked})
    for issue in gh.labeled_issues():
        number = issue["number"]
        want = wanted.get(number, set())
        painted = {lab["name"] for lab in issue["labels"]
                   if lab["name"].startswith(RAIL)}
        for name in sorted(want - painted):
            gh.create_label(name)
            gh.add_label(number, name)
            print(f"#{number} ← {name}", flush=True)
        for stale in sorted(painted - want):
            gh.remove_label(number, stale)
            print(f"#{number} ⌫ {stale}", flush=True)


def _clear_terminal_labels(conn: Connection, gh: GitHub) -> None:
    """Le retrait du label d'état au terminal — inconditionnel.

    Le repeint par tick ne voit que les issues ouvertes : une issue fermée
    par le merge de sa propre PR garderait son `rail:<état>` pour toujours.
    Ici on vise l'issue par son numéro, ouverte ou fermée, et l'effet retient
    que c'est fait. Le premier tick réconcilie donc l'existant — tout item
    déjà terminal dont l'issue porte encore un label du rail — et les ticks
    suivants ne relisent plus rien.
    """
    active = {_issue_number(r["subject_key"])
              for r in _gh_items(conn, gh, "AND w.terminal_at IS NULL")}
    for item in _gh_items(conn, gh, "AND w.terminal_at IS NOT NULL"):
        number = _issue_number(item["subject_key"])
        if number in active:
            continue  # une autre génération tourne : le label est à elle
        key = f"{item['graph']}-g{item['generation']}-unlabeled"
        target = f"gh:{gh.repo}#{number}"
        if not _pending(conn, item["id"], target, key, {"unlabel": RAIL}):
            continue
        for name in gh.issue_labels(number):
            if name.startswith(RAIL):
                gh.remove_label(number, name)
                print(f"#{number} ⌫ {name}", flush=True)
        _applied(conn, target, key)


def _journal(events: list[dict]) -> str:
    """La trajectoire au format du journal : une ligne par transition."""
    return "\n".join(
        f"v{e['item_version']:>2} {e['at']:%H:%M:%S} {e['kind']:<9} "
        f"{(e['from_state'] + ' → ') if e['from_state'] else ''}{e['to_state']}"
        f"{(' [' + e['outcome'] + ']') if e['outcome'] else ''}" for e in events)


def _paint_trajectories(conn: Connection, gh: GitHub, drawn: dict[int, int]) -> None:
    """La trajectoire vivante : l'accusé tient le journal à jour.

    Le label ne dit que le présent ; l'histoire n'arrivait qu'au rapport
    terminal. À chaque transition, le commentaire d'accusé est réécrit —
    une édition ne notifie personne, donc zéro spam, et l'issue devient le
    tableau de bord en direct.

    Comme les labels, c'est une projection : le corps est reconstruit depuis
    la base, jamais complété à l'aveugle. `drawn` retient la version déjà
    peinte pour ne rien faire quand rien n'a bougé ; un PATCH raté n'y entre
    pas et se retente au tick suivant, sans bloquer le rail.
    """
    for item in _gh_items(conn, gh):
        if drawn.get(item["id"]) == item["version"]:
            continue
        number = _issue_number(item["subject_key"])
        marker = f"<!-- graphatom:{_ack_key(item)} -->"
        events = conn.execute(
            "SELECT * FROM event WHERE item_id = %s ORDER BY item_version",
            (item["id"],),
        ).fetchall()
        body = (f"{marker}\n{_ack_body(item)}\n\n"
                f"**Trajectoire** (v{item['version']}, {len(events)} transitions)\n\n"
                f"```\n{_journal(events)}\n```")
        try:
            comment = next(
                (c for c in gh.comments(number) if marker in c["body"]), None)
            if comment is None:
                continue  # l'accusé n'est pas encore posté — au prochain tick
            if comment["body"] != body:
                gh.edit_comment(comment["id"], body)
                print(f"#{number} ✎ trajectoire v{item['version']}", flush=True)
        except (urllib.error.URLError, OSError) as exc:
            print(f"#{number} trajectoire non éditée : {exc} — on réessaie", flush=True)
            continue
        drawn[item["id"]] = item["version"]


def _report_terminals(conn: Connection, gh: GitHub) -> None:
    for item in _gh_items(conn, gh, "AND w.terminal_at IS NOT NULL"):
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
        # la clé porte le graph : deux graphs peuvent traiter la même issue,
        # chacun a droit à son rapport de génération
        _speak(conn, gh, number, item["id"],
               f"{item['graph']}-g{item['generation']}-terminal", body)


def tick(conn: Connection, gh: GitHub, revision: str, allowed: set[str],
         drawn: dict[int, int], said: set[tuple[int, str]]) -> None:
    """Un tour du canal : les gestes dans l'ordre, et l'incident réseau au bout.

    Injoignable, lent ou muet : GitHub casse toujours de la même façon, une
    `OSError` — et un timeout n'est rien d'autre. Le tour s'arrête là, la
    ligne le dit, le tick suivant repart. Rien n'est perdu : chaque geste est
    un effet, commis en base avant d'être fait, donc réconcilié à la reprise.
    """
    try:
        # le battement du worker ne sert qu'à cette projection : le sync ne
        # décide d'aucun état avec — la base reste l'autorité
        stalled = heartbeat.stalled(heartbeat.last(conn, heartbeat.RAIL))
        blocked = _admit_labeled(conn, gh, revision, said)
        _acknowledge(conn, gh)
        _publish_questions(conn, gh)
        _collect_answers(conn, gh, allowed)
        _paint_states(conn, gh, blocked, stalled)
        _paint_trajectories(conn, gh, drawn)
        _report_terminals(conn, gh)
        _clear_terminal_labels(conn, gh)
    except (urllib.error.URLError, OSError) as exc:
        print(f"github injoignable : {exc} — on réessaie", flush=True)


def sync_forever(repo: str, bundle_path: str, poll_s: float = 15.0) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN manquant")
    allowed = set(filter(None, os.environ.get(
        "GRAPHATOM_ANSWERERS", repo.split("/")[0]).split(",")))
    gh = GitHub(repo, token)
    drawn: dict[int, int] = {}   # item → version de trajectoire déjà peinte
    said: set[tuple[int, str]] = set()  # actes de parole d'avant l'admission, déjà dits

    with db.connect() as conn:
        revision = graph.publish(conn, json.loads(open(bundle_path).read()))
        print(f"canal github sur {repo} — révision {revision[:12]}…, "
              f"répondants : {', '.join(sorted(allowed))}", flush=True)
        while True:
            # hors du tick, donc hors de sa rattrape d'incident réseau : ce
            # battement dit que la boucle tourne, pas que GitHub répond
            heartbeat.beat(conn, heartbeat.GITHUB_SYNC)
            tick(conn, gh, revision, allowed, drawn, said)
            time.sleep(poll_s)
