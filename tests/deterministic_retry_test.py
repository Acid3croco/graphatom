"""Une panne identique sans progrès escalade après sa seconde occurrence.

Usage : uv run python tests/deterministic_retry_test.py
"""

import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, graph, kernel, web  # noqa: E402

from outils import etat  # noqa: E402


def bundle(fanout: bool = False) -> dict:
    """Un travail relançable et son escalade, avec fan-out facultatif."""
    config = {"agent": {"cmd": "exit 3", "prompt": "test"}}
    if fanout:
        config["fanout"] = {
            "variants": [{"label": "a"}, {"label": "b"}],
            "reduce": "first_pass",
        }
    return {
        "name": f"retry-identique-{uuid.uuid4().hex[:8]}",
        "entry": "travail",
        "budgets": {"escalations": 3, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {"block": "ACT", "config": config,
                        "edges": {"ok": "fini"}},
            "escalate": {"block": "WAIT", "escalade": True,
                         "config": {"question": "On retente ?",
                                    "options": ["retry"], "owner": "test",
                                    "deadline_minutes": 60},
                         "edges": {"retry": "travail", "expired": "abandon"}},
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


def command(run: dict, value: str = "exit 3") -> None:
    """Pose la commande résolue que le vrai bloc aurait archivée."""
    workspace = blocks.run_workspace(run["item_id"], run)
    workspace.mkdir(parents=True, exist_ok=True)
    blocks.attempt_command(workspace, run).write_text(json.dumps({
        "kind": "shell", "executor": None, "command": value,
    }))


def crash(conn, run: dict, command_value: str = "exit 3") -> None:
    command(run, command_value)
    kernel.apply(conn, run["id"], {
        "outcome": "crashed", "error": "ValueError: passation absente",
        "timeout": False, "exit_code": 3,
        "log_tail": f"texte humain du run {run['id']}",
    })


def item(conn, revision: str) -> int:
    return kernel.admit(conn, revision, f"test:{uuid.uuid4().hex}",
                        _allow_parallel_for_test=True)


def identique(conn, revision: str) -> None:
    """Deux erreurs identiques escaladent et nomment les deux runs."""
    item_id = item(conn, revision)
    first = kernel.claim(conn, item_id)
    crash(conn, first)
    second = kernel.claim(conn, item_id)
    crash(conn, second)

    assert etat(conn, item_id)["state"] == "escalate"
    assert kernel.claim(conn, item_id) is None, "une troisième tentative existe"
    runs = conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s ORDER BY id", (item_id,)
    ).fetchall()
    assert len(runs) == 2, runs
    message = runs[-1]["result"]["retry"]
    assert message == (f"relance identique supprimée — runs {first['id']} "
                       f"et {second['id']}")
    failure = json.loads(blocks.failure_path(item_id).read_text())
    assert failure["retry"] == message
    assert message in web._result(runs[-1]["result"]), "trace UI absente"
    assert web._api_item(conn, item_id)["runs"][-1]["result"]["retry"] == message
    print("1. deux erreurs identiques : escalade sans troisième run, trace API/UI ✓")


def changement(conn, revision: str) -> None:
    """Une commande ou un fichier durable différent conserve la relance."""
    for mode in ("commande", "workspace"):
        item_id = item(conn, revision)
        first = kernel.claim(conn, item_id)
        workspace = blocks.run_workspace(item_id, first)
        workspace.mkdir(parents=True, exist_ok=True)
        if mode == "workspace":
            (workspace / "travail.txt").write_text("avant")
        crash(conn, first)
        second = kernel.claim(conn, item_id)
        if mode == "workspace":
            (workspace / "travail.txt").write_text("après")
        crash(conn, second, "exit 4" if mode == "commande" else "exit 3")
        assert etat(conn, item_id)["state"] == "travail", mode
        assert kernel.claim(conn, item_id)["attempt"] == 3, mode

    item_id = item(conn, revision)
    too_large = "x" * (blocks.SIGNATURE_BYTES + 1)
    crash(conn, kernel.claim(conn, item_id), too_large)
    crash(conn, kernel.claim(conn, item_id), too_large)
    assert etat(conn, item_id)["state"] == "travail"
    print("2. commande ou workspace différent, ou borne dépassée : relance conservée ✓")


def transitoire(conn, revision: str) -> None:
    """Une panne corrigée réussit à la deuxième tentative."""
    item_id = item(conn, revision)
    first = kernel.claim(conn, item_id)
    crash(conn, first)
    second = kernel.claim(conn, item_id)
    command(second)
    kernel.apply(conn, second["id"], {"outcome": "ok", "summary": "corrigé"})
    assert etat(conn, item_id)["state"] == "fini"
    print("3. panne transitoire : succès à la deuxième tentative ✓")


def reduction(conn, revision: str) -> None:
    """Le même mécanisme tranche le verdict réduit d'un fan-out."""
    item_id = item(conn, revision)
    attempts = []
    for expected in (1, 2):
        batch = [kernel.claim(conn, item_id), kernel.claim(conn, item_id)]
        assert {run["attempt"] for run in batch} == {expected}
        for run in batch:
            crash(conn, run)
        attempts.extend(batch)
    assert etat(conn, item_id)["state"] == "escalate"
    assert kernel.claim(conn, item_id) is None
    message = conn.execute(
        "SELECT result FROM node_run WHERE id = %s", (attempts[-2]["id"],)
    ).fetchone()["result"]["retry"]
    assert str(attempts[0]["id"]) in message and str(attempts[-2]["id"]) in message
    print("4. fan-out : seconde réduction identique escaladée sans troisième lot ✓")


def main() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="graphatom-retry-identique-"))
    blocks.DATA_DIR = workspace
    db.init_db()
    try:
        with db.connect() as conn:
            revision = graph.publish(conn, bundle())
            fanout_revision = graph.publish(conn, bundle(fanout=True))
            identique(conn, revision)
            changement(conn, revision)
            transitoire(conn, revision)
            reduction(conn, fanout_revision)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
