"""La table `effect`, en commun : intention commise avant action, marquage après.

Deux appelants la touchent — `blocks.effect` (l'effet générique d'un nœud)
et `github_sync` (chaque prise de parole sur une issue) — avec chacun sa
propre borne de transaction autour du marquage final. Ce module ne porte
que ce qui est identique aux deux : l'INSERT de l'intention, sous conflit
sur `(target_uri, logical_key)`, et l'UPDATE qui la dit appliquée. La
transaction du marquage reste au choix de l'appelant, elle diffère entre
les deux.
"""

import json

from psycopg import Connection


def intend(conn: Connection, item_id: int, run_id: int | None, logical_key: str,
           target_uri: str, intent: dict) -> dict:
    """Commet l'intention d'un effet, clé logique et cible en main ; rend la ligne.

    Le conflit sur `(target_uri, logical_key)` n'écrase rien : une
    ré-exécution relit l'intention déjà commise plutôt que de la redire.
    L'INSERT est son propre point de commit — il existe avant tout accès au
    monde, même quand l'appelant travaille par ailleurs sous une transaction
    plus large.
    """
    with conn.transaction():
        conn.execute(
            "INSERT INTO effect (item_id, run_id, logical_key, target_uri, intent) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (target_uri, logical_key) DO NOTHING",
            (item_id, run_id, logical_key, target_uri, json.dumps(intent)),
        )
    return conn.execute(
        "SELECT * FROM effect WHERE target_uri = %s AND logical_key = %s",
        (target_uri, logical_key),
    ).fetchone()


def mark_applied(conn: Connection, *, op_id: int | None = None,
                  target_uri: str | None = None, logical_key: str | None = None) -> None:
    """Marque un effet appliqué — par `op_id` quand on l'a, par sa clé sinon.

    Ne borde aucune transaction : c'est à l'appelant de la border, ou pas —
    les deux appelants actuels ne le font pas de la même façon.
    """
    if op_id is not None:
        conn.execute("UPDATE effect SET observation = 'applied' WHERE op_id = %s", (op_id,))
    else:
        conn.execute(
            "UPDATE effect SET observation = 'applied' "
            "WHERE target_uri = %s AND logical_key = %s", (target_uri, logical_key),
        )
