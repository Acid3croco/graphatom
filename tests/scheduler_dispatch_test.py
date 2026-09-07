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

Usage : uv run python tests/scheduler_dispatch_test.py
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import (admettre, attendre, bundle_epreuve, bundle_stub,  # noqa: E402
                    data_dir_jetable, etat, provision_postgres, runs_de)

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
INCONNU = 10**9


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


def main() -> None:
    data_dir_jetable("graphatom-dispatch-")
    db.init_db()
    with db.connect() as conn:
        bloc_crashe(conn)
        activation_sur_contrat(conn)
        largeur_et_solo(conn)
        solo_attend_la_voie(conn)
    print("\nscheduler_dispatch : OK")


if __name__ == "__main__":
    main()
