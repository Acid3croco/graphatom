"""Le crash-test : tuer l'ordonnanceur est un cas nominal.

Scénario : lancer trois agents avec un bail long, tuer l'ordonnanceur et
leurs groupes, puis relancer le worker sans attendre le bail :

  1. les trois runs orphelins sont classés en quelques ticks
  2. l'item atteint un état terminal
  3. le journal est une chaîne contiguë v1..vN, sans trou ni doublon
  4. l'effet est exécuté exactement une fois

Usage : uv run python tests/crash_test.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import kill_group, provision_postgres, sh  # noqa: E402

# une base à lui, détruite avec lui : jamais celle que GRAPHATOM_DSN désigne
os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-crash")

from graphatom import blocks, db, kernel  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# le test travaille dans un répertoire à lui : jamais dans ROOT/data, qui
# peut être le data/ vivant d'un rail (workspaces d'items en cours)
WORK = Path(tempfile.mkdtemp(prefix="graphatom-crash-test-"))
# hermétisme : ce dépôt-ci est le seul univers du test. L'agent qui nous
# lance a dans son environnement le clone et l'instance de la production —
# un ordonnanceur lancé d'ici n'en voit ni l'un ni l'autre.
os.environ["GRAPHATOM_REPO_DIR"] = str(ROOT)
os.environ.pop("GRAPHATOM_AGENT_DSN", None)
blocks.DATA_DIR = WORK / "data"
LEASE_S = 600
REAP_S = 5.0
TIMEOUT_S = 90

AGENT = """
if [ ! -f ../repris ]; then sleep 300; fi
printf '%s\n' '## Fait' 'Reprise terminée.' '' '## Appris' 'Rien.' '' \
    '## Pas fait' 'Rien.' > passation-produce.md
printf '{"outcome":"ok","summary":"repris"}' > outcome.json
"""


def bundle() -> dict:
    """Trois agents, puis un effet unique : la plus petite preuve du contrat."""
    return {
        "name": "crash-test",
        "entry": "produce",
        "budgets": {"escalations": 2, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
        "nodes": {
            "produce": {
                "block": "ACT",
                "config": {
                    "lease_s": LEASE_S,
                    "execution": {"kind": "agent", "cmd": AGENT,
                                  "silence_s": LEASE_S},
                    "agent": {"prompt": "Travaille.", "cli": "codex"},
                    "fanout": {"variants": [{"label": f"c{i}"} for i in range(3)],
                               "reduce": "first_pass"},
                },
                "edges": {"ok": "commit"},
            },
            "commit": {
                "block": "EFFECT",
                "config": {"intent": "open_change", "target": "stub://outbox"},
                "edges": {"applied": "close", "uncertain": "abandon"},
            },
            "close": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


def scheduler() -> subprocess.Popen:
    # binaire du venv en direct + groupe de processus dédié : les signaux
    # atteignent le vrai ordonnanceur, pas un wrapper qui laisse un orphelin
    return subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "graphatom"), "run"],
        cwd=WORK, start_new_session=True,
    )


def main() -> None:
    sh("init-db", "--drop")
    spec = WORK / "crash-test.json"
    spec.write_text(json.dumps(bundle()))
    rev = sh("publish", str(spec))
    item_id = int(sh("admit", rev, "pipeline-x:oom"))
    print(f"révision {rev[:12]}…, item {item_id}")

    # phase 1 : les trois agents volent sous un bail long
    proc = scheduler()
    workspace = WORK / "data" / f"item-{item_id}"
    deadline = time.time() + 15
    while time.time() < deadline:
        traces = sorted(workspace.glob("c*/agent.pgid"))
        if len(traces) == 3:
            break
        time.sleep(0.1)
    else:
        sys.exit("ÉCHEC : les trois agents n'ont pas démarré")

    with db.connect() as conn:
        anciens = conn.execute(
            "SELECT id, lease_expires_at, "
            "EXTRACT(epoch FROM lease_expires_at - now()) AS reste FROM node_run "
            "WHERE item_id = %s "
            "AND status = 'running' ORDER BY id", (item_id,),
        ).fetchall()
    assert len(anciens) == 3, anciens
    assert min(float(run["reste"]) for run in anciens) > LEASE_S - 20, anciens

    kill_group(proc, signal.SIGKILL)
    with db.connect() as conn:
        assert kernel.reap(conn) == 0, "un groupe vivant a été pris pour un orphelin"
        encore = conn.execute(
            "SELECT count(*) AS n FROM node_run WHERE id = ANY(%s) "
            "AND status = 'running'", ([run["id"] for run in anciens],),
        ).fetchone()["n"]
        assert encore == 3, encore
    print("trois identités vivantes conservées avant le bail ✓")

    for path in traces:
        os.killpg(json.loads(path.read_text())["pgid"], signal.SIGKILL)
    (workspace / "repris").touch()
    print("ordonnanceur et trois groupes tués sous un bail de 600 s", flush=True)

    # phase 2 : relancer tout de suite, sans attendre le bail
    reprise = time.monotonic()
    proc = scheduler()
    deadline = time.time() + TIMEOUT_S
    try:
        with db.connect() as conn:
            ids = [run["id"] for run in anciens]
            while time.monotonic() - reprise < REAP_S:
                classes = conn.execute(
                    "SELECT count(*) AS n FROM node_run WHERE id = ANY(%s) "
                    "AND status = 'faulted'", (ids,),
                ).fetchone()["n"]
                if classes == 3:
                    break
                time.sleep(0.1)
            else:
                sys.exit("ÉCHEC : les trois orphelins attendent encore leur bail")
            print(f"1. trois runs classés en {time.monotonic() - reprise:.1f}s, "
                  f"bail restant supérieur à {LEASE_S - 20}s ✓")

            while time.time() < deadline:
                item = conn.execute(
                    "SELECT * FROM work_item WHERE id = %s", (item_id,)
                ).fetchone()
                if item["terminal_at"] is not None:
                    break
                time.sleep(0.5)
            else:
                sys.exit("ÉCHEC : l'item n'a pas terminé dans les temps")

            # 1. terminal
            assert item["state"] == "close", item["state"]
            print(f"2. terminal : {item['state']} ✓")

            # 2. journal contigu v1..vN
            versions = [r["item_version"] for r in conn.execute(
                "SELECT item_version FROM event WHERE item_id = %s ORDER BY item_version",
                (item_id,),
            )]
            assert versions == list(range(1, len(versions) + 1)), versions
            assert versions[-1] == item["version"]
            print(f"3. journal contigu v1..v{versions[-1]} ✓")

            # 3. effet exactement une fois
            outbox = (WORK / "data" / "effects_outbox.log").read_text()
            key = "crash-test:pipeline-x:oom:commit"
            assert outbox.count(key) == 1, f"{outbox.count(key)} exécutions"
            print("4. effet exécuté exactement une fois ✓")

            # les trois runs de la première tentative sont tous classés
            statuses = [r["status"] for r in conn.execute(
                "SELECT status FROM node_run WHERE id = ANY(%s)", (ids,)
            )]
            assert statuses == ["faulted"] * 3, statuses
    finally:
        kill_group(proc, signal.SIGTERM)
        shutil.rmtree(WORK, ignore_errors=True)

    print("\ncrash-test : OK — tuer l'ordonnanceur est un cas nominal")
    subprocess.run(["uv", "run", "graphatom", "journal", str(item_id)], cwd=ROOT)


if __name__ == "__main__":
    main()
