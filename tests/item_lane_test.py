"""La voie des items : une issue entière, sa review, puis la suivante.

Le canal GitHub est la file. Deux issues labellisées existent, mais une seule
devient un item. Son fan-out interne reste l'affaire des tests de fan-out ;
ici on prouve la frontière entre deux instances complètes :

  1. #1 est admise, #2 reste `rail:queued`, sans item ni deadline
  2. #2 ne part pas pendant le travail de #1
  3. #2 reste hors base pendant la review humaine de #1
  4. après `merger` et le terminal de #1, #2 est admise et part
  5. deux items hérités en base restent strictement ordonnés, y compris WAIT
  6. deux admissions simultanées sur une voie libre ne créent qu'un item

Usage : uv run python tests/item_lane_test.py
"""

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, channel, db, graph, github_sync, kernel, scheduler  # noqa: E402

INSTANCE = os.environ.get("GRAPHATOM_AGENT_DSN") or db.DSN
os.environ["GRAPHATOM_AGENT_DSN"] = INSTANCE
ITEM_ID = 960000 + os.getpid() % 10000


def bundle() -> dict:
    return {
        "name": "item-lane",
        "entry": "travail",
        "budgets": {"escalations": 1, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {
                "block": "ACT",
                "config": {"duration_s": 0, "lease_s": 600},
                "edges": {"done": "review"},
            },
            "review": {
                "block": "WAIT",
                "config": {"question": "On continue ?", "options": ["merger"],
                           "owner": "test", "deadline_minutes": 60},
                "edges": {"merger": "close", "expired": "abandon"},
            },
            "close": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


class FakeGitHub:
    repo = "o/r"

    def __init__(self):
        self.issues = [
            {"number": 1, "title": "première", "body": "", "labels": []},
            {"number": 2, "title": "seconde", "body": "", "labels": []},
        ]
        self.labels: list[tuple[str, int, str]] = []

    def labeled_issues(self) -> list[dict]:
        return self.issues

    def comments(self, _number: int) -> list[dict]:
        return []

    def issue(self, _number: int) -> dict | None:
        return None

    def post_comment(self, _number: int, _body: str) -> None:
        raise AssertionError("aucun commentaire attendu")

    def create_label(self, _name: str) -> None:
        pass

    def add_label(self, number: int, name: str) -> None:
        self.labels.append(("+", number, name))
        next(i for i in self.issues if i["number"] == number)["labels"].append(
            {"name": name}
        )

    def remove_label(self, number: int, name: str) -> None:
        self.labels.append(("-", number, name))
        issue = next(i for i in self.issues if i["number"] == number)
        issue["labels"] = [label for label in issue["labels"] if label["name"] != name]


def runs(conn, item_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s ORDER BY id", (item_id,)
    ).fetchall()


def item_for(conn, number: int) -> dict | None:
    return conn.execute(
        "SELECT w.* FROM work_item w JOIN subject s ON s.id = w.subject_id "
        "WHERE s.subject_key = %s ORDER BY w.id DESC LIMIT 1", (f"gh:o/r#{number}",)
    ).fetchone()


def main() -> None:
    temporary = Path(tempfile.mkdtemp(prefix="graphatom-item-lane-"))
    blocks.DATA_DIR = temporary / "data"
    os.environ["GRAPHATOM_REPO_DIR"] = str(temporary / "sans-depot")
    saved = (db.DSN, scheduler._execute)
    dsn = db.agent_dsn(ITEM_ID)
    assert dsn, "aucune instance jetable : impossible de tester la voie"
    db.DSN = dsn
    scheduler._execute = lambda _run_id, _item_id: None
    try:
        db.init_db()
        with db.connect() as conn:
            revision = graph.publish(conn, bundle())
            gh = FakeGitHub()

            blocked, queued = github_sync._admit_labeled(conn, gh, revision, set())
            first = item_for(conn, 1)
            assert first and item_for(conn, 2) is None
            assert blocked == set() and queued == {2}, (blocked, queued)
            github_sync._paint_states(conn, gh, blocked, queued, stalled=False)
            assert ("+", 2, github_sync.QUEUED) in gh.labels, gh.labels
            print("1. #1 admise ; #2 `rail:queued`, sans item ni deadline ✓")

            scheduler.tick(conn)
            work = runs(conn, first["id"])
            assert len(work) == 1 and work[0]["node"] == "travail", work
            assert item_for(conn, 2) is None
            kernel.apply(conn, work[0]["id"], {"outcome": "done"})
            print("2. #1 travaille seule ; #2 n'a toujours aucun run ✓")

            scheduler.tick(conn)
            current = item_for(conn, 1)
            question = conn.execute(
                "SELECT * FROM question WHERE item_id = %s AND state = 'open'",
                (first["id"],),
            ).fetchone()
            assert current["state"] == "review" and question["node"] == "review"
            blocked, queued = github_sync._admit_labeled(conn, gh, revision, set())
            assert queued == {2} and item_for(conn, 2) is None
            print("3. #1 en review humaine ; #2 reste hors de la base ✓")

            assert channel.record_answer(
                conn, question["id"], "merger", "test"
            ) is None
            scheduler.tick(conn)
            assert item_for(conn, 1)["terminal_at"] is not None
            blocked, queued = github_sync._admit_labeled(conn, gh, revision, set())
            second = item_for(conn, 2)
            assert second and blocked == queued == set(), (blocked, queued)
            github_sync._paint_states(conn, gh, blocked, queued, stalled=False)
            assert ("-", 2, github_sync.QUEUED) in gh.labels, gh.labels

            scheduler.tick(conn)
            second_runs = runs(conn, second["id"])
            assert len(second_runs) == 1 and second_runs[0]["node"] == "travail"
            print("4. #1 approuvée et terminale ; #2 est admise, puis part ✓")

            # Libère la voie par le chemin normal avant la course d'admission.
            kernel.apply(conn, second_runs[0]["id"], {"outcome": "done"})
            second_question = conn.execute(
                "SELECT * FROM question WHERE item_id = %s AND state = 'open'",
                (second["id"],),
            ).fetchone()
            assert channel.record_answer(
                conn, second_question["id"], "merger", "test"
            ) is None
            scheduler.tick(conn)

            # Simule l'état hérité d'une ancienne version du rail : deux
            # items non terminaux existent déjà. Le plus jeune est pourtant
            # runnable ; seul le plus vieux peut partir.
            oldest = kernel.admit(conn, revision, "legacy:oldest")
            younger = kernel.admit(
                conn, revision, "legacy:younger", _allow_parallel_for_test=True
            )
            scheduler.tick(conn)
            oldest_run = runs(conn, oldest)
            assert len(oldest_run) == 1 and runs(conn, younger) == []
            kernel.apply(conn, oldest_run[0]["id"], {"outcome": "done"})
            oldest_question = conn.execute(
                "SELECT * FROM question WHERE item_id = %s AND state = 'open'",
                (oldest,),
            ).fetchone()
            scheduler.tick(conn)
            assert runs(conn, younger) == [], runs(conn, younger)
            channel.record_answer(conn, oldest_question["id"], "merger", "test")
            scheduler.tick(conn)
            younger_run = runs(conn, younger)
            assert len(younger_run) == 1, younger_run

            # Une réponse déjà présente sur un troisième item hérité ne doit
            # pas passer devant la review du deuxième item, qui tient la tête.
            kernel.apply(conn, younger_run[0]["id"], {"outcome": "done"})
            younger_question = conn.execute(
                "SELECT * FROM question WHERE item_id = %s AND state = 'open'",
                (younger,),
            ).fetchone()
            third = kernel.admit(
                conn, revision, "legacy:third", _allow_parallel_for_test=True
            )
            conn.execute("UPDATE work_item SET state = 'review' WHERE id = %s", (third,))
            third_question = conn.execute(
                "INSERT INTO question "
                "(item_id, node, text, options, owner, deadline, state, answer) "
                "VALUES (%s, 'review', 'héritée', '[\"merger\"]', 'test', "
                "now() + interval '1 hour', 'answered', 'merger') RETURNING id",
                (third,),
            ).fetchone()
            scheduler.tick(conn)
            assert conn.execute(
                "SELECT state FROM question WHERE id = %s", (third_question["id"],)
            ).fetchone()["state"] == "answered"
            channel.record_answer(conn, younger_question["id"], "merger", "test")
            scheduler.tick(conn)
            assert item_for(conn, 2)["terminal_at"] is not None
            assert conn.execute(
                "SELECT terminal_at FROM work_item WHERE id = %s", (younger,)
            ).fetchone()["terminal_at"] is not None
            assert conn.execute(
                "SELECT terminal_at FROM work_item WHERE id = %s", (third,)
            ).fetchone()["terminal_at"] is None
            scheduler.tick(conn)
            assert conn.execute(
                "SELECT terminal_at FROM work_item WHERE id = %s", (third,)
            ).fetchone()["terminal_at"] is not None
            print("5. items hérités : run, review et réponse restent en ordre strict ✓")

            barrier = threading.Barrier(3)
            results: list[int | str] = []
            result_lock = threading.Lock()

            def concurrent_admit(number: int) -> None:
                with db.connect() as other:
                    barrier.wait()
                    try:
                        result: int | str = kernel.admit(
                            other, revision, f"race:{number}"
                        )
                    except kernel.LaneOccupied as exc:
                        result = str(exc)
                with result_lock:
                    results.append(result)

            racers = [
                threading.Thread(target=concurrent_admit, args=(number,))
                for number in (1, 2)
            ]
            for racer in racers:
                racer.start()
            barrier.wait()
            for racer in racers:
                racer.join()

            admitted = [result for result in results if isinstance(result, int)]
            rejected = [result for result in results
                        if isinstance(result, str) and "voie occupée" in result]
            assert len(admitted) == len(rejected) == 1, results
            active = conn.execute(
                "SELECT count(*) AS n FROM work_item WHERE terminal_at IS NULL"
            ).fetchone()["n"]
            assert active == 1, (active, results)
            print("6. deux admissions simultanées → un item, un refus explicite ✓")
    finally:
        db.drop_agent_db()
        db.DSN, scheduler._execute = saved
        shutil.rmtree(temporary, ignore_errors=True)

    print("\nvoie des items : OK — une instance complète, sa review, puis la suivante")


if __name__ == "__main__":
    main()
