"""Le noyau : admission, réservation, et l'unique transition apply().

Le cycle de vie d'un nœud :

    item.state = N, aucun run actif
      → claim()   : réservation transactionnelle — run créé, fence bumpé,
                    version attendue épinglée, bail posé
      → le bloc s'exécute, hors transaction
      → apply()   : valide le résultat, route par l'arête déclarée,
                    version++, événement — le changement de nœud
      → item.state = M

Toute mutation d'état d'item passe par _route() sous verrou : résultat de
bloc (apply), réponse humaine ou échéance (apply_item), faucheur (reap).
Rien d'autre ne bouge un item.

Deux compteurs bornent un item, et ils ne se confondent jamais. Les
tentatives par nœud sont un amortisseur local : elles se comptent sur le
passage courant, et une réponse humaine d'escalade en ouvre un nouveau —
« nouveau cycle, pleine marge ». Le budget d'escalades de l'item, lui, ne
se régénère jamais : c'est lui, et lui seul, qui termine. Il compte les
tours de boucle, pas les traversées : la première visite d'un nœud
d'escalade dans le passage courant est gratuite, la re-entrée décompte —
un item au budget épuisé finit donc son chemin nominal, il ne peut juste
plus boucler.

Les tentatives amortissent une panne, pas un dépassement. Une tentative
`crashed`, `invalid_result` ou `stalled` a droit à sa seconde chance sur
place : l'infra retombe, une sortie malformée se rejoue, un agent pendu
repart. Une tentative `timed_out`, non — le budget a sauté sur un agent qui
travaillait vraiment, la relancer à l'identique le rebrûlerait pour retomber
au même endroit. Elle escalade tout de suite, quel que soit le compteur :
c'est l'humain qui décide de rouvrir un passage, ou d'abandonner.

Une tentative n'est pas toujours un run. Un nœud qui déclare un `fanout` en
réserve K — ses variantes, répétées — et ils courent ensemble : même
tentative, même barrière, un bail et un workspace chacun. La réduction les
ramène à une seule issue avant que l'item n'avance, et c'est cette issue-là
que `_route` traite comme celle d'un nœud ordinaire. L'item garde donc un
seul état, une seule révision, une seule issue de nœud : rien ne fusionne,
un candidat survit, les autres sont révoqués.

Un candidat a aussi son atelier git, et la révocation ne s'arrête pas à son
groupe de processus : le travail du gagnant est promu sur la branche de
l'item, et les ateliers de tous les candidats sont détruits. Sur *tous* les
chemins terminaux — la réduction, mais aussi un `wall_deadline` tombé en
pleine course. Voir `worktree`.
"""

import datetime as dt
import json

from collections import Counter

import psycopg

from . import worktree
from .blocks import agent_alive, lease_autopsy, revoke_orphan
from .graph import KERNEL_OUTCOMES, GraphError, fanout_variants, load_bundle

LEASE_SECONDS = 30
MAX_ATTEMPTS = 3  # défaut central, par passage : réessayer, puis escalader

UTC = dt.timezone.utc


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


# ---------------------------------------------------------------- admission


def admit(conn: psycopg.Connection, revision: str, subject_key: str,
          title: str | None = None) -> int:
    """Crée (ou retrouve) le sujet, ouvre une occurrence — si la lignée le permet.

    Le titre est celui que le canal a sous la main au moment de l'admission :
    il est stocké là, une fois, et personne n'ira le rechercher ailleurs. Un
    sujet sans titre — un autre canal, un autre format — reste sans titre.
    """
    with conn.transaction():
        bundle = load_bundle(conn, revision)
        subject = conn.execute(
            "INSERT INTO subject (graph, subject_key, title) VALUES (%s, %s, %s) "
            "ON CONFLICT (graph, subject_key) DO UPDATE "
            "SET title = COALESCE(EXCLUDED.title, subject.title) "
            "RETURNING id, lineage_budget",
            (bundle["name"], subject_key, title),
        ).fetchone()

        active = conn.execute(
            "SELECT id FROM work_item WHERE subject_id = %s AND terminal_at IS NULL",
            (subject["id"],),
        ).fetchone()
        if active:
            raise RuntimeError(f"occurrence active existante : item {active['id']}")

        if subject["lineage_budget"] <= 0:
            raise RuntimeError("budget de lignée épuisé — ré-admission refusée")
        conn.execute(
            "UPDATE subject SET lineage_budget = lineage_budget - 1 WHERE id = %s",
            (subject["id"],),
        )

        generation = conn.execute(
            "SELECT count(*) AS n FROM work_item WHERE subject_id = %s",
            (subject["id"],),
        ).fetchone()["n"] + 1

        budgets = bundle["budgets"]
        deadline = now() + dt.timedelta(hours=budgets["wall_deadline_hours"])
        item = conn.execute(
            "INSERT INTO work_item (subject_id, generation, revision, state, "
            "escalations, wall_deadline) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (subject["id"], generation, revision, bundle["entry"],
             budgets["escalations"], deadline),
        ).fetchone()
        conn.execute(
            "INSERT INTO event (item_id, item_version, kind, to_state) "
            "VALUES (%s, 1, 'admitted', %s)",
            (item["id"], bundle["entry"]),
        )
        conn.execute("UPDATE work_item SET version = 1 WHERE id = %s", (item["id"],))
        return item["id"]


# --------------------------------------------------------------- réservation


def claim(conn: psycopg.Connection, item_id: int) -> dict | None:
    """Réserve un run pour l'état courant de l'item. None si rien à faire.

    Un nœud en fan-out ne se réserve pas d'un bloc : `claim` rend un candidat
    à la fois, et l'ordonnanceur rappelle tant qu'il en reste. La tentative
    est celle de tous — les K candidats la partagent, avec sa barrière —, et
    chacun a son bail, son numéro et son workspace. Sans `fanout`, il n'y a
    qu'un candidat, il n'a pas de numéro, et rien de tout ceci ne se voit.
    """
    with conn.transaction():
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        if item is None or item["terminal_at"] is not None:
            return None
        bundle = load_bundle(conn, item["revision"])
        node = bundle["nodes"][item["state"]]
        if node.get("terminal") or node["block"] == "WAIT":
            return None

        # les tentatives se comptent sur le passage courant : celles qu'un
        # passage précédent a brûlées restent en base, plus au décompte
        rows = conn.execute(
            "SELECT attempt, candidate FROM node_run "
            "WHERE item_id = %s AND node = %s AND cycle = %s",
            (item_id, item["state"], item["cycle"]),
        ).fetchall()
        attempt = max((r["attempt"] for r in rows), default=0) or 1
        taken = {r["candidate"] or 0 for r in rows if r["attempt"] == attempt}
        fanout = len(fanout_variants(node))
        if len(taken) >= (fanout or 1):  # la tentative est au complet : la suivante
            attempt, taken = attempt + 1, set()

        # un run d'un autre nœud, d'un autre passage ou d'une autre tentative
        # est encore en vol : l'item n'en mène qu'une à la fois
        running = conn.execute(
            "SELECT id FROM node_run WHERE item_id = %s AND status = 'running' "
            "AND NOT (node = %s AND cycle = %s AND attempt = %s)",
            (item_id, item["state"], item["cycle"], attempt),
        ).fetchone()
        if running:
            return None

        # la barrière est celle de la tentative entière : les candidats la
        # partagent, sinon le premier réservé serait dépassé par le suivant
        fence = item["fence"] + (0 if taken else 1)
        if not taken:
            conn.execute("UPDATE work_item SET fence = %s WHERE id = %s",
                         (fence, item_id))
        candidate = next(k for k in range(fanout or 1) if k not in taken)
        lease_s = float((node.get("config") or {}).get("lease_s", LEASE_SECONDS))
        run = conn.execute(
            "INSERT INTO node_run (item_id, node, cycle, attempt, candidate, status, "
            "fence, expected_version, lease_expires_at) "
            "VALUES (%s, %s, %s, %s, %s, 'running', %s, %s, %s) RETURNING *",
            (item_id, item["state"], item["cycle"], attempt,
             candidate if fanout else None, fence, item["version"],
             now() + dt.timedelta(seconds=lease_s)),
        ).fetchone()
        return run


# ---------------------------------------------------------------- transition


def apply(conn: psycopg.Connection, run_id: int, submitted: dict) -> str:
    """L'unique transition pour un résultat de run. Retourne le statut du run.

    L'ordre des verrous est toujours le même — l'item d'abord, ses runs
    ensuite. La réduction classe les perdants sous le verrou de l'item ; un
    perdant qui rendrait au même instant en prenant les verrous dans l'autre
    sens interbloquerait la course, et K candidats la font vraiment courir.
    """
    with conn.transaction():
        owner = conn.execute(
            "SELECT item_id FROM node_run WHERE id = %s", (run_id,)
        ).fetchone()["item_id"]
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (owner,)
        ).fetchone()
        run = conn.execute(
            "SELECT * FROM node_run WHERE id = %s FOR UPDATE", (run_id,)
        ).fetchone()

        # rejets : chacun est une transition durable du run
        if run["status"] != "running":
            # révoqué avant d'avoir rendu — par la réduction, ou par le
            # faucheur : ce qu'il rapporte est classé, jamais routé. Ses
            # jetons comptent quand même dans le prix de l'étape, mais un
            # post-mortem déjà écrit reste celui de qui l'a écrit
            conn.execute(
                "UPDATE node_run SET result = %s WHERE id = %s AND result IS NULL",
                (json.dumps(submitted), run_id),
            )
            return run["status"]
        for status, bad in (("superseded", run["fence"] != item["fence"]),
                            ("stale", run["expected_version"] != item["version"])):
            if bad:
                conn.execute(
                    "UPDATE node_run SET status = %s, result = %s, finished_at = %s "
                    "WHERE id = %s",
                    (status, json.dumps(submitted), now(), run_id),
                )
                return status

        bundle = load_bundle(conn, item["revision"])
        node = bundle["nodes"][run["node"]]
        outcome = submitted.get("outcome")
        if outcome not in (node.get("edges") or {}) and outcome not in KERNEL_OUTCOMES:
            outcome = "invalid_result"

        conn.execute(
            "UPDATE node_run SET status = 'applied', outcome = %s, result = %s, "
            "finished_at = %s WHERE id = %s",
            (outcome, json.dumps(submitted), now(), run_id),
        )
        losers = _settle(conn, item, bundle, run, outcome, kind="result")
    # hors transaction : la grâce du SIGTERM ne tient pas les verrous
    for loser in losers:
        revoke_orphan(item["id"], loser)
    _ateliers(conn, item, run)
    return "applied"


def apply_item(conn: psycopg.Connection, item_id: int, outcome: str, kind: str) -> None:
    """Transition sans run : réponse humaine, échéance de WAIT, wall_deadline.

    Rien ici n'est une réduction : l'item quitte son nœud sans qu'aucun
    candidat ait gagné. Ceux qui couraient encore n'ont donc plus rien à
    garder — leurs ateliers sont détruits, comme après une réduction. C'est
    ce qui tient la promesse sur *tous* les chemins terminaux, et pas
    seulement quand la course va jusqu'au bout.
    """
    with conn.transaction():
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        if item["terminal_at"] is not None:
            return
        bundle = load_bundle(conn, item["revision"])
        run = {"node": item["state"], "id": None, "attempt": 0}
        _route(conn, item, bundle, run, outcome, kind=kind)
    worktree.discard(item_id)  # hors transaction : git ne tient pas les verrous


def _settle(conn, item, bundle, run, outcome: str, kind: str) -> list[int]:
    """L'issue d'un run devient — ou non — celle du nœud. Rend les révoqués.

    Sans fan-out, c'est direct et c'est tout : l'issue du run est celle du
    nœud. Avec, la réduction tranche d'abord, et l'item n'avance que quand
    elle a décidé.
    """
    if run["candidate"] is None:
        _route(conn, item, bundle, run, outcome, kind=kind)
        return []
    return _reduce(conn, item, bundle, run, outcome, kind)


def _reduce(conn, item, bundle, run, outcome: str, kind: str) -> list[int]:
    """`first_pass` : le premier candidat qui réussit gagne, les autres meurent.

    Un succès est une issue que le nœud a déclarée par une arête ; une issue
    du noyau — `crashed`, `timed_out`, `stalled`, `invalid_result` — est un
    échec. La distinction est mécanique, elle ne demande aucun modèle : le
    nœud a nommé son vocabulaire, le noyau nomme ses ratés.

    La réduction est monotone : elle décide dès qu'un candidat réussit, sans
    attendre le plus lent — c'est la seule qui n'introduit aucune attente, et
    c'est pour ça qu'elle vient en premier. Un échec, lui, n'emporte rien
    tout seul : il faut que la course entière soit finie.

    Tous en échec : le nœud prend l'issue la plus fréquente, et `_route` la
    traite comme celle d'un nœud ordinaire — les arêtes d'échec et le compte
    des tentatives ne changent pas d'un pouce.

    Le prédicat s'évalue sous le verrou de l'item et ne dépend que des runs
    terminés : un résultat qui arrive après la décision ne la change jamais.
    """
    reduce = bundle["nodes"][run["node"]]["config"]["fanout"]["reduce"]
    if reduce != "first_pass":  # la publication ne laisse rien passer d'autre
        raise GraphError(f"réduction inconnue à l'exécution : {reduce}")

    if outcome not in KERNEL_OUTCOMES:  # un succès : la course s'arrête là
        losers = _revoke_losers(conn, item, run)
        _route(conn, item, bundle, run, outcome, kind=kind)
        return losers

    batch = conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s AND node = %s AND cycle = %s "
        "AND attempt = %s ORDER BY finished_at, id",
        (item["id"], run["node"], run["cycle"], run["attempt"]),
    ).fetchall()
    if any(r["status"] == "running" for r in batch):
        return []  # la course continue : un candidat en échec n'emporte rien
    perdant = _majoritaire([r for r in batch if r["outcome"]])
    _route(conn, item, bundle, perdant, perdant["outcome"], kind=kind)
    return []


def _majoritaire(runs: list[dict]) -> dict:
    """Le run dont l'issue est la plus fréquente ; à égalité, le premier terminé.

    `runs` arrive trié par date de fin : le parcourir dans cet ordre, c'est
    laisser le premier terminé trancher l'égalité, sans autre arbitrage.
    """
    tally = Counter(r["outcome"] for r in runs)
    frequente = max(tally.values())
    return next(r for r in runs if tally[r["outcome"]] == frequente)


def _revoke_losers(conn, item, winner) -> list[int]:
    """Révoque les candidats encore en vol. Rend leurs runs, à tuer après.

    La révocation a deux moitiés, comme celle du faucheur : l'autorité en
    base, et le processus. Ici, l'autorité — les perdants sont classés
    sur-le-champ, et la barrière de l'item est poussée : ce qu'ils rendront
    plus tard sera classé, jamais routé. Leurs groupes de processus, eux, se
    tuent hors transaction : la grâce du SIGTERM ne tient pas les verrous.
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


def _ateliers(conn, item, run) -> None:
    """La course finie : le gagnant promu, les ateliers des candidats détruits.

    Le prédicat est celui de la réduction, lu sur les mêmes lignes : tant
    qu'un frère court, rien n'est tranché et rien ne bouge. Une fois la
    tentative close, le travail du gagnant rejoint la branche de l'item, et
    tous les ateliers de candidats disparaissent — celui du gagnant compris,
    puisqu'il n'a plus rien d'unique.

    Un nœud sans fan-out n'a pas de candidat : rien de tout ceci ne le
    concerne, et son atelier d'item reste celui du shell du graph.

    Hors transaction, toujours : un `git merge` n'a rien à faire sous le
    verrou d'un item.
    """
    if run["candidate"] is None:
        return
    batch = conn.execute(
        "SELECT candidate, status FROM node_run WHERE item_id = %s AND node = %s "
        "AND cycle = %s AND attempt = %s",
        (item["id"], run["node"], run["cycle"], run["attempt"]),
    ).fetchall()
    if any(r["status"] == "running" for r in batch):
        return
    gagnant = next((r["candidate"] for r in batch if r["status"] == "applied"), None)
    if gagnant is not None:
        worktree.promote(item["id"], gagnant)
    worktree.discard(item["id"])


def _boucle(conn, item, nodes: dict, target: str, cycle: int) -> bool:
    """L'entrée dans ce nœud d'escalade est-elle un tour de boucle ?

    Le budget compte les tours, pas les passages nominaux : la première
    visite d'un nœud d'escalade dans le passage courant est gratuite, la
    re-entrée décompte. Un WAIT d'escalade fait exception — il n'est jamais
    réservé, donc n'a aucune ligne `node_run`, et surtout chaque entrée y
    est une escalade humaine, donc un tour par définition.
    """
    if nodes[target].get("block") == "WAIT":
        return True
    return conn.execute(
        "SELECT 1 FROM node_run WHERE item_id = %s AND node = %s AND cycle = %s",
        (item["id"], target, cycle),
    ).fetchone() is not None


def _route(conn, item, bundle, run, outcome: str, kind: str) -> None:
    """Résout l'arête, débite l'escalade, ouvre le passage, bouge l'item.

    Toujours appelé sous verrou de l'item, dans la transaction de l'appelant.
    """
    nodes = bundle["nodes"]
    node = nodes[run["node"]]
    on_kernel = bundle["on_kernel"]

    if outcome in (node.get("edges") or {}):
        target = node["edges"][outcome]
    elif outcome == "timed_out":
        # un dépassement n'est pas une panne transitoire : la tâche déborde
        # du budget, et la relance à l'identique brûlerait un cycle de plus
        # pour retomber au même endroit. Escalade tout de suite, quel que
        # soit le compteur de tentatives — c'est l'humain qui tranche.
        target = on_kernel["escalate_to"]
    elif outcome in ("crashed", "invalid_result", "stalled"):
        # défaut central : réessayer sur place, puis escalader. `stalled` est
        # de cette famille — un agent pendu n'a rien produit, donc rien
        # brûlé : la relance est la seule chose qui l'ait jamais sauvé.
        if run.get("attempt", 0) < MAX_ATTEMPTS:
            target = run["node"]
        else:
            target = on_kernel["escalate_to"]
    else:  # budget_exhausted, wall_deadline
        target = on_kernel["exhausted_to"]

    # une réponse humaine sur un nœud d'escalade ouvre un passage : en aval,
    # les tentatives repartent de zéro — l'humain vient de juger qu'un cycle
    # complet valait le coup. Le budget d'escalades, lui, reste débité.
    cycle = item["cycle"] + (1 if kind == "answer" and node.get("escalade") else 0)

    # le budget ne paie que les tours de boucle : traverser un nœud
    # d'escalade pour la première fois du passage est gratuit
    if (nodes[target].get("escalade") and target != run["node"]
            and _boucle(conn, item, nodes, target, cycle)):
        if item["escalations"] <= 0:
            outcome, target = "budget_exhausted", on_kernel["exhausted_to"]
        else:
            conn.execute(
                "UPDATE work_item SET escalations = escalations - 1 WHERE id = %s",
                (item["id"],),
            )

    version = item["version"] + 1
    conn.execute(
        "INSERT INTO event (item_id, item_version, kind, from_state, to_state, "
        "outcome, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (item["id"], version, kind, item["state"], target, outcome, run["id"]),
    )
    terminal = now() if nodes[target].get("terminal") else None
    conn.execute(
        "UPDATE work_item SET state = %s, version = %s, cycle = %s, terminal_at = %s "
        "WHERE id = %s",
        (target, version, cycle, terminal, item["id"]),
    )

    # un WAIT s'arme dans la même transaction que l'entrée dans l'état :
    # sinon la réponse peut arriver avant la souscription (réveil perdu)
    if not terminal and nodes[target]["block"] == "WAIT":
        cfg = nodes[target]["config"]
        conn.execute(
            "INSERT INTO question (item_id, node, text, options, owner, deadline) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (item["id"], target, cfg["question"], json.dumps(cfg["options"]),
             cfg["owner"], now() + dt.timedelta(minutes=cfg["deadline_minutes"])),
        )


# ------------------------------------------------------------------ faucheur


def reap(conn: psycopg.Connection) -> int:
    """Runs au bail expiré : révoque (fence++ et pgid), classe, route.

    La révocation a deux moitiés : l'autorité en base, et le processus. Un
    agent lancé par un worker mort ne peut plus rien appliquer, mais il
    travaille encore — le pgid laissé dans le workspace le tue.

    Ce même pgid tranche l'issue, mécaniquement et sans modèle : un groupe
    encore vivant au bout du bail, c'est un agent qui déborde de son budget
    — `timed_out`, escalade directe ; un groupe déjà mort, c'est une panne
    — `crashed`, retry sur place comme avant.
    """
    expired = conn.execute(
        "SELECT id FROM node_run WHERE status = 'running' AND lease_expires_at < %s",
        (now(),),
    ).fetchall()
    for row in expired:
        with conn.transaction():
            # l'item d'abord, son run ensuite : l'ordre des verrous d'`apply`,
            # sinon le faucheur et un candidat qui rend s'interbloquent
            owner = conn.execute(
                "SELECT item_id FROM node_run WHERE id = %s", (row["id"],)
            ).fetchone()["item_id"]
            item = conn.execute(
                "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (owner,)
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM node_run WHERE id = %s FOR UPDATE", (row["id"],)
            ).fetchone()
            if run["status"] != "running":
                continue
            if run["candidate"] is None:
                # révocation d'autorité : un zombie ne peut plus appliquer. En
                # fan-out, la barrière est celle de la tentative entière : un
                # candidat fauché ne révoque pas ses frères, qui courent
                # toujours — seule la réduction pousse la barrière.
                conn.execute(
                    "UPDATE work_item SET fence = fence + 1 WHERE id = %s", (item["id"],)
                )
            # l'agent travaillait-il encore ? La réponse fait l'issue, et le
            # post-mortem se lit dans le résultat du run comme pour un bloc
            post = lease_autopsy(item["id"], run, agent_alive(item["id"], run["id"]))
            conn.execute(
                "UPDATE node_run SET status = 'faulted', outcome = %s, result = %s, "
                "finished_at = %s WHERE id = %s",
                (post["outcome"], json.dumps(post), now(), run["id"]),
            )
            bundle = load_bundle(conn, item["revision"])
            losers = _settle(conn, item, bundle, run, post["outcome"], kind="reaped")
        # hors transaction : la grâce du SIGTERM ne tient pas les verrous
        for orphan in (run["id"], *losers):
            revoke_orphan(item["id"], orphan)
        _ateliers(conn, item, run)
    return len(expired)
