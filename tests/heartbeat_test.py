"""Le test du battement : l'absence de signal devient un signal.

Scénario, sans base ni réseau — la base et GitHub sont des doublures :

  1. le worker tamponne une ligne unique, en UPSERT : plusieurs workers
     écrivent le même battement, et lire ne coûte qu'une ligne
  2. le battement vieillit — vivant, puis à l'arrêt ; jamais battu compte
     comme à l'arrêt
  3. le frontend le dit dans l'en-tête commun : « rail vivant il y a 3 s »,
     ou le bandeau qui prévient que les états affichés sont figés
  4. le canal GitHub pose `rail:stalled` à côté du label d'état des items
     actifs, et le retire au retour du battement — le label d'état, lui,
     ne bouge pas

Usage : uv run python tests/heartbeat_test.py
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import github_sync as gs  # noqa: E402
from graphatom import heartbeat, web  # noqa: E402
from graphatom.kernel import now  # noqa: E402


class FakeCursor:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    """La base : elle retient le SQL, et rend les items actifs qu'on lui donne."""

    def __init__(self, at: dt.datetime | None = None, items: list[dict] | None = None):
        self.at, self.items, self.sql = at, items or [], []

    def execute(self, sql: str, params: tuple = ()):
        self.sql.append(sql)
        if "FROM heartbeat" in sql:
            return FakeCursor([{"at": self.at}] if self.at else [])
        if "FROM work_item w" in sql:
            return FakeCursor(self.items)
        return FakeCursor([])


class FakeGitHub:
    repo = "o/r"

    def __init__(self, issues: list[dict]):
        self.issues = issues
        self.labels: list[tuple[str, int, str]] = []

    def labeled_issues(self) -> list[dict]:
        return self.issues

    def create_label(self, name: str) -> None:
        pass

    def add_label(self, number: int, name: str) -> None:
        self.labels.append(("+", number, name))

    def remove_label(self, number: int, name: str) -> None:
        self.labels.append(("-", number, name))


class RecordingGitHub(gs.GitHub):
    """Le vrai client, sans réseau : on regarde ce qu'il aurait envoyé."""

    def __init__(self):
        super().__init__("o/r", "jeton")
        self.calls: list[tuple[str, str, dict | None]] = []

    def _call(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path, body))
        return None


def main() -> None:
    # 1. tamponner : une ligne, un UPSERT, et une lecture d'une ligne
    conn = FakeConn()
    heartbeat.beat(conn)
    stamp = conn.sql[0]
    assert "INSERT INTO heartbeat" in stamp and "VALUES (1, now())" in stamp, stamp
    assert "ON CONFLICT (id) DO UPDATE" in stamp, stamp
    heartbeat.beat(conn)  # deuxième worker, même ligne : rien d'autre n'est écrit
    assert conn.sql[1] == stamp, conn.sql
    assert heartbeat.last(conn) is None, "base sans battement"
    assert "WHERE id = 1" in conn.sql[-1], conn.sql[-1]
    beaten = now()
    assert heartbeat.last(FakeConn(at=beaten)) == beaten
    print("1. un UPSERT sur la ligne unique, une lecture d'une ligne ✓")

    # 2. l'âge décide, et lui seul
    fresh = now() - dt.timedelta(seconds=3)
    old = now() - dt.timedelta(seconds=heartbeat.STALE_S + 1)
    assert round(heartbeat.age_s(fresh)) == 3, heartbeat.age_s(fresh)
    assert heartbeat.age_s(None) is None
    assert not heartbeat.stalled(fresh)
    assert heartbeat.stalled(old) and heartbeat.stalled(None)
    print("2. vivant à 3 s, à l'arrêt à 2 min, à l'arrêt sans battement ✓")

    # 3. l'en-tête commun du frontend
    assert web._ago(3) == "3 s" and web._ago(200) == "3 min"
    vivant = web._beat(fresh)
    assert "class='beat'" in vivant and "rail vivant il y a 3 s" in vivant, vivant
    arret = web._beat(old)
    assert "class='stalled'" in arret and "les états affichés sont figés" in arret, arret
    assert f"rail à l'arrêt depuis {old:%H:%M}" in arret, arret
    jamais = web._beat(None)
    assert "class='stalled'" in jamais and "jamais tamponné" in jamais, jamais
    print("3. en-tête : rail vivant, ou bandeau — les états sont figés ✓")

    # 4. le label posé par le sync, sur l'issue d'un item actif
    items = [{"id": 1, "subject_key": "gh:o/r#7", "state": "implement", "graph": "code-task"}]
    issues = [{"number": 7, "labels": [{"name": "rail:implement"}]},
              {"number": 8, "labels": []}]   # #8 : admission différée, pas d'item

    gh = FakeGitHub(issues)
    gs._paint_states(FakeConn(items=items), gh, blocked={8}, stalled=True)
    assert gh.labels == [("+", 7, gs.STALLED), ("+", 8, gs.BLOCKED)], gh.labels

    gh = FakeGitHub(issues)   # le worker ne bat toujours pas : rien à repeindre
    issues[0]["labels"].append({"name": gs.STALLED})
    issues[1]["labels"].append({"name": gs.BLOCKED})
    gs._paint_states(FakeConn(items=items), gh, blocked={8}, stalled=True)
    assert gh.labels == [], gh.labels

    gh = FakeGitHub(issues)   # le battement revient : seul `rail:stalled` part
    gs._paint_states(FakeConn(items=items), gh, blocked={8}, stalled=False)
    assert gh.labels == [("-", 7, gs.STALLED)], gh.labels
    print("4. `rail:stalled` posé à côté de l'état, retiré au retour du battement ✓")

    # le rouge de l'alarme : la seule exception à la couleur unie des labels
    gh = RecordingGitHub()
    gh.create_label(gs.STALLED)
    gh.create_label("rail:implement")
    assert [body["color"] for _, _, body in gh.calls] == [gs.STALLED_COLOR, gs.RAIL_COLOR]

    print("\nbattement : OK — le silence du worker se voit sur les deux surfaces")


if __name__ == "__main__":
    main()
