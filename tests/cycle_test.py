"""Un cycle entier du profil `code-task`, de l'admission à `close`.

Les autres tests prennent le fan-out, l'arbitre ou la réduction séparément.
Celui-ci prend le graph de production **tel qu'il est publié** —
`examples/code-task.json` — et le fait traverser par le vrai ordonnanceur,
d'un bout à l'autre : `implement` en fan-out réduit par `keep_n`, le nœud
`judge` derrière, puis les tests, la revue, le retrait, et `close`.

Ce qui est joué pour de vrai : le graph, ses arêtes, ses budgets, la
réduction `keep_n` et son `n`, le nœud arbitre et sa source, l'ordonnanceur,
la base, git — les ateliers des candidats sont de vrais worktrees et leurs
commits de vrais commits. Ce qui est remplacé : les seules `cmd` qui
appellent un modèle ou sortent sur le réseau. Un cycle qui paie trois agents
et un juge ne se joue pas dans une suite de tests ; ce qui route un item, en
revanche, ce sont les arêtes et les issues, et elles sont ici d'origine.

Le chemin attendu, celui du profil :

    ingest → worktree → scope → implement (3 candidats, keep_n = 2)
           → judge (2 finalistes → chosen) → test_backend → test_frontend
           → validate → review (l'humain répond `merger`) → release
           → deploy → verify_deploy → cleanup → close

Rien de la production n'est touché : le dépôt est jetable, son `origin` est
un dépôt nu du même répertoire temporaire, et l'ordonnanceur tourne dans un
`cwd` jetable — les workspaces qu'il crée ne voient pas le `data/` du dépôt.

Usage : uv run python tests/cycle_test.py
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

from graphatom import channel, db, graph, kernel  # noqa: E402

# hermétisme : les agents d'ici sont des scripts shell, ils n'ont pas de base
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

ROOT = Path(__file__).resolve().parents[1]
PROFIL = ROOT / "examples" / "code-task.json"
NUM = 4242  # le numéro d'issue du sujet, celui dont `worktree` tire sa branche
TIMEOUT_S = 180.0

# Le chemin que l'item doit suivre, nœud par nœud. C'est la liste qu'on
# vérifie à la fin : pas seulement « il est arrivé », mais « il est passé
# par là » — un cycle qui atteindrait `close` en sautant l'arbitre ne
# prouverait rien de ce ticket.
ATTENDU = ["ingest", "worktree", "scope", "implement", "judge", "test_backend",
           "test_frontend", "validate", "review", "release", "deploy",
           "verify_deploy", "cleanup", "close"]

# Un candidat de `implement` : il commite pour de vrai dans son atelier, avec
# un mot à lui, puis rend `done`. Trois candidats, trois diffs différents —
# de quoi donner au juge quelque chose à départager.
CANDIDAT = """
set -e
K=$(basename "$(pwd)")
printf 'le candidat %s a travaillé\\n' "$K" >> "$GRAPHATOM_WORKTREE/travail-$K.txt"
git -C "$GRAPHATOM_WORKTREE" add -A
git -C "$GRAPHATOM_WORKTREE" commit -qm "implémentation du candidat $K"
printf '{"input_tokens": 1000, "total_cost_usd": 0.50}' > usage.json
printf '{"outcome": "done", "summary": "candidat %s"}' "$K" > outcome.json
"""

# L'arbitre : il écrit son verdict et élit la première lettre. Ce qu'il
# coûte est dit, et c'est bien plus que ce qu'un candidat coûte — c'est
# l'haltère, et le front doit pouvoir montrer les deux bouts.
JUGE = """
printf 'Les deux finalistes tiennent les critères ; A le fait en moins de lignes.\\n' \\
    > verdict.md
printf '{"input_tokens": 50000, "total_cost_usd": 4.00}' > usage.json
printf '{"outcome": "chosen", "elu": "A", "summary": "le diff le plus court"}' \\
    > outcome.json
"""


def stub(outcome: str) -> str:
    """Un nœud qui rend son issue et rien d'autre — ni modèle, ni réseau."""
    return (f"printf '{{\"outcome\": \"{outcome}\", "
            f"\"summary\": \"doublure de test\"}}' > outcome.json\n")


# Les nœuds dont la `cmd` appelle un modèle ou sort sur le réseau, et l'issue
# qu'on leur fait rendre. Tout le reste du bundle — `ingest`, `worktree`,
# `cleanup`, `review`, et surtout les arêtes et le `fanout` — reste d'origine.
DOUBLURES = {
    "scope": stub("ready"),
    "implement": CANDIDAT,
    "judge": JUGE,
    "test_backend": stub("pass"),
    "test_frontend": stub("pass"),
    "validate": stub("pass"),
    "release": stub("done"),
    "deploy": stub("done"),
    "verify_deploy": stub("pass"),
}


def bundle() -> dict:
    """Le profil de production, ses seules `cmd` de modèle remplacées."""
    spec = json.loads(PROFIL.read_text())
    spec["name"] = "code-task-cycle-test"
    for node, cmd in DOUBLURES.items():
        agent = spec["nodes"][node]["config"]["agent"]
        agent["cmd"] = cmd
        # les budgets d'origine sont ceux d'un vrai modèle : un quart d'heure
        # d'attente pour une doublure qui rend en 50 ms n'apprend rien, et
        # allongerait le test d'autant le jour où l'une se pend
        agent["timeout_s"], agent["silence_s"] = 60, 60
    fanout = spec["nodes"]["implement"]["config"]["fanout"]
    assert fanout["reduce"] == "keep_n", fanout  # le ticket, en une ligne
    assert fanout["n"] == 2, fanout
    assert graph.judge_source(spec["nodes"]["judge"]) == "implement"
    return spec


# ------------------------------------------------------------------ outillage


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def depot(tmp: Path) -> Path:
    """Un clone jetable et son `origin` : ce dont `worktree` a besoin, pas plus.

    Le nœud `worktree` du profil est joué tel quel — il fetch `origin` et
    part d'`origin/main`. Il lui faut donc un vrai remote, et un dépôt nu du
    même répertoire temporaire fait l'affaire.
    """
    nu = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(nu)], check=True)
    amorce = tmp / "amorce"
    subprocess.run(["git", "clone", "-q", str(nu), str(amorce)], check=True)
    git(amorce, "config", "user.email", "cycle@test.invalid")
    git(amorce, "config", "user.name", "cycle")
    (amorce / "socle.txt").write_text("le commit de départ\n")
    git(amorce, "add", "-A")
    git(amorce, "commit", "-qm", "socle")
    git(amorce, "push", "-q", "origin", "main")

    repo = tmp / "repo"
    subprocess.run(["git", "clone", "-q", str(nu), str(repo)], check=True)
    git(repo, "config", "user.email", "cycle@test.invalid")
    git(repo, "config", "user.name", "cycle")
    return repo


def ordonnanceur(work: Path) -> subprocess.Popen:
    """Le vrai rail, dans son groupe de processus, sur un `cwd` jetable."""
    return subprocess.Popen([str(ROOT / ".venv" / "bin" / "graphatom"), "run"],
                            cwd=work, start_new_session=True)


def tuer(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    proc.wait()


def attendre(predicat, seconds: float = TIMEOUT_S):
    """Attend qu'un fait devienne vrai — un cycle n'est pas synchrone."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if (valeur := predicat()):
            return valeur
        time.sleep(0.2)
    return predicat()


def etat(conn, item_id: int) -> dict:
    return conn.execute("SELECT * FROM work_item WHERE id = %s",
                        (item_id,)).fetchone()


def repondre(conn, item_id: int, option: str) -> None:
    """Ce que l'humain fait sur la revue : il répond, le rail reprend."""
    question = attendre(lambda: conn.execute(
        "SELECT * FROM question WHERE item_id = %s AND state = 'open'",
        (item_id,)).fetchone())
    assert question is not None, f"aucune question ouverte sur l'item {item_id}"
    assert question["node"] == "review", question["node"]
    motif = channel.record_answer(conn, question["id"], option, "cycle-test")
    assert motif is None, motif
    print(f"   revue : question [{question['id']}] répondue « {option} » ✓")


# -------------------------------------------------------------------- épreuve


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-cycle-"))
    work = tmp / "work"
    work.mkdir()
    repo = depot(tmp)
    os.environ["GRAPHATOM_REPO_DIR"] = str(repo)

    db.init()
    proc = None
    try:
        with db.connect() as conn:
            rev = graph.publish(conn, bundle())
            item_id = kernel.admit(conn, rev, f"gh:exemple/depot#{NUM}")
            print(f"1. item {item_id} admis sur le profil code-task, "
                  f"atelier jetable {repo}")

            proc = ordonnanceur(work)
            repondre(conn, item_id, "merger")

            fini = attendre(lambda: etat(conn, item_id)["terminal_at"] is not None)
            item = etat(conn, item_id)
            if not fini:
                sys.exit(f"ÉCHEC : l'item est resté sur « {item['state']} » "
                         f"après {TIMEOUT_S} s")
            if item["state"] != "close":
                sys.exit(f"ÉCHEC : terminal sur « {item['state']} », attendu « close »")
            print(f"2. item terminal sur « close » en v{item['version']}, "
                  f"passage {item['cycle']}, {item['escalations']} escalade(s) ✓")

            verifier(conn, item_id)
    finally:
        if proc is not None:
            tuer(proc)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\ncycle : OK — le profil code-task traverse keep_n et judge, "
          "et l'item atteint close")


def verifier(conn, item_id: int) -> None:
    """Le chemin parcouru, les finalistes, et le prix du jugement à part."""
    passage = [e["to_state"] for e in conn.execute(
        "SELECT to_state FROM item_event WHERE item_id = %s AND to_state IS NOT NULL "
        "ORDER BY item_version", (item_id,))]
    chemin = ["ingest"] + passage
    if chemin != ATTENDU:
        sys.exit(f"ÉCHEC : chemin {chemin}\n         attendu {ATTENDU}")
    print(f"3. chemin d'origine tenu, {len(chemin)} nœuds : "
          f"{' → '.join(chemin)} ✓")

    runs = conn.execute(
        "SELECT node, candidate, status, outcome FROM node_run "
        "WHERE item_id = %s ORDER BY id", (item_id,)).fetchall()
    candidats = [r for r in runs if r["node"] == "implement"]
    finalistes = [r for r in candidats if r["status"] == "applied"]
    recales = [r for r in candidats if r["status"] == "superseded"]
    assert len(candidats) == 3, candidats
    assert len(finalistes) == 2, finalistes  # `n` = 2, et pas un de plus
    assert len(recales) == 1, recales
    print(f"4. {len(candidats)} candidats courus, {len(finalistes)} finalistes "
          f"transmis, {len(recales)} recalé ✓")

    arbitre = [r for r in runs if r["node"] == "judge"]
    assert len(arbitre) == 1 and arbitre[0]["outcome"] == "chosen", arbitre
    print("   l'arbitre a couru une fois et a élu ✓")

    # le prix du jugement, à part de celui de la génération : c'est ce que la
    # page de l'item montre, et c'est mesurable ici sur les mêmes lignes
    prix = {r["part"]: r["cout"] for r in conn.execute(
        "SELECT CASE WHEN node = 'judge' THEN 'jugement' ELSE 'candidats' END AS part, "
        "sum((usage->>'total_cost_usd')::float) AS cout FROM node_run "
        "WHERE item_id = %s AND node IN ('judge', 'implement') GROUP BY 1",
        (item_id,))}
    assert prix.get("jugement") == 4.0, prix
    assert prix.get("candidats") == 1.5, prix  # 3 candidats × 0,50
    print(f"5. prix du jugement {prix['jugement']} $ face aux candidats "
          f"{prix['candidats']} $ — deux parts, pas un total ✓")


if __name__ == "__main__":
    main()
