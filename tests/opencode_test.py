"""Le test de l'adaptateur opencode : un modèle gratuit tient le contrat.

La question n'est pas si le modèle est bon, c'est si le harnais suffit. Un
nœud réel du graph tourne donc sous `opencode/deepseek-v4-flash-free`, à
travers `scripts/agent-opencode.sh`, et son issue est relue **en base** :

  1. l'adaptateur sort en erreur nommée quand `opencode` est introuvable
  2. un nœud réel tourne sous le modèle gratuit, et le noyau route son
     issue : `node_run.outcome` en base, et la trace de la commande dans le
     journal de la tentative

Le point 3 du cahier des charges — le modèle muet, `north-mini-code-free` —
n'est pas ici : son silence coûte la borne d'attente entière, et un test qui
dort trois minutes pour voir un `timeout` est un test que personne ne lance.
Il se vérifie à la main, la commande est dans le README.

Le test ne détruit rien : il publie son propre graph, crée son item, et
laisse la base en place — il peut tourner à côté d'un rail vivant.

Le test demande `opencode` sur la machine (ou `OPENCODE_BIN`), le réseau et
`GRAPHATOM_LIVE_OPENCODE=1`. Sans cet accord explicite, il se saute : une
suite locale ne doit jamais attendre un fournisseur externe par accident.

Usage : GRAPHATOM_LIVE_OPENCODE=1 uv run python tests/opencode_test.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, graph, kernel  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ADAPTATEUR = ROOT / "scripts" / "agent-opencode.sh"
MODELE = "opencode/deepseek-v4-flash-free"  # le seul dont le fonctionnement est établi
MUET = "opencode/north-mini-code-free"      # le cas d'erreur, vérifié à la main
NOEUD = "travail"
BORNE_S = 240.0   # la borne de l'adaptateur : le modèle est gratuit, donc lent
BUDGET_S = 300.0  # le budget du nœud, plus long que la borne du script
SILENCE_S = 240.0  # un modèle qui réfléchit n'écrit rien : le chien de garde attend

# hermétisme : pas d'instance jetable, donc aucune base d'item n'est créée ici
os.environ.pop("GRAPHATOM_AGENT_DSN", None)


def opencode() -> str | None:
    """Le binaire opencode, du PATH ou d'`OPENCODE_BIN`. None : rien à tester."""
    return shutil.which(os.environ.get("OPENCODE_BIN", "opencode"))


def bundle(nom: str, cmd: str) -> dict:
    """Un graph minuscule : un nœud d'agent, et deux terminaux."""
    return {
        "name": nom,
        "entry": NOEUD,
        "budgets": {"escalations": 1, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
        "nodes": {
            NOEUD: {
                "block": "ACT",
                "config": {"agent": {
                    "cmd": cmd,
                    "prompt": "Écris le fichier outcome.json de ton workspace, "
                              "avec l'outcome `ok` et un résumé d'une phrase. "
                              "C'est tout le travail demandé.",
                    "timeout_s": BUDGET_S, "silence_s": SILENCE_S,
                }},
                "edges": {"ok": "fini"},
            },
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


def commande(binaire: str) -> str:
    """Le `cmd` du nœud : l'adaptateur, son modèle, et son binaire nommé.

    Le binaire est passé en clair plutôt que laissé au PATH : le worker d'un
    rail n'a pas le PATH d'un shell de connexion, et un test qui échoue sur
    un PATH ne dit rien du modèle.
    """
    return (f'OPENCODE_BIN={binaire} OPENCODE_TIMEOUT_S={BORNE_S:.0f} '
            f'{ADAPTATEUR} {MODELE}')


def opencode_introuvable(workdir: Path) -> None:
    """1. l'adaptateur nomme la commande manquante, il ne se tait pas."""
    ws = workdir / "sans-opencode"
    ws.mkdir()
    (ws / "prompt.md").write_text("peu importe : on n'ira pas jusqu'au modèle\n")
    done = subprocess.run(["/bin/bash", str(ADAPTATEUR)], cwd=ws, env={"PATH": ""},
                          capture_output=True, text=True)

    assert done.returncode == 3, done
    assert "introuvable" in done.stderr, done.stderr
    assert "opencode" in done.stderr, done.stderr
    assert not (ws / "outcome.json").exists(), "une issue inventée sans modèle ?"
    print("1. PATH vidé : code 3, la commande manquante est nommée, aucune "
          "issue inventée ✓")


def noeud_reel(conn, workdir: Path, binaire: str) -> None:
    """2. un nœud réel tourne sous le modèle gratuit, et son issue est en base."""
    revision = graph.publish(conn, bundle(f"opencode-{uuid.uuid4().hex[:8]}",
                                          commande(binaire)))
    item_id = kernel.admit(conn, revision, f"gh:test/opencode#{uuid.uuid4().int % 999}")
    run = kernel.claim(conn, item_id)
    assert run["node"] == NOEUD, run

    item = conn.execute("SELECT * FROM work_item WHERE id = %s", (item_id,)).fetchone()
    paquet = graph.load_bundle(conn, revision)
    node = paquet["nodes"][NOEUD]
    result = blocks.act(blocks.Context(conn, run, item, node, paquet))
    kernel.apply(conn, run["id"], result)

    ligne = conn.execute("SELECT * FROM node_run WHERE id = %s", (run["id"],)).fetchone()
    etat = conn.execute("SELECT * FROM work_item WHERE id = %s", (item_id,)).fetchone()
    assert ligne["outcome"] == "ok", (ligne["outcome"], ligne["result"])
    assert ligne["status"] == "applied", ligne
    assert etat["state"] == "fini", etat  # le noyau a routé l'issue du modèle
    print(f"2. item {item_id} : node_run {run['id']} outcome "
          f"`{ligne['outcome']}` en base, item routé vers `{etat['state']}` ✓")
    print(f"   résumé rendu par le modèle : {ligne['result'].get('summary')!r}")
    if "usage" in ligne["result"]:
        print(f"   usage fusionné dans le run : {ligne['result']['usage']}")

    journal = blocks.attempt_log(blocks.item_workspace(item_id), run)
    trace = journal.read_text()
    assert MODELE in trace, trace[:400]
    assert "agent-opencode:" in trace, trace[:400]
    print(f"   trace de la commande dans {journal.name} : "
          f"{trace.splitlines()[0]} ✓")


def main() -> None:
    if os.environ.get("GRAPHATOM_LIVE_OPENCODE") != "1":
        print("GRAPHATOM_LIVE_OPENCODE=1 absent — test réseau OpenCode SAUTÉ.")
        return
    binaire = opencode()
    if binaire is None:
        print("opencode introuvable sur cette machine — test SAUTÉ.")
        print("Installe-le (npm i -g opencode-ai) ou pose OPENCODE_BIN sur "
              "le chemin du binaire.")
        return
    print(f"opencode : {binaire} — modèle {MODELE}")

    tmp = Path(tempfile.mkdtemp(prefix="graphatom-opencode-"))
    workdir = tmp / "data"
    workdir.mkdir()
    blocks.DATA_DIR = workdir  # les blocs écrivent là, jamais dans data/
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    try:
        opencode_introuvable(workdir)
        with db.connect() as conn:
            noeud_reel(conn, workdir, binaire)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nadaptateur opencode : OK — un nœud réel tourne sous {MODELE}")
    print(f"le modèle muet, lui, se vérifie à la main : "
          f"OPENCODE_TIMEOUT_S=45 {ADAPTATEUR.relative_to(ROOT)} {MUET}")


if __name__ == "__main__":
    main()
