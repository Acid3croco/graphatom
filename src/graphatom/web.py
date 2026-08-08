"""`graphatom serve` — la vitrine du journal, et la surface locale de réponse.

Un http.server stdlib, zéro dépendance. Trois pages :

    /            les questions ouvertes — la seule surface d'écriture
    /items       tous les items : issue, titre, graph, état, version…
    /item/<id>   la trajectoire : graph SVG avec l'état courant marqué,
                 journal v1..vN, runs, effets, questions

Les mêmes vues se lisent en JSON, pour un client qui rend la page lui-même :

    /api/items       la liste, avec l'état, les liens issue et PR
    /api/item/<id>   l'item entier : graph, journal, runs, questions,
                     critères, fichiers du workspace
    /api/questions   les questions ouvertes, et le jeton de `POST /answer`
    /api/heartbeat   les deux battements bruts : le worker, et le canal GitHub
    /api/graphs      les révisions publiées : nom, date, items qui la portent
    /api/graph/<rév> le bundle entier de cette révision, config comprise

Une projection, pas un second modèle : mêmes requêtes, mêmes durées, mêmes
totaux de tokens que les pages — seul le rendu change. Rien ne s'y écrit :
`POST /answer` reste l'unique porte, et sert les deux clients — du JSON à
qui envoie `Accept: application/json`, la redirection d'avant au formulaire.

La page d'un item répond aussi à « combien coûte un cycle ? » : chaque
transition porte sa durée, chaque run ses tokens, et l'en-tête le total
des deux. Rien de nouveau en base — les durées sortent des horodatages du
journal, les tokens du résultat des runs.

L'en-tête commun porte le battement du worker : « rail vivant il y a 3 s »,
ou le bandeau rouge quand plus rien ne bat — les états montrés sont alors
figés, et la page le dit avant qu'on les croie. Une requête d'une ligne
par rendu.

Chaque page se rafraîchit toute seule, sans rien changer au serveur : elle
porte son marqueur de fraîcheur en meta, et quinze lignes de JS le comparent
toutes les 5 s à celui d'un `fetch` de la même URL. Marqueur identique :
rien ne bouge. Marqueur différent : le contenu du conteneur est remplacé,
le scroll reste où il est. Sans JavaScript, le rafraîchissement complet
d'avant tient toujours — il vit dans un `noscript`.

Un sujet de la forme `gh:<owner>/<repo>#<num>` devient partout un lien vers
l'issue, et la page d'un item porte le lien de sa PR quand le nœud release
en a laissé une dans `release.json` — la boucle se ferme dans les deux sens,
sans jamais appeler GitHub depuis ici.

Le titre de l'issue s'affiche partout où l'item se montre : le canal l'a
rangé sur le sujet à l'admission, et cette page ne fait que le lire en
base. Un sujet d'un autre canal n'en a pas : la cellule reste vide.

Tout le reste est en lecture seule : cette interface montre le rail,
elle ne le pilote pas. Pas d'auth, pas d'exposition Internet, pas de
mutation d'items.
"""

import datetime as dt
import html
import json
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote

from . import channel, db, heartbeat
from .blocks import item_workspace
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
.beat { color: #2e7d32; font-size: .85rem; margin: .2rem 0 0; }
.stalled { background: #fde2e2; border: 1px solid #f0a0a0; border-radius: 6px;
           padding: .5rem 1rem; color: #a11020; font-weight: 600; }
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
MARKER = "graphatom-version"

# Le rafraîchissement en douceur, en JS de base : un fetch de la page
# courante toutes les 5 s, le marqueur comparé, et le conteneur repeint
# seulement s'il a bougé. Pas de WebSocket, pas de SSE, pas d'endpoint
# JSON : le serveur ne sait même pas que ce script existe.
LIVE = """
const live = document.getElementById('live');
const mark = doc => doc.querySelector("meta[name=%s]")?.content;
let version = mark(document);
setInterval(async () => {
  // onglet caché, ou l'opérateur a la main sur le formulaire : on ne touche à rien
  if (document.hidden || live.contains(document.activeElement)) return;
  try {
    const html = await (await fetch(location.href, {cache: 'no-store'})).text();
    const fresh = new DOMParser().parseFromString(html, 'text/html');
    if (!mark(fresh) || mark(fresh) === version) return;
    version = mark(fresh);
    live.innerHTML = fresh.getElementById('live').innerHTML;  // le conteneur reste, le scroll aussi
  } catch (err) {
    console.warn('graphatom : rafraîchissement raté', err);  // le prochain tour réessaie
  }
}, 5000);
""" % MARKER


def _marker(version: int, beat: dt.datetime | None) -> str:
    """Le marqueur de fraîcheur de la page : ce qui doit la faire repeindre.

    La version de ce qui est affiché — celle de l'item, ou la plus haute
    des items listés — et l'état du battement, qui change l'en-tête sans
    changer aucune version : sans lui, un rail qui s'arrête ne poserait
    jamais son bandeau rouge sur une page déjà ouverte.
    """
    return f"v{version}-{'stalled' if heartbeat.stalled(beat) else 'live'}"


def _shell(title: str, body: str, beat: dt.datetime | None, version: int) -> str:
    return (f"{HEAD}<title>{html.escape(title)}</title>"
            f"<meta name='{MARKER}' content='{_marker(version, beat)}'>"
            f"<noscript>{REFRESH}</noscript>"  # sans JS : le rechargement d'avant
            f"<style>{STYLE}</style>"
            "<nav><a href='/'>questions</a><a href='/items'>items</a></nav>"
            f"<main id='live'>{_beat(beat)}{body}</main>"
            f"<script>{LIVE}</script>")


def _ago(seconds: float) -> str:
    """Un âge lisible : en secondes sous la minute, en minutes au-delà."""
    return f"{seconds:.0f} s" if seconds < 60 else f"{seconds / 60:.0f} min"


def _beat(at: dt.datetime | None) -> str:
    """Le battement du worker, en en-tête de chaque page.

    Un rail vivant tamponne à chaque tick ; sans battement depuis deux
    minutes, plus rien ne tourne — le faucheur ne classe plus, l'escalade
    ne part plus, et tout ce que la page montre est figé. Ça se dit en
    grand : l'absence de signal est le signal.
    """
    if not heartbeat.stalled(at):
        return f"<p class='beat'>rail vivant il y a {_ago(heartbeat.age_s(at))}</p>"
    if at:
        return (f"<p class='stalled'>rail à l'arrêt depuis {at:%H:%M} — "
                "les états affichés sont figés.</p>")
    return ("<p class='stalled'>rail à l'arrêt — aucun battement en base : le "
            "worker n'a jamais tamponné, les états affichés sont figés.</p>")


def _e(v) -> str:
    return html.escape(str(v))


# ------------------------------------------------------------------- sujet gh

GH_SUBJECT = re.compile(r"gh:([\w.-]+/[\w.-]+)#(\d+)")


def _issue_href(subject_key: str) -> str | None:
    """L'URL de l'issue quand le sujet en est une, sinon None.

    Le frontend ne connaît GitHub que comme un format de sujet parmi
    d'autres : tout ce qui ne matche pas `gh:<owner>/<repo>#<num>` n'a ni
    lien ni numéro, et le noyau reste agnostique.
    """
    m = GH_SUBJECT.fullmatch(subject_key)
    return f"https://github.com/{_e(m[1])}/issues/{_e(m[2])}" if m else None


def _subject(subject_key: str) -> str:
    """Le sujet en HTML : un lien vers l'issue quand il en est une, sinon du texte."""
    href = _issue_href(subject_key)
    return f"<a href='{href}'>{_e(subject_key)}</a>" if href else _e(subject_key)


def _issue(subject_key: str) -> str:
    """Le numéro d'issue seul, en lien — cellule vide pour un autre canal."""
    m = GH_SUBJECT.fullmatch(subject_key)
    if not m:
        return ""
    return f"<a href='{_issue_href(subject_key)}'>#{_e(m[2])}</a>"


def _title(item_id: int, title: str | None) -> str:
    """Le titre du sujet, cliquable vers la page de l'item.

    Le titre vient de la base — le canal l'y a rangé à l'admission. Un sujet
    qui n'est pas une issue GitHub n'en a jamais eu : cellule vide, la ligne
    reste lisible et rien n'est inventé.
    """
    return f"<a href='/item/{item_id}'>{_e(title)}</a>" if title else ""


def _pr(item_id: int) -> str:
    """Le lien vers la PR du cycle, quand le nœud release en a laissé une.

    La source est `release.json`, écrit par le nœud release à côté de son
    markdown : trois clés lues telles quelles, jamais un parseur de prose.
    Pas de fichier, pas d'URL dedans : pas de lien, et rien à dire.
    """
    path = item_workspace(item_id) / "release.json"
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


def _pr_url(item_id: int) -> str | None:
    """L'URL de la PR seule, pour un client qui pose son propre lien.

    Même source que `_pr`, sans l'habillage : pas de `release.json`, pas
    d'URL dedans, ou du JSON cassé — None, et rien d'inventé.
    """
    path = item_workspace(item_id) / "release.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text()).get("pr_url")
    except json.JSONDecodeError:  # la page HTML le signale déjà, en toutes lettres
        return None


# ------------------------------------------------------------------ questions


def _questions_page(questions: list[dict], by: str, token: str, flash: str | None,
                    beat: dt.datetime | None) -> str:
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
            + (f"« {_e(q['item_title'])} » " if q["item_title"] else "")
            + f"en <b>{_e(q['item_state'])}</b> · pour {_e(q['owner'])} "
            f"· avant {q['deadline']:%d/%m %H:%M} · escalades restantes {q['escalations']}"
            f"</div><div class='text'>{_e(q['text'])}</div>"
            f"<form method='post' action='/answer'>"
            f"<input type='hidden' name='question_id' value='{q['id']}'>"
            f"<input type='hidden' name='token' value='{token}'>"
            f"{buttons}</form></div>")
    version = max((q["item_version"] for q in questions), default=0)
    return _shell("graphatom — questions", "".join(parts), beat, version)


# ---------------------------------------------------------------------- items

# un item et son sujet, plus la date de sa dernière transition : le journal
# la porte déjà, aucune colonne à tenir à jour pour le savoir
ITEM_SELECT = (
    "SELECT w.*, s.subject_key, s.graph, s.title, s.lineage_budget, "
    "(SELECT max(at) FROM event e WHERE e.item_id = w.id) AS updated_at "
    "FROM work_item w JOIN subject s ON s.id = w.subject_id "
)


def _items_page(conn, beat: dt.datetime | None) -> str:
    rows = conn.execute(ITEM_SELECT + "ORDER BY w.id DESC").fetchall()
    body = ["<h1>items</h1>"]
    if not rows:
        body.append("<p class='empty'>Aucun item admis.</p>")
    else:
        lines = "".join(
            f"<tr><td><a href='/item/{r['id']}'>{r['id']}</a></td>"
            f"<td>{_issue(r['subject_key'])}</td>"
            f"<td>{_title(r['id'], r['title'])}</td>"
            f"<td>{_e(r['graph'])}</td>"
            f"<td>g{r['generation']}</td>"
            f"<td><span class='badge {'terminal' if r['terminal_at'] else 'active'}'>"
            f"{_e(r['state'])}</span></td>"
            f"<td>v{r['version']}</td><td>{r['escalations']}</td>"
            f"<td>{r['terminal_at'].strftime('%d/%m %H:%M') if r['terminal_at'] else 'actif'}</td></tr>"
            for r in rows)
        body.append("<table><tr><th>item</th><th>issue</th><th>titre</th>"
                    "<th>graph</th><th>gén.</th>"
                    "<th>état</th><th>version</th><th>escalades</th><th>fin</th></tr>"
                    f"{lines}</table>")
    version = max((r["version"] for r in rows), default=0)
    return _shell("graphatom — items", "".join(body), beat, version)


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


# ------------------------------------------------------- durées et tokens


def _spans(events: list[dict]) -> dict[int, float]:
    """Le temps passé dans chaque état, par version d'item.

    Aucune colonne de durée en base : le journal porte déjà un horodatage
    par transition, et l'écart avec la précédente est la durée du séjour
    qu'elle clôt — tentatives comprises, un retry étant lui-même une
    transition.
    """
    return {e["item_version"]: (e["at"] - prev["at"]).total_seconds()
            for prev, e in zip(events, events[1:])}


def _dur(seconds: float | None) -> str:
    """Une durée en français court : 4,2 s, 3 min 12 s, 1 h 04."""
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds:.1f} s".replace(".", ",")
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds % 60):02d} s"
    return f"{int(seconds // 3600)} h {int(seconds % 3600 // 60):02d}"


# étiquettes cosmétiques : un type de token que personne n'a prévu s'affiche
# sous son propre nom, et se compte comme les autres
TOKEN_LABELS = {"input_tokens": "in", "output_tokens": "out",
                "cache_read_input_tokens": "cache lu",
                "cache_creation_input_tokens": "cache écrit",
                "total_cost_usd": "$"}


def _usage(run: dict) -> dict:
    """L'usage de tokens d'un run — vide si l'agent n'en a pas laissé."""
    usage = (run["result"] or {}).get("usage")
    return usage if isinstance(usage, dict) else {}


def _candidate(run: dict) -> str:
    """Le candidat de fan-out d'un run, accolé à sa tentative. Vide sans fan-out.

    K candidats partagent une tentative : sans leur numéro, la table des runs
    montrerait K lignes identiques. Le total de l'item, lui, les additionne
    tous — c'est le prix réel de l'étape, pas celui du seul gagnant.
    """
    candidate = run.get("candidate")
    return "" if candidate is None else f" · <small>c{candidate}</small>"


def _totals(runs: list[dict]) -> dict[str, float]:
    """Le total par type de token de l'item.

    Rien n'est codé en dur : on additionne les clés numériques des usages
    trouvés, quels que soient leurs noms. Un agent qui rapporte ses types
    autrement est compté pareil.
    """
    totals: dict[str, float] = {}
    for run in runs:
        for key, value in _usage(run).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return totals


def _tokens(usage: dict) -> str:
    """Un usage en une ligne : les types tels qu'ils viennent, rien d'inventé."""
    parts = []
    for key, value in usage.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        count = (f"{value:.4f}" if isinstance(value, float)
                 else f"{value:,}".replace(",", " "))
        parts.append(f"{count} {TOKEN_LABELS.get(key, key)}")
    return _e(" · ".join(parts))


# ----------------------------------------------------------------------- item


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    return f"<table><tr>{head}</tr>{''.join(rows)}</table>"


# jsonb ne garde pas l'ordre des clés : le post-mortem d'abord, la trace après
RESULT_ORDER = ["outcome", "exit_code", "timeout", "error"]
RESULT_APART = {"log_tail", "usage"}  # l'un en bloc dessous, l'autre en colonne


def _result(result: dict | None) -> str:
    """Le résultat d'un run : les champs en ligne, la queue de log en bloc.

    L'autopsie d'une tentative crashée (`exit_code`, `timeout`, `log_tail`)
    se lit ici — sans elle, il fallait fouiller le workspace à la main.
    """
    if not result:
        return ""
    keys = [k for k in RESULT_ORDER if k in result]
    keys += sorted(k for k in result if k not in RESULT_ORDER and k not in RESULT_APART)
    fields = " · ".join(f"{_e(k)} <b>{_e(result[k])}</b>" for k in keys)
    tail = result.get("log_tail")
    return fields + (f"<pre>{_e(tail)}</pre>" if tail else "")


def _files(item_id: int) -> list[dict]:
    """Les fichiers du workspace : leur nom, leur taille, l'URL qui les sert.

    La même liste pour la page et pour l'API — un client JSON n'a pas à
    reconstruire l'URL de `/item/<id>/file/<nom>` à la main.
    """
    workspace = item_workspace(item_id)
    paths = sorted(p for p in workspace.iterdir() if p.is_file()) if workspace.is_dir() else []
    return [{"name": p.name, "size": p.stat().st_size,
             "href": f"/item/{item_id}/file/{quote(p.name)}"} for p in paths]


def _criteria(item_id: int) -> str | None:
    """`criteria.md` du workspace, tel quel — None quand le cycle n'en a pas.

    La liste fermée des critères de succès, figée par le nœud scope : c'est
    le contrat du cycle, et un client qui montre l'item doit pouvoir la
    montrer sans aller pêcher le fichier.
    """
    path = item_workspace(item_id) / "criteria.md"
    return path.read_text() if path.is_file() else None


def _item_page(conn, item_id: int, beat: dt.datetime | None) -> str | None:
    item = conn.execute(ITEM_SELECT + "WHERE w.id = %s", (item_id,)).fetchone()
    if item is None:
        return None
    bundle = load_bundle(conn, item["revision"])
    events = conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version", (item_id,)
    ).fetchall()
    runs = conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s ORDER BY id", (item_id,)
    ).fetchall()

    # le temps et les tokens du cycle, d'un coup d'œil et en tête de page
    spans = _spans(events)
    end = item["terminal_at"] or dt.datetime.now(dt.timezone.utc)
    total = (end - events[0]["at"]).total_seconds() if events else 0.0
    totals = _totals(runs)
    # un run n'a de durée que s'il a produit une transition : superseded et
    # stale n'en ont jamais eu, running pas encore
    run_span = {e["run_id"]: spans[e["item_version"]]
                for e in events if e["run_id"] and e["item_version"] in spans}

    state = "terminal" if item["terminal_at"] else "active"
    body = [
        f"<h1>item {item['id']} <small>· {_subject(item['subject_key'])}{_pr(item_id)} · "
        + (f"« {_e(item['title'])} » · " if item["title"] else "")
        + f"g{item['generation']} · {_e(item['graph'])} · rév. {item['revision'][:12]}…"
        "</small></h1>",
        f"<p><span class='badge {state}'>{_e(item['state'])}</span> "
        f"v{item['version']} · passage {item['cycle']} · "
        f"escalades restantes {item['escalations']} · "
        f"lignée restante {item['lineage_budget']} · "
        + (f"terminé {item['terminal_at']:%d/%m %H:%M:%S}" if item["terminal_at"]
           else f"wall deadline {item['wall_deadline']:%d/%m %H:%M}") + "</p>",
        f"<p class='meta'>temps total {_dur(total)}"
        + ("" if item["terminal_at"] else " (en cours)")
        + (f" · {_tokens(totals)}" if totals else " · aucun usage rapporté") + "</p>",
        _graph_svg(bundle, item["state"]),
    ]

    body.append("<h2>journal</h2>" + _table(
        ["v", "à", "durée", "événement", "transition", "issue", "run"],
        [f"<tr><td>v{e['item_version']}</td><td>{e['at']:%d/%m %H:%M:%S}</td>"
         f"<td>{_dur(spans.get(e['item_version']))}</td>"
         f"<td>{_e(e['kind'])}</td>"
         f"<td>{(_e(e['from_state']) + ' → ') if e['from_state'] else ''}{_e(e['to_state'])}</td>"
         f"<td>{_e(e['outcome'] or '')}</td><td>{e['run_id'] or ''}</td></tr>"
         for e in events]))

    if runs:
        body.append("<h2>runs</h2>" + _table(
            ["run", "nœud", "passage", "tentative", "statut", "issue", "fence",
             "bail", "durée", "tokens", "résultat"],
            [f"<tr><td>{r['id']}</td><td>{_e(r['node'])}</td><td>{r['cycle']}</td>"
             f"<td>{r['attempt']}{_candidate(r)}</td>"
             f"<td><span class='badge {_e(r['status'])}'>{_e(r['status'])}</span></td>"
             f"<td>{_e(r['outcome'] or '')}</td><td>{r['fence']}</td>"
             f"<td>{r['lease_expires_at']:%H:%M:%S}</td>"
             f"<td>{_dur(run_span.get(r['id']))}</td>"
             f"<td>{_tokens(_usage(r))}</td>"
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

    files = _files(item_id)
    if files:
        body.append("<h2>workspace</h2>")
        for f in files:
            href, name = f["href"], _e(f["name"])
            if f["name"].endswith(".png"):  # les screenshots des agents = la preview
                body.append(f"<p>{name}</p><a href='{href}'>"
                            f"<img src='{href}' style='max-width:100%;border:1px solid #ddd'></a>")
            else:
                body.append(f"<p><a href='{href}'>{name}</a> "
                            f"<small>({f['size']} o)</small></p>")

    return _shell(f"graphatom — item {item_id}", "".join(body), beat, item["version"])


def _file_response(item_id: int, name: str) -> tuple[bytes, str] | None:
    workspace = item_workspace(item_id).resolve()
    path = (workspace / name).resolve()
    if not (path.is_file() and path.parent == workspace):  # pas de traversée
        return None
    ctype = ("image/png" if path.suffix == ".png"
             else "application/json" if path.suffix == ".json"
             else "text/plain")
    return path.read_bytes(), f"{ctype}; charset=utf-8"


# ------------------------------------------------------------------ API JSON


def _jsonable(value):
    """Ce que `json` ne sait pas rendre : un horodatage, en ISO 8601.

    Le reste passe en texte plutôt qu'en 500 : une valeur inattendue au fond
    d'un JSONB d'agent ne doit pas emporter la réponse entière.
    """
    return value.isoformat() if isinstance(value, dt.datetime) else str(value)


def _api_item_row(row: dict) -> dict:
    """Un item en JSON : ce que la table `/items` montre, plus ses deux liens.

    `status` dit la vie de l'item — actif ou terminal —, `state` reste le
    nœud courant du graph : la page les affiche ensemble dans son badge,
    l'API ne les confond pas non plus.
    """
    return {
        "id": row["id"],
        "subject_key": row["subject_key"],
        "title": row["title"],
        "graph": row["graph"],
        "generation": row["generation"],
        "state": row["state"],
        "status": "terminal" if row["terminal_at"] else "active",
        "version": row["version"],
        "cycle": row["cycle"],
        "escalations": row["escalations"],
        "issue_url": _issue_href(row["subject_key"]),
        "pr_url": _pr_url(row["id"]),
        "terminal_at": row["terminal_at"],
        "updated_at": row["updated_at"],
    }


def _api_items(conn) -> list[dict]:
    """Tous les items, du plus récent au plus ancien — la table `/items`."""
    return [_api_item_row(r) for r in conn.execute(
        ITEM_SELECT + "ORDER BY w.id DESC").fetchall()]


def _api_graph(bundle: dict, current: str) -> dict:
    """De quoi redessiner le graph sans relire la base.

    Les nœuds avec ce que le SVG en dit — bloc, terminal, escalade —, les
    arêtes à plat (une par issue), les racines du placement (`entry` et les
    cibles du noyau) et le nœud courant, celui que la page peint en orange.
    """
    nodes = bundle["nodes"]
    return {
        "name": bundle["name"],
        "entry": bundle["entry"],
        "on_kernel": bundle["on_kernel"],
        "current": current,
        "nodes": [{"name": n, "block": spec.get("block"),
                   "terminal": bool(spec.get("terminal")),
                   "escalade": bool(spec.get("escalade"))}
                  for n, spec in nodes.items()],
        "edges": [{"from": n, "outcome": outcome, "to": target}
                  for n, spec in nodes.items()
                  for outcome, target in (spec.get("edges") or {}).items()],
    }


def _api_graphs(conn) -> list[dict]:
    """Les révisions publiées, la plus récente de chaque nom en tête.

    Le compte d'items est ce qui sépare une révision vivante d'une révision
    oubliée : un item en vol épingle la sienne, et cette révision-là reste
    sa vérité tant qu'il tourne — même si le nom a été republié depuis.
    """
    rows = conn.execute(
        "SELECT g.id, g.name, g.published_at, "
        "(SELECT count(*) FROM work_item w WHERE w.revision = g.id) AS items "
        "FROM graph_revision g ORDER BY g.name, g.published_at DESC, g.id"
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "published_at": r["published_at"],
             "items": r["items"]} for r in rows]


def _api_graph_revision(conn, revision: str) -> dict | None:
    """Le bundle d'une révision, tel qu'il a été publié. None si inconnue.

    Rien n'est recalculé ni résumé : la config entière de chaque nœud sort
    de la base comme elle y est entrée — prompt des agents compris. C'est
    ce que lit le worker qui exécute cette révision, et donc ce que la
    vitrine doit montrer.
    """
    row = conn.execute(
        "SELECT g.id, g.bundle, g.published_at, "
        "(SELECT count(*) FROM work_item w WHERE w.revision = g.id) AS items "
        "FROM graph_revision g WHERE g.id = %s", (revision,)).fetchone()
    if row is None:
        return None
    return row["bundle"] | {"revision": row["id"], "items": row["items"],
                            "published_at": row["published_at"]}


def _api_question(q: dict) -> dict:
    """Une question en JSON, avec le contexte de son item quand la requête l'a joint."""
    payload = {
        "question_id": q["id"], "item_id": q["item_id"], "node": q["node"],
        "text": q["text"], "options": list(q["options"]), "owner": q["owner"],
        "deadline": q["deadline"], "state": q["state"],
        "answer": q["answer"], "answered_by": q["answered_by"],
    }
    if "subject_key" in q:  # les questions ouvertes portent l'item avec elles
        payload |= {"subject_key": q["subject_key"], "item_title": q["item_title"],
                    "item_state": q["item_state"],
                    "issue_url": _issue_href(q["subject_key"])}
    return payload


def _api_item(conn, item_id: int) -> dict | None:
    """La page d'un item en données. None quand l'item n'existe pas.

    Mêmes requêtes que `_item_page`, même arithmétique : les durées sortent
    des horodatages du journal, les tokens du résultat des runs. Un client
    externe rend la même page sans relire la base ni parser du HTML.
    """
    item = conn.execute(ITEM_SELECT + "WHERE w.id = %s", (item_id,)).fetchone()
    if item is None:
        return None
    bundle = load_bundle(conn, item["revision"])
    events = conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version", (item_id,)
    ).fetchall()
    runs = conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s ORDER BY id", (item_id,)
    ).fetchall()
    effects = conn.execute(
        "SELECT * FROM effect WHERE item_id = %s ORDER BY op_id", (item_id,)
    ).fetchall()
    questions = conn.execute(
        "SELECT * FROM question WHERE item_id = %s ORDER BY id", (item_id,)
    ).fetchall()

    spans = _spans(events)
    end = item["terminal_at"] or dt.datetime.now(dt.timezone.utc)
    run_span = {e["run_id"]: spans[e["item_version"]]
                for e in events if e["run_id"] and e["item_version"] in spans}

    return {
        "item": _api_item_row(item) | {
            "revision": item["revision"],
            "lineage_budget": item["lineage_budget"],
            "wall_deadline": item["wall_deadline"],
            "duration_s": (end - events[0]["at"]).total_seconds() if events else 0.0,
            "usage": _totals(runs),
        },
        "graph": _api_graph(bundle, item["state"]),
        "journal": [{"version": e["item_version"], "at": e["at"], "kind": e["kind"],
                     "from_state": e["from_state"], "to_state": e["to_state"],
                     "outcome": e["outcome"], "run_id": e["run_id"],
                     "duration_s": spans.get(e["item_version"])} for e in events],
        "runs": [{"id": r["id"], "node": r["node"], "cycle": r["cycle"],
                  "attempt": r["attempt"], "candidate": r.get("candidate"),
                  "status": r["status"],
                  "outcome": r["outcome"], "fence": r["fence"],
                  "lease_expires_at": r["lease_expires_at"],
                  "duration_s": run_span.get(r["id"]),
                  "usage": _usage(r), "result": r["result"]} for r in runs],
        "effects": [{"op_id": f["op_id"], "logical_key": f["logical_key"],
                     "target_uri": f["target_uri"], "observation": f["observation"]}
                    for f in effects],
        "questions": [_api_question(q) for q in questions],
        "criteria": _criteria(item_id),
        "files": _files(item_id),
    }


def _api_questions(conn, token: str) -> dict:
    """Les questions ouvertes, et le jeton qu'attend `POST /answer`.

    Le jeton est celui du formulaire rendu, jusqu'ici enfoui dans le HTML :
    un client qui le lit ici répond sans jamais charger la page.
    """
    return {"token": token,
            "questions": [_api_question(q) for q in channel.open_questions(conn)]}


def _api_beat(at: dt.datetime | None) -> dict:
    """Un battement brut : son horodatage, son âge, et s'il est périmé."""
    return {"at": at, "ago_s": heartbeat.age_s(at), "stale": heartbeat.stalled(at)}


def _api_heartbeat(rail: dt.datetime | None, sync: dt.datetime | None) -> dict:
    """Les deux battements, pour un client qui pose l'en-tête lui-même.

    Un objet par batteur, sous son identité en base : le worker et le canal
    GitHub sont deux processus séparés, chacun peut mourir seul, et un
    client qui n'en lit qu'un ne verrait pas l'autre se taire.
    """
    return {heartbeat.RAIL: _api_beat(rail), heartbeat.GITHUB_SYNC: _api_beat(sync)}


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

        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload, default=_jsonable, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _wants_json(self) -> bool:
            return "application/json" in self.headers.get("Accept", "")

        def _api(self, path: str) -> None:
            """Les routes JSON — un client qui demande du JSON en reçoit toujours.

            Y compris quand ça rate : une erreur ici est un objet à `error`,
            jamais une page, jamais une trace.
            """
            try:
                with db.connect() as conn:
                    if path == "/api/items":
                        return self._json(200, _api_items(conn))
                    if path == "/api/questions":
                        return self._json(200, _api_questions(conn, token))
                    if path == "/api/heartbeat":
                        return self._json(200, _api_heartbeat(
                            heartbeat.last(conn, heartbeat.RAIL),
                            heartbeat.last(conn, heartbeat.GITHUB_SYNC)))
                    if path == "/api/graphs":
                        return self._json(200, _api_graphs(conn))
                    if path.startswith("/api/item/") and path[10:].isdigit():
                        payload = _api_item(conn, int(path[10:]))
                        if payload is None:
                            return self._json(404, {"error": f"item {path[10:]} inconnu"})
                        return self._json(200, payload)
                    if path.startswith("/api/graph/"):
                        revision = unquote(path[11:])
                        payload = _api_graph_revision(conn, revision)
                        if payload is None:
                            return self._json(404, {"error": f"révision {revision} inconnue"})
                        return self._json(200, payload)
            except Exception as exc:  # une API qui tombe le dit, en JSON
                return self._json(500, {"error": str(exc)})
            self._json(404, {"error": f"route inconnue : {path}"})

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
            if path.startswith("/api/"):
                return self._api(path)
            try:
                with db.connect() as conn:
                    # une ligne : l'en-tête de toute page, le battement du worker
                    beat = heartbeat.last(conn, heartbeat.RAIL)
                    if path == "/":
                        flash = parse_qs(query).get("m", [None])[0]
                        page = _questions_page(channel.open_questions(conn), by, token,
                                               flash, beat)
                    elif path == "/items":
                        page = _items_page(conn, beat)
                    elif path.startswith("/item/") and path[6:].isdigit():
                        page = _item_page(conn, int(path[6:]), beat)
                    else:
                        page = None
            except Exception as exc:  # une vitrine qui tombe le dit, elle ne coupe pas
                return self._respond(500, f"erreur : {_e(exc)}")
            if page is None:
                return self._respond(404, "introuvable")
            self._respond(200, page)

        def _fields(self, body: str) -> dict:
            """Les champs postés : formulaire urlencodé, ou JSON du même nom.

            Le formulaire rendu poste ce qu'un navigateur poste, un client
            JSON poste du JSON — trois clés dans les deux cas, et un seul
            chemin derrière.
            """
            if "application/json" in self.headers.get("Content-Type", ""):
                return json.loads(body)
            return {k: v[0] for k, v in parse_qs(body).items()}

        def _refuse(self, status: int, message: str) -> None:
            """Un refus, dans la langue du client : JSON ou page."""
            if self._wants_json():
                return self._json(status, {"ok": False, "message": message})
            self._respond(status, _e(message))

        def do_POST(self):
            if self.path != "/answer":
                return self._refuse(404, "introuvable")
            length = int(self.headers.get("Content-Length", 0))
            try:
                fields = self._fields(self.rfile.read(length).decode())
                qid, option = int(fields["question_id"]), str(fields["option"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                return self._refuse(400, f"requête illisible : {exc}")
            if fields.get("token") != token:
                return self._refuse(403, "jeton invalide — rechargez la page")
            with db.connect() as conn:
                err = channel.record_answer(conn, qid, option, by)
            msg = err or f"réponse « {option} » enregistrée — le rail reprend"
            if self._wants_json():  # le client JSON lit l'issue, pas une redirection
                return self._json(200, {"ok": err is None, "message": msg})
            self._respond(303, location=f"/?m={quote(msg)}")

    if notify_cmd:
        threading.Thread(target=_notify_loop, args=(notify_cmd, base_url),
                         daemon=True).start()
    print(f"canal humain sur {base_url} (réponses signées « {by} »)", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
