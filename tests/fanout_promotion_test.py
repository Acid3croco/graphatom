"""Le test de la promotion du gagnant : promouvoir d'abord, router ensuite.

Le nœud d'aval d'un fan-out lit l'atelier de l'item. Si la réduction route
l'item avant d'y avoir promu le gagnant, l'aval démarre sur un atelier vide
— une course entre deux fils, qui n'existe plus et ne doit pas revenir.

Scénario, sur la base et sur un dépôt git jetable. Les candidats n'y sont
pas des agents : le test réserve leurs runs, commite dans leurs ateliers et
rend leur issue lui-même. Ce qui est mesuré est l'ordre entre la réduction
et la réservation du nœud suivant, pas la vitesse d'un agent factice :

  8. la promotion est faite **avant** que le nœud suivant ne réserve son
     premier run : l'item bouge, le run suivant est réservé contre cette
     version-là, et l'atelier de l'item porte déjà le travail du gagnant
  9. cent réductions de suite, et pas un diff vide au nœud suivant : la
     course entre la promotion et le démarrage d'aval n'existe plus
 10. une promotion impossible — merge non-ff, atelier du gagnant retiré —
     faute le run de la réduction et l'item ne passe pas au nœud suivant

L'isolation des ateliers eux-mêmes est éprouvée dans
`fanout_isolation_test.py`.

Le test ne détruit rien de la production : son dépôt est jetable, et
`GRAPHATOM_REPO_DIR` est épinglé dessus.

Usage : uv run python tests/fanout_promotion_test.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, kernel, worktree  # noqa: E402

from outils import admettre, ancetre, atelier_item, bundle_epreuve  # noqa: E402
from outils import data_dir_jetable, depot_git, etat, git  # noqa: E402
from outils import reserver_tout  # noqa: E402

# hermétisme : aucun agent ici, donc aucune base dérivée
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

# Une course de deux candidats, puis le nœud d'aval — celui qui, en
# production, lit le diff de l'atelier de l'item.
SUITE = bundle_epreuve("fanout-promotion", {
    "travail": {
        "block": "ACT",
        "config": {"duration_s": 0, "lease_s": 300,
                   "fanout": {"variants": [{"label": "a"}, {"label": "b"}],
                              "reduce": "first_pass"}},
        "edges": {"ok": "suite"},
    },
    "suite": {"block": "ACT", "config": {"duration_s": 0}, "edges": {"ok": "fini"}},
})

CENT = 100  # « cent réductions successives », telles quelles


# ------------------------------------------------------------------ outillage


def item_sur(conn, repo: Path) -> tuple[int, str, Path]:
    """Un item sur le graph d'aval, son atelier, sa branche — ce que le shell prépare."""
    item_id = admettre(conn, SUITE, "fanout-worktree")
    branche, atelier = atelier_item(repo, item_id)
    return item_id, branche, atelier


def commiter(conn, item_id: int) -> tuple[list[dict], dict[int, str]]:
    """Réserve les deux candidats, ouvre leurs ateliers, et les fait commiter.

    C'est ce qu'un vrai candidat fait — `open_run`, puis un commit dans son
    atelier —, sans l'agent qui le ferait. Rend les runs et le commit de
    chacun.
    """
    runs = reserver_tout(conn, item_id)
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


def rendre_en_fil(run_id: int, item_id: int) -> threading.Thread:
    """Le fil qui rend l'issue du gagnant, déjà parti."""
    fil = threading.Thread(target=rendre, args=(run_id, item_id), daemon=True)
    fil.start()
    return fil


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


# -------------------------------------------------------------------- épreuves


def promotion_ralentie(conn, repo: Path, item_id: int, branche: str,
                       atelier: Path, run: dict) -> tuple[dict, str, str | None]:
    """La réduction, promotion ralentie d'une seconde, pendant que l'aval réserve.

    On n'invente pas la course, on l'ouvre en grand. Sur le code d'avant —
    promotion après la transaction — le nœud suivant réservait dans cet
    intervalle-là, sur un atelier vide. Rend le run suivant, et ce qu'il
    voyait de la branche et de l'atelier à l'instant où il a été réservé.
    """
    vraie = worktree.promote

    def lente(item_id: int, candidate: int) -> str | None:
        time.sleep(1.0)
        return vraie(item_id, candidate)

    worktree.promote = lente
    try:
        fil = rendre_en_fil(run["id"], item_id)
        suivant = reservation(conn, item_id)
        # ce que le nœud suivant voit à l'instant où il est réservé, et pas une
        # ligne plus tard : c'est tout l'objet de l'épreuve
        tete = git(repo, "rev-parse", branche)
        vu = (atelier / "c0.txt").read_text() if (atelier / "c0.txt").is_file() else None
    finally:
        worktree.promote = vraie
    fil.join(timeout=60)
    assert not fil.is_alive(), "la réduction n'est jamais revenue"
    return suivant, tete, vu


def ordre(conn, repo: Path) -> None:
    """8. la promotion précède la réservation du nœud suivant, pas l'inverse."""
    item_id, branche, atelier = item_sur(conn, repo)
    runs, commits = commiter(conn, item_id)
    assert len(runs) == 2, runs
    suivant, tete, vu = promotion_ralentie(conn, repo, item_id, branche, atelier, runs[0])

    # l'ordre en base : l'item a bougé, et le run suivant est réservé contre
    # la version que la réduction vient d'écrire
    item = etat(conn, item_id)
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
        item_id, branche, atelier = item_sur(conn, repo)
        runs, _ = commiter(conn, item_id)
        fil = rendre_en_fil(runs[0]["id"], item_id)
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


def item_reste_sur_place(conn, repo: Path, item_id: int, branche: str,
                         commit: str) -> dict:
    """L'item n'est pas entré dans le nœud suivant, et son atelier n'a rien reçu."""
    item = etat(conn, item_id)
    assert item["state"] == "travail", item["state"]
    assert not conn.execute(
        "SELECT 1 FROM event WHERE item_id = %s AND to_state = 'suite'",
        (item_id,)).fetchone(), "l'item a avancé malgré la promotion ratée"
    assert not conn.execute(
        "SELECT 1 FROM node_run WHERE item_id = %s AND node = 'suite'",
        (item_id,)).fetchone(), "un run du nœud suivant a été réservé"
    assert not ancetre(repo, commit, branche), "le travail a fui sur la branche"
    return item


def promotion_impossible(conn, repo: Path) -> None:
    """10. une promotion qui ne peut pas se faire est un échec de nœud."""
    for empecher in (diverger, retirer):
        item_id, branche, atelier = item_sur(conn, repo)
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

        item = item_reste_sur_place(conn, repo, item_id, branche, commits[0])
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
    workdir = data_dir_jetable("graphatom-fanout-promo-")
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-fanout-promo-repo-"))
    repo = depot_git(tmp, "fanout")
    try:
        with db.connect() as conn:
            ordre(conn, repo)
            cent_reductions(conn, repo)
            promotion_impossible(conn, repo)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\npromotion du fan-out : OK — le gagnant promu avant que l'item "
          "n'avance, et une promotion ratée ne route rien")


if __name__ == "__main__":
    main()
