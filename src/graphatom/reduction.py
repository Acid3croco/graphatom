"""La réduction d'un fan-out, la promotion du gagnant, et le ménage d'après.

Une tentative n'est pas toujours un run. Un nœud qui déclare un `fanout` en
réserve K et ils courent ensemble : même tentative, même barrière, un bail
et un workspace chacun. La réduction les ramène à une seule issue avant que
l'item n'avance. Rien ne fusionne : un candidat survit, les autres sont
révoqués.

Deux réductions, et elles ne coûtent pas la même chose. `first_pass` est
monotone : le premier succès tranche, personne n'attend. `keep_n` tranche
dès que n candidats ont réussi et laisse passer ces finalistes au lieu d'un
gagnant — le noyau ne choisit alors rien entre eux, ni ne range leurs
ateliers : c'est le nœud d'aval, un JUDGE arbitre, qui élit et qui range.

Un candidat a aussi son atelier git, et la révocation ne s'arrête pas à son
groupe de processus : le travail du gagnant est promu sur la branche de
l'item, et les ateliers de tous les candidats sont détruits. Quand la
promotion est impossible, le nœud échoue, nommément.
"""

import json
from collections import Counter

from . import process, worktree
from .bundle import KERNEL_OUTCOMES, GraphError
from .policy import now


def reduce(conn, item, run, outcome: str, fanout: dict) -> tuple | None:
    """La réduction du nœud, celle que le bundle a déclarée.

    Rend l'issue du nœud sous la forme d'un triplet — le run qui la porte,
    l'issue, et les candidats révoqués —, ou None tant que la course n'a rien
    tranché. Elle élit, elle ne route pas.

    Un succès est une issue que le nœud a déclarée par une arête ; une issue
    du noyau est un échec. Le prédicat s'évalue sous le verrou de l'item et
    ne dépend que des runs terminés : un résultat qui arrive après la
    décision ne la change jamais.
    """
    if fanout["reduce"] == "first_pass":
        return _first_pass(conn, item, run, outcome)
    if fanout["reduce"] == "keep_n":
        return _keep_n(conn, item, run, fanout["n"])
    # la publication ne laisse rien passer d'autre
    raise GraphError(f"réduction inconnue à l'exécution : {fanout['reduce']}")


def _first_pass(conn, item, run, outcome: str) -> tuple | None:
    """Le premier candidat qui réussit gagne, les autres meurent.

    Un échec, lui, n'emporte rien tout seul : il faut que la course entière
    soit finie. Tous en échec : le nœud prend l'issue la plus fréquente.
    """
    if outcome not in KERNEL_OUTCOMES:  # un succès : la course s'arrête là
        return run, outcome, revoke_losers(conn, item, run)
    batch = _batch(conn, item, run)
    if any(r["status"] == "running" for r in batch):
        return None  # la course continue : un candidat en échec n'emporte rien
    perdant = _majoritaire([r for r in batch if r["outcome"]])
    return perdant, perdant["outcome"], []


def _keep_n(conn, item, run, n: int) -> tuple | None:
    """Les n premiers candidats qui ont réussi passent en aval, ensemble.

    Dès que n réussites existent, aucun candidat encore en vol ne peut
    entrer dans les n premiers : ceux-ci restent `applied` — les finalistes
    —, les réussites suivantes et les candidats en vol deviennent
    `superseded`. Avant n réussites, la course continue tant qu'un candidat
    vole ; un lot fini avec moins de n réussites les passe toutes ; sans
    réussite, l'issue du noyau majoritaire est rendue comme sous `first_pass`.
    """
    batch = _batch(conn, item, run)
    reussis = [r for r in batch if r["outcome"] and r["outcome"] not in KERNEL_OUTCOMES]
    if len(reussis) >= n:
        recales = [r["id"] for r in reussis[n:]]
        if recales:
            conn.execute(
                "UPDATE node_run SET status = 'superseded' WHERE id = ANY(%s)",
                (recales,),
            )
        finaliste = _majoritaire(reussis[:n])
        return finaliste, finaliste["outcome"], revoke_losers(conn, item, finaliste)
    if any(r["status"] == "running" for r in batch):
        return None  # un autre candidat peut encore compléter les finalistes
    if not reussis:
        perdant = _majoritaire([r for r in batch if r["outcome"]])
        return perdant, perdant["outcome"], []
    finaliste = _majoritaire(reussis)
    return finaliste, finaliste["outcome"], []


def _batch(conn, item, run) -> list[dict]:
    """Les runs de la tentative, triés par date de fin — la course entière."""
    return conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s AND node = %s AND cycle = %s "
        "AND attempt = %s ORDER BY finished_at, id",
        (item["id"], run["node"], run["cycle"], run["attempt"]),
    ).fetchall()


def _majoritaire(runs: list[dict]) -> dict:
    """Le run dont l'issue est la plus fréquente ; à égalité, le premier terminé."""
    tally = Counter(r["outcome"] for r in runs)
    frequente = max(tally.values())
    return next(r for r in runs if tally[r["outcome"]] == frequente)


def revoke_losers(conn, item, winner) -> list[int]:
    """Révoque les candidats encore en vol. Rend leurs runs, à tuer après.

    L'autorité en base d'abord — les perdants sont classés sur-le-champ, et
    la barrière de l'item est poussée : ce qu'ils rendront plus tard sera
    classé, jamais routé. Leurs groupes de processus, eux, se tuent hors
    transaction : la grâce du SIGTERM ne tient pas les verrous.
    """
    losers = conn.execute(
        "UPDATE node_run SET status = 'superseded', finished_at = %s "
        "WHERE item_id = %s AND node = %s AND cycle = %s AND attempt = %s "
        "AND status = 'running' AND id <> %s RETURNING id",
        (now(), item["id"], winner["node"], winner["cycle"], winner["attempt"],
         winner["id"]),
    ).fetchall()
    conn.execute(
        "UPDATE work_item SET fence = fence + 1 WHERE id = %s", (item["id"],)
    )
    return [r["id"] for r in losers]


def promouvoir(item, elu, issue: str) -> str | None:
    """Le travail de l'élu rejoint la branche de l'item. Rend l'empêchement.

    Sous le verrou de l'item : `claim` prend ce même verrou, donc le premier
    run du nœud suivant ne peut être réservé qu'une fois le merge fait. Une
    course que personne n'a gagnée ne promeut rien. None quand c'est fait ou
    qu'il n'y avait rien à faire ; sinon la phrase qui dit ce qui l'empêche.
    """
    if issue in KERNEL_OUTCOMES:
        return None
    return worktree.promote(item["id"], elu["candidate"])


def faute(conn, elu, empechement: str) -> None:
    """Le gagnant qu'on n'a pas pu promouvoir : un échec de nœud, nommé.

    Le run de l'élu est reclassé `faulted`, son issue devient `crashed`, et
    son résumé dit ce qui a bloqué ; ce qu'il avait rendu reste là, sous
    `rendu`. Les ateliers ne sont pas rangés, puisque le travail y est encore.
    """
    rendu = conn.execute(
        "SELECT result FROM node_run WHERE id = %s", (elu["id"],)
    ).fetchone()["result"]
    post = {"outcome": "crashed",
            "summary": f"promotion du gagnant impossible — {empechement}",
            "rendu": rendu}
    conn.execute(
        "UPDATE node_run SET status = 'faulted', outcome = 'crashed', result = %s, "
        "finished_at = %s WHERE id = %s",
        (json.dumps(post), now(), elu["id"]),
    )


def menage(item_id: int, orphelins: list[int], ranger: bool) -> None:
    """Ce qui suit la transaction : les orphelins tués, les ateliers rangés.

    Hors transaction, toujours : ni la grâce d'un SIGTERM ni un `git worktree
    remove` n'ont à tenir le verrou d'un item. Perdre ce ménage ne coûte que
    des ateliers en trop, jamais du travail.
    """
    for orphelin in orphelins:
        process.revoke_orphan(item_id, orphelin)
    if ranger:
        worktree.discard(item_id)
