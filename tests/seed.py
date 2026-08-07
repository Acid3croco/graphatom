"""La fixture de peuplement : de quoi remplir les pages, rien de plus.

Un test frontend a besoin d'une base qui contient quelque chose : des
items, un journal, une question ouverte. Il n'a pas besoin d'un SIGKILL
ni d'une attente d'expiration de bail — c'est le travail du crash-test,
qui coûte 90 s dont la moitié de sommeil pur. Ici : publier, admettre,
laisser l'ordonnanceur tourner quelques ticks, s'arrêter. ~15 s.

Deux items sur `examples/supervision.json` :

  1. la trajectoire nominale — ingest…commit → close, journal complet,
     un effet appliqué
  2. la même, une variante près : `decide` répond `raise` au lieu de
     `fix`, l'item s'arrête sur le WAIT `escalate` — une question
     ouverte, un item actif, un état courant à afficher

Rien n'est détruit : la base est créée si absente, jamais droppée, et
les sujets portent le pid — deux exécutions concurrentes ne se marchent
pas dessus. L'ordonnanceur tourne dans un répertoire jetable : les
workspaces qu'il crée ne touchent pas au `data/` du dépôt.

Usage : uv run python tests/seed.py
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

from graphatom import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="graphatom-seed-"))
# hermétisme : ce dépôt-ci est le seul univers de la fixture. L'agent qui
# nous lance a dans son environnement le clone et l'instance de la
# production — l'ordonnanceur lancé d'ici n'en voit ni l'un ni l'autre.
os.environ["GRAPHATOM_REPO_DIR"] = str(ROOT)
os.environ.pop("GRAPHATOM_AGENT_DSN", None)
TIMEOUT_S = 60


def sh(*args: str) -> str:
    out = subprocess.run(
        ["uv", "run", "graphatom", *args],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def scheduler() -> subprocess.Popen:
    # même lancement que le crash-test : binaire du venv, groupe dédié
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


def variant_raise() -> Path:
    """Le même graph, à une issue près : `decide` escalade au lieu de produire."""
    bundle = json.loads((ROOT / "examples" / "supervision.json").read_text())
    bundle["nodes"]["decide"]["config"]["stub_outcome"] = "raise"
    path = WORK / "supervision-raise.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
    return path


def settled(conn, done_id: int, waiting_id: int) -> bool:
    done = conn.execute(
        "SELECT terminal_at FROM work_item WHERE id = %s", (done_id,)
    ).fetchone()
    question = conn.execute(
        "SELECT id FROM question WHERE item_id = %s AND state = 'open'", (waiting_id,)
    ).fetchone()
    return done["terminal_at"] is not None and question is not None


def main() -> None:
    sh("init-db")  # jamais --drop : la base est peut-être partagée
    tag = os.getpid()

    rev = sh("publish", "examples/supervision.json")
    done_id = int(sh("admit", rev, f"seed-nominal:{tag}"))
    rev_raise = sh("publish", str(variant_raise()))
    waiting_id = int(sh("admit", rev_raise, f"seed-question:{tag}"))
    print(f"items admis : #{done_id} (nominal), #{waiting_id} (question)")

    proc = scheduler()
    deadline = time.time() + TIMEOUT_S
    try:
        with db.connect() as conn:
            while time.time() < deadline:
                if settled(conn, done_id, waiting_id):
                    break
                time.sleep(0.5)
            else:
                sys.exit("ÉCHEC : les items n'ont pas atteint l'état attendu")

            item = conn.execute(
                "SELECT state, version FROM work_item WHERE id = %s", (done_id,)
            ).fetchone()
            print(f"#{done_id} terminal : {item['state']} en v{item['version']} ✓")
            q = conn.execute(
                "SELECT id, node, text FROM question WHERE item_id = %s AND state = 'open'",
                (waiting_id,),
            ).fetchone()
            print(f"#{waiting_id} en attente sur {q['node']}, question [{q['id']}] ✓")
    finally:
        kill_group(proc, signal.SIGTERM)
        shutil.rmtree(WORK, ignore_errors=True)

    print("\nseed : OK — la base a des items, un journal et une question ouverte")


if __name__ == "__main__":
    main()
