"""Petits outils partagés entre les scripts de tests/.

Chaque script se lance seul, `uv run python tests/<nom>.py` — c'est ce qui
met `tests/` en tête de `sys.path` et rend `import outils` possible depuis
n'importe lequel d'entre eux. Ce module ne rassemble que ce qui était
recopié à l'identique d'un script à l'autre : les graphs d'épreuve et
leurs nœuds communs, le dépôt git jetable, la réservation d'une tentative.
Ce qui diffère réellement d'un test à l'autre reste où il est.
"""

import atexit
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def provision_postgres(prefix: str = "graphatom-test") -> str:
    """Un Postgres jetable dans un conteneur au nom et au port uniques.

    Le conteneur meurt avec le test (atexit). Chaque script qui a besoin
    d'une base destructible se la provisionne ainsi : aucun test ne touche
    plus jamais la base que GRAPHATOM_DSN désigne dans l'environnement.
    À appeler AVANT d'importer graphatom — db.DSN se fige à l'import.
    """
    name = f"{prefix}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-e", "POSTGRES_USER=graphatom", "-e", "POSTGRES_PASSWORD=graphatom",
         "-e", "POSTGRES_DB=graphatom", "-p", "127.0.0.1:0:5432",
         "postgres:17"],
        check=True, capture_output=True,
    )
    atexit.register(subprocess.run, ["docker", "rm", "-f", name],
                    capture_output=True)
    port = None
    for _ in range(100):
        out = subprocess.run(["docker", "port", name, "5432/tcp"],
                             capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            port = out.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "graphatom"],
                capture_output=True)
            if ready.returncode == 0:
                break
        time.sleep(0.3)
    else:
        raise SystemExit("ÉCHEC : le Postgres jetable ne démarre pas")
    dsn = f"postgresql://graphatom:graphatom@127.0.0.1:{port}/graphatom"
    # pg_isready répond dans le conteneur avant que le serveur n'écoute
    # vraiment côté hôte : l'init de l'image redémarre postgres une fois
    import psycopg
    for _ in range(100):
        try:
            psycopg.connect(dsn).close()
            return dsn
        except psycopg.OperationalError:
            time.sleep(0.3)
    raise SystemExit("ÉCHEC : le Postgres jetable ne répond pas côté hôte")


def git(cwd: Path, *args: str) -> str:
    """Une commande git jouée dans `cwd`, sortie standard rendue nette."""
    out = subprocess.run(["git", "-C", str(cwd), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def sh(*args: str) -> str:
    """La CLI `graphatom` du projet, jouée depuis la racine du dépôt."""
    out = subprocess.run(
        ["uv", "run", "graphatom", *args],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def kill_group(proc: subprocess.Popen, sig: int) -> None:
    """Envoie un signal à tout le groupe de processus, attend qu'il sorte."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        pass
    proc.wait()


def etat(conn, item_id: int) -> dict:
    """L'état courant de l'item, tel que la table `work_item` le porte."""
    return conn.execute(
        "SELECT * FROM work_item WHERE id = %s", (item_id,)
    ).fetchone()


def attendre(predicat, seconds: float = 30.0) -> bool:
    """Attend qu'un fait devienne vrai — une course n'est pas synchrone."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicat():
            return True
        time.sleep(0.05)
    return predicat()


# ------------------------------------------------- les graphs des épreuves
#
# Tous les graphs d'épreuve finissent de la même façon : un WAIT d'escalade
# qui renvoie sur `travail`, et deux terminaux. Ce qui change d'un test à
# l'autre, ce sont les nœuds de devant — le builder les prend tels quels.


def noeuds_escalade(owner: str = "test", deadline_minutes: int = 60) -> dict:
    """Le WAIT d'escalade et les deux terminaux communs à tous les graphs."""
    return {
        "escalate": {
            "block": "WAIT", "escalade": True,
            "config": {"question": "On retente ?", "options": ["retry"],
                       "owner": owner, "deadline_minutes": deadline_minutes},
            "edges": {"retry": "travail", "expired": "abandon"},
        },
        "fini": {"terminal": True},
        "abandon": {"terminal": True},
    }


def bundle_epreuve(name: str, nodes: dict, *, entry: str = "travail",
                   escalations: int = 2, wall_deadline_hours: int = 1,
                   owner: str = "test", deadline_minutes: int = 60) -> dict:
    """Un graph d'épreuve : les nœuds du test, puis l'escalade et ses terminaux."""
    return {
        "name": name,
        "entry": entry,
        "budgets": {"escalations": escalations,
                    "wall_deadline_hours": wall_deadline_hours},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {**nodes, **noeuds_escalade(owner, deadline_minutes)},
    }


def fanout_stub(k: int, keep: int | None = None) -> dict:
    """Un fan-out de K variantes muettes : `first_pass`, ou `keep_n` à `keep`."""
    fanout = {"variants": [{"label": f"v{i}"} for i in range(k)],
              "reduce": "first_pass"}
    if keep is not None:
        fanout |= {"reduce": "keep_n", "n": keep}
    return fanout


def bundle_stub(k: int | None, keep: int | None = None) -> dict:
    """Un ACT muet en fan-out de K candidats — ou sans fan-out du tout.

    `k` à None, c'est le nœud d'avant le fan-out — il sert la non-régression.
    `keep` choisit la réduction : `first_pass` sans lui, `keep_n` avec.
    """
    config: dict = {"duration_s": 0}
    if k is not None:
        config["fanout"] = fanout_stub(k, keep)
    return bundle_epreuve(f"fanout-stub-{k}-{keep}", {
        "travail": {"block": "ACT", "config": config, "edges": {"ok": "fini"}},
    })


# la vitrine du fan-out : la variante lente hérite du modèle du nœud, la
# rapide le surcharge — le pari que le fan-out sert à trancher
FANOUT_VITRINE = {
    "variants": [
        {"label": "opus", "strategy": "raisonne longtemps"},
        {"label": "haiku", "strategy": "va droit au but",
         "agent": {"model": "haiku"}},
    ],
    "repeat": 2,
    "reduce": "first_pass",
}


# ------------------------------------------------------- items, runs, fils
#
# Ces outils importent graphatom à l'appel, jamais à l'import : `db.DSN` se
# fige quand graphatom se charge, et un test qui provisionne son Postgres le
# fait après avoir importé ce module.


def admettre(conn, spec: dict, prefixe: str) -> int:
    """Publie le graph et admet un item dessus, au sujet unique, hors file."""
    from graphatom import graph, kernel
    rev = graph.publish(conn, spec)
    return kernel.admit(
        conn, rev, f"{prefixe}:{uuid.uuid4().hex[:8]}", _allow_parallel_for_test=True
    )


def reserver_tout(conn, item_id: int) -> list[dict]:
    """Réserve la tentative entière — comme l'ordonnanceur, candidat par candidat."""
    from graphatom import graph, kernel
    runs = []
    while (run := kernel.claim(conn, item_id)) is not None:
        runs.append(run)
        assert len(runs) <= graph.FANOUT_MAX_CANDIDATES, "claim ne s'arrête plus"
    return runs


def executer(runs: list[dict], item_id: int) -> list[threading.Thread]:
    """Lance chaque run dans son fil, comme le tick — sans attendre le retour."""
    from graphatom import scheduler
    fils = [threading.Thread(target=scheduler._execute, args=(run["id"], item_id),
                             daemon=True) for run in runs]
    for fil in fils:
        fil.start()
    return fils


def rejoindre(fils: list[threading.Thread], timeout: float,
              message: str = "un candidat n'est jamais revenu") -> None:
    """Attend chaque fil ; un fil encore en vie passé le délai est un échec."""
    for fil in fils:
        fil.join(timeout=timeout)
        assert not fil.is_alive(), message


def runs_de(conn, item_id: int, node: str | None = None) -> list[dict]:
    """Les runs de l'item dans l'ordre — ceux d'un seul nœud si `node` est donné."""
    sql = "SELECT * FROM node_run WHERE item_id = %s"
    args: tuple = (item_id,)
    if node is not None:
        sql, args = sql + " AND node = %s", (item_id, node)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def evenements(conn, item_id: int) -> list[dict]:
    """Les événements de l'item, dans l'ordre de ses versions."""
    return conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version", (item_id,)
    ).fetchall()


def groupe_vivant(pgid: int) -> bool:
    """Reste-t-il quelqu'un dans ce groupe de processus ?"""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def data_dir_jetable(prefix: str) -> Path:
    """Un `DATA_DIR` à soi pour le test ; `workspace` y écrit dès maintenant."""
    from graphatom import workspace
    workspace.DATA_DIR = Path(tempfile.mkdtemp(prefix=prefix))
    return workspace.DATA_DIR


# --------------------------------------------------------- le dépôt jetable


def depot_git(tmp: Path, auteur: str) -> Path:
    """Un dépôt jetable, un commit de socle. `GRAPHATOM_REPO_DIR` y est épinglé.

    `origin/main` y est posé sur le socle : c'est la référence que la porte
    de pertinence oppose au diff d'un atelier, et que `test_backend` lit.
    """
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", f"{auteur}@test.invalid")
    git(repo, "config", "user.name", auteur)
    (repo / "socle.txt").write_text("le commit de départ\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "socle")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    os.environ["GRAPHATOM_REPO_DIR"] = str(repo)
    return repo


def atelier_item(repo: Path, item_id: int) -> tuple[str, Path]:
    """L'atelier et la branche de l'item — ce que le shell du graph prépare."""
    branche = f"rail/issue-{item_id}"
    atelier = repo / ".worktrees" / f"rail-item-{item_id}"
    git(repo, "worktree", "add", "-q", str(atelier), "-b", branche)
    return branche, atelier


def ancetre(repo: Path, sha: str, branche: str) -> bool:
    """Le commit est-il atteignable depuis la branche ?"""
    return subprocess.run(["git", "-C", str(repo), "merge-base",
                           "--is-ancestor", sha, branche]).returncode == 0


def item_neuf(conn, revision: str, prefixe: str) -> int:
    """Un item de plus sur la voie, admis en parallèle : le banc d'essai le veut."""
    from graphatom import kernel
    return kernel.admit(
        conn, revision, f"{prefixe}:{uuid.uuid4().hex[:8]}", _allow_parallel_for_test=True
    )


def route(conn, item_id: int, outcome: str, attempt: int) -> str:
    """Route une issue noyau sur le nœud courant, à un compteur donné.

    Le run est celui d'`apply_item` : un nœud, un compteur, pas de ligne en
    base — c'est bien le seul compteur qu'on veut faire varier.
    """
    from graphatom import graph, routing
    item = etat(conn, item_id)
    paquet = graph.load_bundle(conn, item["revision"])
    run = {"node": item["state"], "id": None, "attempt": attempt}
    with conn.transaction():
        routing.route(conn, item, paquet, run, outcome, kind="result")
    return etat(conn, item_id)["state"]
