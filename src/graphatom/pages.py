"""Les trois pages HTML de `graphatom serve` — le canal de secours.

    /            les questions ouvertes — la seule surface d'écriture
    /items       tous les items : issue, titre, graph, état, version…
    /item/<id>   la trajectoire : graph SVG avec l'état courant marqué,
                 journal v1..vN, runs, effets, questions

Chaque page se rafraîchit toute seule, sans rien changer au serveur : elle
porte son marqueur de fraîcheur en meta, et quinze lignes de JS le comparent
toutes les 5 s à celui d'un `fetch` de la même URL. Sans JavaScript, le
rafraîchissement complet d'avant tient toujours — il vit dans un `noscript`.

L'en-tête commun porte le battement du worker : « rail vivant il y a 3 s »,
ou le bandeau rouge quand plus rien ne bat — les états montrés sont alors
figés, et la page le dit avant qu'on les croie.
"""

import datetime as dt
import html
import json
from urllib.parse import parse_qs

from . import channel, heartbeat, views
from .svg import graph_svg
from .workspace import item_workspace

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
# seulement s'il a bougé. Le serveur ne sait même pas que ce script existe.
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

# étiquettes cosmétiques : un type de token que personne n'a prévu s'affiche
# sous son propre nom, et se compte comme les autres
TOKEN_LABELS = {"input_tokens": "in", "output_tokens": "out",
                "cached_input_tokens": "cache lu",
                "cache_write_input_tokens": "cache écrit",
                "reasoning_output_tokens": "raisonnement",
                "cache_read_tokens": "cache lu",
                "cache_write_tokens": "cache écrit",
                "reasoning_tokens": "raisonnement",
                "cache_read_input_tokens": "cache lu",
                "cache_creation_input_tokens": "cache écrit",
                "total_cost_usd": "$ rapportés"}

# jsonb ne garde pas l'ordre des clés : le post-mortem d'abord, la trace après
RESULT_ORDER = ["outcome", "exit_code", "timeout", "error"]
RESULT_APART = {"_retry_signature", "log_tail", "usage"}


def e(v) -> str:
    return html.escape(str(v))


def _marker(version: int, beat_at: dt.datetime | None) -> str:
    """Le marqueur de fraîcheur de la page : ce qui doit la faire repeindre.

    La version de ce qui est affiché et l'état du battement, qui change
    l'en-tête sans changer aucune version : sans lui, un rail qui s'arrête
    ne poserait jamais son bandeau rouge sur une page déjà ouverte.
    """
    return f"v{version}-{'stalled' if heartbeat.stalled(beat_at) else 'live'}"


def shell(title: str, body: str, beat_at: dt.datetime | None, version: int) -> str:
    return (f"{HEAD}<title>{e(title)}</title>"
            f"<meta name='{MARKER}' content='{_marker(version, beat_at)}'>"
            f"<noscript>{REFRESH}</noscript>"  # sans JS : le rechargement d'avant
            f"<style>{STYLE}</style>"
            "<nav><a href='/'>questions</a><a href='/items'>items</a></nav>"
            f"<main id='live'>{beat(beat_at)}{body}</main>"
            f"<script>{LIVE}</script>")


def ago(seconds: float) -> str:
    """Un âge lisible : en secondes sous la minute, en minutes au-delà."""
    return f"{seconds:.0f} s" if seconds < 60 else f"{seconds / 60:.0f} min"


def beat(at: dt.datetime | None) -> str:
    """Le battement du worker, en en-tête de chaque page.

    Sans battement depuis deux minutes, plus rien ne tourne — et tout ce que
    la page montre est figé. Ça se dit en grand : l'absence de signal est le
    signal.
    """
    if not heartbeat.stalled(at):
        return f"<p class='beat'>rail vivant il y a {ago(heartbeat.age_s(at))}</p>"
    if at:
        return (f"<p class='stalled'>rail à l'arrêt depuis {at:%H:%M} — "
                "les états affichés sont figés.</p>")
    return ("<p class='stalled'>rail à l'arrêt — aucun battement en base : le "
            "worker n'a jamais tamponné, les états affichés sont figés.</p>")


# ------------------------------------------------------------------- liens


def subject(subject_key: str) -> str:
    """Le sujet en HTML : un lien vers l'issue quand il en est une, sinon du texte."""
    href = views.issue_href(subject_key)
    return f"<a href='{e(href)}'>{e(subject_key)}</a>" if href else e(subject_key)


def issue(subject_key: str) -> str:
    """Le numéro d'issue seul, en lien — cellule vide pour un autre canal."""
    m = views.GH_SUBJECT.fullmatch(subject_key)
    if not m:
        return ""
    return f"<a href='{e(views.issue_href(subject_key))}'>#{e(m[2])}</a>"


def title(item_id: int, text: str | None) -> str:
    """Le titre du sujet, cliquable vers la page de l'item — vide sans titre."""
    return f"<a href='/item/{item_id}'>{e(text)}</a>" if text else ""


def pr(item_id: int) -> str:
    """Le lien vers la PR du cycle, quand le nœud release en a laissé une."""
    try:
        found = views.release(item_id)
    except json.JSONDecodeError:  # la page le dit, elle ne devine pas — et reste lisible
        return " · <small>release.json illisible</small>"
    if not found or not found.get("pr_url"):
        return ""
    number = found.get("pr_number")
    sha = str(found.get("merge_sha") or "")
    return (f" · <a href='{e(found['pr_url'])}'>PR{f' #{e(number)}' if number else ''}</a>"
            + (f" (mergée {e(sha[:7])})" if sha else ""))


# ------------------------------------------------------------------ questions


def _question(q: dict, token: str) -> str:
    buttons = "".join(
        f"<button name='option' value='{e(opt)}'>{e(opt)}</button>"
        for opt in q["options"])
    return (
        f"<div class='q'><div class='meta'>"
        f"[{q['id']}] {subject(q['subject_key'])} · <a href='/item/{q['item_id']}'>item {q['item_id']}</a> "
        + (f"« {e(q['item_title'])} » " if q["item_title"] else "")
        + f"en <b>{e(q['item_state'])}</b> · pour {e(q['owner'])} "
        f"· avant {q['deadline']:%d/%m %H:%M} · escalades restantes {q['escalations']}"
        f"</div><div class='text'>{e(q['text'])}</div>"
        f"<form method='post' action='/answer'>"
        f"<input type='hidden' name='question_id' value='{q['id']}'>"
        f"<input type='hidden' name='token' value='{token}'>"
        f"{buttons}</form></div>")


def questions_page(questions: list[dict], by: str, token: str, flash: str | None,
                   beat_at: dt.datetime | None) -> str:
    parts = [f"<h1>graphatom <small>· répondre en tant que {e(by)}</small></h1>"]
    if flash:
        parts.append(f"<p class='flash'>{e(flash)}</p>")
    if not questions:
        parts.append("<p class='empty'>Aucune question ouverte. La page se rafraîchit toute seule.</p>")
    parts.extend(_question(q, token) for q in questions)
    version = max((q["item_version"] for q in questions), default=0)
    return shell("graphatom — questions", "".join(parts), beat_at, version)


# ---------------------------------------------------------------------- items


def _item_row(r: dict) -> str:
    cost = (f"<td>{r['reported_cost_usd']:.4f}</td>"
            if r["reported_cost_usd"] is not None else "<td>—</td>")
    fin = r["terminal_at"].strftime("%d/%m %H:%M") if r["terminal_at"] else "actif"
    return (f"<tr><td><a href='/item/{r['id']}'>{r['id']}</a></td>"
            f"<td>{issue(r['subject_key'])}</td>"
            f"<td>{title(r['id'], r['title'])}</td>"
            f"<td>{e(r['graph'])}</td>"
            f"<td>g{r['generation']}</td>"
            + cost
            + f"<td><span class='badge {'terminal' if r['terminal_at'] else 'active'}'>"
            f"{e(r['state'])}</span></td>"
            f"<td>v{r['version']}</td><td>{r['escalations']}</td>"
            f"<td>{fin}</td></tr>")


def items_page(conn, beat_at: dt.datetime | None) -> str:
    rows = views.items_data(conn)
    body = ["<h1>items</h1>"]
    if not rows:
        body.append("<p class='empty'>Aucun item admis.</p>")
    else:
        body.append(table(["item", "issue", "titre", "graph", "gén.", "coût rapporté $",
                           "état", "version", "escalades", "fin"],
                          [_item_row(r) for r in rows]))
    version = max((r["version"] for r in rows), default=0)
    return shell("graphatom — items", "".join(body), beat_at, version)


# ----------------------------------------------------------------------- item


def dur(seconds: float | None) -> str:
    """Une durée en français court : 4,2 s, 3 min 12 s, 1 h 04."""
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds:.1f} s".replace(".", ",")
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds % 60):02d} s"
    return f"{int(seconds // 3600)} h {int(seconds % 3600 // 60):02d}"


def candidate(run: dict) -> str:
    """Le candidat de fan-out d'un run, accolé à sa tentative. Vide sans fan-out."""
    found = run.get("candidate")
    return "" if found is None else f" · <small>c{found}</small>"


def tokens(usage: dict) -> str:
    """Un usage en une ligne : les types tels qu'ils viennent, rien d'inventé."""
    parts = []
    for key, value in usage.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        count = (f"{value:.4f}" if isinstance(value, float)
                 else f"{value:,}".replace(",", " "))
        parts.append(f"{count} {TOKEN_LABELS.get(key, key)}")
    return e(" · ".join(parts))


def table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    return f"<table><tr>{head}</tr>{''.join(rows)}</table>"


def result(found: dict | None) -> str:
    """Le résultat d'un run : les champs en ligne, la queue de log en bloc."""
    if not found:
        return ""
    keys = [k for k in RESULT_ORDER if k in found]
    keys += sorted(k for k in found if k not in RESULT_ORDER and k not in RESULT_APART)
    fields = " · ".join(f"{e(k)} <b>{e(found[k])}</b>" for k in keys)
    log_tail = found.get("log_tail")
    return fields + (f"<pre>{e(log_tail)}</pre>" if log_tail else "")


def split_line(bundle: dict, lot: list[dict]) -> str:
    """Le prix du jugement en face de celui de la génération — ou rien."""
    split = views.cost_split(bundle, lot)
    if not split["judgement"] and not split["candidates"]:
        return ""
    return ("<p class='meta' id='cout-jugement'>"
            f"jugement {tokens(split['judgement']) or 'aucun usage rapporté'}"
            " · candidats "
            f"{tokens(split['candidates']) or 'aucun usage rapporté'}</p>")


def _header(data: dict) -> list[str]:
    item, bundle = data["item"], data["bundle"]
    state = "terminal" if item["terminal_at"] else "active"
    totals = data["usage"]
    return [
        f"<h1>item {item['id']} <small>· {subject(item['subject_key'])}{pr(item['id'])} · "
        + (f"« {e(item['title'])} » · " if item["title"] else "")
        + f"g{item['generation']} · {e(item['graph'])} · rév. {item['revision'][:12]}…"
        "</small></h1>",
        f"<p><span class='badge {state}'>{e(item['state'])}</span> "
        f"v{item['version']} · passage {item['cycle']} · "
        f"escalades restantes {item['escalations']} · "
        f"lignée restante {item['lineage_budget']} · "
        + (f"terminé {item['terminal_at']:%d/%m %H:%M:%S}" if item["terminal_at"]
           else f"wall deadline {item['wall_deadline']:%d/%m %H:%M}") + "</p>",
        f"<p class='meta'>temps total {dur(data['duration_s'])}"
        + ("" if item["terminal_at"] else " (en cours)")
        + (f" · {tokens(totals)}" if totals else " · aucun usage rapporté") + "</p>",
        split_line(bundle, data["runs"]),
        graph_svg(bundle, item["state"]),
    ]


def _journal(data: dict) -> str:
    spans = data["spans"]
    return "<h2>journal</h2>" + table(
        ["v", "à", "durée", "événement", "transition", "issue", "run"],
        [f"<tr><td>v{ev['item_version']}</td><td>{ev['at']:%d/%m %H:%M:%S}</td>"
         f"<td>{dur(spans.get(ev['item_version']))}</td>"
         f"<td>{e(ev['kind'])}</td>"
         f"<td>{(e(ev['from_state']) + ' → ') if ev['from_state'] else ''}{e(ev['to_state'])}</td>"
         f"<td>{e(ev['outcome'] or '')}</td><td>{ev['run_id'] or ''}</td></tr>"
         for ev in data["events"]])


def _runs(data: dict) -> str:
    run_span = data["run_span"]
    return "<h2>runs</h2>" + table(
        ["run", "nœud", "passage", "tentative", "statut", "issue", "fence",
         "bail", "durée", "tokens", "résultat"],
        [f"<tr><td>{r['id']}</td><td>{e(r['node'])}</td><td>{r['cycle']}</td>"
         f"<td>{r['attempt']}{candidate(r)}</td>"
         f"<td><span class='badge {e(r['status'])}'>{e(r['status'])}</span></td>"
         f"<td>{e(r['outcome'] or '')}</td><td>{r['fence']}</td>"
         f"<td>{r['lease_expires_at']:%H:%M:%S}</td>"
         f"<td>{dur(run_span.get(r['id']))}</td>"
         f"<td>{tokens(views.usage(r))}</td>"
         f"<td>{result(r['result'])}</td></tr>" for r in data["runs"]])


def _effects(effects: list[dict]) -> str:
    return "<h2>effets</h2>" + table(
        ["clé logique", "cible", "observation"],
        [f"<tr><td><code>{e(f['logical_key'])}</code></td><td>{e(f['target_uri'])}</td>"
         f"<td><span class='badge {e(f['observation'])}'>{e(f['observation'])}</span></td></tr>"
         for f in effects])


def _questions(questions: list[dict]) -> str:
    return "<h2>questions</h2>" + table(
        ["q", "nœud", "question", "état", "réponse", "par", "deadline"],
        [f"<tr><td>{q['id']}</td><td>{e(q['node'])}</td><td>{e(q['text'])}</td>"
         f"<td>{e(q['state'])}</td>"
         f"<td>{e(q['answer'] or '')}</td><td>{e(q['answered_by'] or '')}</td>"
         f"<td>{q['deadline']:%d/%m %H:%M}</td></tr>" for q in questions])


def _files(files: list[dict]) -> str:
    parts = ["<h2>workspace</h2>"]
    for f in files:
        href, name = f["href"], e(f["name"])
        if f["name"].endswith(".png"):  # les screenshots des agents = la preview
            parts.append(f"<p>{name}</p><a href='{href}'>"
                         f"<img src='{href}' style='max-width:100%;border:1px solid #ddd'></a>")
        else:
            parts.append(f"<p><a href='{href}'>{name}</a> <small>({f['size']} o)</small></p>")
    return "".join(parts)


def item_page(conn, item_id: int, beat_at: dt.datetime | None) -> str | None:
    data = views.item_data(conn, item_id)
    if data is None:
        return None
    body = _header(data)
    body.append(_journal(data))
    for section, render in (("runs", _runs), ("effects", _effects),
                            ("questions", _questions), ("files", _files)):
        if data[section]:
            body.append(render(data if section == "runs" else data[section]))
    return shell(f"graphatom — item {item_id}", "".join(body), beat_at,
                 data["item"]["version"])


def file_response(item_id: int, name: str) -> tuple[bytes, str] | None:
    workspace = item_workspace(item_id).resolve()
    path = (workspace / name).resolve()
    if not (path.is_file() and path.parent == workspace):  # pas de traversée
        return None
    ctype = ("image/png" if path.suffix == ".png"
             else "application/json" if path.suffix == ".json"
             else "text/plain")
    return path.read_bytes(), f"{ctype}; charset=utf-8"


def render(conn, path: str, query: str, by: str, token: str) -> str | None:
    """La page demandée, ou None quand la route n'existe pas."""
    beat_at = heartbeat.last(conn, heartbeat.RAIL)  # une ligne : l'en-tête de toute page
    if path == "/":
        flash = parse_qs(query).get("m", [None])[0]
        return questions_page(channel.open_questions(conn), by, token, flash, beat_at)
    if path == "/items":
        return items_page(conn, beat_at)
    if path.startswith("/item/") and path[6:].isdigit():
        return item_page(conn, int(path[6:]), beat_at)
    return None
