"""Le test des gardes du noyau : ce que l'admission, la réservation et la
transition refusent — et ce qu'un refus laisse derrière lui : rien.

Sur un Postgres jetable, noyau seul, aucun processus :

  1. un sujet n'a qu'une occurrence active : la seconde admission est
     refusée, et la lignée n'est pas débitée
  2. le budget de lignée compte les générations : la dernière passe, la
     suivante est refusée
  3. `prepare` qui échoue annule l'admission entière — ni item, ni sujet, ni
     budget débité ; `prepare` qui réussit a reçu le numéro de l'item
  4. `claim` ne réserve rien sur un WAIT, sur un item terminal ni sur un
     item inconnu — et n'écrit aucune ligne
  5. barrière poussée pendant le vol : le résultat est classé `superseded`,
     archivé, daté ; l'item ne bouge pas et son journal non plus — le run,
     dans `CLAIMED` depuis `claim`, en sort une fois classé
  6. version attendue dépassée : classé `stale`, même contrat
  7. `apply_item` sur un item terminal ne fait rien
  8. `apply_item` exige d'ouvrir la transaction de plus haut niveau
  9. la transaction d'`apply` qui n'aboutit pas laisse le run en vol — en
     base ET dans `CLAIMED`, pour que le faucheur y lise un bail expiré, pas
     un redémarrage ; le vrai `apply` le classe ensuite

Usage : uv run python tests/kernel_guards_test.py
"""

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import (bundle_epreuve, data_dir_jetable, etat,  # noqa: E402
                    evenements, provision_postgres, runs_de)

# une base à ce test, jamais celle que GRAPHATOM_DSN désigne
os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-noyau")
os.environ.pop("GRAPHATOM_REPO_DIR", None)  # le noyau seul, sans git
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

from graphatom import db, graph, kernel  # noqa: E402
from graphatom.policy import CLAIMED  # noqa: E402

from bords_outils import patche  # noqa: E402

BUNDLE = bundle_epreuve("noyau-gardes", {
    "travail": {"block": "ACT", "config": {}, "edges": {"ok": "fini"}},
})


def sujet() -> str:
    return f"noyau:{uuid.uuid4().hex[:8]}"


def admettre(conn, rev: str, cle: str, prepare=None) -> int:
    return kernel.admit(conn, rev, cle, prepare=prepare, _allow_parallel_for_test=True)


def lignee(conn, cle: str) -> dict | None:
    return conn.execute(
        "SELECT id, lineage_budget FROM subject WHERE subject_key = %s", (cle,)
    ).fetchone()


def occurrences(conn, cle: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM work_item w JOIN subject s ON s.id = w.subject_id "
        "WHERE s.subject_key = %s", (cle,)
    ).fetchone()["n"]


def terminer(conn, item_id: int) -> None:
    conn.execute("UPDATE work_item SET terminal_at = now() WHERE id = %s", (item_id,))


def refuse(action, message: str) -> None:
    """Le geste lève une RuntimeError qui porte ce message."""
    try:
        action()
    except RuntimeError as exc:
        assert message in str(exc), exc
        return
    raise AssertionError(f"pas refusé : « {message} » attendu")


def photo(conn, item_id: int) -> tuple:
    """Ce qu'une transition rejetée ne doit pas changer."""
    item = etat(conn, item_id)
    return item["state"], item["version"], item["fence"], len(evenements(conn, item_id))


def occurrence_unique(conn, rev: str) -> str:
    cle = sujet()
    admettre(conn, rev, cle)
    budget = lignee(conn, cle)["lineage_budget"]
    refuse(lambda: admettre(conn, rev, cle), "occurrence active existante")
    assert occurrences(conn, cle) == 1
    assert lignee(conn, cle)["lineage_budget"] == budget, "le refus a débité"
    print("1. un sujet, une occurrence active : refus sans débit ✓")
    return cle


def budget_de_lignee(conn, rev: str, cle: str) -> None:
    conn.execute("UPDATE work_item SET terminal_at = now() WHERE subject_id = %s",
                 (lignee(conn, cle)["id"],))
    conn.execute("UPDATE subject SET lineage_budget = 1 WHERE subject_key = %s", (cle,))
    derniere = admettre(conn, rev, cle)
    assert etat(conn, derniere)["generation"] == 2
    assert lignee(conn, cle)["lineage_budget"] == 0
    terminer(conn, derniere)
    refuse(lambda: admettre(conn, rev, cle), "budget de lignée épuisé")
    assert occurrences(conn, cle) == 2
    print("2. budget de lignée : la dernière génération passe, la suivante non ✓")


def preparation(conn, rev: str) -> None:
    cle = sujet()

    def panne(_item_id: int) -> None:
        raise RuntimeError("disque plein")

    refuse(lambda: admettre(conn, rev, cle, prepare=panne), "disque plein")
    assert lignee(conn, cle) is None, "le sujet a survécu à l'admission annulée"
    assert occurrences(conn, cle) == 0
    vus: list[int] = []
    item_id = admettre(conn, rev, cle, prepare=vus.append)
    assert vus == [item_id], vus
    print("3. prepare qui échoue : rien n'est admis ; sinon il a vu l'item ✓")


def rien_a_reserver(conn, rev: str) -> int:
    en_attente = admettre(conn, rev, sujet())
    run = kernel.claim(conn, en_attente)
    kernel.apply(conn, run["id"], {"outcome": "timed_out"})
    assert etat(conn, en_attente)["state"] == "escalate"
    assert kernel.claim(conn, en_attente) is None, "un WAIT a été réservé"

    fini = admettre(conn, rev, sujet())
    kernel.apply(conn, kernel.claim(conn, fini)["id"], {"outcome": "ok"})
    assert etat(conn, fini)["terminal_at"] is not None
    assert kernel.claim(conn, fini) is None, "un item terminal a été réservé"
    assert kernel.claim(conn, 10**9) is None, "un item inconnu a été réservé"
    assert len(runs_de(conn, en_attente)) == 1 and len(runs_de(conn, fini)) == 1
    print("4. claim : rien sur un WAIT, un terminal, un inconnu — aucune ligne ✓")
    return fini


def resultat_rejete(conn, rev: str, colonne: str, statut: str) -> None:
    """Le run vole, `colonne` bouge sous lui : son résultat est classé `statut`."""
    item_id = admettre(conn, rev, sujet())
    run = kernel.claim(conn, item_id)
    assert run["id"] in CLAIMED, "réservé, mais pas dans la mémoire du processus"
    conn.execute(f"UPDATE work_item SET {colonne} = {colonne} + 1 WHERE id = %s",
                 (item_id,))
    avant = photo(conn, item_id)
    assert kernel.apply(conn, run["id"], {"outcome": "ok", "summary": "tard"}) == statut
    assert run["id"] not in CLAIMED, "un run classé vole encore pour le faucheur"
    assert photo(conn, item_id) == avant, "un résultat rejeté a routé"
    ligne = runs_de(conn, item_id)[0]
    assert ligne["status"] == statut and ligne["outcome"] is None, ligne
    assert ligne["result"]["summary"] == "tard" and ligne["finished_at"] is not None


def transaction_avortee(conn, rev: str) -> None:
    """9. la transaction d'`apply` n'aboutit pas : le run reste en vol, partout."""
    item_id = admettre(conn, rev, sujet())
    run = kernel.claim(conn, item_id)
    avant = photo(conn, item_id)

    def panne(*_args):
        raise RuntimeError("commit impossible")

    with patche(kernel, "_advance", panne):
        refuse(lambda: kernel.apply(conn, run["id"], {"outcome": "ok"}), "commit impossible")
    assert run["id"] in CLAIMED, "un run non commis a quitté la mémoire du processus"
    assert runs_de(conn, item_id)[0]["status"] == "running", "classé sans commit"
    assert photo(conn, item_id) == avant, "l'item a bougé sans commit"
    assert kernel.apply(conn, run["id"], {"outcome": "ok"}) == "applied"
    assert run["id"] not in CLAIMED and etat(conn, item_id)["state"] == "fini"
    print("9. transaction avortée : le run reste en vol, en base et dans CLAIMED ; "
          "le vrai apply le classe ensuite ✓")


def transitions_sans_run(conn, fini: int) -> None:
    avant = photo(conn, fini)
    assert kernel.apply_item(conn, fini, "expired", kind="deadline") is None
    assert photo(conn, fini) == avant, "un item terminal a bougé"
    print("7. apply_item sur un item terminal : rien ✓")

    with conn.transaction():
        refuse(lambda: kernel.apply_item(conn, fini, "expired", kind="deadline"),
               "hors transaction")
    assert photo(conn, fini) == avant
    print("8. apply_item refuse une transaction déjà ouverte ✓")


def main() -> None:
    data_dir_jetable("graphatom-noyau-")
    db.init_db()
    with db.connect() as conn:
        rev = graph.publish(conn, BUNDLE)
        cle = occurrence_unique(conn, rev)
        budget_de_lignee(conn, rev, cle)
        preparation(conn, rev)
        fini = rien_a_reserver(conn, rev)
        resultat_rejete(conn, rev, "fence", "superseded")
        print("5. barrière poussée : superseded, archivé, item et journal intacts, "
              "run sorti de CLAIMED ✓")
        resultat_rejete(conn, rev, "version", "stale")
        print("6. version dépassée : stale, même contrat ✓")
        transitions_sans_run(conn, fini)
        transaction_avortee(conn, rev)
    print("\nkernel_guards : OK")


if __name__ == "__main__":
    main()
