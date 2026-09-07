"""Le test du serveur `graphatom serve` : la seule porte d'écriture, et les routes.

Sur un Postgres jetable et le vrai serveur, dans un fil :

  1. une route inconnue est un 404 — en JSON sous `/api`, en HTML ailleurs,
     et `POST` ailleurs que sur `/answer` aussi
  2. rien d'illisible n'écrit : corps illisible → 400, mauvais jeton →
     403, option hors contrat ou question inconnue → refus dit en clair ;
     la question reste ouverte après chacun
  3. une réponse valide est prise une fois — et ne route rien : l'état de
     l'item ne bouge qu'au tick suivant, qui ferme la question
  4. le formulaire d'avant tient : 303 vers `/?m=…`, et la page affiche le
     message ; la seconde réponse d'une question réglée est refusée
  5. le contrat de chaque route JSON, sur l'état réel : items, item,
     questions, heartbeat, load, graphs, graph/<rév>
  6. la trace d'un run : le curseur objet, `offset`, `<nom>_offset`, les
     formes invalides → 400, l'item ou le run étranger → 404, un décalage
     au-delà du fichier → contenu vide, et les octets nouveaux qui suivent
  7. les fichiers du workspace : PNG en image/png, JSON en application/json,
     et aucune traversée — ni `..`, ni un sous-répertoire, ni un lien
     symbolique qui sort du workspace
  8. la base qui tombe : 500 en JSON sous `/api`, 500 en page ailleurs
  9. `notify_once` pousse le JSON de la question sur stdin de la commande,
     ne la marque dite que si la commande sort en 0 ; `notify_loop`
     survit à une base absente, et `serve --notify-cmd` dit chaque
     question ouverte en une passe
 10. N réponses valides et contraires, au même instant, sur la même question :
     une seule est prise, les autres refusées « déjà réglée », la base porte
     une ligne `answered` et une seule réponse
 11. un client qui garde sa connexion ouverte ne retient pas l'arrêt
 12. un POST sans Content-Length est refusé en 400 sans attendre de corps

Usage : uv run python tests/web_server_test.py
"""

import json
import socket
import threading
import time
from urllib.parse import quote, unquote

# web_outils épingle GRAPHATOM_DSN sur la base jetable : il passe avant graphatom
from web_outils import (  # noqa: E402
    BUNDLE_VITRINE, OPTIONS, TMP, appel, get_json, jeton, question_de, run_de,
    serveur, sujet, vitrine,
)
from outils import attendre, etat  # noqa: E402

from graphatom import api, db, graph, heartbeat, scheduler, web  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

JSON = {"Accept": "application/json", "Content-Type": "application/json"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
NOTIF = TMP / "notifications.jsonl"
BY = "web-test"
COUREURS = 6  # les réponses lâchées au même instant sur une même question


def post_answer(base: str, fields, headers: dict = JSON) -> tuple[int, dict, bytes]:
    body = json.dumps(fields) if headers is JSON else fields
    return appel(base, "POST", "/answer", body.encode(), headers)


def reponse_json(base: str, fields: dict) -> tuple[int, dict]:
    status, _, body = post_answer(base, fields)
    return status, json.loads(body)


def ouverte(conn, qid: int) -> bool:
    q = conn.execute("SELECT state, answered_by FROM question WHERE id = %s",
                     (qid,)).fetchone()
    return q["state"] == "open" and q["answered_by"] is None


def ligne_vers(fichier) -> str:
    """La commande de notification : stdin en une ligne de plus dans le fichier."""
    return f"(cat; echo) >> {fichier}"


def notifiees() -> list[dict]:
    if not NOTIF.is_file():
        return []
    return [json.loads(line) for line in NOTIF.read_text().splitlines() if line]


# ------------------------------------------------------------------ preuves


def preuve_routes(base: str) -> None:
    """1. une route inconnue : 404 dans la langue du client."""
    status, payload = get_json(base, "/api/nope")
    assert (status, payload) == (404, {"error": "route inconnue : /api/nope"}), payload
    status, en_tetes, body = appel(base, "GET", "/nope")
    assert status == 404 and body == b"introuvable", (status, body)
    assert en_tetes["Content-Type"].startswith("text/html"), en_tetes
    assert appel(base, "GET", "/item/abc")[0] == 404, "un id non numérique n'est pas une page"
    assert get_json(base, "/api/item/abc") == (404, {"error": "route inconnue : /api/item/abc"}), \
        "un id non numérique n'est pas une route JSON non plus"
    status, _, body = appel(base, "POST", "/nope", b"", JSON)
    assert (status, json.loads(body)) == (404, {"ok": False, "message": "introuvable"})
    status, _, body = appel(base, "POST", "/nope", b"")
    assert (status, body) == (404, b"introuvable"), (status, body)
    autre = serveur("autre-canal")  # sans notification : chaque serveur frappe son jeton
    assert jeton(autre) != jeton(base) and len(jeton(autre)) == 32, "un jeton par serveur"
    print("1. route inconnue → 404 en JSON sous /api, en HTML ailleurs ✓")


def preuve_refus(conn, base: str, token: str, qid: int) -> None:
    """2. rien d'illisible, rien de non signé, rien d'hors contrat n'écrit."""
    for corps in ("{pas du json", '{"option": "go"}', '{"question_id": "x", "option": "go"}',
                  "[1]"):
        status, _, body = appel(base, "POST", "/answer", corps.encode(), JSON)
        assert status == 400, (corps, status, body)
        assert json.loads(body)["message"].startswith("requête illisible"), body
    status, _, body = appel(base, "POST", "/answer", b"", FORM)
    assert status == 400 and body.startswith(b"requ"), (status, body)
    status, _, body = post_answer(base, f"question_id={qid}&option=go", FORM)
    assert (status, body) == (403, "jeton invalide — rechargez la page".encode()), body
    status, payload = reponse_json(base, {"question_id": qid, "option": "go", "token": "faux"})
    assert (status, payload["ok"]) == (403, False), payload
    assert ouverte(conn, qid), "aucun refus n'écrit"
    status, payload = reponse_json(base, {"question_id": qid, "option": "peut-être",
                                          "token": token})
    assert status == 200 and payload == {
        "ok": False, "message": f"option invalide — attendu : {', '.join(OPTIONS)}"}, payload
    status, payload = reponse_json(base, {"question_id": qid + 1000, "option": "go",
                                          "token": token})
    assert payload == {"ok": False, "message": "question inconnue"}, payload
    assert ouverte(conn, qid), "un refus du canal n'écrit pas non plus"
    print("2. illisible → 400, mauvais jeton → 403, hors contrat → refus : rien n'est écrit ✓")


def preuve_reponse(conn, base: str, token: str, qid: int, item_id: int) -> None:
    """3. une réponse valide, prise une fois ; le rail reprend au tick, pas ici."""
    avant = etat(conn, item_id)
    _, ouvertes = get_json(base, "/api/questions")
    (q,) = ouvertes["questions"]
    assert (q["question_id"], q["options"], q["item_state"]) == (qid, OPTIONS, "attend"), q
    assert q["subject_key"] == etat_sujet(conn, item_id) and q["issue_url"] is None, q
    status, payload = reponse_json(base, {"question_id": qid, "option": "go", "token": token})
    assert status == 200 and payload == {
        "ok": True, "message": "réponse « go » enregistrée — le rail reprend"}, payload
    q = question_de(conn, item_id)
    assert (q["state"], q["answer"], q["answered_by"]) == ("answered", "go", BY), q
    assert q["answered_at"] is not None
    apres = etat(conn, item_id)
    assert (apres["state"], apres["version"]) == (avant["state"], avant["version"]), \
        "le canal enregistre, il ne route pas"
    status, payload = reponse_json(base, {"question_id": qid, "option": "stop", "token": token})
    assert payload == {"ok": False, "message": "question déjà réglée (answered)"}, payload
    assert question_de(conn, item_id)["answer"] == "go", "la première réponse reste"
    scheduler.tick(conn)
    assert etat(conn, item_id)["state"] == "fini", "c'est le tick qui route"
    assert question_de(conn, item_id)["state"] == "closed"
    print("3. réponse valide prise une fois, l'item ne bouge qu'au tick ✓")


def preuve_formulaire(conn, base: str, token: str, qid: int, item_id: int) -> None:
    """4. le formulaire d'avant : 303 vers la page, qui affiche le message."""
    status, en_tetes, _ = post_answer(
        base, f"question_id={qid}&option=stop&token={token}", FORM)
    message = "réponse « stop » enregistrée — le rail reprend"
    assert status == 303 and en_tetes["Location"] == f"/?m={quote(message)}", en_tetes
    assert question_de(conn, item_id)["answered_by"] == BY
    status, _, body = appel(base, "GET", en_tetes["Location"])
    assert status == 200 and f"<p class='flash'>{message}</p>" in body.decode(), body[:600]
    status, en_tetes, _ = post_answer(
        base, f"question_id={qid}&option=go&token={token}", FORM)
    assert status == 303 and "d%C3%A9j%C3%A0%20r%C3%A9gl%C3%A9e" in en_tetes["Location"], \
        "le refus fait le même chemin que la réponse : la page le dit"
    assert unquote(en_tetes["Location"]) == "/?m=question déjà réglée (answered)"
    scheduler.tick(conn)
    assert etat(conn, item_id)["state"] == "abandon"
    print("4. formulaire → 303 /?m=…, la page affiche le message, le refus aussi ✓")


def preuve_api(conn, base: str, items: list[int], rev: str) -> None:
    """5. le contrat de chaque route JSON, sur l'état réel."""
    status, liste = get_json(base, "/api/items")
    assert status == 200 and [i["id"] for i in liste] == sorted(items, reverse=True), liste
    assert {i["status"] for i in liste} == {"terminal"} and liste[0]["state"] == "abandon"
    status, item = get_json(base, f"/api/item/{items[0]}")
    assert status == 200 and item["item"]["id"] == items[0], item
    assert item["graph"]["current"] == "fini" and item["item"]["revision"] == rev
    assert [ev["kind"] for ev in item["journal"]] == ["admitted"] + ["result"] * 3 + ["answer"]
    assert [r["node"] for r in item["runs"]] == ["ingest", "travail", "verif"], item["runs"]
    assert item["runs"][1]["usage"]["input_tokens"] == 1200, item["runs"][1]
    assert item["questions"][0]["answer"] == "go" and item["questions"][0]["state"] == "closed"
    assert get_json(base, "/api/item/999999") == (404, {"error": "item 999999 inconnu"})
    status, questions = get_json(base, "/api/questions")
    assert questions["questions"] == [] and questions["token"], questions
    heartbeat.beat(conn, heartbeat.RAIL, "abc123", None)
    status, beats = get_json(base, "/api/heartbeat")
    assert beats["rail"]["sha"] == "abc123" and beats["rail"]["stale"] is False, beats
    assert beats["rail"]["ago_s"] < 5 and beats["github-sync"] == {
        "at": None, "ago_s": None, "stale": True}, beats
    assert api.beats(None, None)["rail"] == {"at": None, "ago_s": None, "stale": True,
                                             "sha": None, "started_at": None}, \
        "un battement brut, sans ligne de worker : aucune identité inventée"
    status, charge = get_json(base, "/api/load")
    assert charge["running"] == 0 and charge["max_runs"] == scheduler.MAX_RUNS, charge
    status, graphs = get_json(base, "/api/graphs")
    assert graphs == [{"id": rev, "name": "web-vitrine", "items": len(items),
                       "published_at": graphs[0]["published_at"]}], graphs
    status, bundle = get_json(base, f"/api/graph/{rev}")
    assert status == 200, "une révision connue répond 200, comme les collections"
    assert bundle["nodes"] == BUNDLE_VITRINE["nodes"] and bundle["items"] == len(items)
    assert get_json(base, "/api/graph/r%C3%A9v-x") == (
        404, {"error": "révision rév-x inconnue"}), "la révision est décodée"
    print("5. /api/items, item, questions, heartbeat, load, graphs, graph/<rév> ✓")


def trace_url(item_id: int, run_id: int, query: str = "") -> str:
    return f"/api/item/{item_id}/run/{run_id}/trace{query}"


def preuve_curseur(base: str, item_id: int, run_id: int, atelier) -> None:
    """6a. le curseur : objet, `offset`, `<nom>_offset` — et les formes refusées."""
    status, trace = get_json(base, trace_url(item_id, run_id))
    assert status == 200 and trace["status"] == "applied", trace
    assert trace["events"]["type"] == "codex" and trace["log"]["content"] == "abcdef\n", trace
    tailles = {nom: (atelier / f).stat().st_size for nom, f in TRACES.items()}
    assert trace["cursor"] == tailles and tailles["log"] == 7, (trace["cursor"], tailles)
    _, trace = get_json(base, trace_url(item_id, run_id, "?cursor=" + quote('{"log":3}')))
    assert trace["log"]["content"] == "def\n" and trace["events"]["offset"] == 0, trace
    _, trace = get_json(base, trace_url(item_id, run_id, "?offset=2"))
    assert {trace[n]["offset"] for n in ("events", "log", "command")} == {2}, trace
    _, trace = get_json(base, trace_url(item_id, run_id, "?offset=2&log_offset=4"))
    assert (trace["log"]["offset"], trace["events"]["offset"]) == (4, 2), trace
    _, trace = get_json(base, trace_url(item_id, run_id, "?log_offset=999"))
    assert (trace["log"]["content"], trace["log"]["next_offset"]) == ("", 7), \
        "au-delà du fichier : rien à lire, et la vraie fin en retour"
    for query, motif in (("cursor={", "curseur invalide"),
                         ("cursor=[1]", "le curseur doit être un objet"),
                         ("offset=-1", "curseur codex invalide"),
                         ("offset=abc", "invalid literal"),
                         ('cursor={"log":true}', "curseur log invalide"),
                         ("log_offset=1.5", "invalid literal")):
        cle, _, valeur = query.partition("=")
        status, erreur = get_json(base, trace_url(item_id, run_id, f"?{cle}={quote(valeur)}"))
        assert status == 400 and motif in erreur["error"], (query, erreur)
    with (atelier / TRACES["log"]).open("a") as flux:
        flux.write("ghi\n")
    _, suite = get_json(base, trace_url(item_id, run_id, "?cursor=" + quote('{"log":7}')))
    assert suite["log"]["content"] == "ghi\n" and suite["cursor"]["log"] == 11, suite
    print("6. trace : curseur objet, offset, <nom>_offset, 400 sur l'illisible, la suite ✓")


def preuve_trace_identite(base: str, item_id: int, run_id: int, autre_item: int,
                          autre_run: int) -> None:
    """6b. l'identité : item, run, et l'appartenance de l'un à l'autre."""
    refus = (404, {"error": "item ou run inconnu"})
    assert get_json(base, trace_url(999999, run_id)) == refus
    assert get_json(base, trace_url(item_id, 999999)) == refus
    assert get_json(base, trace_url(autre_item, run_id)) == refus, \
        "un run d'un autre item n'est pas servi sous celui-ci"
    status, nue = get_json(base, trace_url(autre_item, autre_run))
    assert status == 200 and nue["events"] == {
        "type": None, "state": "missing", "content": "", "offset": 0, "next_offset": 0,
        "media_type": "application/x-ndjson"}, "sans trace archivée ni vive : manquante, sans fournisseur"
    assert nue["log"]["state"] == "missing" and nue["cursor"] == {"events": 0, "log": 0, "command": 0}
    print("   trace : item inconnu, run inconnu, run d'un autre item → 404 ; sans fichier → manquante ✓")


def preuve_fichiers(base: str, item_id: int, autre_item: int) -> None:
    """7. les fichiers du workspace : le type par suffixe, aucune traversée."""
    atelier, ailleurs = bureau.item_workspace(item_id), bureau.item_workspace(autre_item)
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
    (atelier / "shot.png").write_bytes(png)
    (atelier / "notes.md").write_text("des notes\n")
    (atelier / "c0").mkdir()
    (atelier / "c0" / "dedans.txt").write_text("sous-répertoire\n")
    ailleurs.mkdir(exist_ok=True)
    (ailleurs / "secret.md").write_text("l'autre item\n")
    (atelier / "lien.md").symlink_to(ailleurs / "secret.md")
    assert (atelier / "lien.md").read_text() == "l'autre item\n", "le lien lui-même se lit"
    for nom, ctype in (("shot.png", "image/png"), ("release.json", "application/json"),
                       ("notes.md", "text/plain")):
        status, en_tetes, body = appel(base, "GET", f"/item/{item_id}/file/{nom}")
        assert status == 200 and en_tetes["Content-Type"] == f"{ctype}; charset=utf-8", en_tetes
        assert body == (atelier / nom).read_bytes(), nom
    for nom in ("absent.md", f"..%2Fitem-{autre_item}%2Fsecret.md", "c0%2Fdedans.txt",
                "lien.md"):
        status, _, body = appel(base, "GET", f"/item/{item_id}/file/{nom}")
        assert (status, body) == (404, b"introuvable"), (nom, status)
    assert appel(base, "GET", f"/item/{item_id}/file/c0/dedans.txt")[0] == 404, \
        "un chemin à cinq segments n'est même pas une route de fichier"
    print("7. fichiers : png, json, texte par suffixe ; .., sous-répertoire et lien "
          "vers l'extérieur → 404 ✓")


def preuve_course(conn, base: str, token: str, item_id: int, qid: int) -> None:
    """10. N réponses valides et contraires au même instant : une seule est prise."""
    depart, issues, fautes = threading.Barrier(COUREURS), [], []

    def repond(option: str) -> None:
        try:
            depart.wait(5)
            issues.append(reponse_json(base, {"question_id": qid, "option": option,
                                              "token": token}))
        except BaseException as exc:  # noqa: BLE001 — relevée par la preuve
            fautes.append(exc)

    fils = [threading.Thread(target=repond, args=(OPTIONS[i % len(OPTIONS)],))
            for i in range(COUREURS)]
    for fil in fils:
        fil.start()
    for fil in fils:
        fil.join(10)
    if fautes:
        raise fautes[0]
    assert len(issues) == COUREURS and {s for s, _ in issues} == {200}, issues
    lignes = conn.execute("SELECT state, answer FROM question WHERE item_id = %s",
                          (item_id,)).fetchall()
    assert len(lignes) == 1 and lignes[0]["state"] == "answered", lignes
    prises = [p["message"] for _, p in issues if p["ok"]]
    assert prises == [f"réponse « {lignes[0]['answer']} » enregistrée — le rail reprend"]
    refusees = [p["message"] for _, p in issues if not p["ok"]]
    assert refusees == ["question déjà réglée (answered)"] * (COUREURS - 1), refusees
    print(f"10. {COUREURS} réponses contraires au même instant : une prise, "
          f"{COUREURS - 1} refusées « déjà réglée », une ligne answered ✓")


def preuve_panne(base: str) -> None:
    """8. la base qui tombe : l'erreur est dite, en JSON ou en page."""
    def tombe():
        raise RuntimeError("base injoignable <x>")
    vrai = db.connect
    web.db.connect = tombe
    try:
        assert get_json(base, "/api/items") == (500, {"error": "base injoignable <x>"})
        status, en_tetes, body = appel(base, "GET", "/")
        assert (status, body) == (500, "erreur : base injoignable &lt;x&gt;".encode()), body
        assert en_tetes["Content-Type"].startswith("text/html")
    finally:
        web.db.connect = vrai
    assert get_json(base, "/api/load")[0] == 200, "la base revenue, le serveur aussi"
    print("8. base tombée → 500 JSON sous /api, 500 HTML ailleurs, échappé ✓")


def preuve_notify_once(conn, base: str, item_id: int, qid: int) -> None:
    """9a. la commande reçoit le JSON, et ne marque la question dite qu'en 0."""
    dites, ratees = TMP / "dites.jsonl", TMP / "ratees.jsonl"
    lignes: list[str] = []
    web.print = lambda *a, **_k: lignes.append(" ".join(map(str, a)))
    try:
        vus: set[int] = set()
        web.notify_once(ligne_vers(dites), "http://ici/", vus)
        payload = json.loads(dites.read_text())
        assert vus == {qid} and payload["question_id"] == qid, (vus, payload)
        assert payload["options"] == OPTIONS and payload["url"] == "http://ici/", payload
        assert payload["subject"] == etat_sujet(conn, item_id) and payload["owner"] == "test"
        web.notify_once(ligne_vers(dites), "http://ici/", vus)
        assert len(dites.read_text().splitlines()) == 1, "une question déjà dite ne repart pas"
        pas_vus: set[int] = set()
        for _ in range(2):
            web.notify_once(f"{ligne_vers(ratees)}; exit 3", "http://ici/", pas_vus)
        assert pas_vus == set() and len(ratees.read_text().splitlines()) == 2, \
            "une commande en échec laisse la question due : la passe suivante la redit"
        assert lignes == [f"notify: question {qid} — commande en code 3, on réessaie"] * 2
    finally:
        del web.print
    assert attendre(lambda: qid in {n["question_id"] for n in notifiees()}, 5), \
        "serve --notify-cmd dit la question ouverte de lui-même"
    assert [n["url"] for n in notifiees() if n["question_id"] == qid] == [base]
    print("9. notify : JSON sur stdin, dite seulement en code 0, redite sinon ✓")


def etat_sujet(conn, item_id: int) -> str:
    return conn.execute(
        "SELECT s.subject_key FROM work_item w JOIN subject s ON s.id = w.subject_id "
        "WHERE w.id = %s", (item_id,)).fetchone()["subject_key"]


def preuve_notify_loop() -> None:
    """9b. la boucle survit à une base absente : elle le dit, et continue."""
    lignes: list[str] = []
    web.print = lambda *a, **_k: lignes.append(" ".join(map(str, a)))
    vrai = db.connect

    def tombe():
        raise RuntimeError("base partie")
    web.db.connect = tombe
    try:
        assert attendre(lambda: "notify: base partie" in lignes, 5), lignes
    finally:
        web.db.connect = vrai
        del web.print
    print("   notify_loop : la base absente est dite, la boucle ne meurt pas ✓")


def preuve_notify_serve(conn, item_2: int, qid_2: int) -> None:
    """9c. la boucle du serveur, revenue de la panne, dit la question suivante."""
    assert attendre(lambda: qid_2 in {n["question_id"] for n in notifiees()},
                    100 * web.NOTIFY_POLL_S), "dite en moins de cent passes"
    ids = [n["question_id"] for n in notifiees()]
    assert question_de(conn, item_2)["id"] == qid_2 and len(ids) == 2, ids
    assert len(ids) == len(set(ids)), f"chaque question dite une fois : {ids}"
    fils = [t for t in threading.enumerate() if t.name == "notify"]
    assert fils and all(t.daemon for t in fils), "la boucle de notification est un fil démon"
    print("   serve --notify-cmd : chaque question ouverte dite, une seule fois, en démon ✓")


TRACES = {"events": "codex-travail-1-1.jsonl", "log": "agent-travail-1-1.log",
          "command": "command-travail-1-1.json"}


def traces_du_run(item_id: int, run: dict):
    """Les trois traces de la tentative, sous les noms que le rail leur donne."""
    atelier = bureau.run_workspace(item_id, run)
    atelier.mkdir(parents=True, exist_ok=True)
    assert bureau.attempt_log(atelier, run) == atelier / TRACES["log"]
    assert bureau.attempt_command(atelier, run) == atelier / TRACES["command"]
    (atelier / TRACES["events"]).write_text('{"type":"start"}\n')
    (atelier / TRACES["log"]).write_text("abcdef\n")
    (atelier / TRACES["command"]).write_text("{}")
    (atelier / "release.json").write_text('{"pr_url": "https://x/pull/1"}')
    return atelier


def preuve_client_qui_traine() -> None:
    """11. Un client qui garde sa connexion ouverte ne retient pas le serveur.

    Les fils de requête sont des démons : le serveur s'arrête pendant qu'une
    connexion traîne encore, et le fil qui la sert ne bloque personne.
    """
    server = web.build_server("127.0.0.1", 0, "jeton", "web")
    assert server.daemon_threads is True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lent = socket.create_connection(server.server_address)
    lent.sendall(b"GET /api/load HTTP/1.1\r\nHost: x\r\n")  # jamais terminée
    time.sleep(0.3)
    fils = [t for t in threading.enumerate() if t.daemon and "Thread-" in t.name]
    assert fils, "le fil de la connexion traînante est un démon"
    debut = time.monotonic()
    server.shutdown()
    server.server_close()
    assert time.monotonic() - debut < 5, "l'arrêt n'attend pas le client qui traîne"
    lent.close()
    print("11. un client qui traîne ne retient pas l'arrêt : fils démons, arrêt immédiat ✓")


def preuve_sans_longueur(base: str) -> None:
    """12. Un POST sans Content-Length : rien à lire, refus immédiat en 400.

    Sans en-tête, la longueur vaut zéro : le serveur ne tente pas de lire un
    corps qui ne viendra jamais — un client muet ne bloque pas un fil.
    """
    host, port = base.split("/")[2].split(":")
    with socket.create_connection((host, int(port)), timeout=5) as muet:
        muet.sendall(b"POST /answer HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        premiere = muet.recv(64).split(b"\r\n", 1)[0]
    assert b" 400 " in premiere, premiere
    print("12. POST sans Content-Length : 400 tout de suite, aucun corps attendu ✓")


def main() -> None:
    db.init_db(drop=True)
    web.NOTIFY_POLL_S = 0.05
    with db.connect() as conn:
        base = serveur(BY, notify_cmd=ligne_vers(NOTIF))
        token = jeton(base)
        rev = graph.publish(conn, BUNDLE_VITRINE)
        item_1 = vitrine(conn, sujet("gh-like"))
        qid_1 = question_de(conn, item_1)["id"]
        preuve_routes(base)
        preuve_refus(conn, base, token, qid_1)
        preuve_notify_once(conn, base, item_1, qid_1)
        preuve_notify_loop()
        preuve_reponse(conn, base, token, qid_1, item_1)
        item_2 = vitrine(conn)
        qid_2 = question_de(conn, item_2)["id"]
        preuve_notify_serve(conn, item_2, qid_2)
        preuve_formulaire(conn, base, token, qid_2, item_2)
        preuve_client_qui_traine()
        preuve_sans_longueur(base)
        preuve_api(conn, base, [item_1, item_2], rev)
        run = run_de(conn, item_1, "travail")
        atelier = traces_du_run(item_1, run)
        preuve_curseur(base, item_1, run["id"], atelier)
        preuve_trace_identite(base, item_1, run["id"], item_2,
                              run_de(conn, item_2, "travail")["id"])
        preuve_fichiers(base, item_1, item_2)
        item_3 = vitrine(conn)
        preuve_course(conn, base, token, item_3, question_de(conn, item_3)["id"])
        preuve_panne(base)
    web.NOTIFY_POLL_S = 3600  # la boucle de notification s'endort avant que la base parte
    time.sleep(0.3)
    print("\nserveur : OK — une seule porte d'écriture, et elle ne route pas")


if __name__ == "__main__":
    main()
