"""Le test de l'isolation git des candidats d'un fan-out.

Les candidats écrivent du code. Sans atelier à eux, les K se marchent dessus
dans le worktree de l'item : c'est cette isolation-là qu'on vérifie, de
l'ouverture des ateliers à leur destruction.

Scénario, sur la base et sur un dépôt git jetable — de vrais candidats, qui
committent vraiment dans leur atelier :

  1. chaque candidat a son worktree `.worktrees/rail-item-<N>-c<k>` sur sa
     branche, partie de celle de l'item ; K worktrees distincts, K branches
  2. le commit d'un candidat n'est visible ni chez son voisin, ni chez
     l'item avant la réduction
  3. un bloc trouve son atelier sans le deviner : le chemin est dans son
     environnement, et chaque candidat écrit celui qu'il a reçu
  4. après la réduction `first_pass` : le commit du gagnant est sur la
     branche de l'item, celui d'un perdant ne l'est pas, et l'item n'a
     qu'une branche
  5. après la réduction : plus un worktree, plus une branche de candidat
  6. `wall_deadline` tombé en pleine course : les ateliers des K candidats
     sont détruits eux aussi — la promesse tient sur tous les chemins
  7. un atelier hors de `$GRAPHATOM_REPO_DIR/.worktrees/`, ou dont la
     branche n'est pas celle attendue pour ce candidat, survit

La branche d'un candidat est `rail/issue-<M>-c<k>` et non
`rail/issue-<M>/c<k>` : git refuse qu'une référence soit à la fois un
fichier et un répertoire. Voir `worktree` — et `implementation.md` de
l'item, qui le dit pour le critère qui nommait l'autre forme.

Le test ne détruit rien de la production : son dépôt est jetable, et
`GRAPHATOM_REPO_DIR` est épinglé dessus.

Usage : uv run python tests/fanout_worktree_test.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, graph, kernel, scheduler, worktree  # noqa: E402

# hermétisme : les agents d'ici sont des scripts shell, ils n'ont pas de base
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

# Chaque candidat dit où il travaille, y committe, et laisse sa marque. Le
# numéro de candidat se lit sur son workspace, comme partout ailleurs.
PREAMBULE = """
set -e
k=$(basename "$(pwd)"); k=${k#c}
WT="$GRAPHATOM_WORKTREE"
printf '%s' "$WT" > worktree.txt
printf 'travail du candidat %s\\n' "$k" > "$WT/c$k.txt"
git -C "$WT" add -A
git -C "$WT" commit -qm "candidat $k"
git -C "$WT" rev-parse HEAD > commit.txt
: > pret
"""

# le lent ne rendra jamais son issue : la réduction ou l'échéance le tue avant
LENT = PREAMBULE + "sleep 120\n"
# le rapide attend le signal du test, puis gagne
RAPIDE = PREAMBULE + """
while [ ! -f ../go ]; do sleep 0.1; done
printf '{"outcome": "ok", "summary": "candidat %s"}' "$k" > outcome.json
"""

PROMPT = "Tu travailles sur {subject_key}, dans l'atelier de {label}."


def bundle(labels: list[str]) -> dict:
    """Le graph de la course : un ACT en fan-out, un candidat par label."""
    variantes = [{"label": nom, "agent": {"cmd": RAPIDE if nom == "rapide" else LENT}}
                 for nom in labels]
    return {
        "name": f"fanout-worktree-{'-'.join(labels)}",
        "entry": "travail",
        "budgets": {"escalations": 2, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {
                "block": "ACT",
                "config": {
                    "agent": {"cmd": LENT, "prompt": PROMPT,
                              "timeout_s": 120, "silence_s": 120},
                    "lease_s": 300,  # le faucheur n'a rien à faire ici
                    "fanout": {"variants": variantes, "reduce": "first_pass"},
                },
                "edges": {"ok": "fini"},
            },
            "escalate": {
                "block": "WAIT", "escalade": True,
                "config": {"question": "On retente ?", "options": ["retry"],
                           "owner": "test", "deadline_minutes": 60},
                "edges": {"retry": "travail", "expired": "abandon"},
            },
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


# ------------------------------------------------------------------ outillage


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def depot(tmp: Path) -> Path:
    """Un dépôt jetable, un commit de socle. `GRAPHATOM_REPO_DIR` y est épinglé."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "fanout@test.invalid")
    git(repo, "config", "user.name", "fanout")
    (repo / "socle.txt").write_text("le commit de départ\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "socle")
    os.environ["GRAPHATOM_REPO_DIR"] = str(repo)
    return repo


def inscrits(repo: Path) -> dict[Path, str]:
    """`git worktree list --porcelain`, lu par le test et pas par le module."""
    table, chemin = {}, None
    for ligne in git(repo, "worktree", "list", "--porcelain").splitlines():
        if ligne.startswith("worktree "):
            chemin = Path(ligne[len("worktree "):])
        elif ligne.startswith("branch refs/heads/") and chemin is not None:
            table[chemin] = ligne[len("branch refs/heads/"):]
    return table


def attendre(predicat, seconds: float = 30.0) -> bool:
    """Attend qu'un fait devienne vrai — une course n'est pas synchrone."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicat():
            return True
        time.sleep(0.05)
    return predicat()


def ancetre(repo: Path, sha: str, branche: str) -> bool:
    """Le commit est-il atteignable depuis la branche ?"""
    return subprocess.run(["git", "-C", str(repo), "merge-base",
                           "--is-ancestor", sha, branche]).returncode == 0


def nouvel_item(conn, repo: Path, labels: list[str]) -> tuple[int, str]:
    """Un item, son atelier, sa branche — ce que le shell du graph préparerait."""
    rev = graph.publish(conn, bundle(labels))
    item_id = kernel.admit(conn, rev, f"fanout-worktree:{uuid.uuid4().hex[:8]}")
    branche = f"rail/issue-{item_id}"
    git(repo, "worktree", "add", "-q",
        str(repo / ".worktrees" / f"rail-item-{item_id}"), "-b", branche)
    return item_id, branche


def lancer(conn, item_id: int) -> list[dict]:
    """Réserve la tentative entière et lance les K candidats, comme l'ordonnanceur."""
    runs = []
    while (run := kernel.claim(conn, item_id)) is not None:
        runs.append(run)
        assert len(runs) <= graph.FANOUT_MAX_CANDIDATES, "claim ne s'arrête plus"
    for run in runs:
        threading.Thread(target=scheduler._execute, args=(run["id"], item_id),
                         daemon=True).start()
    return runs


# -------------------------------------------------------------------- épreuves


def course(conn, workdir: Path, repo: Path) -> None:
    """1 à 5 : trois ateliers isolés, un gagnant promu, plus rien derrière."""
    item_id, branche = nouvel_item(conn, repo, ["lent", "lent", "rapide"])
    socle = git(repo, "rev-parse", branche)
    item_wt = repo / ".worktrees" / f"rail-item-{item_id}"
    workspace = workdir / f"item-{item_id}"
    runs = lancer(conn, item_id)
    assert len(runs) == 3, runs

    prets = [workspace / f"c{k}" / "pret" for k in range(3)]
    assert attendre(lambda: all(p.is_file() for p in prets)), \
        f"les trois candidats n'ont pas committé : {[p.is_file() for p in prets]}"

    # 1. un worktree par candidat, sur sa branche, partie de celle de l'item
    table = inscrits(repo)
    ateliers = {k: item_wt.with_name(f"{item_wt.name}-c{k}") for k in range(3)}
    for k, chemin in ateliers.items():
        assert chemin in table, f"candidat {k} : {chemin} n'est pas un worktree de {repo}"
        assert table[chemin] == f"{branche}-c{k}", (k, table[chemin])
        assert ancetre(repo, socle, f"{branche}-c{k}"), \
            f"la branche du candidat {k} ne part pas de celle de l'item"
    assert len(set(ateliers.values())) == 3, ateliers
    assert len({table[c] for c in ateliers.values()}) == 3, table
    assert table[item_wt] == branche, "l'atelier de l'item reste sur sa branche"
    print(f"1. item {item_id} : 3 worktrees {sorted(c.name for c in ateliers.values())} "
          f"sur 3 branches {branche}-c0..c2, toutes parties de {socle[:7]} ✓")

    # 2. le commit d'un candidat ne se voit que chez lui
    for k in range(3):
        for autre in (*ateliers.values(), item_wt):
            vu = git(autre, "log", "--oneline")
            attendu = autre == ateliers[k]
            assert (f"candidat {k}" in vu) == attendu, \
                f"« candidat {k} » {'absent de' if attendu else 'visible dans'} {autre}"
            assert (autre / f"c{k}.txt").is_file() == attendu, \
                f"c{k}.txt {'absent de' if attendu else 'visible dans'} {autre}"
    assert git(item_wt, "rev-parse", "HEAD") == socle, \
        "l'atelier de l'item a bougé avant la réduction"
    print("2. chaque commit n'est visible que dans l'atelier de son candidat ; "
          f"celui de l'item est resté sur {socle[:7]} ✓")

    # 3. le bloc n'a rien deviné : le chemin lui a été donné
    for k, chemin in ateliers.items():
        recu = (workspace / f"c{k}" / "worktree.txt").read_text()
        assert recu == str(chemin), f"candidat {k} : reçu {recu}, attendu {chemin}"
    print("3. chaque candidat a écrit le chemin qu'il a reçu dans "
          "GRAPHATOM_WORKTREE, et c'est le sien ✓")

    commits = {k: (workspace / f"c{k}" / "commit.txt").read_text().strip()
               for k in range(3)}

    # le signal : le rapide rend son issue, la réduction tranche
    (workspace / "go").write_text("")
    assert attendre(lambda: conn.execute(
        "SELECT state FROM work_item WHERE id = %s", (item_id,)
    ).fetchone()["state"] == "fini"), "la réduction n'a pas tranché"

    gagnant = conn.execute(
        "SELECT candidate FROM node_run WHERE item_id = %s AND status = 'applied'",
        (item_id,),
    ).fetchone()["candidate"]
    assert gagnant == 2, f"le rapide devait gagner, c'est c{gagnant}"

    # la réduction rend la main avant que les ateliers soient rangés : promotion
    # puis destruction, dans cet ordre. On attend le dernier geste, l'effacement
    # des branches, pour que 4 et 5 lisent un état fini et non un état à moitié.
    assert attendre(lambda: git(repo, "branch", "--list", f"{branche}-c*") == ""), \
        "les ateliers des candidats n'ont pas été rangés"

    # 4. le travail du gagnant est sur la branche de l'item, pas celui d'un perdant
    assert ancetre(repo, commits[2], branche), \
        f"le commit du gagnant {commits[2][:7]} n'a pas rejoint {branche}"
    for perdant in (0, 1):
        assert not ancetre(repo, commits[perdant], branche), \
            f"le commit du perdant c{perdant} a fui sur {branche}"
    assert git(item_wt, "rev-parse", "HEAD") == commits[2], \
        "l'atelier de l'item n'est pas sur le commit du gagnant"
    assert (item_wt / "c2.txt").is_file(), "le travail du gagnant n'est pas là"
    branches = git(repo, "branch", "--list", "--format=%(refname:short)",
                   f"{branche}*").splitlines()
    assert branches == [branche], f"l'item doit n'avoir qu'une branche : {branches}"
    print(f"4. c2 promu : {commits[2][:7]} atteignable depuis {branche}, "
          f"c0 et c1 non, et {branche} est la seule branche de l'item ✓")

    # 5. plus un atelier de candidat, ni sur le disque ni dans le dépôt
    table = inscrits(repo)
    restants = [c for c in table if c.name.startswith(f"{item_wt.name}-c")]
    assert not restants, f"worktrees de candidats survivants : {restants}"
    assert all(not c.exists() for c in ateliers.values()), \
        [c for c in ateliers.values() if c.exists()]
    assert git(repo, "branch", "--list", f"{branche}-c*") == "", "branches survivantes"
    assert item_wt.is_dir(), "l'atelier de l'item, lui, reste au shell du graph"
    print("5. plus un worktree ni une branche de candidat ; "
          "l'atelier de l'item est intact ✓")


def echeance(conn, workdir: Path, repo: Path) -> None:
    """6. `wall_deadline` en pleine course : les ateliers partent quand même."""
    item_id, branche = nouvel_item(conn, repo, ["lent", "lent"])
    item_wt = repo / ".worktrees" / f"rail-item-{item_id}"
    workspace = workdir / f"item-{item_id}"
    runs = lancer(conn, item_id)
    assert len(runs) == 2, runs

    prets = [workspace / f"c{k}" / "pret" for k in range(2)]
    assert attendre(lambda: all(p.is_file() for p in prets)), "candidats jamais partis"
    table = inscrits(repo)
    ateliers = [item_wt.with_name(f"{item_wt.name}-c{k}") for k in range(2)]
    assert all(c in table for c in ateliers), f"les deux ateliers doivent courir : {table}"
    assert all(r["status"] == "running" for r in conn.execute(
        "SELECT status FROM node_run WHERE item_id = %s", (item_id,)).fetchall()), \
        "les deux candidats doivent courir encore"

    conn.execute("UPDATE work_item SET wall_deadline = now() - interval '1 hour' "
                 "WHERE id = %s", (item_id,))
    scheduler._settle_waits(conn)
    item = conn.execute("SELECT * FROM work_item WHERE id = %s", (item_id,)).fetchone()
    assert item["state"] == "abandon", item["state"]
    assert item["terminal_at"] is not None, "wall_deadline mène au terminal"

    table = inscrits(repo)
    restants = [c for c in table if c.name.startswith(f"{item_wt.name}-c")]
    assert not restants, f"worktrees de candidats survivants : {restants}"
    assert all(not c.exists() for c in ateliers), [c for c in ateliers if c.exists()]
    assert git(repo, "branch", "--list", f"{branche}-c*") == "", "branches survivantes"
    assert item_wt.is_dir() and table[item_wt] == branche, \
        "l'atelier de l'item survit : c'est le cleanup du graph qui le retire"
    print(f"6. item {item_id} : wall_deadline en pleine course → les 2 ateliers "
          "et leurs branches détruits, celui de l'item intact ✓")

    for run in runs:  # les agents courent encore : on ne laisse pas d'orphelin
        blocks.revoke_orphan(item_id, run["id"])


def leurres(conn, repo: Path) -> None:
    """7. hors de `.worktrees/`, ou sur une autre branche : jamais détruit."""
    item_id, branche = nouvel_item(conn, repo, ["lent"])
    item_wt = repo / ".worktrees" / f"rail-item-{item_id}"

    mien = worktree.open_run(item_id, 0)
    assert mien == item_wt.with_name(f"{item_wt.name}-c0"), mien

    # le bon nom, la bonne place, une branche qui n'est pas celle attendue
    branche_leurre = item_wt.with_name(f"{item_wt.name}-c9")
    git(repo, "worktree", "add", "-q", str(branche_leurre), "-b", "bricolage-perso")
    # la bonne branche, mais hors du `.worktrees/` du dépôt
    ailleurs = repo / "ailleurs" / f"rail-item-{item_id}-c8"
    git(repo, "worktree", "add", "-q", str(ailleurs), "-b", f"{branche}-c8")

    retirees = worktree.discard(item_id)
    assert retirees == [f"{branche}-c0"], f"seul l'atelier du candidat 0 part : {retirees}"
    assert not mien.exists(), f"{mien} aurait dû partir"

    table = inscrits(repo)
    assert branche_leurre.is_dir() and table.get(branche_leurre) == "bricolage-perso", \
        f"un worktree sur une autre branche a été détruit : {branche_leurre}"
    assert ailleurs.is_dir() and table.get(ailleurs) == f"{branche}-c8", \
        f"un worktree hors de .worktrees/ a été détruit : {ailleurs}"
    assert git(repo, "branch", "--list", "bricolage-perso") != "", "branche du leurre"
    assert git(repo, "branch", "--list", f"{branche}-c8") != "", "branche du leurre"
    print(f"7. item {item_id} : c0 détruit ; le worktree sur `bricolage-perso` et "
          f"celui hors de .worktrees/ survivent, branches comprises ✓")


def main() -> None:
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-fanout-wt-"))
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-fanout-repo-"))
    blocks.DATA_DIR = workdir
    repo = depot(tmp)
    try:
        with db.connect() as conn:
            course(conn, workdir, repo)
            echeance(conn, workdir, repo)
            leurres(conn, repo)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nisolation git du fan-out : OK — un atelier par candidat, "
          "le gagnant promu, et rien qui traîne derrière")


if __name__ == "__main__":
    main()
