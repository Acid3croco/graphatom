"""Une prise de parole du rail sur une issue = un effet réconciliable.

Chaque commentaire porte une clé logique : l'intention est commise en base
avant le POST, et la cible est relue avant de parler — un crash entre le
POST et le marquage ne fait jamais deux commentaires. Ce module est sous
tous les autres modules GitHub : l'admission, les questions et le tick y
parlent, aucun ne parle ailleurs.
"""

import os

from psycopg import Connection

from . import effects
from .github_client import GitHub
from .workspace import item_workspace

ADMITTED = "-admitted"      # la fin de la clé de l'accusé : la marque, sur l'issue,
                            # qu'un item existe déjà pour elle — la découpe s'y fie
                            # pour ne pas réécrire une issue déjà partie


def issue_number(subject_key: str) -> int:
    return int(subject_key.rsplit("#", 1)[1])


def web_url() -> str:
    return os.environ.get("GRAPHATOM_WEB_URL", "http://127.0.0.1:8850")


def marker(key: str) -> str:
    return f"<!-- graphatom:{key} -->"


def pending(conn: Connection, item_id: int, target: str, key: str,
            intent: dict) -> bool:
    """Commet l'intention d'un effet ; faux si cet effet est déjà appliqué.

    Pas de run_id : une prise de parole sur une issue n'est pas rattachée à
    une tentative précise, elle l'est à l'item.
    """
    row = effects.intend(conn, item_id, None, key, target, intent)
    return row["observation"] != "applied"


def applied(conn: Connection, target: str, key: str) -> None:
    effects.mark_applied(conn, target_uri=target, logical_key=key)


def _post_unless_said(gh: GitHub, number: int, key: str, body: str) -> None:
    """Réconciliation : la cible sait-elle déjà ? (crash possible entre POST et marquage)"""
    if not any(marker(key) in c["body"] for c in gh.comments(number)):
        gh.post_comment(number, f"{marker(key)}\n{body}")
        print(f"#{number} ← {key}", flush=True)


def speak(conn: Connection, gh: GitHub, number: int, item_id: int,
          key: str, body: str) -> None:
    """Une prise de parole du rail = un effet : commis avant, réconcilié par marqueur."""
    target = f"gh:{gh.repo}#{number}"
    if not pending(conn, item_id, target, key, {"comment": body}):
        return
    _post_unless_said(gh, number, key, body)
    applied(conn, target, key)


def say_once(gh: GitHub, number: int, key: str, body: str,
             said: set[tuple[int, str]]) -> None:
    """Une prise de parole d'avant l'admission : sans item, donc sans effet.

    Le marqueur posé dans l'issue tient lieu de mémoire — c'est déjà la
    réconciliation de `speak`, sans la ligne en base. `said` évite d'aller
    relire les commentaires à chaque tick ; au redémarrage, le marqueur
    reprend le relais.
    """
    if (number, key) in said:
        return
    _post_unless_said(gh, number, key, body)
    said.add((number, key))


def gh_items(conn: Connection, gh: GitHub, where: str = "") -> list[dict]:
    """Les items dont le sujet est une issue de ce dépôt."""
    return conn.execute(
        "SELECT w.*, s.subject_key, s.graph FROM work_item w "
        "JOIN subject s ON s.id = w.subject_id "
        f"WHERE s.subject_key LIKE %s {where} ORDER BY w.id",
        (f"gh:{gh.repo}#%",),
    ).fetchall()


def cite(item_id: int, name: str, limit: int, title: str, web: str) -> str:
    """Un fichier du workspace, cité en bloc, avec le lien vers sa preview.

    Le rail ne réécrit pas ce qu'un agent a écrit : il le montre, borné aux
    premières lignes, avec de quoi lire le reste. Pas de fichier : rien du
    tout, et le texte autour reste exactement ce qu'il était.
    """
    try:
        lines = (item_workspace(item_id) / name).read_text().splitlines()
    except OSError:
        return ""
    if not lines:  # fichier vide : rien à citer, et surtout pas un bloc vide
        return ""
    quoted = [f"> {line}".rstrip() for line in lines[:limit]]
    reste = len(lines) - limit
    if reste > 0:
        quoted.append(f"> *(… {reste} lignes de plus)*")
    return (f"{title} — [{name}]({web}/item/{item_id}/file/{name})\n\n"
            + "\n".join(quoted) + "\n\n")
