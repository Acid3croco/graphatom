"""Le test des candidats gratuits d'`implement` : déclaration et CLI absente.

La course d'`implement` compte un candidat qui ne coûte rien : sa commande
passe par `scripts/agent-opencode.sh` sur
`opencode/deepseek-v4-flash-free`, et le reste — le prompt, les budgets, les
portes — est celui de ses deux frères codex.
Ce qu'on mesure avec lui n'a de sens que s'il ne perd jamais en silence :
une CLI absente doit se lire comme une CLI absente dans le résultat du run,
pas comme du code qui ne compile pas.

Scénario :

  1. le fan-out d'`implement` déclare bien ce candidat — l'adaptateur, le
     modèle gratuit, l'atelier du candidat comme répertoire de travail, les
     portes de tout le monde —, sa commande ne porte aucun identifiant, et
     le nombre de candidats reste sous la limite dure
  2. la même variante jouée pour de vrai, avec un binaire `opencode`
     introuvable : son `node_run` en base ne porte aucune issue de succès, et
     le résultat enregistré nomme la commande manquante. Les portes, elles,
     n'ont pas tourné — c'est ce qui distingue l'échec de CLI de l'échec de
     code, qui lui laisse un `portes.md`
  3. deux adaptateurs lancés en même temps reçoivent deux bases OpenCode
     distinctes, mais gardent la configuration, les identifiants et le cache
     communs de la session de l'hôte

Le test ne demande pas `opencode` sur la machine : c'est son absence qu'il
éprouve. Il ne détruit rien non plus — son dépôt est jetable, et il publie
son propre graph.

Usage : uv run python tests/fanout_opencode_test.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, executors, graph, kernel, scheduler  # noqa: E402

from outils import git  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PAQUET = json.loads((ROOT / "examples" / "code-task.json").read_text())
REEL = PAQUET["nodes"]["implement"]
VARIANTES = REEL["config"]["fanout"]["variants"]

ADAPTATEUR = "scripts/agent-opencode.sh"
MODELE = "opencode/deepseek-v4-flash-free"

# hermétisme : l'agent d'ici est un script shell, il n'a pas de base
os.environ.pop("GRAPHATOM_AGENT_DSN", None)


def gratuits() -> list[int]:
    """Le rang du candidat qui déclare l'exécuteur gratuit."""
    rangs = []
    for rang in range(len(VARIANTES)):
        candidat = graph.candidate_node(REEL, rang)
        resolu = executors.resolve(PAQUET, candidat)
        if resolu.cli == "opencode" and resolu.model == MODELE:
            rangs.append(rang)
    assert len(rangs) == 1, f"un candidat gratuit attendu, vu {rangs}"
    return rangs


def bundle_gratuit() -> dict:
    """Le graph du candidat gratuit seul : sa variante, ses arêtes raccourcies.

    Ses frères n'ont rien à faire ici : un candidat suffit pour éprouver
    l'absence de la CLI. La variante est celle de l'exemple mot pour mot,
    posée sur la config du nœud par le même recouvrement que dans le rail.
    """
    node = json.loads(json.dumps(REEL))  # une copie : le nœud réel ne bouge pas
    node["config"]["fanout"]["variants"] = [
        json.loads(json.dumps(VARIANTES[gratuits()[0]]))
    ]
    node["config"]["agent"]["timeout_s"] = 60  # rien à attendre : la CLI manque
    node["config"]["agent"]["silence_s"] = 60
    node["config"]["lease_s"] = 300
    node["edges"] = {"done": "fini"}
    return {
        "name": f"opencode-absent-{uuid.uuid4().hex[:8]}",
        "entry": "implement",
        "agent": json.loads(json.dumps(PAQUET["agent"])),
        "budgets": {"escalations": 1, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
        "nodes": {
            "implement": node,
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


def depot(tmp: Path) -> Path:
    """Un dépôt jetable qui porte les scripts, et son `origin/main`.

    L'adaptateur est lu depuis l'atelier du candidat : il faut donc un vrai
    dépôt, mais rien de plus que `scripts/` — la commande s'arrête sur la
    CLI manquante bien avant de toucher au code.
    """
    repo = tmp / "repo"
    shutil.copytree(ROOT / "scripts", repo / "scripts")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "opencode@test.invalid")
    git(repo, "config", "user.name", "opencode")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "socle")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    os.environ["GRAPHATOM_REPO_DIR"] = str(repo)
    return repo


# -------------------------------------------------------------------- épreuves


def declaration() -> None:
    """1. la variante est là, elle dit son adaptateur, son modèle, son atelier."""
    paquet = json.loads((ROOT / "examples" / "code-task.json").read_text())
    graph.validate(paquet)

    candidats = len(graph.fanout_variants(REEL))
    assert candidats <= graph.FANOUT_MAX_CANDIDATES, candidats

    for rang in gratuits():
        candidat = graph.candidate_node(REEL, rang)
        resolu = executors.resolve(paquet, candidat)
        env = executors.environment(resolu)
        cmd = candidat["config"]["agent"]["cmd"]
        assert (resolu.cli, resolu.model, resolu.effort) == \
            ("opencode", MODELE, None), resolu
        assert env["GRAPHATOM_AGENT_CLI"] == "opencode", env
        assert env["OPENCODE_MODEL"] == MODELE, env
        assert "agent-declared.sh" in cmd, cmd
        assert "portes.sh" in cmd, f"la variante ne lance pas ses portes : {cmd}"
        for mot in ("KEY", "TOKEN", "SECRET", "PASSWORD", "api_key", "Bearer"):
            assert mot not in json.dumps(env), f"un identifiant déclaré : {mot}"

    labels = ", ".join(VARIANTES[r]["label"] for r in gratuits())
    print(f"1. candidats gratuits « {labels} » : {ADAPTATEUR} sur {MODELE}, "
          f"atelier du candidat, portes de tout le monde, aucun identifiant — "
          f"{candidats} candidats ≤ {graph.FANOUT_MAX_CANDIDATES} ✓")


def cli_absente(conn, workdir: Path, repo: Path) -> None:
    """2. binaire introuvable : pas d'issue de succès, et la commande est nommée."""
    absent = repo.parent / "pas-d-opencode"
    assert not absent.exists(), absent
    os.environ["OPENCODE_BIN"] = str(absent)

    revision = graph.publish(conn, bundle_gratuit())
    item_id = kernel.admit(
        conn, revision, f"gh:test/opencode#{uuid.uuid4().int % 999}",
        _allow_parallel_for_test=True,
    )
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / f"rail-item-{item_id}"),
        "-b", f"rail/issue-{item_id}")

    run = kernel.claim(conn, item_id)
    assert run is not None and run["candidate"] == 0, run
    scheduler._execute(run["id"], item_id)

    ligne = conn.execute("SELECT * FROM node_run WHERE id = %s", (run["id"],)).fetchone()
    result = ligne["result"] or {}
    trace = result.get("log_tail", "")

    assert ligne["outcome"] not in (REEL["edges"] or {}), \
        f"le candidat a rendu l'issue de succès du nœud : {ligne['outcome']}"
    assert ligne["outcome"] == "crashed", (ligne["outcome"], result)
    assert result.get("exit_code") == 3, result  # le code nommé de l'adaptateur
    assert "introuvable" in trace, trace
    assert str(absent) in trace, trace
    assert "agent-opencode:" in trace, trace

    espace = blocks.run_workspace(item_id, run)
    assert not (espace / blocks.OUTCOME_NAME).exists(), "une issue inventée sans modèle ?"
    assert not (espace / "portes.md").exists(), \
        "les portes ont tourné : l'échec se lirait comme un échec de code"

    print(f"2. item {item_id} : node_run {run['id']} outcome « {ligne['outcome']} » "
          f"en base, exit_code 3, aucun portes.md ✓")
    print(f"   la commande manquante, dans le résultat du run : "
          f"{trace.splitlines()[0]}")


def etats_isoles(tmp: Path) -> None:
    """3. deux candidats concurrents ne partagent pas la base de la CLI."""
    binaire = tmp / "faux-opencode.sh"
    binaire.write_text("""#!/usr/bin/env bash
set -u
printf '%s|%s|%s|%s|%s\n' "$PWD" "$OPENCODE_DB" "$XDG_DATA_HOME" \
    "$XDG_STATE_HOME" "$XDG_CACHE_HOME" >> "$CHECK_DIR/departs"
while [ "$(wc -l < "$CHECK_DIR/departs")" -lt 2 ]; do sleep 0.02; done
if ! mkdir "${OPENCODE_DB}.verrou" 2>/dev/null; then
    echo 'database is locked' >&2
    exit 88
fi
sleep 0.1
printf '{"outcome":"done","summary":"base isolée"}\n' > outcome.json
printf '{"type":"text","part":{"text":"done"}}\n'
rmdir "${OPENCODE_DB}.verrou"
""")
    binaire.chmod(0o755)

    coordination = tmp / "coordination"
    coordination.mkdir()
    (coordination / "departs").touch()
    donnees = tmp / "donnees-partagees"
    etat = tmp / "etat-partage"
    cache = tmp / "cache-partage"
    for path in (donnees, etat, cache):
        path.mkdir()

    processus = []
    espaces = []
    for candidat in range(2):
        espace = tmp / f"c{candidat}"
        atelier = tmp / f"atelier-c{candidat}"
        espace.mkdir()
        atelier.mkdir()
        (espace / "prompt.md").write_text("rends done\n")
        env = os.environ | {
            "CHECK_DIR": str(coordination),
            "GRAPHATOM_WORKSPACE": str(espace),
            "OPENCODE_BIN": str(binaire),
            "OPENCODE_DIR": str(atelier),
            "OPENCODE_TIMEOUT_S": "5",
            "XDG_DATA_HOME": str(donnees),
            "XDG_STATE_HOME": str(etat),
            "XDG_CACHE_HOME": str(cache),
        }
        processus.append(subprocess.Popen(
            ["/bin/bash", str(ROOT / ADAPTATEUR), MODELE],
            cwd=espace, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        ))
        espaces.append(espace)

    sorties = [proc.communicate(timeout=10)[0] for proc in processus]
    assert [proc.returncode for proc in processus] == [0, 0], sorties
    assert all("database is locked" not in sortie for sortie in sorties), sorties

    lignes = (coordination / "departs").read_text().splitlines()
    assert len(lignes) == 2, lignes
    vus = [ligne.split("|") for ligne in lignes]
    bases = {champs[1] for champs in vus}
    assert bases == {str(espace / ".opencode" / "opencode.db")
                     for espace in espaces}, vus
    assert {champs[2] for champs in vus} == {str(donnees)}, vus
    assert {champs[3] for champs in vus} == {str(etat)}, vus
    assert {champs[4] for champs in vus} == {str(cache)}, vus
    assert all((espace / "outcome.json").exists() for espace in espaces)

    relatif = tmp / "c-relatif"
    atelier_relatif = tmp / "atelier-relatif"
    relatif.mkdir()
    atelier_relatif.mkdir()
    (relatif / "prompt.md").write_text("rends done\n")
    done = subprocess.run(
        ["/bin/bash", str(ROOT / ADAPTATEUR), MODELE], cwd=relatif,
        env=os.environ | {
            "CHECK_DIR": str(coordination),
            "GRAPHATOM_WORKSPACE": str(relatif),
            "OPENCODE_BIN": str(binaire),
            "OPENCODE_DIR": str(atelier_relatif),
            "OPENCODE_STATE_DIR": "etat-relatif",
            "OPENCODE_TIMEOUT_S": "5",
            "XDG_DATA_HOME": str(donnees),
            "XDG_STATE_HOME": str(etat),
            "XDG_CACHE_HOME": str(cache),
        },
        capture_output=True, text=True, timeout=10,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    champs = (coordination / "departs").read_text().splitlines()[-1].split("|")
    assert champs[1] == str(relatif / "etat-relatif" / "opencode.db"), champs
    assert Path(champs[1]).is_absolute(), champs

    print("3. deux adaptateurs concurrents : deux bases locales, configuration "
          "et cache partagés, aucun verrou croisé ; override relatif rendu "
          "absolu ✓")


def main() -> None:
    declaration()
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-opencode-absent-"))
    blocks.DATA_DIR = workdir
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-opencode-absent-repo-"))
    try:
        repo = depot(tmp)
        etats_isoles(tmp)
        with db.connect() as conn:
            cli_absente(conn, workdir, repo)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\ncandidats gratuits : OK — une CLI absente dit son nom, et ne se "
          "confond pas avec un échec de code")


if __name__ == "__main__":
    main()
