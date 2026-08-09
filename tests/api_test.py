"""Le test de l'API JSON du canal web : la projection, sans base ni serveur.

Les six lectures sont des fonctions pures d'une base et d'un workspace —
elles se jouent ici sur une base factice et un répertoire jetable :

  1. `/api/items` : les clés du contrat, `status` qui n'est pas `state`,
     l'URL de l'issue quand le sujet en est une, celle de la PR quand
     `release.json` en porte une
  2. `/api/item/<id>` : les sept clés de la page, et un item inconnu qui
     ne rend rien du tout — c'est ce None que la route tourne en 404
  3. le graph redessinable sans la base — fan-out d'un nœud compris, un
     candidat par entrée —, les runs avec durée et tokens, les fichiers du
     workspace avec l'URL qui les sert
  4. `/api/questions` : le jeton de `POST /answer`, jusqu'ici enfoui dans
     le HTML, et les options de chaque question ouverte
  5. `/api/heartbeat`, et tout le reste sérialisable : les horodatages
     sortent en ISO 8601, jamais un `TypeError` au moment de répondre ;
     `/api/load`, la charge du rail — runs en vol et plafonds effectifs
  6. `/api/graphs` et `/api/graph/<rév>` : les révisions publiées avec le
     compte d'items qui les portent, et le bundle entier d'une révision —
     config des nœuds comprise —, une révision inconnue rendant None

Usage : uv run python tests/api_test.py
"""

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, quota, scheduler, web  # noqa: E402

T0 = dt.datetime(2026, 8, 7, 10, 0, tzinfo=dt.timezone.utc)
ISSUE = "gh:Acid3croco/graphatom#66"
ISSUE_URL = "https://github.com/Acid3croco/graphatom/issues/66"
PR_URL = "https://github.com/Acid3croco/graphatom/pull/70"

BUNDLE = {
    "name": "démo", "entry": "ingest", "budgets": {},
    "agent": {"cli": "claude", "model": "opus", "effort": "high"},
    "on_kernel": {"escalate_to": "escalate", "exhausted_to": "closed"},
    "nodes": {
        "ingest": {"block": "FETCH", "edges": {"ok": "decide"}},
        "decide": {"block": "JUDGE",
                   "config": {"lease_s": 600,
                              "agent": {"cmd": "vrai", "prompt": "le prompt entier",
                                        "timeout_s": 900}},
                   "edges": {"fix": "closed", "raise": "escalate"}},
        "escalate": {"block": "WAIT", "escalade": True,
                     "edges": {"retry": "ingest", "expired": "closed"}},
        "closed": {"terminal": True},
    },
}


# un nœud en fan-out, tel qu'un graph le déclare : deux variantes jouées
# deux fois, dont une qui surcharge le modèle structuré du nœud
FANOUT_NODE = {
    "block": "ACT",
    "config": {
        "agent": {"prompt": "le prompt du nœud"},
        "fanout": {
            "variants": [
                {"label": "opus", "strategy": "raisonne longtemps"},
                {"label": "haiku", "strategy": "va droit au but",
                 "agent": {"model": "haiku"}},
            ],
            "repeat": 2,
            "reduce": "first_pass",
        },
    },
    "edges": {"ok": "closed"},
}


class FakeCursor:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):  # `open_questions` fait un `list(...)` du curseur
        return iter(self.rows)


class FakeConn:
    """La base : chaque requête rend les lignes de la table qu'elle nomme.

    L'ordre compte — `ITEM_SELECT` nomme `event` dans sa sous-requête de
    `updated_at`, donc `work_item` doit être testé avant.
    """

    def __init__(self, **tables):
        self.tables = tables

    def execute(self, sql: str, params: tuple = ()):
        for name, rows in self.tables.items():
            if f"FROM {name}" in sql:
                return FakeCursor(rows)
        return FakeCursor([])


class CountConn:
    """La base qui compte les runs et les places de construction."""

    def __init__(self, running: int, builds: int = 0,
                 solo_running: int = 0, solo_waiting: int = 0):
        self.running = running
        self.builds = builds
        self.solo_running = solo_running
        self.solo_waiting = solo_waiting

    def execute(self, sql: str, params: tuple = ()):
        if "FROM work_item" in sql:
            return FakeCursor([
                {"id": i, "state": "travail", "revision": "solo-rev"}
                for i in range(1, self.solo_running + self.solo_waiting + 1)
            ])
        if "SELECT DISTINCT item_id" in sql:
            return FakeCursor([{"item_id": i}
                               for i in range(1, self.solo_running + 1)])
        if "FROM graph_revision" in sql:
            return FakeCursor([{"bundle": {"nodes": {"travail": {
                "block": "ACT", "config": {"solo": True}}}}}])
        return FakeCursor([{"n": self.builds if "pg_locks" in sql else self.running}])


def item_row(item_id: int, subject_key: str, terminal: bool) -> dict:
    return {"id": item_id, "subject_key": subject_key, "title": "un titre",
            "graph": "code-task", "generation": 1, "revision": "abc123",
            "state": "closed" if terminal else "decide", "version": 4, "cycle": 1,
            "fence": 0, "escalations": 2, "lineage_budget": 2,
            "wall_deadline": T0 + dt.timedelta(hours=6),
            "terminal_at": T0 + dt.timedelta(seconds=30) if terminal else None,
            "updated_at": T0 + dt.timedelta(seconds=30)}


def conn_of(item_id: int, subject_key: str = ISSUE, terminal: bool = True) -> FakeConn:
    events = [
        {"item_id": item_id, "item_version": 1, "at": T0, "kind": "admitted",
         "from_state": None, "to_state": "ingest", "outcome": None, "run_id": None},
        {"item_id": item_id, "item_version": 2, "at": T0 + dt.timedelta(seconds=10),
         "kind": "result", "from_state": "ingest", "to_state": "decide",
         "outcome": "ok", "run_id": 7},
    ]
    runs = [{"id": 7, "item_id": item_id, "node": "ingest", "cycle": 1, "attempt": 1,
             "candidate": None, "status": "applied", "fence": 1, "expected_version": 1,
             "lease_expires_at": T0 + dt.timedelta(minutes=20),
             "finished_at": T0 + dt.timedelta(seconds=10), "outcome": "ok",
             "result": {"outcome": "ok", "usage": {"input_tokens": 120,
                                                   "output_tokens": 30}}}]
    questions = [{"id": 5, "item_id": item_id, "node": "escalate",
                  "text": "On garde ?", "options": ["merger", "abandonner"],
                  "owner": "Acid3croco", "deadline": T0 + dt.timedelta(hours=2),
                  "state": "open", "answer": None, "answered_by": None,
                  "answered_at": None}]
    return FakeConn(work_item=[item_row(item_id, subject_key, terminal)],
                    graph_revision=[{"bundle": BUNDLE}], event=events,
                    node_run=runs, effect=[], question=questions)


def open_conn(item_id: int) -> FakeConn:
    """La base vue par `open_questions` : la question, jointe à son item."""
    question = {"id": 5, "item_id": item_id, "node": "escalate",
                "text": "On garde ?", "options": ["merger", "abandonner"],
                "owner": "Acid3croco", "deadline": T0 + dt.timedelta(hours=2),
                "state": "open", "answer": None, "answered_by": None,
                "subject_key": ISSUE, "item_title": "un titre",
                "item_state": "escalate", "item_version": 4, "escalations": 2}
    return FakeConn(question=[question])


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        blocks.DATA_DIR = Path(tmp)  # le seul répertoire de données, celui des blocs
        workspace = blocks.item_workspace(14)
        workspace.mkdir()
        (workspace / "criteria.md").write_text("1. ça marche — preuve : ce test\n")
        (workspace / "release.json").write_text(json.dumps({"pr_url": PR_URL}))

        # 1. la liste : les clés du contrat, et les deux liens
        items = web._api_items(conn_of(14))
        assert len(items) == 1, items
        item = items[0]
        for key in ("id", "subject_key", "title", "state", "status", "issue_url",
                    "pr_url", "updated_at"):
            assert key in item, key
        assert item["state"] == "closed" and item["status"] == "terminal", item
        assert item["issue_url"] == ISSUE_URL and item["pr_url"] == PR_URL, item
        assert item["total_cost_usd"] == 0, item
        print("1. /api/items : id, titre, état, status, issue, PR ✓")

        # tous les runs comptent, y compris les candidats et ceux que le rail
        # n'a pas retenus ; chaque item garde son propre total
        costs = FakeConn(
            work_item=[item_row(14, ISSUE, True),
                       item_row(15, "pipeline-x:sans-coût", False)],
            node_run=[
                {"item_id": 14, "status": "applied", "candidate": None,
                 "result": {"usage": {"total_cost_usd": 0.0123}}},
                {"item_id": 14, "status": "superseded", "candidate": 0,
                 "result": {"usage": {"total_cost_usd": 0.0045}}},
                {"item_id": 14, "status": "faulted", "candidate": 1,
                 "result": {"usage": {"total_cost_usd": "inconnu"}}},
                {"item_id": 15, "status": "applied", "candidate": None,
                 "result": {"usage": {"input_tokens": 9}}},
                {"item_id": 15, "status": "superseded", "candidate": 0,
                 "result": None},
            ],
        )
        totals = {entry["id"]: entry["total_cost_usd"]
                  for entry in web._api_items(costs)}
        assert totals == {14: 0.0168, 15: 0}, totals
        print("   coûts : tous les runs, usages absents ignorés, items séparés ✓")

        # un sujet d'un autre canal n'a ni issue ni PR inventées
        plain = web._api_items(conn_of(99, "pipeline-x:oom", terminal=False))[0]
        assert plain["issue_url"] is None and plain["pr_url"] is None, plain
        assert plain["status"] == "active" and plain["state"] == "decide", plain
        print("2. autre canal, item actif : aucun lien inventé, status actif ✓")

        # 2. l'item entier : les sept clés de la page, et l'item inconnu
        payload = web._api_item(conn_of(14), 14)
        for key in ("item", "graph", "journal", "runs", "questions", "criteria",
                    "files"):
            assert key in payload, key
        assert web._api_item(FakeConn(work_item=[]), 999999) is None
        print("3. /api/item/14 : les sept clés, et l'inconnu rend None ✓")

        # 3. de quoi redessiner sans la base, et de quoi chiffrer le cycle
        graph = payload["graph"]
        assert graph["current"] == "closed", graph
        assert {n["name"] for n in graph["nodes"]} == set(BUNDLE["nodes"]), graph
        assert {"from": "decide", "outcome": "raise", "to": "escalate"} in graph["edges"]
        assert graph["entry"] == "ingest" and graph["on_kernel"], graph
        run = payload["runs"][0]
        assert run["node"] == "ingest" and run["attempt"] == 1, run
        assert run["duration_s"] == 10.0, run  # l'écart des deux transitions
        assert run["usage"] == {"input_tokens": 120, "output_tokens": 30}, run
        assert payload["item"]["usage"]["input_tokens"] == 120, payload["item"]
        assert payload["item"]["duration_s"] == 30.0, payload["item"]
        assert payload["journal"][1]["duration_s"] == 10.0, payload["journal"]
        # un candidat qui a perdu n'a produit aucune transition, donc aucune
        # durée : `finished_at` est la seule date qui le situe
        assert run["candidate"] is None and run["finished_at"] is not None, run
        print("4. graph redessinable, runs chiffrés, journal daté ✓")

        # le fan-out d'un nœud, un candidat par entrée : sans lui, le client
        # ne saurait pas nommer les candidats qu'il déplie
        assert all("fanout" not in n for n in graph["nodes"]), graph["nodes"]
        fanout = web._api_fanout(BUNDLE, FANOUT_NODE)
        assert fanout["reduce"] == "first_pass" and fanout["repeat"] == 2, fanout
        # variantes × répétition : chaque variante deux fois, dans l'ordre
        labels = [c["variant"]["label"] for c in fanout["candidates"]]
        assert labels == ["opus", "opus", "haiku", "haiku"], labels
        # la variante surcharge le modèle, les deux héritent la CLI du graph
        agents = [c["agent"] for c in fanout["candidates"]]
        assert agents[0] == {"cli": "claude", "model": "opus", "effort": "high"}, agents
        assert agents[3] == {"cli": "claude", "model": "haiku", "effort": "high"}, agents
        assert all(c["cmd"] is None for c in fanout["candidates"]), fanout
        assert web._api_fanout(BUNDLE, {"block": "ACT", "config": {}}) is None
        print("   fanout projeté : 4 agents structurés effectifs ✓")

        # L'ancienne révision de l'item 115 ne portait que ces commandes.
        legacy = {"nodes": {}, "agent": {}}
        commands = [
            "CODEX_MODEL=gpt-5.6-luna CODEX_REASONING_EFFORT=medium "
            "CODEX_TIMEOUT_S=1500 bash \"${GRAPHATOM_WORKTREE:-.}/scripts/agent-codex.sh\"",
            "CODEX_MODEL=gpt-5.6-sol CODEX_REASONING_EFFORT=high "
            "CODEX_TIMEOUT_S=1500 bash \"${GRAPHATOM_WORKTREE:-.}/scripts/agent-codex.sh\"",
            "OPENCODE_TIMEOUT_S=1500 bash \"${GRAPHATOM_WORKTREE:-.}/scripts/agent-opencode.sh\" "
            "opencode/deepseek-v4-flash-free",
        ]
        projected = [web._api_candidate(
            legacy, {"config": {"fanout": {"variants": [
                {"agent": {"cmd": command}},
            ]}}}, 0, {},
        )["agent"] for command in commands]
        assert projected[0] == {
            "cli": "codex", "model": "gpt-5.6-luna", "effort": "medium",
            "cmd_uses_executor": True, "cmd": commands[0],
        }, projected[0]
        assert projected[1] == {
            "cli": "codex", "model": "gpt-5.6-sol", "effort": "high",
            "cmd_uses_executor": True, "cmd": commands[1],
        }, projected[1]
        assert projected[2] == {
            "cli": "opencode", "model": "deepseek-v4-flash-free",
            "cmd_uses_executor": True, "cmd": commands[2],
        }, projected[2]
        shell = web._api_candidate(
            legacy, {"config": {"fanout": {"variants": [
                {"agent": {"cmd": "printf 'agent-codex.sh ordinaire'"}},
            ]}}}, 0, {},
        )["agent"]
        assert shell == {"cmd": "printf 'agent-codex.sh ordinaire'"}, shell
        structured = web._api_candidate(
            legacy, {"config": {"fanout": {"variants": [{"agent": {
                "cmd": "bash scripts/agent-codex.sh", "cli": "claude",
                "model": "opus", "effort": "low", "cmd_uses_executor": False,
            }}]}}}, 0, {},
        )["agent"]
        assert structured == {
            "cli": "claude", "model": "opus", "effort": "low",
            "cmd": "bash scripts/agent-codex.sh",
        }, structured
        print("   commandes historiques : métadonnées d'exécuteur restaurées ✓")

        # les fichiers du workspace, avec l'URL qui les sert
        names = {f["name"]: f["href"] for f in payload["files"]}
        assert names["criteria.md"] == "/item/14/file/criteria.md", names
        assert set(names) == {"criteria.md", "release.json"}, names
        assert payload["criteria"].startswith("1. ça marche"), payload["criteria"]
        print("5. files : nom + href, et criteria.md servi tel quel ✓")

        # 4. les questions ouvertes, avec le jeton de POST /answer
        answers = web._api_questions(open_conn(14), token="jeton-secret")
        assert answers["token"] == "jeton-secret", answers
        q = answers["questions"][0]
        assert q["question_id"] == 5 and q["item_id"] == 14, q
        assert q["options"] == ["merger", "abandonner"] and q["text"] == "On garde ?", q
        # la question ouverte porte son item avec elle — le client n'a rien à joindre
        assert q["issue_url"] == ISSUE_URL and q["item_state"] == "escalate", q
        print("6. /api/questions : le jeton sort du HTML, les options avec ✓")

        # 5. le battement, et tout le reste sérialisable tel quel
        beat = web._api_heartbeat(None, None)
        assert set(beat) == {"rail", "github-sync"}, beat
        assert beat["rail"] == {"at": None, "ago_s": None, "stale": True}, beat
        old = web._api_heartbeat(T0, T0)
        assert old["rail"]["stale"] is True, "un vieux battement est à l'arrêt"
        assert old["github-sync"]["stale"] is True, "et celui du canal aussi"
        body = json.dumps(payload, default=web._jsonable, ensure_ascii=False)
        assert T0.isoformat() in body, "les horodatages sortent en ISO 8601"
        assert json.loads(body)["item"]["id"] == 14
        print("7. /api/heartbeat, et le payload sérialisable en ISO 8601 ✓")

        # la charge du rail : les runs en vol, et les plafonds qui les bornent
        charge = web._api_load(CountConn(
            4, builds=1, solo_running=1, solo_waiting=2))
        assert charge == {"running": 4, "max_runs": scheduler.MAX_RUNS,
                           "max_runs_per_item": scheduler.MAX_RUNS_PER_ITEM,
                          "max_active_items": scheduler.MAX_ACTIVE_ITEMS,
                          "solo": {"running": 1, "waiting": 2},
                          "builds": 1, "max_builds": quota.MAX_BUILDS}, charge
        # Le plafond par item reste sous le global dès que la machine a de
        # quoi. Sur une petite machine les deux tombent sur le plancher de
        # `FANOUT_MAX_CANDIDATES` : une course se réserve entière, donc
        # interdire à un item d'atteindre la largeur maximale reviendrait à
        # interdire la course elle-même.
        assert charge["max_runs_per_item"] <= charge["max_runs"], \
            "un item pourrait dépasser la capacité globale"
        print(f"7b. /api/load : {charge['running']} runs et {charge['builds']} "
              f"construction en vol ; plafonds {charge['max_runs']} et "
              f"{charge['max_builds']} ✓")

        # 6. les graphs publiés : la liste des révisions, avec leurs items
        published = [
            {"id": "rev-neuve", "name": "code-task", "published_at": T0, "items": 2},
            {"id": "rev-vieille", "name": "code-task",
             "published_at": T0 - dt.timedelta(days=1), "items": 7},
        ]
        graphs = web._api_graphs(FakeConn(graph_revision=published))
        # la fonction ne retrie rien : l'ordre est celui de la requête — par
        # nom, puis date décroissante, donc la plus récente en tête
        assert [g["id"] for g in graphs] == ["rev-neuve", "rev-vieille"], graphs
        assert graphs[0]["name"] == "code-task" and graphs[0]["items"] == 2, graphs
        assert graphs[0]["published_at"] == T0, graphs
        print("8. /api/graphs : nom, révision, date, items qui la portent ✓")

        # le bundle d'une révision, tel qu'il a été publié — rien de résumé
        revision = FakeConn(graph_revision=[{"id": "rev-neuve", "bundle": BUNDLE,
                                             "published_at": T0, "items": 2}])
        one = web._api_graph_revision(revision, "rev-neuve")
        assert one["revision"] == "rev-neuve" and one["items"] == 2, one
        assert one["nodes"] == BUNDLE["nodes"], one
        assert one["nodes"]["decide"]["config"]["agent"]["prompt"] == "le prompt entier"
        assert one["entry"] == "ingest" and one["on_kernel"], one
        assert web._api_graph_revision(FakeConn(graph_revision=[]), "inconnue") is None
        print("9. /api/graph/<rév> : le bundle entier, l'inconnue rend None ✓")

        # la trace d'un run vient de son identité en base, jamais d'un chemin
        run = {"id": 31, "item_id": 14, "node": "implement", "cycle": 2,
               "attempt": 1, "candidate": None, "status": "running"}
        traces = FakeConn(work_item=[{"id": 14}], node_run=[run])
        (workspace / "codex.jsonl").write_text('{"type":"start"}\n')
        blocks.attempt_log(workspace, run).write_text("début\n")
        blocks.attempt_command(workspace, run).write_text('{"kind":"model"}\n')
        first = web._api_run_trace(traces, 14, 31)
        assert first["events"]["type"] == "codex", first
        assert first["events"]["state"] == "available", first
        assert first["log"]["type"] == "log" and first["command"]["type"] == "command"
        with (workspace / "codex.jsonl").open("a") as stream:
            stream.write('{"type":"done"}\n')
        second = web._api_run_trace(traces, 14, 31, first["cursor"])
        assert second["events"]["content"] == '{"type":"done"}\n', second
        assert second["log"]["content"] == second["command"]["content"] == "", second

        candidate = run | {"id": 32, "candidate": 2, "status": "applied"}
        candidate_workspace = blocks.run_workspace(14, candidate)
        candidate_workspace.mkdir()
        (candidate_workspace / "opencode-events.jsonl").write_text('{"type":"text"}\n')
        blocks._archive(candidate_workspace, blocks.attempt_name(candidate))
        blocks.attempt_command(candidate_workspace, candidate).write_text("")
        terminal = web._api_run_trace(
            FakeConn(work_item=[{"id": 14}], node_run=[candidate]), 14, 32)
        assert terminal["events"]["type"] == "opencode", terminal
        assert terminal["command"]["state"] == "empty", terminal
        assert terminal["log"]["state"] == "missing", terminal
        assert web._api_run_trace(FakeConn(work_item=[]), 14, 31) is None
        assert web._api_run_trace(FakeConn(work_item=[{"id": 14}], node_run=[]), 14, 999) is None
        other = run | {"item_id": 15}
        assert web._api_run_trace(
            FakeConn(work_item=[{"id": 14}], node_run=[other]), 14, 31) is None
        print("10. /api/item/<item>/run/<run>/trace : identité, sources et curseur ✓")

    print("\napi : OK — les pages se lisent en JSON, sans dépendance ni écriture")


if __name__ == "__main__":
    main()
