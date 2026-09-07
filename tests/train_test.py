"""La porte du train : le noyau fonctionnel, et ses cas de panne, en un script.

C'est LE test du rail — la métrique unique du repo. Il prouve, sur une base
Postgres jetable qu'il provisionne lui-même, les comportements que le README
promet :

  partie 1 — les transitions (noyau seul, aucun processus) :
    1. avancement nominal, journal contigu v1..vN
    2. issue inconnue (« le nœud rend n'importe quoi ») → invalid_result,
       retries sur place, puis escalade
    3. crash → retry sur place, avec sa marge de tentatives
    4. timed_out / starved → escalade immédiate ; stalled → retry sur place
    5. résultat tardif → superseded, l'état de l'item ne bouge pas
    6. le budget d'escalades ne se régénère jamais → budget_exhausted
    7. voie unique : deux admissions → un seul item ; deux claims → un seul run
    8. passerelle d'effets : même clé logique, une seule intention

  partie 2 — les processus réels (« le nœud ne rend rien ») :
    9. commande qui rend une issue valide → l'item avance
   10. outcome.json illisible → crashed, retry
   11. issue hors contrat → invalid_result
   12. commande pendue et muette → couperet → stalled, retry sur place
   13. commande pendue après avoir produit → couperet → timed_out, escalade
   14. bail expiré, agent déjà mort → faucheur → crashed
   15. bail expiré, agent encore vivant → faucheur → timed_out, et le
       groupe de processus est bien révoqué
   16. le même échec, à l'identique, une seconde fois → escalade anticipée,
       sans brûler le reste de la marge

  partie 3 — le train entier (ordonnanceur réel, blocs stub) :
   17. admission → FETCH → ACT → JUDGE → WAIT → réponse → terminal
   18. échéance de question dépassée → expired
   19. wall_deadline dépassée → exhausted_to

Hermétique : aucun réseau, aucun agent LLM, aucun GitHub. Postgres vient
d'un conteneur docker jetable — GRAPHATOM_DSN de l'environnement n'est
jamais touché ; seule une base explicitement donnée par GRAPHATOM_TRAIN_DSN
est réutilisée. Les workspaces vont dans un répertoire temporaire.

Usage : uv run python tests/train_test.py
"""

import subprocess
import threading
import time

# train_outils épingle GRAPHATOM_DSN sur la base jetable : il passe avant graphatom
from train_outils import (  # noqa: E402
    BUNDLE_TRAIN, admet, attendre, avance, bail_expire, bundle_commande,
    bundle_kernel, execute_bloc, issue_du_run, journal, journal_contigu,
    libere_la_voie, termine, sujet, tick_jusqu_a,
)
from outils import etat  # noqa: E402

from graphatom import channel, db, effects, graph, kernel  # noqa: E402
from graphatom import process, reaper, workspace as bureau  # noqa: E402
from graphatom.workspace import PGID_FILE  # noqa: E402
from graphatom.policy import MAX_ATTEMPTS  # noqa: E402


# ------------------------------------------------- partie 1 : les transitions

def part1_nominal(conn) -> None:
    item_id = admet(conn, bundle_kernel())
    item = avance(conn, item_id, {"outcome": "ok", "summary": "fait"})
    assert item["state"] == "fini" and item["terminal_at"] is not None, item
    evts = journal(conn, item_id)
    assert [e["kind"] for e in evts] == ["admitted", "result"], evts
    journal_contigu(conn, item_id)
    print(" 1. avancement nominal, journal contigu ✓")


def part1_issue_inconnue(conn) -> None:
    item_id = admet(conn, bundle_kernel())
    for i in range(1, MAX_ATTEMPTS + 1):
        run = kernel.claim(conn, item_id)
        assert run["attempt"] == i, (run["attempt"], i)
        # du n'importe quoi différent à chaque fois : pas d'échec « répété »
        kernel.apply(conn, run["id"],
                     {"outcome": "banana", "summary": f"verbiage {i}"})
    outcomes = [r["outcome"] for r in conn.execute(
        "SELECT outcome FROM node_run WHERE item_id = %s ORDER BY id",
        (item_id,)).fetchall()]
    assert outcomes == ["invalid_result"] * MAX_ATTEMPTS, outcomes
    assert etat(conn, item_id)["state"] == "escalate"
    termine(conn, item_id)
    print(f" 2. issue inconnue → invalid_result ×{MAX_ATTEMPTS}, "
          "puis escalade ✓")


def part1_crash_et_repetition(conn) -> None:
    # un crash isolé se retente sur place
    item_id = admet(conn, bundle_kernel())
    item = avance(conn, item_id, {"outcome": "crashed", "error": "boum 1"})
    assert item["state"] == "travail", item["state"]
    run = kernel.claim(conn, item_id)
    assert run["attempt"] == 2, run["attempt"]
    kernel.apply(conn, run["id"], {"outcome": "ok"})
    print(" 3. crash → retry sur place, tentative 2 ✓")


def part1_couperets(conn) -> None:
    # timed_out escalade dès la première tentative
    item_id = admet(conn, bundle_kernel())
    item = avance(conn, item_id, {"outcome": "timed_out"})
    assert item["state"] == "escalate", item["state"]
    termine(conn, item_id)

    # starved aussi : réessayer sans ressource ne rend rien
    item_id = admet(conn, bundle_kernel())
    item = avance(conn, item_id, {"outcome": "starved"})
    assert item["state"] == "escalate", item["state"]
    termine(conn, item_id)

    # stalled, lui, se retente sur place : c'est l'infra, pas la tâche
    item_id = admet(conn, bundle_kernel())
    item = avance(conn, item_id, {"outcome": "stalled"})
    assert item["state"] == "travail", item["state"]
    termine(conn, item_id)
    print(" 4. timed_out/starved → escalade immédiate ; stalled → retry ✓")


def part1_resultat_tardif(conn) -> None:
    item_id = admet(conn, bundle_kernel())
    run = kernel.claim(conn, item_id)
    # l'item part sans lui : la transition sans run supplante les runs en vol
    kernel.apply_item(conn, item_id, "ok", kind="deadline")
    evts_avant = len(journal(conn, item_id))
    statut = kernel.apply(conn, run["id"], {"outcome": "ok", "summary": "tard"})
    assert statut == "superseded", statut
    assert len(journal(conn, item_id)) == evts_avant, "un tardif a routé"
    ligne = issue_du_run(conn, run["id"])
    assert ligne["status"] == "superseded"
    assert ligne["result"] is not None, "le résultat tardif n'est pas archivé"
    print(" 5. résultat tardif → superseded, archivé, sans effet ✓")


def part1_budget(conn) -> None:
    item_id = admet(conn, bundle_kernel(escalations=1))
    # première escalade : le budget passe à 0
    item = avance(conn, item_id, {"outcome": "timed_out"})
    assert item["state"] == "escalate" and item["escalations"] == 0, item
    # la réponse humaine rouvre un passage, sans rendre de budget
    kernel.apply_item(conn, item_id, "retry", kind="answer")
    item = etat(conn, item_id)
    assert item["cycle"] == 2 and item["escalations"] == 0, item
    # nouvelle escalade demandée, budget épuisé : terminaison structurelle
    item = avance(conn, item_id, {"outcome": "timed_out"})
    assert item["state"] == "abandon", item["state"]
    assert journal(conn, item_id)[-1]["outcome"] == "budget_exhausted"
    journal_contigu(conn, item_id)
    print(" 6. budget d'escalades épuisé → budget_exhausted → abandon ✓")


def part1_voie_unique(conn) -> None:
    item_id = admet(conn, bundle_kernel())
    rev = graph.publish(conn, bundle_kernel())
    try:
        kernel.admit(conn, rev, sujet())
        raise AssertionError("la voie occupée a admis un second item")
    except kernel.LaneOccupied:
        pass

    # deux claims concurrents : un seul run réservé
    resultats = []
    def _claim():
        with db.connect() as c:
            resultats.append(kernel.claim(c, item_id))
    fils = [threading.Thread(target=_claim) for _ in range(2)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    reserves = [r for r in resultats if r is not None]
    assert len(reserves) == 1, resultats
    en_vol = conn.execute(
        "SELECT count(*) AS n FROM node_run WHERE item_id = %s "
        "AND status = 'running'", (item_id,)).fetchone()["n"]
    assert en_vol == 1, en_vol
    kernel.apply(conn, reserves[0]["id"], {"outcome": "ok"})
    print(" 7. voie unique : 1 item, 1 run — jamais deux ✓")


def part1_effets(conn) -> None:
    item_id = admet(conn, bundle_kernel())
    run = kernel.claim(conn, item_id)
    op1 = effects.intend(conn, item_id, run["id"], "cle:unique",
                         "stub://train", {"geste": "premier"})
    op2 = effects.intend(conn, item_id, run["id"], "cle:unique",
                         "stub://train", {"geste": "second"})
    assert op1["op_id"] == op2["op_id"], "l'intention a été redite"
    assert op2["intent"]["geste"] == "premier", "l'intention a été écrasée"
    effects.mark_applied(conn, op_id=op1["op_id"])
    op3 = effects.intend(conn, item_id, run["id"], "cle:unique",
                         "stub://train", {"geste": "troisième"})
    assert op3["observation"] == "applied"
    kernel.apply(conn, run["id"], {"outcome": "ok"})
    print(" 8. effet : une clé logique, une intention, un seul geste ✓")


# ------------------------------------------- partie 2 : les processus réels

def part2_commande_ok(conn) -> None:
    item_id = admet(conn, bundle_commande(
        "printf '{\"outcome\": \"ok\", \"summary\": \"fait\"}' > outcome.json"))
    run = kernel.claim(conn, item_id)
    execute_bloc(conn, item_id, run)
    assert etat(conn, item_id)["state"] == "fini"
    print(" 9. commande réelle, issue valide → l'item avance ✓")


def part2_outcome_illisible(conn) -> None:
    item_id = admet(conn, bundle_commande("printf '{\"broken' > outcome.json"))
    run = kernel.claim(conn, item_id)
    execute_bloc(conn, item_id, run)
    ligne = issue_du_run(conn, run["id"])
    assert ligne["outcome"] == "crashed", ligne
    assert etat(conn, item_id)["state"] == "travail"
    termine(conn, item_id)
    print("10. outcome.json illisible → crashed, retry sur place ✓")


def part2_issue_hors_contrat(conn) -> None:
    item_id = admet(conn, bundle_commande(
        "printf '{\"outcome\": \"banana\", \"summary\": \"?\"}' > outcome.json"))
    run = kernel.claim(conn, item_id)
    execute_bloc(conn, item_id, run)
    ligne = issue_du_run(conn, run["id"])
    assert ligne["outcome"] == "invalid_result", ligne
    termine(conn, item_id)
    print("11. issue hors contrat → invalid_result ✓")


def part2_pendu_muet(conn) -> None:
    item_id = admet(conn, bundle_commande("sleep 30", timeout_s=6, silence_s=1))
    run = kernel.claim(conn, item_id)
    debut = time.monotonic()
    execute_bloc(conn, item_id, run)
    duree = time.monotonic() - debut
    ligne = issue_du_run(conn, run["id"])
    assert ligne["outcome"] == "stalled", ligne
    assert duree < 15, f"le couperet a trop tardé : {duree:.1f}s"
    assert etat(conn, item_id)["state"] == "travail"
    termine(conn, item_id)
    print("12. commande pendue et muette → couperet → stalled, retry ✓")


def part2_pendu_apres_travail(conn) -> None:
    item_id = admet(conn, bundle_commande(
        "echo je-travaille; sleep 30", timeout_s=6, silence_s=1))
    run = kernel.claim(conn, item_id)
    execute_bloc(conn, item_id, run)
    ligne = issue_du_run(conn, run["id"])
    assert ligne["outcome"] == "timed_out", ligne
    assert etat(conn, item_id)["state"] == "escalate"
    termine(conn, item_id)
    print("13. pendu après avoir produit → timed_out, escalade ✓")


def part2_faucheur_mort(conn) -> None:
    item_id = admet(conn, bundle_commande("true", lease_s=1))
    run = kernel.claim(conn, item_id)
    attendre(lambda: etat(conn, item_id) and bail_expire(conn, run["id"]), 10)
    fauches = reaper.reap(conn)
    assert fauches == 1, fauches
    ligne = issue_du_run(conn, run["id"])
    assert ligne["status"] == "faulted" and ligne["outcome"] == "crashed", ligne
    assert etat(conn, item_id)["state"] == "travail", "crashed doit se retenter"
    termine(conn, item_id)
    print("14. bail expiré, agent déjà mort → faucheur → crashed ✓")


def part2_faucheur_vivant(conn) -> None:
    item_id = admet(conn, bundle_commande("true", lease_s=1))
    run = kernel.claim(conn, item_id)
    workspace = bureau.run_workspace(item_id, run)
    workspace.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    process.write_pgid(workspace / PGID_FILE, proc, "sleep 60", run["id"])
    attendre(lambda: bail_expire(conn, run["id"]), 10)
    fauches = reaper.reap(conn)
    assert fauches == 1, fauches
    ligne = issue_du_run(conn, run["id"])
    assert ligne["outcome"] == "timed_out", ligne
    assert etat(conn, item_id)["state"] == "escalate"
    # la révocation a deux moitiés : l'autorité, et le processus
    attendre(lambda: proc.poll() is not None, 10)
    termine(conn, item_id)
    print("15. bail expiré, agent vivant → timed_out, groupe révoqué ✓")


def part2_echec_identique(conn) -> None:
    item_id = admet(conn, bundle_commande("exit 3"))
    run = kernel.claim(conn, item_id)
    execute_bloc(conn, item_id, run)
    assert etat(conn, item_id)["state"] == "travail", "premier crash : retry"
    run = kernel.claim(conn, item_id)
    assert run["attempt"] == 2, run["attempt"]
    execute_bloc(conn, item_id, run)
    assert etat(conn, item_id)["state"] == "escalate", \
        "un échec identique répété doit escalader sans brûler la marge"
    termine(conn, item_id)
    print("16. le même échec deux fois → escalade anticipée ✓")


# --------------------------------------- partie 3 : le train entier, en stub

def part3_train_complet(conn) -> None:
    item_id = admet(conn, BUNDLE_TRAIN)
    tick_jusqu_a(conn, item_id, "attend")
    question = conn.execute(
        "SELECT * FROM question WHERE item_id = %s AND state = 'open'",
        (item_id,)).fetchone()
    assert question is not None, "le WAIT n'a pas armé sa question"
    channel.record_answer(conn, question["id"], "go", by="train")
    tick_jusqu_a(conn, item_id, "fini")
    journal_contigu(conn, item_id)
    chemin = [e["kind"] for e in journal(conn, item_id)]
    assert chemin[0] == "admitted" and chemin[-1] == "answer", chemin
    print("17. train entier : ingest → travail → verif → question → fini ✓")


def part3_echeance_question(conn) -> None:
    item_id = admet(conn, BUNDLE_TRAIN)
    tick_jusqu_a(conn, item_id, "attend")
    conn.execute("UPDATE question SET deadline = now() - interval '1 minute' "
                 "WHERE item_id = %s AND state = 'open'", (item_id,))
    tick_jusqu_a(conn, item_id, "abandon")
    assert journal(conn, item_id)[-1]["kind"] == "deadline"
    print("18. question sans réponse → expired → abandon ✓")


def part3_wall_deadline(conn) -> None:
    item_id = admet(conn, BUNDLE_TRAIN)
    tick_jusqu_a(conn, item_id, "attend")
    conn.execute("UPDATE work_item SET wall_deadline = now() - interval '1 minute' "
                 "WHERE id = %s", (item_id,))
    tick_jusqu_a(conn, item_id, "abandon")
    assert journal(conn, item_id)[-1]["kind"] == "wall"
    journal_contigu(conn, item_id)
    print("19. wall_deadline dépassée → abandon, journal contigu ✓")


PARTIES = (
    ("— partie 1 : les transitions —", (
        part1_nominal, part1_issue_inconnue, part1_crash_et_repetition,
        part1_couperets, part1_resultat_tardif, part1_budget,
        part1_voie_unique, part1_effets)),
    ("— partie 2 : les processus réels —", (
        part2_commande_ok, part2_outcome_illisible, part2_issue_hors_contrat,
        part2_pendu_muet, part2_pendu_apres_travail, part2_faucheur_mort,
        part2_faucheur_vivant, part2_echec_identique)),
    ("— partie 3 : le train entier —", (
        part3_train_complet, part3_echeance_question, part3_wall_deadline)),
)


def main() -> None:
    debut = time.monotonic()
    db.init_db()
    with db.connect() as conn:
        for titre, preuves in PARTIES:
            # une base réutilisée peut garder un item d'un run précédent, et
            # chaque partie laisse les siens : la voie unique doit être libre
            # avant la première admission de la suivante
            libere_la_voie(conn)
            print(titre)
            for preuve in preuves:
                preuve(conn)

    print(f"\ntrain : OK — {time.monotonic() - debut:.1f}s")


if __name__ == "__main__":
    main()
