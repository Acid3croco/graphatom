"""Le test des pages HTML du canal web : les vraies valeurs, dans le vrai DOM.

Sur un Postgres jetable, trois items réels — un terminé, un en attente d'une
réponse, un passé par un fan-out — et les pages rendues dessus :

  1. `/` : la question ouverte avec son sujet, son item, son titre, ses
     options en boutons, le jeton caché ; le message de `?m=` échappé ;
     l'en-tête du battement, vivant ou figé, dans le marqueur
  2. `/items` : une ligne par item, du plus récent au plus ancien — issue
     et titre en liens, coût rapporté à quatre décimales ou tiret, badge
     actif ou terminal, version, escalades, fin datée ou « actif »
  3. `/item/<id>` terminé : en-tête avec PR et révision, badge terminal,
     temps total, tokens ; le SVG avec l'état courant marqué ; journal,
     runs, effets, questions et workspace avec les valeurs de la base
  4. `/item/<id>` actif : wall deadline, « en cours », aucun usage, et les
     sections vides absentes ; l'item inconnu et les routes hors contrat
     ne rendent rien
  5. le fan-out : le candidat accolé à sa tentative, le run recalé, et le
     prix du jugement face à celui des candidats
  6. les rendus purs : durées, tokens, résultat, battement, release cassée
  7. le SVG : arêtes fusionnées, retours en voies séparées, étiquettes
     décalées, couleurs des nœuds, escalade en pointillé

Usage : uv run python tests/web_pages_test.py
"""

import datetime as dt
import json

# web_outils épingle GRAPHATOM_DSN sur la base jetable : il passe avant graphatom
from web_outils import BUNDLE_VITRINE, USAGE, question_de, run_de, sujet, vitrine  # noqa: E402
from outils import bundle_stub, etat, reserver_tout  # noqa: E402

from graphatom import api, channel, db, effects, heartbeat, kernel, pages, scheduler, svg, views  # noqa: E402
from graphatom import graph, workspace as bureau  # noqa: E402
from graphatom.policy import now  # noqa: E402

BY, TOKEN = "pages-test", "jeton-x"
ISSUE = "gh:Acid3croco/graphatom#5"
ISSUE_URL = "https://github.com/Acid3croco/graphatom/issues/5"
PR_URL = "https://github.com/Acid3croco/graphatom/pull/31"
TITRE = "Le titre <5>"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(16))


def rendu(conn, path: str, query: str = "") -> str | None:
    return pages.render(conn, path, query, BY, TOKEN)


def marqueur(page: str) -> str:
    tag = f"<meta name='{pages.MARKER}' content='"
    debut = page.index(tag) + len(tag)
    return page[debut:page.index("'", debut)]


# ------------------------------------------------------------------ l'état


def item_termine(conn) -> int:
    """Un item passé par toute la vitrine : réponse, effet, fichiers."""
    item_id = vitrine(conn, sujet("web"))
    assert channel.record_answer(conn, question_de(conn, item_id)["id"], "go", BY) is None
    scheduler.tick(conn)
    assert etat(conn, item_id)["state"] == "fini"
    run = run_de(conn, item_id, "travail")
    row = effects.intend(conn, item_id, run["id"], f"web-vitrine:{run['id']}:travail",
                         "stub://outbox", {"intent": "noop"})
    effects.mark_applied(conn, op_id=row["op_id"])
    atelier = bureau.item_workspace(item_id)
    atelier.mkdir(parents=True, exist_ok=True)
    (atelier / "release.json").write_text(json.dumps(
        {"pr_number": 31, "pr_url": PR_URL, "merge_sha": "a1b2c3d4e5f6"}))
    (atelier / "shot.png").write_bytes(PNG)
    (atelier / "notes.md").write_text("des notes\n")
    (atelier / "criteria.md").write_text("1. ça marche\n")
    return item_id


def item_fanout(conn) -> int:
    """Un ACT en fan-out de deux candidats : le premier `ok` gagne, l'autre est recalé."""
    rev = graph.publish(conn, bundle_stub(2))
    item_id = kernel.admit(conn, rev, sujet("fanout"))
    runs = reserver_tout(conn, item_id)
    assert [r["candidate"] for r in runs] == [0, 1], runs
    kernel.apply(conn, runs[0]["id"], {"outcome": "ok", "usage": {"input_tokens": 5}})
    assert etat(conn, item_id)["state"] == "fini"
    return item_id


# ------------------------------------------------------------------ preuves


def preuve_vide(conn) -> None:
    """0. une base sans rien : les deux listes le disent, sans une ligne inventée."""
    questions, items = rendu(conn, "/"), rendu(conn, "/items")
    assert "<p class='empty'>Aucune question ouverte. La page se rafraîchit toute seule.</p>" in questions
    assert "<div class='q'>" not in questions and marqueur(questions) == "v0-stalled"
    assert "<h1>items</h1><p class='empty'>Aucun item admis.</p>" in items and "<table>" not in items
    assert views.items_data(conn) == [] and api.items(conn) == []
    print("0. base vide : aucune question, aucun item, marqueur v0 ✓")


def preuve_questions(conn, item_id: int) -> None:
    """1. la question ouverte, telle que la base la porte — et l'en-tête."""
    q, item = question_de(conn, item_id), etat(conn, item_id)
    conn.execute("DELETE FROM heartbeat")  # le tick d'avant a tamponné : on repart de rien
    page = rendu(conn, "/", "m=<b>")
    assert f"<h1>graphatom <small>· répondre en tant que {BY}</small></h1>" in page
    assert "<p class='flash'>&lt;b&gt;</p>" in page, "le message est échappé"
    assert (f"[{q['id']}] <a href='{ISSUE_URL}'>{ISSUE}</a> · <a href='/item/{item_id}'>"
            f"item {item_id}</a> « Le titre &lt;5&gt; » en <b>attend</b> · pour test · "
            f"avant {q['deadline']:%d/%m %H:%M} · escalades restantes {item['escalations']}"
            "</div><div class='text'>On livre ?</div>") in page, page
    assert (f"<input type='hidden' name='question_id' value='{q['id']}'>"
            f"<input type='hidden' name='token' value='{TOKEN}'>"
            "<button name='option' value='go'>go</button>"
            "<button name='option' value='stop'>stop</button></form>") in page
    assert marqueur(page) == f"v{item['version']}-stalled", marqueur(page)
    assert "aucun battement en base" in page and "<p class='flash'>" not in rendu(conn, "/")
    heartbeat.beat(conn, heartbeat.RAIL)
    vivante = rendu(conn, "/")
    assert marqueur(vivante) == f"v{item['version']}-live" and "rail vivant il y a" in vivante
    print("1. / : sujet, item, titre, options, jeton ; message échappé ; battement ✓")


def ligne_items(page: str, item_id: int) -> str:
    debut = page.index(f"<tr><td><a href='/item/{item_id}'>")
    return page[debut:page.index("</tr>", debut) + 5]


def preuve_items(conn, termine: int, actif: int, fanout: int) -> None:
    """2. la table des items : une ligne par item, valeurs de la base."""
    page = rendu(conn, "/items")
    ordre = [int(p.split("'>")[0]) for p in page.split("<tr><td><a href='/item/")[1:]]
    assert ordre == [actif, fanout, termine], "du plus récent au plus ancien"
    fini = etat(conn, termine)
    assert ligne_items(page, termine) == (
        f"<tr><td><a href='/item/{termine}'>{termine}</a></td><td></td><td></td>"
        f"<td>web-vitrine</td><td>g1</td><td>0.0123</td>"
        f"<td><span class='badge terminal'>fini</span></td><td>v{fini['version']}</td>"
        f"<td>{fini['escalations']}</td><td>{fini['terminal_at']:%d/%m %H:%M}</td></tr>")
    attend = etat(conn, actif)
    assert ligne_items(page, actif) == (
        f"<tr><td><a href='/item/{actif}'>{actif}</a></td>"
        f"<td><a href='{ISSUE_URL}'>#5</a></td>"
        f"<td><a href='/item/{actif}'>Le titre &lt;5&gt;</a></td>"
        f"<td>web-vitrine</td><td>g1</td><td>—</td>"
        f"<td><span class='badge active'>attend</span></td><td>v{attend['version']}</td>"
        f"<td>{attend['escalations']}</td><td>actif</td></tr>")
    assert marqueur(page) == f"v{max(fini['version'], attend['version'])}-live"
    print("2. /items : issue et titre en liens, coût, badge, version, fin ✓")


def preuve_en_tete(conn, item_id: int, page: str) -> None:
    """3a. l'en-tête de l'item terminé : PR, révision, badge, temps, tokens."""
    item, data = etat(conn, item_id), views.item_data(conn, item_id)
    assert (f"<h1>item {item_id} <small>· {data['item']['subject_key']} · "
            f"<a href='{PR_URL}'>PR #31</a> (mergée a1b2c3d) · g1 · web-vitrine · "
            f"rév. {item['revision'][:12]}…</small></h1>") in page, page[:900]
    assert (f"<p><span class='badge terminal'>fini</span> v{item['version']} · passage 1 · "
            f"escalades restantes {item['escalations']} · "
            f"lignée restante {data['item']['lineage_budget']} · "
            f"terminé {item['terminal_at']:%d/%m %H:%M:%S}</p>") in page, page[:1200]
    assert (f"<p class='meta'>temps total {pages.dur(data['duration_s'])} · "
            "1 200 in · 30 out · 0.0123 $ rapportés</p>") in page, page[:1400]
    assert "id='cout-jugement'" not in page, "ni juge ni candidats : pas de partage à montrer"
    assert page.count("fill='#ffb74d'") == 1 and "<svg viewBox=" in page
    assert svg.graph_svg(data["bundle"], "fini") in page, "le graph, l'état courant marqué"
    assert data["bundle"]["nodes"] == BUNDLE_VITRINE["nodes"]
    print("3. /item : en-tête avec PR, révision, badge terminal, temps, tokens, SVG ✓")


def preuve_journal_runs(conn, item_id: int, page: str) -> None:
    """3b. le journal et les runs : chaque cellule vient d'une ligne de la base."""
    data = views.item_data(conn, item_id)
    ev = data["events"][2]
    assert (ev["kind"], ev["from_state"], ev["to_state"]) == ("result", "travail", "verif")
    assert (f"<tr><td>v3</td><td>{ev['at']:%d/%m %H:%M:%S}</td>"
            f"<td>{pages.dur(data['spans'][3])}</td><td>result</td>"
            f"<td>travail → verif</td><td>ok</td><td>{ev['run_id']}</td></tr>") in page
    assert (f"<tr><td>v1</td><td>{data['events'][0]['at']:%d/%m %H:%M:%S}</td>"
            "<td></td><td>admitted</td><td>ingest</td><td></td><td></td></tr>") in page
    r = run_de(conn, item_id, "travail")
    assert (f"<tr><td>{r['id']}</td><td>travail</td><td>1</td><td>1</td>"
            "<td><span class='badge applied'>applied</span></td>"
            f"<td>ok</td><td>{r['fence']}</td><td>{r['lease_expires_at']:%H:%M:%S}</td>"
            f"<td>{pages.dur(data['run_span'][r['id']])}</td>"
            "<td>1 200 in · 30 out · 0.0123 $ rapportés</td>"
            "<td>outcome <b>ok</b><pre>dernières lignes</pre></td></tr>") in page, page
    print("   journal v1..vN daté et chronométré, runs avec statut, bail, tokens, résultat ✓")


def preuve_sections(conn, item_id: int, page: str) -> None:
    """3c. effets, questions, workspace : présents, avec les vraies valeurs."""
    q, r = question_de(conn, item_id), run_de(conn, item_id, "travail")
    assert (f"<h2>effets</h2><table><tr><th>clé logique</th><th>cible</th><th>observation</th></tr>"
            f"<tr><td><code>web-vitrine:{r['id']}:travail</code></td><td>stub://outbox</td>"
            "<td><span class='badge applied'>applied</span></td></tr></table>") in page
    assert (f"<tr><td>{q['id']}</td><td>attend</td><td>On livre ?</td><td>closed</td>"
            f"<td>go</td><td>{BY}</td><td>{q['deadline']:%d/%m %H:%M}</td></tr>") in page
    href = f"/item/{item_id}/file"
    assert (f"<h2>workspace</h2><p><a href='{href}/criteria.md'>criteria.md</a> <small>(14 o)</small></p>"
            f"<p><a href='{href}/notes.md'>notes.md</a> <small>(10 o)</small></p>"
            f"<p><a href='{href}/release.json'>release.json</a>") in page, "triés par nom"
    assert api.item(conn, item_id)["criteria"] == "1. ça marche\n", "l'API sert criteria.md tel quel"
    assert (f"<p>shot.png</p><a href='{href}/shot.png'><img src='{href}/shot.png' "
            "style='max-width:100%;border:1px solid #ddd'></a>") in page, "le PNG est une preview"
    print("   effets avec clé logique, questions avec réponse et auteur, workspace avec preview ✓")


def preuve_actif(conn, item_id: int) -> None:
    """4. l'item actif : en cours, sans usage, sections vides absentes ; l'inconnu rend None."""
    item = etat(conn, item_id)
    page = rendu(conn, f"/item/{item_id}")
    assert f"« Le titre &lt;5&gt; » · g1 · web-vitrine" in page and "PR" not in page.split("</h1>")[0]
    assert (f"<span class='badge active'>attend</span> v{item['version']} · passage 1 · "
            f"escalades restantes {item['escalations']}") in page
    assert f"wall deadline {item['wall_deadline']:%d/%m %H:%M}</p>" in page
    assert " (en cours) · aucun usage rapporté</p>" in page, "aucun run n'a rapporté d'usage"
    assert page.count("<h2>") == 3 and "<h2>effets</h2>" not in page and "<h2>workspace</h2>" not in page
    assert f"<td>attend</td><td>On livre ?</td><td>open</td><td></td><td></td>" in page
    assert marqueur(page) == f"v{item['version']}-live"
    for path in ("/item/999999", "/item/abc", "/nope", "/api/items", "/items/"):
        assert rendu(conn, path) is None, path
    print("4. /item actif : wall deadline, en cours, sans usage, sections vides absentes ✓")


def preuve_fanout(conn, item_id: int) -> None:
    """5. le fan-out : le candidat accolé, le recalé, le partage du prix."""
    page = rendu(conn, f"/item/{item_id}")
    runs = views.runs(conn, "WHERE nr.item_id = %s ORDER BY nr.id", (item_id,))
    assert [r["status"] for r in runs] == ["applied", "superseded"], runs
    assert f"<td>{runs[0]['id']}</td><td>travail</td><td>1</td><td>1 · <small>c0</small></td>" in page
    assert (f"<td>1 · <small>c1</small></td><td><span class='badge superseded'>superseded</span>"
            "</td><td></td>") in page, "le recalé n'a ni issue ni usage"
    assert ("<p class='meta' id='cout-jugement'>jugement aucun usage rapporté · "
            "candidats 5 in</p>") in page, page
    assert ligne_items(rendu(conn, "/items"), item_id).count("<td>—</td>") == 1, \
        "des tokens sans coût rapporté : un tiret, pas un zéro"
    print("5. fan-out : c0 et c1 accolés, le recalé en badge, jugement face aux candidats ✓")


def preuve_rendus_purs() -> None:
    """6. les rendus purs : durées, tokens, résultat, battement."""
    assert [pages.dur(s) for s in (None, 4.25, 192, 3840)] == ["", "4,2 s", "3 min 12 s", "1 h 04"]
    assert pages.tokens({"input_tokens": 1234567, "total_cost_usd": 0.5, "model": "opus",
                         "cached": True, "inconnu_tokens": 2}) == \
        "1 234 567 in · 0.5000 $ rapportés · 2 inconnu_tokens", "textes et booléens ignorés"
    assert pages.tokens({}) == "" and pages.candidate({"candidate": None}) == ""
    assert pages.result({"z": 1, "error": "<x>", "outcome": "crashed", "_retry_signature": "s",
                         "usage": {}, "log_tail": "fin"}) == \
        "outcome <b>crashed</b> · error <b>&lt;x&gt;</b> · z <b>1</b><pre>fin</pre>"
    assert pages.result(None) == "" and pages.result({"outcome": "ok"}) == "outcome <b>ok</b>"
    vieux = now() - dt.timedelta(minutes=10)
    assert pages.beat(vieux) == (f"<p class='stalled'>rail à l'arrêt depuis {vieux:%H:%M} — "
                                 "les états affichés sont figés.</p>")
    assert pages.ago(59.6) == "60 s" and pages.ago(150) == "2 min"
    print("6. dur, tokens, result, beat : les formats du contrat ✓")


def preuve_release_cassee(conn, item_id: int) -> None:
    """6b. release.json illisible : la page le dit, l'API ne devine pas."""
    release = bureau.item_workspace(item_id) / "release.json"
    intact = release.read_text()
    release.write_text("{pas du json")
    try:
        assert views.pr_url(item_id) is None and pages.pr(item_id) == " · <small>release.json illisible</small>"
        (entree,) = [i for i in api.items(conn) if i["id"] == item_id]
        assert entree["pr_url"] is None, entree
        assert "release.json illisible" in rendu(conn, f"/item/{item_id}")
    finally:
        release.write_text(intact)
    print("   release.json cassé : dit en page, None en JSON ✓")


BUNDLE_SVG = {
    "name": "svg", "entry": "a", "on_kernel": {"escalate_to": "b"},
    "nodes": {
        "a": {"block": "ACT", "edges": {"ok": "c", "ko": "c", "far": "d"}},
        "b": {"block": "JUDGE&", "edges": {"ok": "c"}},
        "c": {"block": "CHECK", "edges": {"done": "e", "back": "a"}},
        "d": {"block": "WAIT", "escalade": True, "edges": {"retry": "b", "expired": "e"}},
        "e": {"terminal": True},
    },
}


def preuve_svg() -> None:
    """7. le SVG : placement, arêtes, étiquettes, couleurs."""
    assert svg.layers(BUNDLE_SVG) == [["a", "b"], ["c", "d"], ["e"]], "la cible on_kernel en couche 0"
    dessin = svg.graph_svg(BUNDLE_SVG, "c")
    assert dessin.startswith("<svg viewBox='0 0 567 178' ") and dessin.endswith("</svg>")
    assert "<path d='M144,29.0 C184,29.0 157,29.0 197,29.0' fill='none'" in dessin, "a → c en avant"
    assert "y='24.0' font-size='9' fill='#777' text-anchor='middle'>ok, ko</text>" in dessin, \
        "deux issues vers la même cible, un seul trait"
    assert dessin.count("x='170.5' y='56.0'") == 1 and dessin.count("x='170.5' y='46.0'") == 1, \
        "a → d et b → c ont le même milieu : la seconde étiquette est décalée, pas empilée"
    assert "<path d='M197,29.0 C137,172 204,172 144,29.0'" in dessin, "c → a contourne par le bas"
    assert "<path d='M197,93.0 C137,158 204,158 144,93.0'" in dessin, "d → b, une voie plus haut"
    assert "y='168'" in dessin and "y='154'" in dessin, "une étiquette par voie"
    assert dessin.count("marker-end='url(#arr)'") == 7, "sept couples (source, cible)"
    assert ("<rect x='197' y='12' width='132' height='34' rx='7' fill='#ffb74d' stroke='#888'/>"
            "<text x='263.0' y='33.0' font-size='11' text-anchor='middle'>c · CHECK</text>") in dessin
    assert dessin.count("fill='#ffb74d'") == 1, "un seul état courant"
    assert "fill='#e3ecf7' stroke='#888'/><text x='78.0' y='33.0' font-size='11' " \
           "text-anchor='middle'>a · ACT</text>" in dessin
    assert "fill='#e3ecf7' stroke='#888' stroke-dasharray='4 2'/>" in dessin, "l'escalade en pointillé"
    assert "fill='#eee' stroke='#888'/><text x='448.0' y='33.0' font-size='11' " \
           "text-anchor='middle'>e</text>" in dessin, "un terminal : gris, sans bloc"
    assert ">b · JUDGE&amp;</text>" in dessin, "les libellés sont échappés"
    print("7. SVG : couches, arêtes fusionnées, voies de retour, étiquettes décalées, couleurs ✓")


def preuve_partage(conn, fanout: int) -> None:
    """5b. le prix du jugement face à celui des candidats — et le graph qui le permet."""
    bundle = {"name": "juge", "entry": "gen", "on_kernel": {}, "agent": {"cli": "codex"},
              "nodes": {"gen": {"block": "ACT", "config": {"fanout": {
                            "variants": [{"label": "x"}], "reduce": "keep_n", "n": 1}}},
                        "juge": {"block": "JUDGE", "config": {"finalists_from": "gen"}}}}
    tentative = {"cycle": 1, "attempt": 1, "status": "applied", "finished_at": now()}
    lot = [{"id": 3, "node": "juge", "candidate": None, "outcome": "chosen",
            "result": {"usage": {"a": 1}, "elu": "A", "summary": "x l'emporte"}} | tentative,
           {"id": 2, "node": "gen", "candidate": 0, "outcome": "ok",
            "result": {"usage": {"b": 2, "modele": "x"}}} | tentative,
           {"id": 1, "node": "gen", "candidate": None, "outcome": "ok",
            "result": {"usage": {"c": 3.5}}} | tentative]
    assert views.cost_split(bundle, lot) == {"judgement": {"a": 1}, "candidates": {"b": 2},
                                             "other": {"c": 3.5}}, "trois parts disjointes"
    decision = views.decision(999, bundle, lot)
    assert decision["finalists"] == [{"letter": "A", "candidate": 0}], "un seul candidat, lettre A"
    assert decision["selected"] == {"letter": "A", "candidate": 0} and decision["verdict"] is None
    assert (decision["summary"], decision["judge"]["cli"]) == ("x l'emporte", "codex"), decision
    seul = views.decision(999, bundle, [lot[0] | {"outcome": "sole", "result": {}}, *lot[1:]])
    assert seul["selected"] == {"letter": "A", "candidate": 0} and seul["judge"] is None, \
        "un finaliste unique passe sans juge : la lettre A, aucun modèle appelé"
    aucun = views.decision(999, bundle, [lot[0] | {"outcome": "none", "result": {}}, lot[2]])
    assert aucun["finalists"] == [] and aucun["selected"] is None, aucun
    assert pages.split_line(bundle, lot) == ("<p class='meta' id='cout-jugement'>"
                                             "jugement 1 a · candidats 2 b</p>")
    noeuds = {n["name"]: n for n in api.graph(bundle, "juge")["nodes"]}
    assert noeuds["juge"]["finalists_from"] == "gen" and "fanout" not in noeuds["juge"]
    assert noeuds["gen"]["fanout"] == {"reduce": "keep_n", "repeat": 1, "candidates": [
        {"variant": {"label": "x"}, "agent": {"cli": "codex"}, "cmd": None}]}
    assert views.totals(lot) == {"a": 1, "b": 2, "c": 3.5}, "les textes d'usage ne s'additionnent pas"
    assert api.item(conn, fanout)["item"]["usage_split"] == {
        "judgement": {}, "candidates": {"input_tokens": 5}, "other": {}}, "l'API porte le partage"
    print("   partage : jugement, candidats, autres ; le graph nomme fanout et source ✓")


def main() -> None:
    db.init_db(drop=True)
    with db.connect() as conn:
        preuve_vide(conn)
        termine = item_termine(conn)
        fanout = item_fanout(conn)
        actif = vitrine(conn, ISSUE, TITRE, travail={"outcome": "ok"})
        preuve_questions(conn, actif)
        preuve_items(conn, termine, actif, fanout)
        page = rendu(conn, f"/item/{termine}")
        preuve_en_tete(conn, termine, page)
        preuve_journal_runs(conn, termine, page)
        preuve_sections(conn, termine, page)
        preuve_actif(conn, actif)
        preuve_fanout(conn, fanout)
        preuve_partage(conn, fanout)
        preuve_rendus_purs()
        preuve_release_cassee(conn, termine)
        preuve_svg()
    print("\npages : OK — chaque cellule vient de la base, rien n'est inventé")


if __name__ == "__main__":
    main()
