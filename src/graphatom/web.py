"""`graphatom serve` — la vitrine du journal, et la surface locale de réponse.

Un http.server stdlib, zéro dépendance. Trois pages :

    /            les questions ouvertes — la seule surface d'écriture
    /items       tous les items : sujet, état, version, escalades
    /item/<id>   la trajectoire : graph SVG avec l'état courant marqué,
                 journal v1..vN, runs, effets, questions

Un sujet de la forme `gh:<owner>/<repo>#<num>` devient partout un lien vers
l'issue, et la page d'un item porte le lien de sa PR quand le nœud release
en a laissé une dans `release.json` — la boucle se ferme dans les deux sens,
sans jamais appeler GitHub depuis ici.

Tout le reste est en lecture seule : cette interface montre le rail,
elle ne le pilote pas. Pas d'auth, pas d'exposition Internet, pas de
mutation d'items.
"""

import html
import json
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote

from . import channel, db
from .blocks import DATA_DIR
from .graph import load_bundle

STYLE = """
body { font-family: system-ui, sans-serif; max-width: 58rem; margin: 2rem auto;
       padding: 0 1rem; color: #1a1a1a; }
h1 { font-size: 1.3rem; } h1 small { color: #888; font-weight: normal; }
h2 { font-size: 1.05rem; margin-top: 1.6rem; }
nav a { margin-right: 1rem; color: #555; }
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
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #eee; }
th { color: #666; font-weight: 600; }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: 9px;
         font-size: .8rem; background: #eef; }
.badge.terminal { background: #e6e6e6; color: #555; }
.badge.active, .badge.applied { background: #d9f2d9; }
.badge.faulted, .badge.stale, .badge.superseded, .badge.rejected { background: #fde2e2; }
.badge.uncertain { background: #fff3cd; }
svg { max-width: 100%; height: auto; }
pre { white-space: pre-wrap; word-break: break-word; font-size: .8rem;
      background: #f6f6f6; border-radius: 6px; padding: .4rem .6rem;
      margin: .4rem 0 0; max-height: 16rem; overflow: auto; }
"""

REFRESH = "<meta http-equiv='refresh' content='5'>"
HEAD = "<!doctype html><meta charset='utf-8'>"


def _shell(title: str, body: str, refresh: bool = True) -> str:
    return (f"{HEAD}{REFRESH if refresh else ''}<title>{html.escape(title)}</title>"
            f"<style>{STYLE}</style>"
            "<nav><a href='/'>questions</a><a href='/items'>items</a></nav>"
            + body)


def _e(v) -> str:
    return html.escape(str(v))


# ------------------------------------------------------------------- sujet gh

GH_SUBJECT = re.compile(r"gh:([\w.-]+/[\w.-]+)#(\d+)")


def _subject(subject_key: str) -> str:
    """Le sujet en HTML : un lien vers l'issue quand il en est une, sinon du texte.

    Le frontend ne connaît GitHub que comme un format de sujet parmi
    d'autres : tout ce qui ne matche pas `gh:<owner>/<repo>#<num>` reste
    du texte brut, et le noyau reste agnostique.
    """
    m = GH_SUBJECT.fullmatch(subject_key)
    if not m:
        return _e(subject_key)
    return (f"<a href='https://github.com/{_e(m[1])}/issues/{_e(m[2])}'>"
            f"{_e(subject_key)}</a>")


def _pr(item_id: int) -> str:
    """Le lien vers la PR du cycle, quand le nœud release en a laissé une.

    La source est `release.json`, écrit par le nœud release à côté de son
    markdown : trois clés lues telles quelles, jamais un parseur de prose.
    Pas de fichier, pas d'URL dedans : pas de lien, et rien à dire.
    """
    path = DATA_DIR / f"item-{item_id}" / "release.json"
    if not path.is_file():
        return ""
    try:
        release = json.loads(path.read_text())
    except json.JSONDecodeError:  # la page le dit, elle ne devine pas — et reste lisible
        return " · <small>release.json illisible</small>"
    if not release.get("pr_url"):
        return ""
    number = release.get("pr_number")
    sha = str(release.get("merge_sha") or "")
    return (f" · <a href='{_e(release['pr_url'])}'>PR{f' #{_e(number)}' if number else ''}</a>"
            + (f" (mergée {_e(sha[:7])})" if sha else ""))


# ------------------------------------------------------------------ questions


def _questions_page(questions: list[dict], by: str, token: str, flash: str | None) -> str:
    parts = [f"<h1>graphatom <small>· répondre en tant que {_e(by)}</small></h1>"]
    if flash:
        parts.append(f"<p class='flash'>{_e(flash)}</p>")
    if not questions:
        parts.append("<p class='empty'>Aucune question ouverte. La page se rafraîchit toute seule.</p>")
    for q in questions:
        buttons = "".join(
            f"<button name='option' value='{_e(opt)}'>{_e(opt)}</button>"
            for opt in q["options"])
        parts.append(
            f"<div class='q'><div class='meta'>"
            f"[{q['id']}] {_subject(q['subject_key'])} · <a href='/item/{q['item_id']}'>item {q['item_id']}</a> "
            f"en <b>{_e(q['item_state'])}</b> · pour {_e(q['owner'])} "
            f"· avant {q['deadline']:%d/%m %H:%M} · escalades restantes {q['escalations']}"
            f"</div><div class='text'>{_e(q['text'])}</div>"
            f"<form method='post' action='/answer'>"
            f"<input type='hidden' name='question_id' value='{q['id']}'>"
            f"<input type='hidden' name='token' value='{token}'>"
            f"{buttons}</form></div>")
    return _shell("graphatom — questions", "".join(parts))


# ---------------------------------------------------------------------- items


def _items_page(conn) -> str:
    rows = conn.execute(
        "SELECT w.*, s.subject_key, s.graph FROM work_item w "
        "JOIN subject s ON s.id = w.subject_id ORDER BY w.id DESC"
    ).fetchall()
    body = ["<h1>items</h1>"]
    if not rows:
        body.append("<p class='empty'>Aucun item admis.</p>")
    else:
        lines = "".join(
            f"<tr><td><a href='/item/{r['id']}'>{r['id']}</a></td>"
            f"<td>{_e(r['graph'])}</td><td>{_subject(r['subject_key'])}</td>"
            f"<td>g{r['generation']}</td>"
            f"<td><span class='badge {'terminal' if r['terminal_at'] else 'active'}'>"
            f"{_e(r['state'])}</span></td>"
            f"<td>v{r['version']}</td><td>{r['escalations']}</td>"
            f"<td>{r['terminal_at'].strftime('%d/%m %H:%M') if r['terminal_at'] else 'actif'}</td></tr>"
            for r in rows)
        body.append("<table><tr><th>item</th><th>graph</th><th>sujet</th><th>gén.</th>"
                    "<th>état</th><th>version</th><th>escalades</th><th>fin</th></tr>"
                    f"{lines}</table>")
    return _shell("graphatom — items", "".join(body))


# ----------------------------------------------------------------- graph SVG


def _layers(bundle: dict) -> list[list[str]]:
    """Couches BFS depuis entry + cibles on_kernel — pour le placement."""
    nodes = bundle["nodes"]
    depth: dict[str, int] = {}
    queue = [(bundle["entry"], 0)]
    for target in bundle["on_kernel"].values():
        queue.append((target, 0))
    while queue:
        n, d = queue.pop(0)
        if n in depth and depth[n] <= d:
            continue
        depth[n] = d
        for t in (nodes[n].get("edges") or {}).values():
            queue.append((t, d + 1))
    layers: list[list[str]] = [[] for _ in range(max(depth.values()) + 1)]
    for n in sorted(depth, key=lambda k: (depth[k], k)):
        layers[depth[n]].append(n)
    return layers


def _graph_svg(bundle: dict, current: str) -> str:
    nodes = bundle["nodes"]
    layers = _layers(bundle)
    W, H, DX, DY, X0, Y0 = 132, 34, 185, 64, 12, 12
    pos = {n: (X0 + d * DX, Y0 + i * DY)
           for d, layer in enumerate(layers) for i, n in enumerate(layer)}
    # une arête par couple (source, cible) : deux issues vers la même cible
    # partagent le trait et l'étiquette, sinon elles se superposent illisiblement
    edges: list[tuple[str, str, list[str]]] = []
    for n, spec in nodes.items():
        merged: dict[str, list[str]] = {}
        for outcome, t in (spec.get("edges") or {}).items():
            merged.setdefault(t, []).append(outcome)
        edges.extend((n, t, outcomes) for t, outcomes in merged.items())

    # chaque arête de retour prend sa propre voie sous le graph : deux retours
    # partageant le même milieu écrivaient leur étiquette au même point
    backs = [(n, t) for n, t, _ in edges if pos[t][0] <= pos[n][0]]
    lanes = {couple: i for i, couple in enumerate(backs)}
    width = X0 + len(layers) * DX
    height = Y0 + max(len(la) for la in layers) * DY + 10 + 14 * len(lanes)

    parts = [f"<svg viewBox='0 0 {width} {height}' xmlns='http://www.w3.org/2000/svg'>",
             "<defs><marker id='arr' markerWidth='7' markerHeight='7' refX='6' refY='3' "
             "orient='auto'><path d='M0,0 L6,3 L0,6' fill='none' stroke='#999'/></marker></defs>"]
    used: set[tuple[float, float]] = set()
    for n, t, outcomes in edges:
        x1, y1 = pos[n]
        x2, y2 = pos[t]
        lane = lanes.get((n, t))
        if lane is None:
            sx, sy = x1 + W, y1 + H / 2
            ex, ey = x2, y2 + H / 2
            d = f"M{sx},{sy} C{sx + 40},{sy} {ex - 40},{ey} {ex},{ey}"
            lx, ly = (sx + ex) / 2, (sy + ey) / 2 - 5
        else:  # arête de retour : contourne par le bas, dans sa voie
            sx, sy = x1, y1 + H / 2
            ex, ey = x2 + W, y2 + H / 2
            my = height - 6 - lane * 14
            d = f"M{sx},{sy} C{sx - 60},{my} {ex + 60},{my} {ex},{ey}"
            lx, ly = (sx + ex) / 2, my - 4
        while (lx, ly) in used:  # deux milieux confondus : on décale, jamais on n'empile
            ly -= 10
        used.add((lx, ly))
        parts.append(f"<path d='{d}' fill='none' stroke='#999' marker-end='url(#arr)'/>")
        parts.append(f"<text x='{lx}' y='{ly}' font-size='9' fill='#777' "
                     f"text-anchor='middle'>{_e(', '.join(outcomes))}</text>")
    for n, spec in nodes.items():
        x, y = pos[n]
        fill = "#ffb74d" if n == current else ("#eee" if spec.get("terminal") else "#e3ecf7")
        dash = " stroke-dasharray='4 2'" if spec.get("escalade") else ""
        parts.append(f"<rect x='{x}' y='{y}' width='{W}' height='{H}' rx='7' "
                     f"fill='{fill}' stroke='#888'{dash}/>")
        label = n if spec.get("terminal") else f"{n} · {spec['block']}"
        parts.append(f"<text x='{x + W / 2}' y='{y + H / 2 + 4}' font-size='11' "
                     f"text-anchor='middle'>{_e(label)}</text>")
    parts.append("</svg>")
    return "".join(parts)


# ----------------------------------------------------------------------- item


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    return f"<table><tr>{head}</tr>{''.join(rows)}</table>"


# jsonb ne garde pas l'ordre des clés : le post-mortem d'abord, la trace après
RESULT_ORDER = ["outcome", "exit_code", "timeout", "error"]


def _result(result: dict | None) -> str:
    """Le résultat d'un run : les champs en ligne, la queue de log en bloc.

    L'autopsie d'une tentative crashée (`exit_code`, `timeout`, `log_tail`)
    se lit ici — sans elle, il fallait fouiller le workspace à la main.
    """
    if not result:
        return ""
    keys = [k for k in RESULT_ORDER if k in result]
    keys += sorted(k for k in result if k not in RESULT_ORDER and k != "log_tail")
    fields = " · ".join(f"{_e(k)} <b>{_e(result[k])}</b>" for k in keys)
    tail = result.get("log_tail")
    return fields + (f"<pre>{_e(tail)}</pre>" if tail else "")


def _item_page(conn, item_id: int) -> str | None:
    item = conn.execute(
        "SELECT w.*, s.subject_key, s.graph, s.lineage_budget FROM work_item w "
        "JOIN subject s ON s.id = w.subject_id WHERE w.id = %s", (item_id,)
    ).fetchone()
    if item is None:
        return None
    bundle = load_bundle(conn, item["revision"])
    state = "terminal" if item["terminal_at"] else "active"
    body = [
        f"<h1>item {item['id']} <small>· {_subject(item['subject_key'])}{_pr(item_id)} · "
        f"g{item['generation']} · {_e(item['graph'])} · rév. {item['revision'][:12]}…</small></h1>",
        f"<p><span class='badge {state}'>{_e(item['state'])}</span> "
        f"v{item['version']} · escalades restantes {item['escalations']} · "
        f"lignée restante {item['lineage_budget']} · "
        + (f"terminé {item['terminal_at']:%d/%m %H:%M:%S}" if item["terminal_at"]
           else f"wall deadline {item['wall_deadline']:%d/%m %H:%M}") + "</p>",
        _graph_svg(bundle, item["state"]),
    ]

    events = conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version", (item_id,)
    ).fetchall()
    body.append("<h2>journal</h2>" + _table(
        ["v", "à", "événement", "transition", "issue", "run"],
        [f"<tr><td>v{e['item_version']}</td><td>{e['at']:%d/%m %H:%M:%S}</td>"
         f"<td>{_e(e['kind'])}</td>"
         f"<td>{(_e(e['from_state']) + ' → ') if e['from_state'] else ''}{_e(e['to_state'])}</td>"
         f"<td>{_e(e['outcome'] or '')}</td><td>{e['run_id'] or ''}</td></tr>"
         for e in events]))

    runs = conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s ORDER BY id", (item_id,)
    ).fetchall()
    if runs:
        body.append("<h2>runs</h2>" + _table(
            ["run", "nœud", "tentative", "statut", "issue", "fence", "bail", "résultat"],
            [f"<tr><td>{r['id']}</td><td>{_e(r['node'])}</td><td>{r['attempt']}</td>"
             f"<td><span class='badge {_e(r['status'])}'>{_e(r['status'])}</span></td>"
             f"<td>{_e(r['outcome'] or '')}</td><td>{r['fence']}</td>"
             f"<td>{r['lease_expires_at']:%H:%M:%S}</td>"
             f"<td>{_result(r['result'])}</td></tr>" for r in runs]))

    effects = conn.execute(
        "SELECT * FROM effect WHERE item_id = %s ORDER BY op_id", (item_id,)
    ).fetchall()
    if effects:
        body.append("<h2>effets</h2>" + _table(
            ["clé logique", "cible", "observation"],
            [f"<tr><td><code>{_e(f['logical_key'])}</code></td><td>{_e(f['target_uri'])}</td>"
             f"<td><span class='badge {_e(f['observation'])}'>{_e(f['observation'])}</span></td></tr>"
             for f in effects]))

    questions = conn.execute(
        "SELECT * FROM question WHERE item_id = %s ORDER BY id", (item_id,)
    ).fetchall()
    if questions:
        body.append("<h2>questions</h2>" + _table(
            ["q", "nœud", "question", "état", "réponse", "par", "deadline"],
            [f"<tr><td>{q['id']}</td><td>{_e(q['node'])}</td><td>{_e(q['text'])}</td>"
             f"<td>{_e(q['state'])}</td>"
             f"<td>{_e(q['answer'] or '')}</td><td>{_e(q['answered_by'] or '')}</td>"
             f"<td>{q['deadline']:%d/%m %H:%M}</td></tr>" for q in questions]))

    workspace = DATA_DIR / f"item-{item_id}"
    files = sorted(p for p in workspace.iterdir() if p.is_file()) if workspace.is_dir() else []
    if files:
        body.append("<h2>workspace</h2>")
        for p in files:
            href = f"/item/{item_id}/file/{quote(p.name)}"
            if p.suffix == ".png":  # les screenshots des agents = la preview
                body.append(f"<p>{_e(p.name)}</p><a href='{href}'>"
                            f"<img src='{href}' style='max-width:100%;border:1px solid #ddd'></a>")
            else:
                body.append(f"<p><a href='{href}'>{_e(p.name)}</a> "
                            f"<small>({p.stat().st_size} o)</small></p>")

    return _shell(f"graphatom — item {item_id}", "".join(body))


def _file_response(item_id: int, name: str) -> tuple[bytes, str] | None:
    workspace = (DATA_DIR / f"item-{item_id}").resolve()
    path = (workspace / name).resolve()
    if not (path.is_file() and path.parent == workspace):  # pas de traversée
        return None
    ctype = ("image/png" if path.suffix == ".png"
             else "application/json" if path.suffix == ".json"
             else "text/plain")
    return path.read_bytes(), f"{ctype}; charset=utf-8"


# --------------------------------------------------------------------- notify


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


# ---------------------------------------------------------------------- serve


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
            path, _, query = self.path.partition("?")
            parts = path.split("/")
            if len(parts) == 5 and parts[1] == "item" and parts[2].isdigit() and parts[3] == "file":
                found = _file_response(int(parts[2]), unquote(parts[4]))
                if found is None:
                    return self._respond(404, "introuvable")
                data, ctype = found
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                return self.wfile.write(data)
            try:
                with db.connect() as conn:
                    if path == "/":
                        flash = parse_qs(query).get("m", [None])[0]
                        page = _questions_page(channel.open_questions(conn), by, token, flash)
                    elif path == "/items":
                        page = _items_page(conn)
                    elif path.startswith("/item/") and path[6:].isdigit():
                        page = _item_page(conn, int(path[6:]))
                    else:
                        page = None
            except Exception as exc:  # une vitrine qui tombe le dit, elle ne coupe pas
                return self._respond(500, f"erreur : {_e(exc)}")
            if page is None:
                return self._respond(404, "introuvable")
            self._respond(200, page)

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
