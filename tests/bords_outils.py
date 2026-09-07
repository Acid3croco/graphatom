"""Les outils des preuves de bord : un bloc sans base, ses issues, ses faux.

Les preuves de bord — `agent_edges_test`, `prompt_edges_test`,
`arbitrage_test`, `gates_test` — jouent les blocs avec de petites commandes
shell et une base réduite à ses réponses. Ce module ne porte que ce qu'elles
partagent : la base simulée, le contexte d'un run, les nœuds d'épreuve et
l'écriture d'une issue depuis le shell.
"""

import contextlib
import io
import json
import shlex
from pathlib import Path

SUJET = "test:bords"
NOEUD = "travail"
BUDGET_S = 5


class FauxConn:
    """Une base réduite à ses réponses : la clé du sujet, puis ce que le test pose.

    `lignes` associe un fragment de SQL à la ligne que `fetchone` rend,
    `listes` fait de même pour `fetchall`. Une requête que rien ne nomme
    rend None, ou une liste vide — jamais une erreur : le bloc doit savoir
    travailler sans prédécesseur, sans tentative antérieure, sans finaliste.
    """

    def __init__(self, lignes: dict | None = None, listes: dict | None = None):
        self.lignes = {"FROM subject": {"subject_key": SUJET}, **(lignes or {})}
        self.listes = listes or {}
        self.sql = ""
        self.requetes: list[str] = []

    def execute(self, sql: str, *_args):
        self.sql = sql
        self.requetes.append(sql)
        return self

    def fetchone(self) -> dict | None:
        return next((ligne for motif, ligne in self.lignes.items()
                     if motif in self.sql), None)

    def fetchall(self) -> list[dict]:
        return next((rows for motif, rows in self.listes.items()
                     if motif in self.sql), [])


class ConnMuette:
    """Une base qu'on n'a pas le droit d'interroger : la porte doit se taire avant."""

    def execute(self, sql: str, *_args):
        raise AssertionError(f"requête interdite : {sql}")


def issue(outcome: str = "ok", **champs) -> str:
    """La commande shell qui écrit `outcome.json` avec l'issue demandée."""
    contrat = json.dumps({"outcome": outcome, "summary": "fait", **champs})
    return f"printf '%s' {shlex.quote(contrat)} > outcome.json"


def passation(node: str = NOEUD) -> str:
    """La commande shell qui écrit une passation valide pour le nœud."""
    return (f"printf '%s\\n' '## Fait' 'Tout.' '## Appris' 'Rien.' "
            f"'## Pas fait' 'Rien.' > passation-{node}.md")


def commande(cmd: str, **execution) -> dict:
    """Un nœud ACT qui exécute une commande déterministe : ni prompt ni passation."""
    return {"block": "ACT", "edges": {"ok": "fini"},
            "config": {"execution": {"kind": "command", "cmd": cmd,
                                     "timeout_s": BUDGET_S, "silence_s": BUDGET_S,
                                     **execution}}}


def agent(cmd: str, *, prompt: str = "fais le travail", passation: bool = False,
          **config) -> dict:
    """Un nœud ACT dont l'agent est une commande : prompt rendu, passation à la
    demande, et toute clé de config supplémentaire posée telle quelle."""
    return {"block": "ACT", "edges": {"ok": "fini", "pass": "fini"},
            "config": {"execution": {"kind": "agent", "cmd": cmd,
                                     "timeout_s": BUDGET_S, "silence_s": BUDGET_S},
                       "agent": {"prompt": prompt, "passation": passation},
                       **config}}


def contexte(spec: dict, conn=None, *, node: str = NOEUD, attempt: int = 1,
             cycle: int = 1, item_id: int = 0, candidate: int | None = None):
    """Le contexte d'un run sur `spec`, dans le `DATA_DIR` courant."""
    from graphatom import context
    run = {"id": attempt, "node": node, "cycle": cycle, "attempt": attempt}
    if candidate is not None:
        run["candidate"] = candidate
    return context.Context(conn or FauxConn(), run,
                           {"id": item_id, "subject_id": 1}, spec, {"name": "bords"})


@contextlib.contextmanager
def sortie_capturee():
    """Ce que le module imprime, pour l'affirmer plutôt que le lire."""
    tampon = io.StringIO()
    with contextlib.redirect_stdout(tampon):
        yield tampon


@contextlib.contextmanager
def patche(module, nom: str, valeur):
    """Un attribut de module remplacé le temps d'une preuve, puis remis."""
    ancien = getattr(module, nom)
    setattr(module, nom, valeur)
    try:
        yield
    finally:
        setattr(module, nom, ancien)


def attend(predicat, secondes: float = 5.0) -> bool:
    """Attend qu'un fait devienne vrai — une course n'est pas synchrone."""
    import time
    fin = time.monotonic() + secondes
    while time.monotonic() < fin:
        if predicat():
            return True
        time.sleep(0.05)
    return predicat()


def lignes(path: Path) -> list[str]:
    """Les lignes d'un fichier, ou aucune s'il n'existe pas."""
    return path.read_text().splitlines() if path.exists() else []
