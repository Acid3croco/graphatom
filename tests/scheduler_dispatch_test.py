"""Le test du dispatch : ce que le tick fait d'un bloc, et ce qu'il refuse.

Sur un Postgres jetable, blocs stub, sans thread — l'exécuteur est appelé
là où l'ordonnanceur le lancerait :

  1. un bloc qui lève ne route pas : son run est `crashed`, l'erreur avec,
     et le nœud se retente
  2. l'activation du worker ne se déclenche que sur une issue `done` qui
     porte un `deploy_sha` — le contrat, pas le nom du nœud
  3. la largeur d'un item : 1 hors fan-out, K en fan-out, 0 sur un item
     terminal, inconnu, ou posé sur un nœud que son graph ignore
  4. `solo` : vrai sur le nœud qui le déclare, faux sur un terminal ou un
     inconnu
  5. un nœud solo attend que le rail soit vide : rien n'est réservé sous lui
     tant qu'un run vole — aucune ligne, aucun bail —, puis il prend la voie
  6. les passes rendent des comptes, pas des drapeaux : un rail vide compte
     0 ; `_settle_waits` compte 1 par geste — une réponse routée, un
     `wall_deadline`, une question expirée — et le tick additionne ses
     passes : la réponse réglée plus le run parti derrière elle font 2
  7. `MAX_ACTIVE_ITEMS` vaut 1 tel que livré : de deux items sur la voie,
     un tick ne réserve que la tête
  8. `_reserve` s'arrête à la place qu'on lui donne, puis à ce que la course
     a encore à réserver — jamais au-delà
  9. le bloc part dans un fil démon : mourir n'attend aucun agent

Usage : uv run python tests/scheduler_dispatch_test.py
"""

import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import (admettre, attendre, bundle_epreuve, bundle_stub,  # noqa: E402
                    data_dir_jetable, etat, provision_postgres, route, runs_de)

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-dispatch")
os.environ.pop("GRAPHATOM_REPO_DIR", None)
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

from graphatom import db, kernel, scheduler  # noqa: E402


def acte(name: str, config: dict, edges: dict | None = None) -> dict:
    return bundle_epreuve(name, {
        "travail": {"block": "ACT", "config": config,
                    "edges": edges or {"ok": "fini"}},
    })


PLAIN = acte("dispatch-plain", {"lease_s": 600})
SOLO = acte("dispatch-solo", {"lease_s": 600, "solo": True})
DEPLOIEMENT = acte("dispatch-deploiement", {}, {"done": "fini", "ok": "fini"})
ATTENTE = acte("dispatch-attente", {"lease_s": 600},
               {"done": "escalate", "ok": "fini"})
INCONNU = 10**9


def muet(run_id: int, item_id: int) -> None:
    """L'exécuteur factice des comptes : le run vole, et ne rend rien."""


@contextmanager
def executeur(bloc):
    """Le tick lance `bloc` à la place de `_execute`, le temps du bloc `with`."""
    garde = scheduler._execute
    scheduler._execute = bloc
    try:
        yield
    finally:
        scheduler._execute = garde


def vider(conn) -> None:
    """Rend chaque run en vol par `ok` : la voie est libre, aucun item actif."""
    for run in conn.execute(
            "SELECT id FROM node_run WHERE status = 'running'").fetchall():
        kernel.apply(conn, run["id"], {"outcome": "ok"})
    assert kernel.active_item(conn) is None, "un item reste actif sur la voie"


def en_attente(conn) -> int:
    """Un item de tête arrivé sur le WAIT d'escalade par une transition —
    c'est elle qui arme la question, une admission directe n'en pose pas."""
    item_id = admettre(conn, ATTENTE, "dispatch")
    assert route(conn, item_id, "done", 1) == "escalate"
    return item_id


def repondre(conn, item_id: int) -> None:
    """La réponse humaine est arrivée : la question passe à `answered`."""
    conn.execute("UPDATE question SET state = 'answered', answer = 'retry' "
                 "WHERE item_id = %s AND state = 'open'", (item_id,))


def executer_avec(conn, spec: dict, bloc) -> tuple[int, dict]:
    """Un item sur ce graph, son run joué par `bloc` — hors thread."""
    item_id = admettre(conn, spec, "dispatch")
    run = kernel.claim(conn, item_id)
    garde = scheduler.BLOCKS["ACT"]
    scheduler.BLOCKS["ACT"] = bloc
    try:
        scheduler._execute(run["id"], item_id)
    finally:
        scheduler.BLOCKS["ACT"] = garde
    return item_id, runs_de(conn, item_id)[0]


def bloc_qui_leve(_ctx) -> dict:
    raise RuntimeError("boum du bloc")


def bloc_crashe(conn) -> None:
    item_id, ligne = executer_avec(conn, acte("dispatch-leve", {}), bloc_qui_leve)
    assert (ligne["status"], ligne["outcome"]) == ("applied", "crashed"), ligne
    assert ligne["result"]["error"] == "boum du bloc", ligne["result"]
    assert etat(conn, item_id)["state"] == "travail", "crashed se retente"
    print("1. un bloc qui lève : crashed, l'erreur avec, retry sur place ✓")


def activation_sur_contrat(conn) -> None:
    appels: list = []
    garde = scheduler.activation
    scheduler.activation = SimpleNamespace(reconcile=appels.append)
    try:
        for issue, attendu in (({"outcome": "done", "deploy_sha": "abc123"}, 1),
                               ({"outcome": "done"}, 0),
                               ({"outcome": "ok", "deploy_sha": "abc123"}, 0)):
            appels.clear()
            _, ligne = executer_avec(conn, DEPLOIEMENT, lambda _ctx, i=issue: dict(i))
            assert ligne["status"] == "applied", ligne
            assert len(appels) == attendu, (issue, appels)
    finally:
        scheduler.activation = garde
    print("2. l'activation ne suit qu'une issue done porteuse d'un deploy_sha ✓")


def largeur_et_solo(conn) -> None:
    plain = admettre(conn, PLAIN, "dispatch")
    fanout = admettre(conn, bundle_stub(3), "dispatch")
    solo = admettre(conn, SOLO, "dispatch")
    assert [scheduler._largeur(conn, i) for i in (plain, fanout, solo)] == [1, 3, 1]
    assert [scheduler._solo(conn, i) for i in (plain, fanout, solo)] == [False, False, True]

    conn.execute("UPDATE work_item SET state = 'fantome' WHERE id = %s", (plain,))
    assert scheduler._largeur(conn, plain) == 0, "un nœud inconnu demande des runs"
    assert scheduler._solo(conn, plain) is False
    print("3. largeur : 1, K, 0 sur un nœud que le graph ignore ✓")

    conn.execute("UPDATE work_item SET terminal_at = now() WHERE terminal_at IS NULL")
    assert scheduler._largeur(conn, solo) == 0 and scheduler._largeur(conn, INCONNU) == 0
    assert scheduler._solo(conn, solo) is False and scheduler._solo(conn, INCONNU) is False
    print("4. solo : vrai où déclaré ; faux sur un terminal, un inconnu ✓")


def solo_attend_la_voie(conn) -> None:
    lances: list[tuple[int, int]] = []

    def executeur_muet(run_id: int, item_id: int) -> None:
        lances.append((run_id, item_id))

    garde = scheduler._execute, scheduler.MAX_ACTIVE_ITEMS
    scheduler._execute, scheduler.MAX_ACTIVE_ITEMS = executeur_muet, 2
    try:
        en_vol = admettre(conn, PLAIN, "dispatch")
        run = kernel.claim(conn, en_vol)
        solo = admettre(conn, SOLO, "dispatch")
        assert scheduler._dispatch(conn) == 0, "le solo est parti sous un run en vol"
        assert runs_de(conn, solo) == [] and lances == [], "le solo a réservé"

        kernel.apply(conn, run["id"], {"outcome": "ok"})
        assert scheduler._dispatch(conn) == 1
        assert attendre(lambda: lances) and lances[0][1] == solo, lances
        assert runs_de(conn, solo)[0]["status"] == "running"
    finally:
        scheduler._execute, scheduler.MAX_ACTIVE_ITEMS = garde
    print("5. un solo attend le rail vide sans rien réserver, puis prend la voie ✓")


def comptes_des_passes(conn) -> None:
    """6 : chaque passe rend le nombre de gestes qu'elle a faits."""
    vider(conn)
    assert scheduler.tick(conn) == 0, "un rail vide a compté du travail"
    assert scheduler._settle_waits(conn) == 0, "aucune tête, et un geste compté"
    print("6. rail vide : le tick rend 0, _settle_waits rend 0 ✓")

    tete = en_attente(conn)
    assert scheduler._settle_waits(conn) == 0, "une question ouverte a compté"
    repondre(conn, tete)
    assert scheduler._settle_waits(conn) == 1, "une réponse routée compte 1"
    assert etat(conn, tete)["state"] == "travail", etat(conn, tete)
    conn.execute("UPDATE work_item SET wall_deadline = now() - interval '1 minute' "
                 "WHERE id = %s", (tete,))
    assert scheduler._settle_waits(conn) == 1, "un wall_deadline compte 1"
    assert etat(conn, tete)["state"] == "abandon", etat(conn, tete)
    print("   une question ouverte : 0 ; sa réponse routée : 1 ; "
          "le wall_deadline de l'item : 1 ✓")

    expiree = en_attente(conn)
    conn.execute("UPDATE question SET deadline = now() - interval '1 minute' "
                 "WHERE item_id = %s", (expiree,))
    assert scheduler._settle_waits(conn) == 1, "une question expirée compte 1"
    assert etat(conn, expiree)["state"] == "abandon", etat(conn, expiree)
    print("   une question expirée : 1, et l'item est parti par `expired` ✓")

    repondue = en_attente(conn)
    repondre(conn, repondue)
    with executeur(muet):
        assert scheduler.tick(conn) == 2, "le tick n'additionne pas ses passes"
    assert [r["status"] for r in runs_de(conn, repondue)] == ["running"]
    print("   un tick : la réponse réglée, plus le run parti derrière elle — 2 ✓")


def une_instance_a_la_fois(conn) -> None:
    """7 : la constante livrée vaut 1 — deux items sur la voie, la tête seule part."""
    vider(conn)
    assert scheduler.MAX_ACTIVE_ITEMS == 1, scheduler.MAX_ACTIVE_ITEMS
    assert scheduler.MAX_RUNS >= 2, "le plafond global bornerait à sa place"
    tete = admettre(conn, PLAIN, "dispatch")
    suivant = admettre(conn, PLAIN, "dispatch")
    with executeur(muet):
        assert scheduler.tick(conn) == 1, "un tick a réservé au-delà de la tête"
    assert [r["status"] for r in runs_de(conn, tete)] == ["running"]
    assert runs_de(conn, suivant) == [], "le second item est parti sous la tête"
    print(f"7. MAX_ACTIVE_ITEMS = {scheduler.MAX_ACTIVE_ITEMS} : la tête {tete} "
          f"vole, l'item {suivant} n'a aucune ligne ✓")


def reserve_borne_par_la_place(conn) -> None:
    """8 : `_reserve` réserve `place` runs au plus, puis ce qui reste de la course."""
    course = admettre(conn, bundle_stub(3), "dispatch")
    nes = 0
    for place, attendu in ((0, 0), (1, 1), (2, 2), (5, 0)):
        with executeur(muet):
            assert scheduler._reserve(conn, course, place) == attendu, place
        nes += attendu
        assert len(runs_de(conn, course)) == nes, (place, runs_de(conn, course))
    assert {r["status"] for r in runs_de(conn, course)} == {"running"}
    print("8. course de 3 : place 0 → 0, place 1 → 1, place 2 → 2 — les deux "
          "qui restaient —, place 5 → 0, la course est entière ✓")


def bloc_en_fil_demon(conn) -> None:
    """9 : le fil qui porte le bloc est un démon — il ne retient pas le processus."""
    porte = threading.Event()
    fils: list[threading.Thread] = []

    def executeur_bloque(run_id: int, item_id: int) -> None:
        fils.append(threading.current_thread())
        porte.wait()

    item_id = admettre(conn, PLAIN, "dispatch")
    with executeur(executeur_bloque):
        assert scheduler._reserve(conn, item_id, 1) == 1
        assert attendre(lambda: fils), "le bloc n'a jamais démarré"
    fil = fils[0]
    try:  # la porte s'ouvre quoi qu'il arrive : un fil non démon bloqué retiendrait le verdict
        assert fil in threading.enumerate() and fil.is_alive(), "le fil est déjà mort"
        assert fil.daemon is True, "un fil non démon retiendrait le processus à sa mort"
    finally:
        porte.set()
    fil.join(timeout=5)
    assert not fil.is_alive(), "le bloc rendu ne rend pas son fil"
    print("9. le bloc court dans un fil démon, encore vivant après _reserve ✓")


def main() -> None:
    data_dir_jetable("graphatom-dispatch-")
    db.init_db()
    with db.connect() as conn:
        bloc_crashe(conn)
        activation_sur_contrat(conn)
        largeur_et_solo(conn)
        solo_attend_la_voie(conn)
        comptes_des_passes(conn)
        une_instance_a_la_fois(conn)
        reserve_borne_par_la_place(conn)
        bloc_en_fil_demon(conn)
    print("\nscheduler_dispatch : OK")


if __name__ == "__main__":
    main()
