"""Le test du timeout : un serveur muet ne gèle plus le canal GitHub.

Scénario, sans base ni réseau — la base est une doublure, et « GitHub » est
un socket local qui accepte la connexion puis se tait pour toujours :

  1. chaque appel sortant du module porte le timeout nommé, jamais un littéral
  2. la constante gouverne l'attente : raccourcie, l'appel rend la main d'autant
  3. un tick complet contre le serveur muet rend la main bien avant 60 s
  4. et le classe en incident réseau nominal : « github injoignable : … — on
     réessaie », pas un crash — la boucle repart au tick suivant

Usage : uv run python tests/timeout_test.py
"""

import contextlib
import io
import os
import socket
import sys
import threading
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import github_sync as gs  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1] / "src" / "graphatom" / "github_sync.py"
CALLS = ("urlopen", "urlretrieve", "HTTPConnection", "create_connection")
COURT = 2.0   # le timeout de l'appel chronométré : la constante, en plus court
MARGE = 5.0   # ce qu'on tolère au-dessus : de l'ordonnancement, pas un gel


class FakeCursor:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    """La base : juste de quoi mener le tick jusqu'à son premier appel réseau."""

    def execute(self, sql: str, params: tuple = ()):
        if "FROM graph_revision" in sql:
            return FakeCursor([{"bundle": {"name": "code-task"}}])
        return FakeCursor([])


def mute_server() -> int:
    """Monte un socket local qui accepte la connexion et ne répond jamais.

    Le pire cas du réseau — pire qu'un refus, qui est immédiat et lisible :
    une porte ouverte sur le silence. Sans timeout, le client attend là
    jusqu'à la fin des temps, et personne ne voit rien.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list[socket.socket] = []  # les connexions restent ouvertes, et muettes

    def accept_forever() -> None:
        while True:
            held.append(server.accept()[0])

    threading.Thread(target=accept_forever, daemon=True).start()
    return server.getsockname()[1]


def main() -> None:
    # aucun proxy ne s'interpose : le test ne tape que son propre socket
    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(var, None)

    # 1. la constante nommée, à chaque appel sortant, et jamais un littéral
    outgoing = [line.strip() for line in SOURCE.read_text().splitlines()
                if any(f"{call}(" in line for call in CALLS)]
    assert outgoing, f"aucun appel sortant trouvé dans {SOURCE.name}"
    for line in outgoing:
        assert "timeout=TIMEOUT_S" in line, line
    print(f"1. {len(outgoing)} appel sortant, borné par TIMEOUT_S = {gs.TIMEOUT_S} s ✓")

    # 2. l'attente suit la constante : raccourcie, l'appel rend la main d'autant
    gs.API = f"http://127.0.0.1:{mute_server()}"
    gh = gs.GitHub("o/r", "jeton")
    reel, gs.TIMEOUT_S = gs.TIMEOUT_S, COURT
    start = time.monotonic()
    try:
        gh.labeled_issues()
        raise AssertionError("le serveur muet a répondu quelque chose ?")
    except (urllib.error.URLError, OSError) as exc:
        erreur = exc
    attente = time.monotonic() - start
    gs.TIMEOUT_S = reel
    assert COURT <= attente < COURT + MARGE, attente
    print(f"2. timeout à {COURT} s : appel rendu en {attente:.1f} s — {erreur} ✓")

    # 3 et 4. le tick complet : borné par la vraie constante, et classé
    sortie = io.StringIO()
    start = time.monotonic()
    with contextlib.redirect_stdout(sortie):
        gs.tick(FakeConn(), gh, "révision", {"Acid3croco"}, {}, set())
    duree = time.monotonic() - start
    ligne = sortie.getvalue().strip()
    assert gs.TIMEOUT_S - 1 <= duree < 60, duree
    print(f"3. tick rendu en {duree:.1f} s, jamais gelé (< 60 s) ✓")

    assert ligne.startswith("github injoignable : "), ligne
    assert ligne.endswith(" — on réessaie"), ligne
    print(f"4. incident réseau nominal, aucun traceback : {ligne} ✓")

    print("\ntimeout : OK — un serveur muet coûte un tick, plus l'éternité")


if __name__ == "__main__":
    main()
