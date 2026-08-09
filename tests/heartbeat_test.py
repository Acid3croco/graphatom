"""Le test des battements : l'absence de signal devient un signal.

Scénario, sans base ni réseau — la base et GitHub sont des doublures :

  1. chaque processus tamponne sa ligne, en UPSERT sur son identité : le
     worker sous `rail`, le canal GitHub sous `github-sync` ; plusieurs
     workers écrivent le même battement, et lire ne coûte qu'une ligne
  2. le battement vieillit — vivant, puis à l'arrêt ; jamais battu compte
     comme à l'arrêt
  3. `/api/heartbeat` rend les deux battements, chacun avec son
     horodatage, son âge en secondes et son état périmé
  4. le frontend le dit dans l'en-tête commun : « rail vivant il y a 3 s »,
     ou le bandeau qui prévient que les états affichés sont figés
  5. le canal GitHub pose `rail:stalled` à côté du label d'état des items
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

    def __init__(self, beats: dict[str, dt.datetime] | None = None,
                 items: list[dict] | None = None):
        self.beats, self.items = beats or {}, items or []
        self.sql: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()):
        self.sql.append((sql, params))
        if "FROM heartbeat" in sql:
            at = self.beats.get(params[0])
            return FakeCursor([{"at": at}] if at else [])
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
    # 1. tamponner : une ligne par batteur, un UPSERT sur son identité
    conn = FakeConn()
    heartbeat.beat(conn, heartbeat.RAIL)
    stamp, who = conn.sql[0]
    assert "INSERT INTO heartbeat (who, at) VALUES (%s, now())" in stamp, stamp
    assert "ON CONFLICT (who) DO UPDATE" in stamp, stamp
    assert who == (heartbeat.RAIL,), who
    heartbeat.beat(conn, heartbeat.RAIL)  # deuxième worker, même ligne
    assert conn.sql[1] == (stamp, (heartbeat.RAIL,)), conn.sql
    heartbeat.beat(conn, heartbeat.GITHUB_SYNC)  # le canal : sa ligne à lui
    assert conn.sql[2] == (stamp, (heartbeat.GITHUB_SYNC,)), conn.sql
    assert heartbeat.last(conn, heartbeat.RAIL) is None, "base sans battement"
    assert "WHERE who = %s" in conn.sql[-1][0], conn.sql[-1]
    beaten = now()
    conn = FakeConn(beats={heartbeat.RAIL: beaten})
    assert heartbeat.last(conn, heartbeat.RAIL) == beaten
    assert heartbeat.last(conn, heartbeat.GITHUB_SYNC) is None, "le canal n'a pas battu"
    print("1. un UPSERT par batteur, une lecture d'une ligne ✓")

    # 2. l'âge décide, et lui seul
    fresh = now() - dt.timedelta(seconds=3)
    old = now() - dt.timedelta(seconds=heartbeat.STALE_S + 1)
    assert round(heartbeat.age_s(fresh)) == 3, heartbeat.age_s(fresh)
    assert heartbeat.age_s(None) is None
    assert not heartbeat.stalled(fresh)
    assert heartbeat.stalled(old) and heartbeat.stalled(None)
    print("2. vivant à 3 s, à l'arrêt à 2 min, à l'arrêt sans battement ✓")

    # 3. la route : un objet par batteur, trois champs chacun
    payload = web._api_heartbeat(fresh, old)
    assert set(payload) == {heartbeat.RAIL, heartbeat.GITHUB_SYNC}, payload
    rail, sync = payload[heartbeat.RAIL], payload[heartbeat.GITHUB_SYNC]
    assert rail["at"] == fresh and round(rail["ago_s"]) == 3 and not rail["stale"], rail
    assert sync["at"] == old and sync["stale"] is True, sync
    jamais_battu = {"at": None, "ago_s": None, "stale": True}
    assert web._api_heartbeat(None, None)[heartbeat.GITHUB_SYNC] == jamais_battu
    print("3. /api/heartbeat : les deux battements, horodatage, âge, périmé ✓")

    # 4. l'en-tête commun du frontend
    assert web._ago(3) == "3 s" and web._ago(200) == "3 min"
    vivant = web._beat(fresh)
    assert "class='beat'" in vivant and "rail vivant il y a 3 s" in vivant, vivant
    arret = web._beat(old)
    assert "class='stalled'" in arret and "les états affichés sont figés" in arret, arret
    assert f"rail à l'arrêt depuis {old:%H:%M}" in arret, arret
    jamais = web._beat(None)
    assert "class='stalled'" in jamais and "jamais tamponné" in jamais, jamais
    print("4. en-tête : rail vivant, ou bandeau — les états sont figés ✓")

    # 5. le label posé par le sync, sur l'issue d'un item actif
    items = [{"id": 1, "subject_key": "gh:o/r#7", "state": "implement", "graph": "code-task"}]
    issues = [{"number": 7, "labels": [{"name": "rail:implement"}]},
              {"number": 8, "labels": []}]   # #8 : admission différée, pas d'item

    gh = FakeGitHub(issues)
    gs._paint_states(FakeConn(items=items), gh, blocked={8}, queued=set(),
                     stalled=True)
    assert gh.labels == [("+", 7, gs.STALLED), ("+", 8, gs.BLOCKED)], gh.labels

    gh = FakeGitHub(issues)   # le worker ne bat toujours pas : rien à repeindre
    issues[0]["labels"].append({"name": gs.STALLED})
    issues[1]["labels"].append({"name": gs.BLOCKED})
    gs._paint_states(FakeConn(items=items), gh, blocked={8}, queued=set(),
                     stalled=True)
    assert gh.labels == [], gh.labels

    gh = FakeGitHub(issues)   # le battement revient : seul `rail:stalled` part
    gs._paint_states(FakeConn(items=items), gh, blocked={8}, queued=set(),
                     stalled=False)
    assert gh.labels == [("-", 7, gs.STALLED)], gh.labels
    print("5. `rail:stalled` posé à côté de l'état, retiré au retour du battement ✓")

    # Deux processus de sync peuvent voir la voie libre. Le perdant reçoit
    # « voie occupée » après l'admission du gagnant et classe brièvement le
    # même numéro en file. La vérité active de la base doit gagner.
    issues = [{"number": 7, "labels": [{"name": gs.QUEUED}]}]
    gh = FakeGitHub(issues)
    gs._paint_states(FakeConn(items=items), gh, blocked=set(), queued={7},
                     stalled=False)
    assert gh.labels == [("+", 7, "rail:implement"),
                         ("-", 7, gs.QUEUED)], gh.labels
    print("6. course de deux syncs : l'état actif gagne sur `rail:queued` ✓")

    # le rouge de l'alarme : la seule exception à la couleur unie des labels
    gh = RecordingGitHub()
    gh.create_label(gs.STALLED)
    gh.create_label("rail:implement")
    assert [body["color"] for _, _, body in gh.calls] == [gs.STALLED_COLOR, gs.RAIL_COLOR]

    print("\nbattement : OK — le silence de chaque processus se voit")


if __name__ == "__main__":
    main()
