"""Le test de la réduction du fan-out : ce qui arrive après la décision.

La course elle-même — de vrais candidats, leurs workspaces, leurs jetons —
est éprouvée dans `fanout_course_test.py`. Ici les candidats sont muets, et
c'est le test qui rend leurs issues : ce qui se mesure est ce que le noyau
fait d'une issue selon le moment où elle arrive.

Scénario, sur la base — claim/apply et `_settle_waits`, sans exécuteur :

  7. un résultat de perdant appliqué après la décision ne bouge ni l'état,
     ni la révision, ni le passage : il est classé, jamais routé
  7 bis. `wall_deadline` tombé en pleine course révoque tous les runs, et
     leurs résultats tardifs sont classés sans nouveau routage
  7 ter. un WAIT expire dans une seule transaction, le run résiduel avec
     lui, et le ménage se fait après le commit
  8. tous en échec : l'issue majoritaire est routée comme une issue de nœud
     ordinaire ; à égalité, celle du run terminé en premier
  9. `keep_n` : elle tranche dès que n candidats ont réussi, laisse passer
     ces finalistes et révoque les candidats encore en vol ; moins de n
     réussites et zéro réussite gardent le traitement de fin de lot

Le test ne détruit rien : il crée son sujet, ses items, et laisse la base
en place — il peut tourner à côté d'un rail vivant.

Usage : uv run python tests/fanout_reduction_test.py
"""

import os
import shutil
import sys
from pathlib import Path

from psycopg.pq import TransactionStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, kernel, policy, process, reduction, scheduler  # noqa: E402

from outils import admettre, bundle_stub, data_dir_jetable, etat  # noqa: E402
from outils import evenements, reserver_tout, runs_de  # noqa: E402

# hermétisme : sans instance jetable dans l'environnement, le bloc ne dérive
# aucune base — les candidats muets d'ici n'en ont pas besoin
os.environ.pop("GRAPHATOM_AGENT_DSN", None)


def bundle_wait() -> dict:
    """Un travail qui entre dans un WAIT, puis expire au terminal."""
    return {
        "name": "wait-expiry-stale-run",
        "entry": "travail",
        "budgets": {"escalations": 2, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "attente", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {
                "block": "ACT", "config": {"duration_s": 0},
                "edges": {"ok": "attente"},
            },
            "attente": {
                "block": "WAIT",
                "config": {
                    "question": "On continue ?", "options": ["continue"],
                    "owner": "test", "deadline_minutes": 60,
                },
                "edges": {"continue": "fini", "expired": "abandon"},
            },
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


def nouvel_item(conn, bundle: dict) -> int:
    """Un item sur ce graph, au sujet préfixé du nom du graph."""
    return admettre(conn, bundle, bundle["name"])


# -------------------------------------------------------------------- épreuves


def resultat_tardif(conn) -> None:
    """7. un résultat de perdant arrivé après la décision est classé, jamais routé."""
    item_id = nouvel_item(conn, bundle_stub(3))
    runs = reserver_tout(conn, item_id)
    assert len(runs) == 3, runs
    assert kernel.apply(conn, runs[0]["id"], {"outcome": "ok"}) == "applied"
    apres = etat(conn, item_id)
    assert apres["state"] == "fini", apres["state"]

    perdant = runs[1]
    statut = kernel.apply(conn, perdant["id"], {"outcome": "ok", "usage": {"t": 1}})
    assert statut == "superseded", statut
    encore = etat(conn, item_id)
    for cle in ("version", "state", "cycle", "fence", "terminal_at"):
        assert encore[cle] == apres[cle], f"{cle} a bougé : {apres[cle]} → {encore[cle]}"
    assert not [e for e in evenements(conn, item_id) if e["run_id"] == perdant["id"]], \
        "le perdant ne route rien"
    classe = conn.execute(
        "SELECT * FROM node_run WHERE id = %s", (perdant["id"],)
    ).fetchone()
    assert classe["status"] == "superseded", classe["status"]
    assert (classe["result"] or {}).get("usage") == {"t": 1}, \
        "ce qu'il rapporte est classé — ses jetons comptent quand même"
    print(f"7. item {item_id} : résultat tardif du run {perdant['id']} classé "
          f"« {statut} », item inchangé (version {encore['version']}, "
          f"état {encore['state']}, passage {encore['cycle']}) ✓")


def transition_sans_run(conn) -> None:
    """7 bis. Une échéance révoque toute la course avant de déplacer l'item."""
    item_id = nouvel_item(conn, bundle_stub(3))
    runs = reserver_tout(conn, item_id)
    assert all(run["id"] in policy.CLAIMED for run in runs)

    kernel.apply_item(conn, item_id, "wall_deadline", kind="wall")
    apres = etat(conn, item_id)
    assert apres["state"] == "abandon" and apres["terminal_at"] is not None, apres
    version = apres["version"]
    events = len(evenements(conn, item_id))
    classes = runs_de(conn, item_id)
    assert all(run["status"] == "superseded" for run in classes), classes
    assert all(run["finished_at"] is not None for run in classes), classes
    assert not any(run["id"] in policy.CLAIMED for run in runs)

    conn.execute(
        "UPDATE node_run SET lease_expires_at = now() - interval '1 hour' "
        "WHERE item_id = %s", (item_id,),
    )
    reapables = conn.execute(
        "SELECT id FROM node_run WHERE item_id = %s AND status = 'running' "
        "AND lease_expires_at < now()", (item_id,),
    ).fetchall()
    assert not reapables, f"le faucheur pourrait reprendre des runs révoqués : {reapables}"
    stable = etat(conn, item_id)
    assert stable["version"] == version and stable["state"] == "abandon", stable
    assert len(evenements(conn, item_id)) == events

    for run in runs:
        statut = kernel.apply(
            conn, run["id"],
            {"outcome": "ok", "usage": {"input_tokens": 1}},
        )
        assert statut == "superseded", statut
    encore = etat(conn, item_id)
    assert encore["version"] == version and encore["state"] == "abandon", encore
    assert len(evenements(conn, item_id)) == events
    assert all((run["result"] or {}).get("usage") == {"input_tokens": 1}
               for run in runs_de(conn, item_id))
    print(f"7 bis. item {item_id} : wall_deadline révoque {len(runs)} runs ; "
          "leurs résultats tardifs sont classés sans nouveau routage ✓")


def residu_en_vol(conn, item_id: int, run_id: int) -> None:
    """Reproduit le résidu observé en production : un ancien run reste en vol
    alors que l'item attend déjà une réponse — et la question est échue."""
    conn.execute(
        "UPDATE node_run SET status = 'running', outcome = NULL, result = NULL, "
        "finished_at = NULL, lease_expires_at = now() - interval '1 hour' "
        "WHERE id = %s", (run_id,),
    )
    policy.CLAIMED.add(run_id)
    conn.execute(
        "UPDATE question SET deadline = now() - interval '1 minute' "
        "WHERE item_id = %s AND state = 'open'", (item_id,),
    )


def regler_les_waits(conn, item_id: int) -> list:
    """`_settle_waits` sur cet item seul, en notant l'état de la transaction
    au moment où le ménage est appelé."""
    transactions = []
    vrai_menage = reduction.menage

    def observe(item: int, run_ids: list[int], ranger: bool) -> None:
        transactions.append(conn.info.transaction_status)
        vrai_menage(item, run_ids, ranger)

    reduction.menage = observe
    vrai_active_item = kernel.active_item
    kernel.active_item = lambda _conn: etat(conn, item_id)
    try:
        assert scheduler._settle_waits(conn) == 1
    finally:
        kernel.active_item = vrai_active_item
        reduction.menage = vrai_menage
    return transactions


def expiration_wait(conn) -> None:
    """7 ter. WAIT expire atomiquement, puis le ménage se fait hors transaction."""
    item_id = nouvel_item(conn, bundle_wait())
    run = kernel.claim(conn, item_id)
    assert run is not None
    kernel.apply(conn, run["id"], {"outcome": "ok"})
    avant = etat(conn, item_id)
    assert avant["state"] == "attente" and avant["terminal_at"] is None, avant
    events = len(evenements(conn, item_id))

    residu_en_vol(conn, item_id, run["id"])
    transactions = regler_les_waits(conn, item_id)

    apres = etat(conn, item_id)
    question = conn.execute(
        "SELECT state FROM question WHERE item_id = %s", (item_id,)
    ).fetchone()
    dernier = evenements(conn, item_id)[-1]
    classe = runs_de(conn, item_id)[0]
    assert question["state"] == "expired", question
    assert dernier["kind"] == "deadline" and dernier["outcome"] == "expired", dernier
    assert apres["state"] == "abandon" and apres["terminal_at"] is not None, apres
    assert apres["version"] == avant["version"] + 1
    assert len(evenements(conn, item_id)) == events + 1
    assert classe["status"] == "superseded" and classe["finished_at"] is not None, classe
    assert transactions == [TransactionStatus.IDLE], transactions

    version = apres["version"]
    assert kernel.apply(
        conn, run["id"], {"outcome": "ok", "usage": {"input_tokens": 1}}
    ) == "superseded"
    stable = etat(conn, item_id)
    assert stable["version"] == version and stable["state"] == "abandon", stable
    assert len(evenements(conn, item_id)) == events + 1
    print(f"7 ter. item {item_id} : WAIT expiré et run révoqué dans une seule "
          "transaction ; ménage après commit ✓")


def tous_en_echec(conn) -> None:
    """8. tous les candidats échouent : l'issue majoritaire est routée."""
    item_id = nouvel_item(conn, bundle_stub(3))
    runs = reserver_tout(conn, item_id)
    depart = etat(conn, item_id)
    for run, issue in zip(runs, ("crashed", "stalled", "crashed")):
        kernel.apply(conn, run["id"], {"outcome": issue})
        item = etat(conn, item_id)
        if run is not runs[-1]:  # un échec seul n'emporte rien : la course continue
            assert item["version"] == depart["version"], \
                "un candidat en échec ne route pas tant qu'un frère court"
    assert item["state"] == "travail", item["state"]
    assert item["version"] == depart["version"] + 1, "une seule issue de nœud"
    dernier = evenements(conn, item_id)[-1]
    assert dernier["outcome"] == "crashed", dernier["outcome"]
    print(f"8. item {item_id} : crashed ×2 contre stalled ×1 → « crashed » routé "
          "comme une issue de nœud ordinaire, retour sur `travail` ✓")


def egalite(conn) -> None:
    """8 (suite). à égalité, c'est le run terminé en premier qui tranche."""
    for premier, second in (("crashed", "stalled"), ("stalled", "crashed")):
        item_id = nouvel_item(conn, bundle_stub(2))
        runs = reserver_tout(conn, item_id)
        assert len(runs) == 2, runs
        kernel.apply(conn, runs[0]["id"], {"outcome": premier})
        kernel.apply(conn, runs[1]["id"], {"outcome": second})
        dernier = evenements(conn, item_id)[-1]
        assert dernier["outcome"] == premier, (dernier["outcome"], premier)
        print(f"   item {item_id} : {premier} et {second} à égalité → "
              f"« {premier} », celle du run terminé en premier ✓")


def deux_reussites(conn, item_id: int, runs: list[dict]) -> list[tuple[int, int]]:
    """Un succès ne suffit pas ; le deuxième tranche sans attendre les autres.

    `_menage` doit recevoir les runs des trois candidats restants pour tuer
    leurs groupes de processus hors transaction : rend ce qu'il a reçu.
    """
    depart = etat(conn, item_id)
    revoques = []
    ancien_revoke = process.revoke_orphan
    process.revoke_orphan = lambda item, run: revoques.append((item, run))
    try:
        assert kernel.apply(conn, runs[0]["id"], {"outcome": "ok"}) == "applied"
        attente = etat(conn, item_id)
        assert attente["version"] == depart["version"], attente
        assert attente["state"] == "travail", attente["state"]

        assert kernel.apply(conn, runs[1]["id"], {"outcome": "ok"}) == "applied"
    finally:
        process.revoke_orphan = ancien_revoke
    return revoques


def keep_n_tranche(conn) -> None:
    """9. `keep_n` tranche à n réussites et révoque les candidats en vol."""
    item_id = nouvel_item(conn, bundle_stub(5, keep=2))
    runs = reserver_tout(conn, item_id)
    assert len(runs) == 5, runs
    depart = etat(conn, item_id)
    revoques = deux_reussites(conn, item_id, runs)

    item = etat(conn, item_id)
    assert item["version"] == depart["version"] + 1, "une seule issue de nœud"
    assert item["state"] == "fini", item["state"]
    assert item["terminal_at"] is not None, item

    finaux = {r["candidate"]: r for r in runs_de(conn, item_id)}
    finalistes = sorted(k for k, r in finaux.items()
                        if r["status"] == "applied" and r["outcome"] == "ok")
    assert finalistes == [0, 1], f"les deux premières réussites passent : {finalistes}"
    perdants = [finaux[k] for k in (2, 3, 4)]
    assert all(r["status"] == "superseded" for r in perdants), perdants
    assert all(r["finished_at"] is not None for r in perdants), perdants
    assert sorted(revoques) == sorted((item_id, r["id"]) for r in perdants), revoques

    routages = [e for e in evenements(conn, item_id) if e["kind"] == "result"]
    assert len(routages) == 1 and routages[0]["outcome"] == "ok", routages
    assert routages[0]["run_id"] == finaux[0]["id"], routages[0]
    print(f"9. item {item_id} : deux réussites sur cinq suffisent ; c2, c3 et c4 "
          "révoqués et rendus au ménage ; item routé travail → fini ✓")


def keep_n_lot_ancien(conn) -> None:
    """9 (suite). un lot qui porte déjà plus de n réussites : seules les n premières restent.

    Un lot ancien peut déjà porter plus de n réussites au moment où cette
    règle est déployée. Une réussite finie n'est pas un processus à tuer,
    contrairement aux runs encore en vol.
    """
    item_id = nouvel_item(conn, bundle_stub(5, keep=2))
    runs = reserver_tout(conn, item_id)
    conn.execute(
        "UPDATE node_run SET status = 'applied', outcome = 'ok', finished_at = now() "
        "WHERE id = ANY(%s)", ([r["id"] for r in runs[:3]],),
    )
    ancien = etat(conn, item_id)
    with conn.transaction():
        verdict = reduction._keep_n(conn, ancien, runs[2], 2)
    finaliste, issue, a_tuer = verdict
    classes = {r["candidate"]: r for r in runs_de(conn, item_id)}
    assert finaliste["id"] == runs[0]["id"] and issue == "ok", verdict
    assert [classes[k]["status"] for k in range(5)] == [
        "applied", "applied", "superseded", "superseded", "superseded",
    ], classes
    assert sorted(a_tuer) == sorted(r["id"] for r in runs[3:]), a_tuer
    print(f"   item {item_id} : une réussite tardive de trop est classée, mais seuls "
          "les deux runs encore en vol sont rendus pour terminaison ✓")


def keep_n_moins_de_n(conn) -> None:
    """9 (suite). moins de n réussites : la course attend sa fin, puis laisse passer."""
    item_id = nouvel_item(conn, bundle_stub(3, keep=2))
    runs = reserver_tout(conn, item_id)
    depart = etat(conn, item_id)
    for index, (run, issue) in enumerate(zip(runs, ("ok", "crashed", "stalled"))):
        kernel.apply(conn, run["id"], {"outcome": issue})
        item = etat(conn, item_id)
        if index < 2:
            assert item["version"] == depart["version"], item
            assert item["state"] == "travail", item["state"]
    assert item["state"] == "fini", item["state"]
    assert item["version"] == depart["version"] + 1, item["version"]
    assert [r["candidate"] for r in runs_de(conn, item_id)
            if r["status"] == "applied" and r["outcome"] == "ok"] == [0]
    print(f"   item {item_id} : une réussite sur deux requises attend la fin du lot, "
          "puis passe comme avant ✓")


def keep_n_zero(conn) -> None:
    """9 (suite). zéro réussite : rien de neuf, l'issue majoritaire se route comme avant."""
    item_id = nouvel_item(conn, bundle_stub(2, keep=2))
    runs = reserver_tout(conn, item_id)
    for run in runs:
        kernel.apply(conn, run["id"], {"outcome": "crashed"})
    item = etat(conn, item_id)
    assert item["state"] == "travail", item["state"]
    assert evenements(conn, item_id)[-1]["outcome"] == "crashed"
    assert not [r for r in runs_de(conn, item_id) if r["status"] == "superseded"], \
        "aucune réussite à recaler : rien n'est classé"
    print(f"   item {item_id} : zéro finaliste → « crashed » routé comme sous "
          "first_pass, retour sur `travail` ✓")


def main() -> None:
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    workdir = data_dir_jetable("graphatom-fanout-reduction-")
    try:
        with db.connect() as conn:
            resultat_tardif(conn)
            transition_sans_run(conn)
            expiration_wait(conn)
            tous_en_echec(conn)
            egalite(conn)
            keep_n_tranche(conn)
            keep_n_lot_ancien(conn)
            keep_n_moins_de_n(conn)
            keep_n_zero(conn)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\nfan-out : OK — K candidats courent, la réduction garde ses finalistes, "
          "et l'item n'a jamais eu qu'une issue")


if __name__ == "__main__":
    main()
