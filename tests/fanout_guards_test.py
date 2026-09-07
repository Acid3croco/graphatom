"""Le test des gardes du fan-out : la relance identique par lots, le faucheur
face aux candidats, et une réduction que la publication n'a pas vue.

Sur un Postgres jetable, noyau seul. La signature d'un échec est ici son
champ `error` — le calcul réel a son propre test, `deterministic_retry` :

  1. un lot précédent qui n'est pas entièrement comparable — un candidat
     sans signature — conserve la relance du lot entier
  2. un lot partiel ne se compare pas à un lot complet : la relance est
     conservée ; le lot complété, identique, escalade sans brûler la marge
  3. le faucheur ne classe rien sur une observation périmée : un run rendu
     entre le balayage et le verrou est laissé tel qu'il s'est rendu
  4. un run étranger dont l'agent est certainement mort est fauché avant son
     bail, et le post-mortem nomme le redémarrage du worker
  5. un candidat fauché ne pousse pas la barrière de l'item : ses frères
     volent encore et leur issue route ; un run seul fauché la pousse
  6. une réduction inconnue à l'exécution lève, et la transaction est
     défaite : l'item ne bouge pas, le run n'est pas consommé
  7. un candidat muet dans les deux lots rend le lot courant incomparable,
     même quand son frère répète sa signature à l'identique
  8. avant son bail, un run n'est fauché que s'il est étranger ET que son
     agent est certainement mort : étranger mais vivant, étranger sans
     trace, ou mort mais réservé ici — il vole encore, jusqu'à son bail

Usage : uv run python tests/fanout_guards_test.py
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import (admettre, bundle_epreuve, data_dir_jetable,  # noqa: E402
                    etat, evenements, fanout_stub, provision_postgres,
                    reserver_tout, runs_de)

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-fanout-gardes")
os.environ.pop("GRAPHATOM_REPO_DIR", None)
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

from graphatom import db, kernel, process, reaper  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402
from graphatom.bundle import GraphError  # noqa: E402
from graphatom.policy import CLAIMED  # noqa: E402

BAIL_S = 600  # loin : seul le test fait expirer un bail


def graphe(name: str, fanout: bool) -> dict:
    config: dict = {"lease_s": BAIL_S}
    if fanout:
        config["fanout"] = fanout_stub(2)
    return bundle_epreuve(name, {
        "travail": {"block": "ACT", "config": config, "edges": {"ok": "fini"}},
    })


FANOUT, SEUL = graphe("fanout-gardes", True), graphe("fanout-gardes-seul", False)


def crash(conn, run: dict, erreur: str | None = None) -> None:
    """Un candidat rend `crashed` — signé par son erreur, muet sans elle."""
    resultat = {"outcome": "crashed"} | ({"error": erreur} if erreur else {})
    kernel.apply(conn, run["id"], resultat)


def expirer(conn, run_id: int) -> None:
    conn.execute("UPDATE node_run SET lease_expires_at = now() - interval '1 second' "
                 "WHERE id = %s", (run_id,))


def lot_incomparable(conn) -> None:
    item_id = admettre(conn, FANOUT, "fanout-gardes")
    c0, c1 = reserver_tout(conn, item_id)
    crash(conn, c0)  # muet : sa signature n'existe pas
    crash(conn, c1, "boum")
    assert etat(conn, item_id)["state"] == "travail"
    c0, c1 = reserver_tout(conn, item_id)
    assert c0["attempt"] == 2
    crash(conn, c0, "boum")
    crash(conn, c1, "boum")
    assert etat(conn, item_id)["state"] == "travail", "relance supprimée à tort"
    assert kernel.claim(conn, item_id)["attempt"] == 3
    assert not any("retry" in (r["result"] or {}) for r in runs_de(conn, item_id))
    print("1. un lot précédent incomparable conserve la relance ✓")


def lot_partiel(conn) -> None:
    item_id = admettre(conn, FANOUT, "fanout-gardes")
    for run in reserver_tout(conn, item_id):
        crash(conn, run, "boum")
    c0 = kernel.claim(conn, item_id)
    assert (c0["attempt"], c0["candidate"]) == (2, 0), c0
    crash(conn, c0, "boum")  # {0} face à {0, 1} : pas comparable
    assert etat(conn, item_id)["state"] == "travail", "lot partiel comparé"
    c1 = kernel.claim(conn, item_id)
    assert (c1["attempt"], c1["candidate"]) == (2, 1), c1
    crash(conn, c1, "boum")
    item = etat(conn, item_id)
    assert item["state"] == "escalate", item["state"]
    note = runs_de(conn, item_id)[2]["result"].get("retry", "")
    assert "relance identique supprimée" in note, note
    print("2. un lot partiel n'est pas comparé ; complété, il escalade ✓")


def observation_perimee(conn) -> None:
    item_id = admettre(conn, SEUL, "fanout-gardes")
    run = kernel.claim(conn, item_id)
    expirer(conn, run["id"])  # suspect au balayage
    vrai = kernel._locked

    def rendu_juste_avant(conn_faucheur, run_id: int):
        kernel._locked = vrai  # une seule fois : l'apply ci-dessous le rappelle
        with db.connect() as bloc:
            assert kernel.apply(bloc, run_id, {"outcome": "ok"}) == "applied"
        return vrai(conn_faucheur, run_id)

    kernel._locked = rendu_juste_avant
    try:
        assert reaper.reap(conn) == 0, "un run rendu a été fauché"
    finally:
        kernel._locked = vrai
    ligne = runs_de(conn, item_id)[0]
    assert (ligne["status"], ligne["outcome"]) == ("applied", "ok"), ligne
    assert [e["kind"] for e in evenements(conn, item_id)] == ["admitted", "result"]
    print("3. rendu entre le balayage et le verrou : jamais fauché ✓")


def tracer_agent(item_id: int, run: dict) -> subprocess.Popen:
    """Un vrai agent — un `sleep` dans son groupe — et sa trace dans le workspace."""
    atelier = bureau.run_workspace(item_id, run)
    atelier.mkdir(parents=True, exist_ok=True)
    agent = subprocess.Popen(["sleep", "60"], start_new_session=True)
    process.write_pgid(atelier / bureau.PGID_FILE, agent, "sleep 60", run["id"])
    return agent


def orphelin_certainement_mort(conn) -> None:
    item_id = admettre(conn, SEUL, "fanout-gardes")
    run = kernel.claim(conn, item_id)
    CLAIMED.discard(run["id"])  # ce qu'un redémarrage du worker oublie
    agent = tracer_agent(item_id, run)
    agent.kill()
    agent.wait()
    assert process.agent_state(item_id, run["id"]) == process.AGENT_DEAD
    assert run["lease_expires_at"] > kernel.now(), "le bail devait courir encore"

    assert reaper.reap(conn) == 1
    ligne = runs_de(conn, item_id)[0]
    assert (ligne["status"], ligne["outcome"]) == ("faulted", "crashed"), ligne
    assert "redémarrage du worker" in ligne["result"]["error"], ligne["result"]
    assert "déjà mort" in ligne["result"]["error"], ligne["result"]
    assert etat(conn, item_id)["state"] == "travail", "crashed se retente"
    print("4. run étranger, agent mort : fauché avant son bail, cause nommée ✓")


def candidat_fauche(conn) -> None:
    item_id = admettre(conn, FANOUT, "fanout-gardes")
    c0, c1 = reserver_tout(conn, item_id)
    barriere = etat(conn, item_id)["fence"]
    expirer(conn, c0["id"])
    assert reaper.reap(conn) == 1
    assert etat(conn, item_id)["fence"] == barriere, "un candidat a poussé la barrière"
    lignes = runs_de(conn, item_id)
    assert (lignes[0]["status"], lignes[0]["outcome"]) == ("faulted", "crashed")
    assert lignes[1]["status"] == "running", "le frère a été révoqué"
    assert kernel.apply(conn, c1["id"], {"outcome": "ok"}) == "applied"
    assert etat(conn, item_id)["state"] == "fini"

    seul = admettre(conn, SEUL, "fanout-gardes")
    run = kernel.claim(conn, seul)
    barriere = etat(conn, seul)["fence"]
    expirer(conn, run["id"])
    assert reaper.reap(conn) == 1
    assert etat(conn, seul)["fence"] == barriere + 1, "un run seul doit la pousser"
    print("5. candidat fauché : barrière intacte, le frère route ; seul : poussée ✓")


def reduction_corrompue(conn) -> None:
    item_id = admettre(conn, graphe("fanout-corrompu", True), "fanout-gardes")
    conn.execute(
        "UPDATE graph_revision SET bundle = jsonb_set(bundle, "
        "'{nodes,travail,config,fanout,reduce}', '\"best_by\"') WHERE id = %s",
        (etat(conn, item_id)["revision"],),
    )
    run = kernel.claim(conn, item_id)
    try:
        kernel.apply(conn, run["id"], {"outcome": "ok"})
    except GraphError as exc:
        assert "best_by" in str(exc), exc
    else:
        raise AssertionError("une réduction inconnue a réduit")
    item = etat(conn, item_id)
    assert (item["state"], item["version"]) == ("travail", 1), item
    assert runs_de(conn, item_id)[0]["status"] == "running", "le run a été consommé"
    print("6. réduction inconnue : levée, item et run intacts ✓")


def candidat_muet_repete(conn) -> None:
    """7. le même candidat muet dans les deux lots : rien n'est comparable."""
    item_id = admettre(conn, FANOUT, "fanout-gardes")
    for attempt in (1, 2):
        c0, c1 = reserver_tout(conn, item_id)
        assert c0["attempt"] == attempt
        # le signé finit premier : c'est son run que la réduction route, et
        # c'est donc bien le lot — pas sa propre signature — qui est comparé
        crash(conn, c1, "boum")
        crash(conn, c0)
        assert etat(conn, item_id)["state"] == "travail", "relance supprimée à tort"
    assert kernel.claim(conn, item_id)["attempt"] == 3
    assert not any("retry" in (r["result"] or {}) for r in runs_de(conn, item_id))
    print("7. un candidat muet des deux côtés : le lot reste incomparable ✓")


def volant(conn, prefixe: str) -> dict:
    """Un run seul réservé sur un item neuf — dans CLAIMED, comme tout run réservé ici."""
    return kernel.claim(conn, admettre(conn, SEUL, prefixe))


def faux_orphelins(conn) -> None:
    """8. avant le bail : fauché seulement si étranger ET certainement mort."""
    etranger_vivant = volant(conn, "fanout-gardes")
    CLAIMED.discard(etranger_vivant["id"])
    agent = tracer_agent(etranger_vivant["item_id"], etranger_vivant)
    etranger_muet = volant(conn, "fanout-gardes")
    CLAIMED.discard(etranger_muet["id"])  # aucune trace : identité indécidable
    mien = volant(conn, "fanout-gardes")
    mort = tracer_agent(mien["item_id"], mien)
    mort.kill()
    mort.wait()
    try:
        assert process.agent_state(etranger_vivant["item_id"], etranger_vivant["id"]) \
            == process.AGENT_ALIVE
        assert process.agent_state(etranger_muet["item_id"], etranger_muet["id"]) \
            == process.AGENT_UNKNOWN
        assert process.agent_state(mien["item_id"], mien["id"]) == process.AGENT_DEAD
        assert reaper.reap(conn) == 0, "fauché avant son bail sans être étranger et mort"
        for run in (etranger_vivant, etranger_muet, mien):
            assert runs_de(conn, run["item_id"])[0]["status"] == "running", run["id"]
        expirer(conn, mien["id"])
        assert reaper.reap(conn) == 1, "le bail échu devait faucher le mien, lui seul"
        assert runs_de(conn, mien["item_id"])[0]["status"] == "faulted"
        # les deux étrangers volent toujours : ce qu'ils rendent route encore
        for run in (etranger_vivant, etranger_muet):
            assert kernel.apply(conn, run["id"], {"outcome": "ok"}) == "applied"
            assert etat(conn, run["item_id"])["state"] == "fini"
    finally:
        agent.kill()
        agent.wait()
    print("8. étranger vivant, étranger sans trace, mort mais mien : jamais fauchés "
          "avant le bail ; le bail échu fauche ✓")


def signature_lisible(_item_id: int, run: dict, _outcome: str) -> str | None:
    """La signature d'un échec, ici : son erreur — None quand il est muet."""
    return (run.get("result") or {}).get("error")


def main() -> None:
    data_dir_jetable("graphatom-fanout-gardes-")
    signature_reelle, kernel.retry_signature = kernel.retry_signature, signature_lisible
    db.init_db()
    try:
        with db.connect() as conn:
            lot_incomparable(conn)
            lot_partiel(conn)
            observation_perimee(conn)
            orphelin_certainement_mort(conn)
            candidat_fauche(conn)
            reduction_corrompue(conn)
            candidat_muet_repete(conn)
            faux_orphelins(conn)
    finally:
        kernel.retry_signature = signature_reelle
    print("\nfanout_guards : OK")


if __name__ == "__main__":
    main()
