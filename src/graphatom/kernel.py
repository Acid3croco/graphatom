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

La promotion du gagnant n'est pas un ménage d'après-coup : c'est une
conséquence de la décision, et elle est dans la même transaction qu'elle.
La réduction élit, promeut, et **seulement ensuite** route l'item — le tout
sous le verrou de l'item, celui-là même que `claim` prend. Le premier run
du nœud suivant ne peut donc pas être réservé avant que le travail du
gagnant soit sur la branche de l'item : l'ordre est garanti, pas probable.
C'est la même propriété que « un résultat qui arrive après la décision ne
la change jamais » — la décision et ses conséquences forment un tout.

Et quand la promotion est impossible — merge non-ff, atelier disparu, git
en erreur —, le nœud échoue, nommément : l'item ne part pas en avant sur un
atelier vide en croyant que tout va bien. Voir `_promouvoir` et `_faute`.

Deux réductions, et elles ne coûtent pas la même chose. `first_pass` est
monotone : le premier succès tranche, personne n'attend. `keep_n` tranche
dès que n candidats ont réussi et laisse passer ces finalistes au lieu d'un
gagnant — le noyau ne choisit alors rien entre eux, ni ne range leurs
ateliers : c'est le nœud d'aval, un JUDGE arbitre, qui élit et qui range.
Voir `blocks.judge`.
"""

import datetime as dt
import json

from collections import Counter
from collections.abc import Callable

import psycopg

from . import worktree
from .blocks import (AGENT_ALIVE, AGENT_DEAD, agent_state, lease_autopsy,
                     retry_signature, revoke_orphan, write_failure_trace)
from .graph import KERNEL_OUTCOMES, GraphError, fanout_variants, load_bundle

LEASE_SECONDS = 30
# la marge du reaper sur le couperet de l'agent : le process est coupé
# AVANT le bail, sinon le reaper révoquerait un agent encore vivant qui
# continue d'écrire dans un worktree dont l'item a été repris
AGENT_LEASE_MARGIN_S = 60
MAX_ATTEMPTS = 3  # défaut central, par passage : réessayer, puis escalader
# « GRAPHATO » en ASCII, dans la plage signée de l'advisory lock PostgreSQL.
# Toutes les voies d'admission nominales prennent la même clé : deux canaux
# concurrents ne peuvent pas voir la place libre puis ouvrir deux items.
ADMISSION_LOCK = 0x475241504841544F


class LaneOccupied(RuntimeError):
    """L'admission a trouvé l'unique voie occupée par un autre item."""

    def __init__(self, item_id: int, state: str):
        self.item_id = item_id
        self.state = state
        super().__init__(f"voie occupée par l'item {item_id} ({state})")


def agent_timeout_s(config: dict, default: float) -> float:
    """Le couperet du process d'un nœud, dérivé de son bail.

    Un `timeout_s` encore porté par le nœud est honoré — l'exception reste
    visible, elle n'est plus la norme. Sans lui, le couperet descend du bail
    (`lease_s`) moins `AGENT_LEASE_MARGIN_S` ; un nœud sans bail garde
    `default`, le 570 s historique quand rien n'est dit.
    """
    execution = config.get("execution") or {}
    if "timeout_s" in execution:
        return float(execution["timeout_s"])
    agent = config.get("agent") or {}
    if "timeout_s" in agent:
        return float(agent["timeout_s"])
    lease = config.get("lease_s")
    if lease is not None:
        return float(lease) - AGENT_LEASE_MARGIN_S
    return default

# Les runs que *ce processus-ci* a réservés et qui volent encore. En mémoire,
# donc vide au démarrage : un worker qui vient de naître ne reconnaît aucun
# des runs `running` qu'il trouve en base, et c'est exactement l'information
# qui manquait au faucheur — un run qu'il n'a pas réservé a été emporté par
# la mort du worker d'avant, son agent n'y est pour rien. Voir `reap`.
# Le tick le remplit, les threads des blocs le vident : un set suffit, `add`
# et `discard` ne se marchent pas dessus.
CLAIMED: set[int] = set()

UTC = dt.timezone.utc


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


# ---------------------------------------------------------------- admission


def active_item(conn: psycopg.Connection) -> dict | None:
    """L'unique item non terminal du rail, ou None quand la voie est libre."""
    return conn.execute(
        "SELECT id, state FROM work_item WHERE terminal_at IS NULL "
        "ORDER BY id LIMIT 1"
    ).fetchone()


def admit(conn: psycopg.Connection, revision: str, subject_key: str,
          title: str | None = None,
          prepare: Callable[[int], None] | None = None, *,
          _allow_parallel_for_test: bool = False) -> int:
    """Crée (ou retrouve) le sujet, ouvre une occurrence — si la lignée le permet.

    Le titre est celui que le canal a sous la main au moment de l'admission :
    il est stocké là, une fois, et personne n'ira le rechercher ailleurs. Un
    sujet sans titre — un autre canal, un autre format — reste sans titre.

    Par défaut, un seul item non terminal existe dans tout le rail. Le verrou
    de transaction ferme la course entre deux canaux d'admission.
    `_allow_parallel_for_test` n'est qu'un outil de banc d'essai pour les
    tests explicites des plafonds multi-items ; le nom privé et explicite
    interdit de le confondre avec une option de production.

    `prepare`, s'il existe, prépare les ressources locales du nouvel item
    avant le commit. Une erreur annule ainsi l'admission au lieu de laisser
    un item admis sans les ressources dont son premier nœud a besoin.
    """
    with conn.transaction():
        if not _allow_parallel_for_test:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADMISSION_LOCK,))
            if active := active_item(conn):
                raise LaneOccupied(active["id"], active["state"])
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
        if prepare:
            prepare(item["id"])
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

    Le run réservé est noté dans `CLAIMED`, la mémoire du processus : c'est
    ce qui dira au faucheur, plus tard, si ce run est le sien.
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
        CLAIMED.add(run["id"])  # ce run est le nôtre, tant qu'on vit
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

        run = {**run, "outcome": outcome, "result": submitted}
        if outcome in ("crashed", "invalid_result"):
            signature = retry_signature(item["id"], run, outcome)
            if signature is not None:
                submitted = {**submitted, "_retry_signature": signature}
                run["result"] = submitted
        conn.execute(
            "UPDATE node_run SET status = 'applied', outcome = %s, result = %s, "
            "finished_at = %s WHERE id = %s",
            (outcome, json.dumps(submitted), now(), run_id),
        )
        # Le routage écrit la trace d'échec dans cette même transaction. Le
        # run relu avant la mise à jour ne porte pas encore son résultat : on
        # lui donne donc le rendu exact que la base vient d'enregistrer.
        revoques, ranger = _settle(conn, item, bundle, run, outcome, kind="result")
        # `keep_n` recale les réussites au-delà de la n-ième, et une promotion
        # impossible faute le gagnant : ce run-ci peut être l'un des deux, et
        # rendre « applied » mentirait à l'appelant
        statut = conn.execute(
            "SELECT status FROM node_run WHERE id = %s", (run_id,)
        ).fetchone()["status"]
    # rendu et classé : ce run ne vole plus, il sort de la mémoire du
    # processus. Ici et pas à l'entrée : un `apply` qui échoue — le thread
    # d'un bloc qui perd la base — laisse le run en vol sous ce worker-ci,
    # qui n'a pas redémarré. Le faucheur doit y lire « bail expiré », pas un
    # redémarrage. Les trois rejets plus haut laissent leur numéro derrière
    # eux : leur run est déjà classé, le faucheur ne le reverra jamais.
    CLAIMED.discard(run_id)
    _menage(item["id"], revoques, ranger)
    return statut


def apply_item(
    conn: psycopg.Connection,
    item_id: int,
    outcome: str,
    kind: str,
    *,
    before_route: Callable[[], object] | None = None,
) -> None:
    """Transition sans run : réponse humaine, échéance de WAIT, wall_deadline.

    Cette entrée doit ouvrir la transaction de plus haut niveau. L'appelant
    peut lui donner `before_route` pour y joindre une mutation connexe, par
    exemple fermer la question d'un WAIT. Le ménage ne commence ainsi
    qu'après la validation de l'ensemble par PostgreSQL.

    Rien ici n'est une réduction : l'item quitte son nœud sans qu'aucun
    candidat ait gagné. Ceux qui couraient encore n'ont donc plus rien à
    garder — leurs ateliers sont détruits, comme après une réduction. C'est
    ce qui tient la promesse sur *tous* les chemins terminaux, et pas
    seulement quand la course va jusqu'au bout.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("apply_item exige une connexion hors transaction")

    with conn.transaction():
        item = conn.execute(
            "SELECT * FROM work_item WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        if before_route is not None:
            before_route()
        if item["terminal_at"] is not None:
            return
        revoked = conn.execute(
            "UPDATE node_run SET status = 'superseded', finished_at = %s "
            "WHERE item_id = %s AND status = 'running' RETURNING id",
            (now(), item_id),
        ).fetchall()
        bundle = load_bundle(conn, item["revision"])
        run = {"node": item["state"], "id": None, "attempt": 0}
        _route(conn, item, bundle, run, outcome, kind=kind)
    run_ids = [row["id"] for row in revoked]
    for run_id in run_ids:
        CLAIMED.discard(run_id)
    _menage(item_id, run_ids, True)


def _settle(conn, item, bundle, run, outcome: str, kind: str) -> tuple[list[int], bool]:
    """L'issue d'un run devient — ou non — celle du nœud. Rend le ménage d'après.

    Sans fan-out, c'est direct et c'est tout : l'issue du run est celle du
    nœud, et il n'y a aucun atelier de candidat en jeu.

    Avec, trois gestes dans cet ordre, et l'ordre *est* le contrat : la
    réduction élit, la promotion porte le travail de l'élu sur la branche de
    l'item, le routage fait avancer l'item. Le tout dans la transaction de
    l'appelant, donc sous le verrou de l'item — celui que `claim` prend
    aussi : le nœud suivant ne peut rien réserver avant que la promotion soit
    faite et la transaction close.

    Rend ce qui reste à faire une fois la transaction close, et rien d'autre :
    les runs à tuer, et s'il faut ranger les ateliers. Voir `_menage`.
    """
    if run["candidate"] is None:
        _route(conn, item, bundle, run, outcome, kind=kind)
        return [], False

    fanout = bundle["nodes"][run["node"]]["config"]["fanout"]
    verdict = _reduce(conn, item, run, outcome, fanout)
    if verdict is None:
        return [], False  # la course continue : personne n'a tranché

    elu, issue, revoques = verdict
    # `keep_n` n'élit pas un gagnant, elle laisse passer des finalistes : ni
    # promotion ni rangement ici, c'est le nœud arbitre d'aval qui promeut
    # l'élu et range le reste. Ranger ici détruirait ce qu'on fait juger.
    if fanout["reduce"] == "keep_n":
        _route(conn, item, bundle, elu, issue, kind=kind)
        return revoques, False

    empechement = _promouvoir(item, elu, issue)
    if empechement is not None:
        _faute(conn, elu, empechement)
        _route(conn, item, bundle, elu, "crashed", kind=kind)
        return revoques, False  # les ateliers restent : le travail est dedans
    _route(conn, item, bundle, elu, issue, kind=kind)
    return revoques, True


def _reduce(conn, item, run, outcome: str, fanout: dict) -> tuple | None:
    """La réduction du nœud, celle que le bundle a déclarée.

    Rend l'issue du nœud sous la forme d'un triplet — le run qui la porte,
    l'issue, et les candidats révoqués —, ou None tant que la course n'a rien
    tranché. Elle élit, elle ne route pas : ce que le noyau fait de son
    verdict est l'affaire de `_settle`.

    Un succès est une issue que le nœud a déclarée par une arête ; une issue
    du noyau — `crashed`, `timed_out`, `stalled`, `invalid_result` — est un
    échec. La distinction est mécanique, elle ne demande aucun modèle : le
    nœud a nommé son vocabulaire, le noyau nomme ses ratés. Les deux
    réductions la lisent pareil.

    Le prédicat s'évalue sous le verrou de l'item et ne dépend que des runs
    terminés : un résultat qui arrive après la décision ne la change jamais.
    """
    if fanout["reduce"] == "first_pass":
        return _first_pass(conn, item, run, outcome)
    if fanout["reduce"] == "keep_n":
        return _keep_n(conn, item, run, fanout["n"])
    # la publication ne laisse rien passer d'autre
    raise GraphError(f"réduction inconnue à l'exécution : {fanout['reduce']}")


def _first_pass(conn, item, run, outcome: str) -> tuple | None:
    """Le premier candidat qui réussit gagne, les autres meurent.

    La réduction est monotone : elle décide dès qu'un candidat réussit, sans
    attendre le plus lent — c'est la seule qui n'introduit aucune attente, et
    c'est pour ça qu'elle vient en premier. Un échec, lui, n'emporte rien
    tout seul : il faut que la course entière soit finie.

    Tous en échec : le nœud prend l'issue la plus fréquente, et `_settle` la
    traite comme celle d'un nœud ordinaire — les arêtes d'échec et le compte
    des tentatives ne changent pas d'un pouce.
    """
    if outcome not in KERNEL_OUTCOMES:  # un succès : la course s'arrête là
        return run, outcome, _revoke_losers(conn, item, run)

    batch = _batch(conn, item, run)
    if any(r["status"] == "running" for r in batch):
        return None  # la course continue : un candidat en échec n'emporte rien
    perdant = _majoritaire([r for r in batch if r["outcome"]])
    return perdant, perdant["outcome"], []


def _keep_n(conn, item, run, n: int) -> tuple | None:
    """Les n premiers candidats qui ont réussi passent en aval, ensemble.

    La décision est monotone : dès que n réussites existent, aucun candidat
    encore en vol ne peut entrer dans les n premiers. Ceux-ci restent
    `applied` — ce sont les finalistes, et c'est à ce statut que le nœud
    arbitre les reconnaîtra. Les réussites suivantes et les candidats encore
    en vol deviennent `superseded` ; ces derniers sont rendus à `_menage`
    pour que leurs groupes de processus soient tués hors transaction.

    Avant n réussites, la course continue tant qu'un candidat vole. Cela
    préserve le traitement des échecs : si le lot complet compte moins de n
    réussites, elles passent toutes ; sans réussite, l'issue du noyau
    majoritaire est rendue exactement comme sous `first_pass`.

    L'item, lui, avance sur une seule arête : celle de l'issue majoritaire des
    finalistes. Le choix entre eux n'appartient pas au noyau — c'est tout
    l'objet du nœud d'aval.
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
        revoques = _revoke_losers(conn, item, finaliste)
        return finaliste, finaliste["outcome"], revoques

    if any(r["status"] == "running" for r in batch):
        return None  # un autre candidat peut encore compléter les finalistes

    if not reussis:
        perdant = _majoritaire([r for r in batch if r["outcome"]])
        return perdant, perdant["outcome"], []

    # Le lot est fini sans avoir produit n réussites. Elles restent toutes
    # finalistes, comme avant la règle de décision anticipée.
    finaliste = _majoritaire(reussis)
    return finaliste, finaliste["outcome"], []


def _batch(conn, item, run) -> list[dict]:
    """Les runs de la tentative, triés par date de fin — la course entière.

    L'ordre est celui qui départage : le premier terminé tranche une égalité
    d'issues, et c'est lui aussi que `keep_n` garde en premier.
    """
    return conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s AND node = %s AND cycle = %s "
        "AND attempt = %s ORDER BY finished_at, id",
        (item["id"], run["node"], run["cycle"], run["attempt"]),
    ).fetchall()


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


def _promouvoir(item, elu, issue: str) -> str | None:
    """Le travail de l'élu rejoint la branche de l'item. Rend l'empêchement.

    Sous le verrou de l'item, et c'est là tout l'enjeu : `claim` prend ce
    même verrou, donc le premier run du nœud suivant ne peut être réservé
    qu'une fois le merge fait et la transaction close. Le prix est un `git
    merge --ff-only` local — quelques millisecondes, plafonnées par
    `worktree.GIT_TIMEOUT_S` — tenu sous une ligne verrouillée. Le prix de
    l'ordre inverse était un item sur deux qui repartait sur un atelier vide.

    Une course que personne n'a gagnée ne promeut rien : le travail d'un
    candidat en échec n'a aucune raison de devenir celui de l'item.

    Rend None quand c'est fait — ou qu'il n'y avait rien à faire, un rail
    sans dépôt ni atelier n'ayant aucun commit à porter. Rend sinon la phrase
    qui dit ce qui l'empêche. Voir `worktree.promote`.
    """
    if issue in KERNEL_OUTCOMES:
        return None
    return worktree.promote(item["id"], elu["candidate"])


def _faute(conn, elu, empechement: str) -> None:
    """Le gagnant qu'on n'a pas pu promouvoir : un échec de nœud, nommé.

    Une promotion ratée est un échec du nœud, pas un succès suivi d'une porte
    d'aval qui se plaint d'un diff vide. Le run de l'élu est donc reclassé
    `faulted`, son issue devient `crashed`, et son résumé dit ce qui a
    bloqué ; ce qu'il avait rendu reste là, sous `rendu`. Le nœud se rejoue
    alors sur place comme après n'importe quelle panne, puis escalade — et
    les ateliers des candidats ne sont pas rangés, puisque le travail y est
    encore.
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


def _menage(item_id: int, orphelins: list[int], ranger: bool) -> None:
    """Ce qui suit la transaction : les orphelins tués, les ateliers rangés.

    Hors transaction, toujours : ni la grâce d'un SIGTERM ni un `git worktree
    remove` n'ont à tenir le verrou d'un item. Perdre ce ménage — un worker
    tué juste après le commit — ne coûte que des ateliers en trop, jamais du
    travail : la promotion, elle, est de l'autre côté de la barrière.
    """
    for orphelin in orphelins:
        revoke_orphan(item_id, orphelin)
    if ranger:
        worktree.discard(item_id)


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


def _retry_batch(conn, item: dict, run: dict, attempt: int) -> dict | None:
    """Rend candidat → (run, signature) pour un lot entièrement comparable."""
    rows = conn.execute(
        "SELECT id, candidate, result FROM node_run WHERE item_id = %s "
        "AND node = %s AND cycle = %s AND attempt = %s ORDER BY candidate",
        (item["id"], run["node"], run["cycle"], attempt),
    ).fetchall()
    batch = {}
    for row in rows:
        signature = (row.get("result") or {}).get("_retry_signature")
        if row.get("candidate") is None or not signature:
            return None
        batch[row["candidate"]] = (row["id"], signature)
    return batch or None


def _repeated_failure(conn, item: dict, run: dict, outcome: str) -> int | None:
    """Rend le run antérieur si toute la tentative répète le même échec.

    Un nœud simple compare son run précédent. Un fan-out compare les lots
    complets, candidat par candidat : le progrès d'un seul candidat suffit à
    conserver la relance du lot entier.
    """
    result = run.get("result") or {}
    signature = result.get("_retry_signature")
    if outcome not in ("crashed", "invalid_result") or not signature:
        return None
    if run.get("candidate") is None:
        previous = conn.execute(
            "SELECT id, result FROM node_run WHERE item_id = %s AND node = %s "
            "AND cycle = %s AND candidate IS NULL AND attempt < %s "
            "ORDER BY attempt DESC LIMIT 1",
            (item["id"], run["node"], run["cycle"], run["attempt"]),
        ).fetchone()
        if (previous and (previous.get("result") or {}).get("_retry_signature")
                == signature):
            return previous["id"]
        return None

    previous = conn.execute(
        "SELECT max(attempt) AS attempt FROM node_run WHERE item_id = %s "
        "AND node = %s AND cycle = %s AND attempt < %s",
        (item["id"], run["node"], run["cycle"], run["attempt"]),
    ).fetchone()
    previous_attempt = previous["attempt"] if previous else None
    if previous_attempt is None:
        return None
    current_batch = _retry_batch(conn, item, run, run["attempt"])
    previous_batch = _retry_batch(conn, item, run, previous_attempt)
    if (current_batch is None or previous_batch is None
            or set(current_batch) != set(previous_batch)):
        return None
    if any(current_batch[candidate][1] != previous_batch[candidate][1]
           for candidate in current_batch):
        return None
    return previous_batch[run["candidate"]][0]


def _route(conn, item, bundle, run, outcome: str, kind: str) -> None:
    """Résout l'arête, débite l'escalade, ouvre le passage, bouge l'item.

    Toujours appelé sous verrou de l'item, dans la transaction de l'appelant.
    """
    nodes = bundle["nodes"]
    node = nodes[run["node"]]
    on_kernel = bundle["on_kernel"]

    # Seul un run porte un journal et un rapport. Réponses humaines et
    # échéances passent aussi ici, mais n'inventent pas une tentative. La
    # trace se prend avant une éventuelle conversion en `budget_exhausted` :
    # elle dit l'issue que le nœud a réellement rendue.
    result = run.get("result") or {}
    repeated = (_repeated_failure(conn, item, run, outcome)
                if run.get("id") is not None else None)
    if repeated is not None:
        message = (f"relance identique supprimée — runs {repeated} "
                   f"et {run['id']}")
        result = {**result, "retry": message}
        run = {**run, "result": result}
        conn.execute("UPDATE node_run SET result = %s WHERE id = %s",
                     (json.dumps(result), run["id"]))

    if run.get("id") is not None:
        write_failure_trace(item["id"], run, outcome)

    if outcome in (node.get("edges") or {}):
        target = node["edges"][outcome]
    elif outcome in ("timed_out", "starved"):
        # Un dépassement et un fournisseur affamé ne sont pas des pannes
        # transitoires. La relance à l'identique retomberait au même endroit :
        # hors budget pour l'un, sans crédits pour l'autre. Escalade tout de
        # suite, quel que soit le compteur de tentatives.
        target = on_kernel["escalate_to"]
    elif outcome in ("crashed", "invalid_result", "stalled"):
        # défaut central : réessayer sur place, puis escalader. `stalled` est
        # de cette famille — un agent pendu n'a rien produit, donc rien
        # brûlé : la relance est la seule chose qui l'ait jamais sauvé.
        if repeated is None and run.get("attempt", 0) < MAX_ATTEMPTS:
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
    """Runs au bail expiré ou certainement orphelins : classe et route.

    La révocation a deux moitiés : l'autorité en base, et le processus. Un
    agent lancé par un worker mort ne peut plus rien appliquer, mais il
    travaille encore — le pgid laissé dans le workspace le tue.

    Ce même pgid tranche l'issue, mécaniquement et sans modèle : un groupe
    encore vivant au bout du bail, c'est un agent qui déborde de son budget
    — `timed_out`, escalade directe ; un groupe déjà mort, c'est une panne
    — `crashed`, retry sur place comme avant.

    `CLAIMED` tranche, lui, le post-mortem : un run que ce worker n'a pas
    réservé vient d'un worker qui n'est plus là — un déploiement, une
    migration, un SIGKILL. L'issue ne change pas, la cause probable si :
    lire « bail expiré, agent déjà mort » ferait chercher un agent instable
    là où il n'y a qu'un processus qui a redémarré.

    Avant le bail, seul un run étranger dont l'identité est certainement
    morte passe. Une trace absente ou indécidable garde le bail. La sonde est
    refaite sous les verrous : une observation périmée ne classe aucun run.
    """
    running = conn.execute(
        "SELECT id, item_id, lease_expires_at FROM node_run WHERE status = 'running'"
    ).fetchall()
    suspects = [row for row in running
                if row["lease_expires_at"] < now()
                or (row["id"] not in CLAIMED
                    and agent_state(row["item_id"], row["id"]) == AGENT_DEAD)]
    reaped = 0
    for row in suspects:
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
            state = agent_state(item["id"], run["id"])
            expired = run["lease_expires_at"] < now()
            if not expired and (run["id"] in CLAIMED or state != AGENT_DEAD):
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
            # post-mortem se lit dans le résultat du run comme pour un bloc.
            # Un run qui n'est pas dans CLAIMED n'a pas été réservé par ce
            # worker-ci : il a survécu à celui d'avant, qui l'a emporté.
            orphaned = run["id"] not in CLAIMED
            CLAIMED.discard(run["id"])
            post = lease_autopsy(item["id"], run,
                                 state == AGENT_ALIVE, orphaned)
            run = {**run, "outcome": post["outcome"], "result": post}
            if post["outcome"] == "crashed":
                signature = retry_signature(item["id"], run, post["outcome"])
                if signature is not None:
                    post = {**post, "_retry_signature": signature}
                    run["result"] = post
            conn.execute(
                "UPDATE node_run SET status = 'faulted', outcome = %s, result = %s, "
                "finished_at = %s WHERE id = %s",
                (post["outcome"], json.dumps(post), now(), run["id"]),
            )
            bundle = load_bundle(conn, item["revision"])
            revoques, ranger = _settle(conn, item, bundle, run, post["outcome"],
                                       kind="reaped")
        _menage(item["id"], [run["id"], *revoques], ranger)
        reaped += 1
    return reaped
