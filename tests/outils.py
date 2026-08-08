"""Petits outils partagés entre les scripts de tests/.

Chaque script se lance seul, `uv run python tests/<nom>.py` — c'est ce qui
met `tests/` en tête de `sys.path` et rend `import outils` possible depuis
n'importe lequel d'entre eux. Ce module ne rassemble que ce qui était
recopié à l'identique d'un script à l'autre ; ce qui diffère réellement
(comme `depot`, propre au dépôt jetable de chaque test) reste où il est.
"""

import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
