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
    config = {"execution": {"kind": "agent", "cmd": "exit 3"},
              "agent": {"prompt": "test", "cli": "codex"}}
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
        "kind": "composed",
        "executor": {"cli": "codex", "model": None, "effort": None},
        "command": value,
    }))


def crash(conn, run: dict, command_value: str = "exit 3",
          human: str | None = None, provider_traces: bool = False) -> None:
    command(run, command_value)
    workspace = blocks.run_workspace(run["item_id"], run)
    if human is not None:
        (workspace / f"{run['node']}.md").write_text(human)
        blocks.passation_path(run["item_id"], run).write_text(f"passation {human}")
    if provider_traces:
        name = blocks.attempt_name(run)
        (workspace / f"codex-{name}.jsonl").write_text('{"event":"same"}\n')
        (workspace / f"opencode-events-{name}.jsonl").write_text(
            '{"event":"same"}\n')
        (workspace / "codex-errors.log").write_text(f"stderr codex {run['id']}\n")
        (workspace / "opencode-errors.log").write_text(
            f"stderr opencode {run['id']}\n")
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
    crash(conn, first, human="première formulation", provider_traces=True)
    second = kernel.claim(conn, item_id)
    crash(conn, second, human="autre formulation", provider_traces=True)

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

    item_id = item(conn, revision)
    first = kernel.claim(conn, item_id)
    workspace = blocks.run_workspace(item_id, first)
    workspace.mkdir(parents=True, exist_ok=True)
    for index in range(blocks.SIGNATURE_ENTRIES + 1):
        (workspace / f"agent-ignore-{index}.log").write_text("")
    crash(conn, first)
    crash(conn, kernel.claim(conn, item_id))
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


def reduction_avec_progres(conn, revision: str) -> None:
    """Le progrès d'un seul candidat conserve la relance du lot entier."""
    item_id = item(conn, revision)
    first = [kernel.claim(conn, item_id), kernel.claim(conn, item_id)]
    for run in first:
        crash(conn, run)
    second = [kernel.claim(conn, item_id), kernel.claim(conn, item_id)]
    crash(conn, second[0])
    crash(conn, second[1], command_value="exit 4")
    assert etat(conn, item_id)["state"] == "travail"
    third = [kernel.claim(conn, item_id), kernel.claim(conn, item_id)]
    assert {run["attempt"] for run in third} == {3}
    kernel.apply(conn, third[0]["id"], {"outcome": "ok"})
    print("5. fan-out : progrès d'un candidat, troisième lot conservé ✓")


def limites(conn, revision: str) -> None:
    """Un autre item et un autre cycle ne peuvent fournir le précédent."""
    first_item = item(conn, revision)
    crash(conn, kernel.claim(conn, first_item))

    other_item = item(conn, revision)
    first = kernel.claim(conn, other_item)
    crash(conn, first)
    other_second = kernel.claim(conn, other_item)
    assert other_second["attempt"] == 2
    kernel.apply(conn, other_second["id"], {"outcome": "ok"})

    second = kernel.claim(conn, first_item)
    crash(conn, second)
    assert etat(conn, first_item)["state"] == "escalate"
    kernel.apply_item(conn, first_item, "retry", kind="answer")
    next_cycle = kernel.claim(conn, first_item)
    assert next_cycle["cycle"] == 2 and next_cycle["attempt"] == 1
    crash(conn, next_cycle)
    cycle_second = kernel.claim(conn, first_item)
    assert cycle_second["attempt"] == 2
    kernel.apply(conn, cycle_second["id"], {"outcome": "ok"})
    print("6. limites : aucun run d'un autre item ou cycle n'est comparé ✓")


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
            reduction_avec_progres(conn, fanout_revision)
            limites(conn, revision)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
