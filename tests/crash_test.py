"""Le crash-test du milestone 1 : tuer l'ordonnanceur est un cas nominal.

Scénario : publier le graph d'exemple, admettre un item, lancer
l'ordonnanceur, le tuer en SIGKILL en plein milieu d'un ACT, le relancer —
et vérifier que l'item termine correctement :

  1. l'item atteint un état terminal
  2. le journal est une chaîne contiguë v1..vN, sans trou ni doublon
  3. l'effet a été exécuté exactement une fois (clé logique unique dans l'outbox)
  4. la tentative fauchée est classée, pas perdue

Usage : uv run python tests/crash_test.py
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# le test travaille dans un répertoire à lui : jamais dans ROOT/data, qui
# peut être le data/ vivant d'un rail (workspaces d'items en cours)
WORK = Path(tempfile.mkdtemp(prefix="graphatom-crash-test-"))
# hermétisme : ce dépôt-ci est le seul univers du test. L'agent qui nous
# lance a dans son environnement le clone et l'instance de la production —
# un ordonnanceur lancé d'ici n'en voit ni l'un ni l'autre.
os.environ["GRAPHATOM_REPO_DIR"] = str(ROOT)
os.environ.pop("GRAPHATOM_AGENT_DSN", None)
KILL_AFTER_S = 2.0  # l'ACT dure 4 s et démarre vite : on tue en plein vol, garanti
TIMEOUT_S = 90


def sh(*args: str) -> str:
    out = subprocess.run(
        ["uv", "run", "graphatom", *args],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def scheduler() -> subprocess.Popen:
    # binaire du venv en direct + groupe de processus dédié : les signaux
    # atteignent le vrai ordonnanceur, pas un wrapper qui laisse un orphelin
    return subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "graphatom"), "run"],
        cwd=WORK, start_new_session=True,
    )


def kill_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        pass
    proc.wait()


def main() -> None:
    sh("init-db", "--drop")
    rev = sh("publish", "examples/supervision.json")
    item_id = int(sh("admit", rev, "pipeline-x:oom"))
    print(f"révision {rev[:12]}…, item {item_id}")

    # phase 1 : tourner, puis SIGKILL en plein ACT
    proc = scheduler()
    time.sleep(KILL_AFTER_S)
    kill_group(proc, signal.SIGKILL)
    print(f"ordonnanceur tué (SIGKILL) après {KILL_AFTER_S}s", flush=True)

    # le bail du run orphelin doit expirer avant que le faucheur le classe
    from graphatom.kernel import LEASE_SECONDS
    print(f"attente d'expiration du bail ({LEASE_SECONDS}s)…")
    time.sleep(LEASE_SECONDS + 1)

    # phase 2 : relancer, attendre le terminal
    proc = scheduler()
    deadline = time.time() + TIMEOUT_S
    try:
        with db.connect() as conn:
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
            assert item["state"] in ("close", "close_unresolved"), item["state"]
            print(f"1. terminal : {item['state']} ✓")

            # 2. journal contigu v1..vN
            versions = [r["item_version"] for r in conn.execute(
                "SELECT item_version FROM event WHERE item_id = %s ORDER BY item_version",
                (item_id,),
            )]
            assert versions == list(range(1, len(versions) + 1)), versions
            assert versions[-1] == item["version"]
            print(f"2. journal contigu v1..v{versions[-1]} ✓")

            # 3. effet exactement une fois
            outbox = (WORK / "data" / "effects_outbox.log").read_text()
            key = "supervision:pipeline-x:oom:commit"
            assert outbox.count(key) == 1, f"{outbox.count(key)} exécutions"
            print("3. effet exécuté exactement une fois ✓")

            # 4. la tentative fauchée est classée
            statuses = [r["status"] for r in conn.execute(
                "SELECT status FROM node_run WHERE item_id = %s", (item_id,)
            )]
            assert "faulted" in statuses, statuses
            assert "running" not in statuses, statuses
            print(f"4. tentative fauchée classée ({statuses}) ✓")
    finally:
        kill_group(proc, signal.SIGTERM)
        shutil.rmtree(WORK, ignore_errors=True)

    print("\ncrash-test : OK — tuer l'ordonnanceur est un cas nominal")
    subprocess.run(["uv", "run", "graphatom", "journal", str(item_id)], cwd=ROOT)


if __name__ == "__main__":
    main()
