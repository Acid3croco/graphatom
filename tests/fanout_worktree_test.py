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
  8. la promotion est faite **avant** que le nœud suivant ne réserve son
     premier run : l'item bouge, le run suivant est réservé contre cette
     version-là, et l'atelier de l'item porte déjà le travail du gagnant
  9. cent réductions de suite, et pas un diff vide au nœud suivant : la
     course entre la promotion et le démarrage d'aval n'existe plus
 10. une promotion impossible — merge non-ff, atelier du gagnant retiré —
     faute le run de la réduction et l'item ne passe pas au nœud suivant

La branche d'un candidat est `rail/issue-<M>-c<k>` et non
`rail/issue-<M>/c<k>` : git refuse qu'une référence soit à la fois un
fichier et un répertoire. Voir `worktree` — et `implementation.md` de
l'item, qui le dit pour le critère qui nommait l'autre forme.

Le test ne détruit rien de la production : son dépôt est jetable, et
`GRAPHATOM_REPO_DIR` est épinglé dessus.

Usage : uv run python tests/fanout_worktree_test.py
"""

import json
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

from outils import attendre, git  # noqa: E402

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
printf '%s\\n' '## Fait' 'Travail committé.' '' '## Appris' 'Rien.' '' \\
    '## Pas fait' 'Rien.' > passation-travail.md
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


def depot(tmp: Path) -> Path:
    """Un dépôt jetable, un commit de socle. `GRAPHATOM_REPO_DIR` y est épinglé.

    `origin/main` y est posé sur le socle : c'est la référence que la porte
    de pertinence oppose au diff d'un atelier, et l'épreuve 9 la lit comme
    `test_backend` la lit.
    """
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "fanout@test.invalid")
    git(repo, "config", "user.name", "fanout")
    (repo / "socle.txt").write_text("le commit de départ\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "socle")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
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


def ancetre(repo: Path, sha: str, branche: str) -> bool:
    """Le commit est-il atteignable depuis la branche ?"""
    return subprocess.run(["git", "-C", str(repo), "merge-base",
                           "--is-ancestor", sha, branche]).returncode == 0


def groupe_vivant(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def item_sur(conn, repo: Path, spec: dict) -> tuple[int, str, Path]:
    """Un item sur ce bundle, son atelier, sa branche — ce que le shell prépare."""
    rev = graph.publish(conn, spec)
    item_id = kernel.admit(
        conn, rev, f"fanout-worktree:{uuid.uuid4().hex[:8]}", _allow_parallel_for_test=True
    )
    branche = f"rail/issue-{item_id}"
    atelier = repo / ".worktrees" / f"rail-item-{item_id}"
    git(repo, "worktree", "add", "-q", str(atelier), "-b", branche)
    return item_id, branche, atelier


def nouvel_item(conn, repo: Path, labels: list[str]) -> tuple[int, str]:
    """Un item de la course d'agents, avec un candidat par label."""
    item_id, branche, _ = item_sur(conn, repo, bundle(labels))
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
    pgids = [json.loads((workspace / f"c{k}" / blocks.PGID_FILE).read_text())["pgid"]
             for k in range(2)]
    table = inscrits(repo)
    ateliers = [item_wt.with_name(f"{item_wt.name}-c{k}") for k in range(2)]
    assert all(c in table for c in ateliers), f"les deux ateliers doivent courir : {table}"
    assert all(r["status"] == "running" for r in conn.execute(
        "SELECT status FROM node_run WHERE item_id = %s", (item_id,)).fetchall()), \
        "les deux candidats doivent courir encore"

    conn.execute("UPDATE work_item SET wall_deadline = now() - interval '1 hour' "
                 "WHERE id = %s", (item_id,))
    vrai_active_item = kernel.active_item
    kernel.active_item = lambda _conn: {"id": item_id}
    try:
        assert scheduler._settle_waits(conn) == 1
    finally:
        kernel.active_item = vrai_active_item
    item = conn.execute("SELECT * FROM work_item WHERE id = %s", (item_id,)).fetchone()
    assert item["state"] == "abandon", item["state"]
    assert item["terminal_at"] is not None, "wall_deadline mène au terminal"
    version = item["version"]
    classes = conn.execute(
        "SELECT status, finished_at FROM node_run WHERE item_id = %s ORDER BY id",
        (item_id,),
    ).fetchall()
    assert all(run["status"] == "superseded" for run in classes), classes
    assert all(run["finished_at"] is not None for run in classes), classes
    assert attendre(lambda: all(not groupe_vivant(pgid) for pgid in pgids)), pgids

    assert kernel.apply(
        conn, runs[0]["id"], {"outcome": "ok", "usage": {"input_tokens": 1}}
    ) == "superseded"
    stable = conn.execute("SELECT * FROM work_item WHERE id = %s", (item_id,)).fetchone()
    assert stable["version"] == version and stable["state"] == "abandon", stable

    table = inscrits(repo)
    restants = [c for c in table if c.name.startswith(f"{item_wt.name}-c")]
    assert not restants, f"worktrees de candidats survivants : {restants}"
    assert all(not c.exists() for c in ateliers), [c for c in ateliers if c.exists()]
    assert git(repo, "branch", "--list", f"{branche}-c*") == "", "branches survivantes"
    assert item_wt.is_dir() and table[item_wt] == branche, \
        "l'atelier de l'item survit : c'est le cleanup du graph qui le retire"
    print(f"6. item {item_id} : wall_deadline en pleine course → les 2 ateliers "
          "et leurs branches détruits, celui de l'item intact ✓")

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


# ------------------------------------------- l'ordre : promouvoir, puis router

# Le graph des épreuves 8 à 10 : une course de deux candidats, puis le nœud
# d'aval — celui qui, en production, lit le diff de l'atelier de l'item.
# Les candidats n'y sont pas des agents : le test réserve leurs runs, commite
# dans leurs ateliers et rend leur issue lui-même. Ce qui est mesuré est
# l'ordre entre la réduction et la réservation du nœud suivant, pas la
# vitesse d'un agent factice.
SUITE = {
    "name": "fanout-promotion",
    "entry": "travail",
    "budgets": {"escalations": 2, "wall_deadline_hours": 1},
    "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
    "nodes": {
        "travail": {
            "block": "ACT",
            "config": {"duration_s": 0, "lease_s": 300,
                       "fanout": {"variants": [{"label": "a"}, {"label": "b"}],
                                  "reduce": "first_pass"}},
            "edges": {"ok": "suite"},
        },
        "suite": {"block": "ACT", "config": {"duration_s": 0}, "edges": {"ok": "fini"}},
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

CENT = 100  # « cent réductions successives », telles quelles


def commiter(conn, item_id: int) -> tuple[list[dict], dict[int, str]]:
    """Réserve les deux candidats, ouvre leurs ateliers, et les fait commiter.

    C'est ce qu'un vrai candidat fait — `open_run`, puis un commit dans son
    atelier —, sans l'agent qui le ferait. Rend les runs et le commit de
    chacun.
    """
    runs = []
    while (run := kernel.claim(conn, item_id)) is not None:
        runs.append(run)
        assert len(runs) <= graph.FANOUT_MAX_CANDIDATES, "claim ne s'arrête plus"
    commits = {}
    for run in runs:
        k = run["candidate"]
        chemin = worktree.open_run(item_id, k)
        assert chemin is not None, f"atelier du candidat {k} non ouvert"
        (chemin / f"c{k}.txt").write_text(f"travail du candidat {k}\n")
        git(chemin, "add", "-A")
        git(chemin, "commit", "-qm", f"candidat {k}")
        commits[k] = git(chemin, "rev-parse", "HEAD")
    return runs, commits


def rendre(run_id: int, item_id: int) -> None:
    """L'issue d'un candidat, appliquée d'un autre fil et d'une autre connexion.

    C'est la place du vrai : `scheduler._execute` applique depuis le thread
    du bloc, pendant que le tick réserve depuis le sien. La course entre les
    deux est exactement ce que les épreuves 8 et 9 mesurent.
    """
    with db.connect() as conn:
        kernel.apply(conn, run_id, {"outcome": "ok"})


def reservation(conn, item_id: int) -> dict:
    """Le premier run du nœud suivant, réservé dès que l'item y entre.

    La boucle de réservation de l'ordonnanceur, et rien d'autre : aucune
    temporisation, aucune attente d'un fait extérieur. `claim` se bloque de
    lui-même sur le verrou de l'item tant que la réduction ne l'a pas rendu.
    """
    limite = time.time() + 60
    while (run := kernel.claim(conn, item_id)) is None:
        assert time.time() < limite, "le nœud suivant n'a jamais été réservé"
    return run


def clore(conn, repo: Path, item_id: int, branche: str, atelier: Path, run: dict) -> None:
    """Le tour est fini : l'item va au terminal, son atelier quitte le dépôt."""
    kernel.apply(conn, run["id"], {"outcome": "ok"})
    git(repo, "worktree", "remove", "--force", str(atelier))
    git(repo, "branch", "-D", branche)


def ordre(conn, repo: Path) -> None:
    """8. la promotion précède la réservation du nœud suivant, pas l'inverse."""
    item_id, branche, atelier = item_sur(conn, repo, SUITE)
    runs, commits = commiter(conn, item_id)
    assert len(runs) == 2, runs

    # la promotion est ralentie d'une seconde : on n'invente pas la course, on
    # l'ouvre en grand. Sur le code d'avant — promotion après la transaction —
    # le nœud suivant réservait dans cet intervalle-là, sur un atelier vide.
    vraie = worktree.promote

    def lente(item_id: int, candidate: int) -> str | None:
        time.sleep(1.0)
        return vraie(item_id, candidate)

    worktree.promote = lente
    try:
        fil = threading.Thread(target=rendre, args=(runs[0]["id"], item_id),
                               daemon=True)
        fil.start()
        suivant = reservation(conn, item_id)
        # ce que le nœud suivant voit à l'instant où il est réservé, et pas une
        # ligne plus tard : c'est tout l'objet de l'épreuve
        tete = git(repo, "rev-parse", branche)
        vu = (atelier / "c0.txt").read_text() if (atelier / "c0.txt").is_file() else None
    finally:
        worktree.promote = vraie
    fil.join(timeout=60)
    assert not fil.is_alive(), "la réduction n'est jamais revenue"

    # l'ordre en base : l'item a bougé, et le run suivant est réservé contre
    # la version que la réduction vient d'écrire
    item = conn.execute("SELECT * FROM work_item WHERE id = %s", (item_id,)).fetchone()
    passage = conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    assert item["state"] == "suite", item["state"]
    assert suivant["node"] == "suite" and suivant["candidate"] is None, suivant
    assert passage["to_state"] == "suite" and passage["outcome"] == "ok", passage
    assert suivant["expected_version"] == passage["item_version"] == item["version"], \
        (suivant["expected_version"], passage["item_version"], item["version"])

    # et le travail du gagnant était déjà là, sur la branche de l'item
    assert tete == commits[0], \
        f"la branche de l'item était sur {tete[:7]}, pas sur {commits[0][:7]}"
    assert vu == "travail du candidat 0\n", \
        f"le nœud suivant a démarré sur un atelier sans le travail du gagnant : {vu!r}"
    assert ancetre(repo, commits[0], branche), commits[0]
    print(f"8. item {item_id} : promotion faite avant la réservation du nœud "
          f"suivant — {branche} sur {tete[:7]} et c0.txt lisible à l'instant où "
          f"« suite » réserve son run (version {item['version']}) ✓")
    clore(conn, repo, item_id, branche, atelier, suivant)


def cent_reductions(conn, repo: Path) -> None:
    """9. cent courses de suite, et pas un diff vide au nœud suivant."""
    vides = []
    for tour in range(CENT):
        item_id, branche, atelier = item_sur(conn, repo, SUITE)
        runs, _ = commiter(conn, item_id)
        fil = threading.Thread(target=rendre, args=(runs[0]["id"], item_id),
                               daemon=True)
        fil.start()
        suivant = reservation(conn, item_id)
        # le diff que la porte de pertinence lit, mot pour mot, à l'instant où
        # le nœud suivant démarre
        diff = git(atelier, "diff", "--name-only", "origin/main")
        if not diff:
            vides.append((tour, item_id))
        fil.join(timeout=60)
        assert not fil.is_alive(), f"tour {tour} : la réduction n'est jamais revenue"
        clore(conn, repo, item_id, branche, atelier, suivant)
    assert not vides, f"diff vide au nœud suivant sur {len(vides)} tours : {vides}"
    print(f"9. {CENT} réductions successives : diff non vide face à origin/main au "
          "démarrage du nœud suivant, à chaque tour, sans une seule attente ✓")


def diverger(repo: Path, item_id: int, atelier: Path) -> str:
    """L'item avance de son côté : le `merge --ff-only` du gagnant est refusé."""
    (atelier / "ailleurs.txt").write_text("l'item a bougé de son côté\n")
    git(atelier, "add", "-A")
    git(atelier, "commit", "-qm", "commit de l'item")
    return "merge non-ff"


def retirer(repo: Path, item_id: int, atelier: Path) -> str:
    """L'atelier du gagnant disparaît : sa branche reste, son worktree non."""
    git(repo, "worktree", "remove", "--force", str(worktree.candidate_path(item_id, 0)))
    return "atelier du gagnant retiré"


def promotion_impossible(conn, repo: Path) -> None:
    """10. une promotion qui ne peut pas se faire est un échec de nœud."""
    for empecher in (diverger, retirer):
        item_id, branche, atelier = item_sur(conn, repo, SUITE)
        runs, commits = commiter(conn, item_id)
        dit = empecher(repo, item_id, atelier)

        statut = kernel.apply(conn, runs[0]["id"], {"outcome": "ok"})
        assert statut == "faulted", f"{dit} : le gagnant doit être fauté, pas {statut}"
        ligne = conn.execute(
            "SELECT * FROM node_run WHERE id = %s", (runs[0]["id"],)
        ).fetchone()
        assert ligne["outcome"] == "crashed", ligne["outcome"]
        assert "promotion" in ligne["result"]["summary"], ligne["result"]
        assert ligne["result"]["rendu"] == {"outcome": "ok"}, ligne["result"]

        # l'item n'est pas entré dans le nœud suivant, et son atelier n'a rien reçu
        item = conn.execute("SELECT * FROM work_item WHERE id = %s",
                            (item_id,)).fetchone()
        assert item["state"] == "travail", item["state"]
        assert not conn.execute(
            "SELECT 1 FROM event WHERE item_id = %s AND to_state = 'suite'",
            (item_id,)).fetchone(), "l'item a avancé malgré la promotion ratée"
        assert not conn.execute(
            "SELECT 1 FROM node_run WHERE item_id = %s AND node = 'suite'",
            (item_id,)).fetchone(), "un run du nœud suivant a été réservé"
        assert not ancetre(repo, commits[0], branche), "le travail a fui sur la branche"

        # et le travail du gagnant est encore là : on ne range pas ce qu'on
        # n'a pas pu promouvoir
        assert git(repo, "branch", "--list", f"{branche}-c0") != "", \
            "la branche du gagnant a été détruite avec son travail dedans"
        print(f"10. item {item_id} : {dit} → run fauté « {ligne['result']['summary']} », "
              f"item resté sur « {item['state']} », branche du gagnant intacte ✓")

        worktree.discard(item_id)
        git(repo, "worktree", "remove", "--force", str(atelier))
        git(repo, "branch", "-D", branche)


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
            ordre(conn, repo)
            cent_reductions(conn, repo)
            promotion_impossible(conn, repo)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nisolation git du fan-out : OK — un atelier par candidat, "
          "le gagnant promu avant que l'item n'avance, et rien qui traîne derrière")


if __name__ == "__main__":
    main()
