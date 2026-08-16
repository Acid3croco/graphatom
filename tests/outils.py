"""Petits outils partagés entre les scripts de tests/.

Chaque script se lance seul, `uv run python tests/<nom>.py` — c'est ce qui
met `tests/` en tête de `sys.path` et rend `import outils` possible depuis
n'importe lequel d'entre eux. Ce module ne rassemble que ce qui était
recopié à l'identique d'un script à l'autre ; ce qui diffère réellement
(comme `depot`, propre au dépôt jetable de chaque test) reste où il est.
"""

import atexit
import os
import subprocess
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
