"""Le contexte d'un bloc : ce que le noyau lui donne, et rien de plus.

Un bloc reçoit un contexte et retourne {"outcome": ...} plus ce qu'il veut.
Il ne touche jamais la base : le noyau réserve avant, applique après.
"""

import time

import psycopg

from .workspace import run_workspace
from .worktree import run_worktree


class Context:
    def __init__(self, conn: psycopg.Connection, run: dict, item: dict,
                 node: dict, bundle: dict):
        self.conn = conn
        self.run = run
        self.item = item
        self.node = node
        self.bundle = bundle
        self.config = node.get("config") or {}
        # ce qu'un bloc ajoute au prompt de son nœud, tel quel : posé après
        # l'interpolation, il n'est ni rempli ni expansé — un diff qui porte
        # un `$HOME` reste un diff, il ne devient pas un chemin
        self.extra = ""
        self.workspace = run_workspace(item["id"], run)
        self.workspace.mkdir(parents=True, exist_ok=True)
        # l'atelier suit le bureau : celui du candidat, ouvert ici s'il ne
        # l'est pas encore, ou celui de l'item — que le shell du graph prépare
        self.worktree = run_worktree(item["id"], run)

    def simulate_work(self) -> None:
        time.sleep(float(self.config.get("duration_s", 0)))
