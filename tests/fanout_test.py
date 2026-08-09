"""Le test du fan-out : K candidats concurrents sur un nœud, une seule issue.

Ce que le noyau doit tenir : l'item garde un seul état, une seule révision,
une seule issue de nœud. Ce qui se multiplie, ce sont les runs.

Scénario, sur la base — claim/apply, l'exécuteur de l'ordonnanceur pour la
course réelle, et de vrais agents factices pour les candidats :

  0. non-régression : un nœud sans `fanout` garde son run unique, sans
     numéro de candidat, dans le workspace de l'item
  1. un nœud à `variants` × `repeat` réserve K runs concurrents : même
     `(item, nœud, passage, tentative)`, un bail et un numéro chacun, et la
     barrière de la tentative qu'ils partagent
  2. chaque candidat écrit dans son propre workspace `data/item-<N>/c<k>/` —
     journal, prompt, `outcome.json` et trace de pgid, rien d'écrasé
  3. une variante surcharge les clés qu'elle nomme et hérite des autres ;
     `label` et `strategy` s'interpolent dans le prompt et dans la commande
  4. `first_pass` : le premier succès gagne, l'item est routé sur son arête,
     et les groupes de processus des perdants sont morts
  5. les jetons de tous les candidats sont enregistrés, et le total de
     l'item est bien la somme des K — pas celui du seul gagnant
  6. un résultat de perdant appliqué après la décision ne bouge ni l'état,
     ni la révision, ni le passage : il est classé, jamais routé
  7. tous en échec : l'issue majoritaire est routée comme une issue de nœud
     ordinaire ; à égalité, celle du run terminé en premier
  8. `keep_n` : elle tranche dès que n candidats ont réussi, laisse passer
     ces finalistes et révoque les candidats encore en vol ; moins de n
     réussites et zéro réussite gardent le traitement de fin de lot

Le test ne détruit rien : il crée son sujet, ses items, et laisse la base
en place — il peut tourner à côté d'un rail vivant.

Usage : uv run python tests/fanout_test.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from psycopg.pq import TransactionStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, graph, kernel, scheduler, web  # noqa: E402

from outils import attendre, etat  # noqa: E402

# hermétisme : sans instance jetable dans l'environnement, le bloc ne dérive
# aucune base — les agents factices d'ici n'en ont pas besoin
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

# Chaque candidat déclare sa consommation à partir du nom de son workspace :
# c0 → 100 jetons, c1 → 101, et ainsi de suite. Le total de l'item doit être
# leur somme, et aucun candidat seul ne peut la produire.
JETONS_BASE = 100
PREAMBULE = """
k=$(basename "$(pwd)"); k=${k#c}
printf '{"input_tokens": %d, "total_cost_usd": 0.25}' $((JETONS + k)) > usage.json
printf '%s' '{label}' > label.txt
""".replace("JETONS", str(JETONS_BASE))

# le candidat lent ne rendra jamais son issue : la réduction le tue avant
LENT = PREAMBULE + "sleep 60\n" + """printf '{"outcome": "ok"}' > outcome.json"""
# le rapide joue sa propre commande — celle que sa variante surcharge
RAPIDE = PREAMBULE + """
printf '%s' 'commande surchargée' > surcharge.txt
printf '%s\\n' '## Fait' 'Course terminée.' '' '## Appris' 'Rien.' '' \\
    '## Pas fait' 'Rien.' > passation-travail.md
printf '{"outcome": "ok", "summary": "%s"}' '{label}' > outcome.json
"""

PROMPT = "Tu travailles sur {subject_key}. Stratégie : {strategy}."
LABELS = {0: "lent", 1: "lent", 2: "rapide", 3: "rapide"}  # variants × repeat = 4


def bundle_agent() -> dict:
    """Le graph de la course : un ACT en fan-out, quatre candidats réels.

    Deux variantes jouées deux fois. La lente hérite de tout le nœud ; la
    rapide ne surcharge que sa commande — son prompt, ses budgets et ses
    arêtes restent ceux du nœud.
    """
    return {
        "name": "fanout-agent-test",
        "entry": "travail",
        "budgets": {"escalations": 2, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {
                "block": "ACT",
                "config": {
                    "agent": {"cmd": LENT, "prompt": PROMPT,
                              "timeout_s": 60, "silence_s": 60},
                    "fanout": {
                        "variants": [
                            {"label": "lent", "strategy": "prends ton temps"},
                            {"label": "rapide", "strategy": "va droit au but",
                             "agent": {"cmd": RAPIDE}},
                        ],
                        "repeat": 2,
                        "reduce": "first_pass",
                    },
                },
                "edges": {"ok": "fini"},
            },
            "escalate": {
                "block": "WAIT", "escalade": True,
                "config": {"question": "On retente ?", "options": ["retry"],
                           "owner": "test", "deadline_minutes": 60},
                "edges": {"retry": "travail", "expired": "abandon"},
            },
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


def bundle_stub(k: int | None, keep: int | None = None) -> dict:
    """Le même graph en stub : pas d'agent, K candidats, ou aucun fan-out.

    `k` à None, c'est le nœud d'avant le fan-out — il sert la non-régression.
    `keep` choisit la réduction : `first_pass` sans lui, `keep_n` avec.
    """
    config: dict = {"duration_s": 0}
    if k is not None:
        config["fanout"] = {"variants": [{"label": f"v{i}"} for i in range(k)],
                            "reduce": "first_pass"}
        if keep is not None:
            config["fanout"] |= {"reduce": "keep_n", "n": keep}
    return {
        "name": f"fanout-stub-{k}-{keep}",
        "entry": "travail",
        "budgets": {"escalations": 2, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {"block": "ACT", "config": config, "edges": {"ok": "fini"}},
            "escalate": {
                "block": "WAIT", "escalade": True,
                "config": {"question": "On retente ?", "options": ["retry"],
                           "owner": "test", "deadline_minutes": 60},
                "edges": {"retry": "travail", "expired": "abandon"},
            },
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


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


# ------------------------------------------------------------------ outillage


def vivant(pgid: int) -> bool:
    """Reste-t-il quelqu'un dans ce groupe de processus ?"""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def runs_de(conn, item_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s ORDER BY id", (item_id,)
    ).fetchall()


def evenements(conn, item_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version", (item_id,)
    ).fetchall()


def reserve_tout(conn, item_id: int) -> list[dict]:
    """Réserve la tentative entière — comme l'ordonnanceur, candidat par candidat."""
    runs = []
    while (run := kernel.claim(conn, item_id)) is not None:
        runs.append(run)
        assert len(runs) <= graph.FANOUT_MAX_CANDIDATES, "claim ne s'arrête plus"
    return runs


def nouvel_item(conn, bundle: dict) -> int:
    rev = graph.publish(conn, bundle)
    return kernel.admit(
        conn, rev, f"{bundle['name']}:{uuid.uuid4().hex[:8]}", _allow_parallel_for_test=True
    )


# -------------------------------------------------------------------- épreuves


def sans_fanout(conn) -> None:
    """0. un nœud sans `fanout` : un run, sans numéro, dans le workspace de l'item."""
    item_id = nouvel_item(conn, bundle_stub(None))
    runs = reserve_tout(conn, item_id)
    assert len(runs) == 1, f"un nœud sans fan-out réserve un seul run : {runs}"
    run = runs[0]
    assert run["candidate"] is None, run["candidate"]
    assert blocks.run_workspace(item_id, run) == blocks.item_workspace(item_id)
    kernel.apply(conn, run["id"], {"outcome": "ok"})
    item = etat(conn, item_id)
    assert item["state"] == "fini", item["state"]
    assert item["version"] == 2, item["version"]
    print(f"0. item {item_id} sans fan-out : un run sans candidat, "
          "workspace de l'item, routage inchangé ✓")


def course(conn, workdir: Path) -> int:
    """1 à 5 : la course réelle des quatre candidats, et sa réduction."""
    item_id = nouvel_item(conn, bundle_agent())

    # 1. la tentative entière se réserve : K runs concurrents
    runs = reserve_tout(conn, item_id)
    assert len(runs) == 4, f"quatre candidats attendus, vu {len(runs)}"
    cles = {(r["item_id"], r["node"], r["cycle"], r["attempt"]) for r in runs}
    assert cles == {(item_id, "travail", 1, 1)}, cles
    assert sorted(r["candidate"] for r in runs) == [0, 1, 2, 3], runs
    assert len({r["id"] for r in runs}) == 4, "quatre lignes node_run distinctes"
    assert all(r["lease_expires_at"] is not None for r in runs), "un bail chacun"
    assert all(r["status"] == "running" for r in runs), runs
    # la barrière est celle de la tentative : les candidats la partagent, sinon
    # le premier réservé serait dépassé par le suivant avant même de courir
    fences = {r["fence"] for r in runs}
    assert len(fences) == 1, f"les candidats d'une tentative partagent la barrière : {fences}"
    assert fences.pop() == etat(conn, item_id)["fence"], "la barrière est celle de l'item"
    print(f"1. item {item_id} : 4 node_run pour (travail, passage 1, tentative 1), "
          "candidats 0-3, un bail chacun, barrière partagée ✓")

    par_candidat = {r["candidate"]: r for r in runs}
    workspace = workdir / f"item-{item_id}"

    # les lents partent d'abord : leurs groupes doivent être en vol quand la
    # réduction tranche, sinon il n'y aurait rien à révoquer
    fils = []
    for k in (0, 1):
        fil = threading.Thread(target=scheduler._execute,
                               args=(par_candidat[k]["id"], item_id), daemon=True)
        fil.start()
        fils.append(fil)
    traces = [workspace / f"c{k}" / blocks.PGID_FILE for k in (0, 1)]
    assert attendre(lambda: all(t.is_file() for t in traces), 15.0), \
        f"les candidats lents n'ont pas laissé leur trace : {traces}"
    pgids = {k: json.loads(trace.read_text())["pgid"]
             for k, trace in zip((0, 1), traces)}
    assert len(set(pgids.values())) == 2, f"un groupe par candidat : {pgids}"
    print(f"2. traces de pgid distinctes, une par candidat : {pgids} ✓")

    for k in (2, 3):
        fil = threading.Thread(target=scheduler._execute,
                               args=(par_candidat[k]["id"], item_id), daemon=True)
        fil.start()
        fils.append(fil)
    for fil in fils:
        fil.join(timeout=60)
        assert not fil.is_alive(), "un candidat n'est jamais revenu"

    # 3. chaque candidat a son propre workspace, et rien n'y est écrasé
    for k in range(4):
        cw = workspace / f"c{k}"
        assert cw.is_dir(), f"workspace du candidat {k} absent : {cw}"
        assert (cw / "agent-travail-1-1.log").is_file(), f"journal du candidat {k}"
        assert (cw / "prompt-travail-1-1.md").is_file(), f"prompt du candidat {k}"
        assert (cw / "usage-travail-1-1.json").is_file(), f"usage du candidat {k}"
    for fixe in ("agent-travail-1-1.log", "label.txt", blocks.OUTCOME_NAME):
        assert not (workspace / fixe).exists(), \
            f"{fixe} ne doit pas être écrit dans le workspace de l'item"
    fichiers = {k: sorted(p.name for p in (workspace / f"c{k}").iterdir())
                for k in range(4)}
    print("3. quatre workspaces distincts :\n" + "\n".join(
        f"   c{k} : {', '.join(noms)}" for k, noms in fichiers.items()) + " ✓")

    # 4. la variante s'interpole, surcharge ce qu'elle nomme et hérite du reste
    for k, label in LABELS.items():
        cw = workspace / f"c{k}"
        assert (cw / "label.txt").read_text() == label, \
            f"candidat {k} : le label reçu dans sa commande"
        prompt = (cw / "prompt-travail-1-1.md").read_text()
        strategie = "prends ton temps" if label == "lent" else "va droit au but"
        assert f"Stratégie : {strategie}." in prompt, f"candidat {k} : {prompt[:200]}"
        assert "Tu travailles sur fanout-agent-test:" in prompt, \
            f"candidat {k} : le prompt du nœud est hérité"
        assert str(cw) in prompt, f"candidat {k} : le contrat nomme son workspace"
        # seule la variante rapide surcharge `cmd` : elle seule laisse ce fichier
        surcharge = (cw / "surcharge.txt").is_file()
        assert surcharge == (label == "rapide"), f"candidat {k} : cmd surchargée ?"
    print("4. label et stratégie interpolés dans la commande et le prompt ; "
          "la variante à `cmd` surchargé joue bien la sienne, l'autre hérite ✓")

    # 5. first_pass : un seul gagnant, l'item routé sur son arête, perdants morts
    finaux = {r["candidate"]: r for r in runs_de(conn, item_id)}
    gagnants = [r for r in finaux.values() if r["status"] == "applied"]
    assert len(gagnants) == 1, f"un seul candidat survit : {gagnants}"
    gagnant = gagnants[0]
    assert gagnant["outcome"] == "ok", gagnant["outcome"]
    assert LABELS[gagnant["candidate"]] == "rapide", gagnant["candidate"]
    assert all(finaux[k]["status"] == "superseded" for k in range(4)
               if k != gagnant["candidate"]), finaux
    item = etat(conn, item_id)
    assert item["state"] == "fini", item["state"]
    assert item["terminal_at"] is not None, "l'arête du gagnant mène au terminal"
    routages = [e for e in evenements(conn, item_id) if e["kind"] == "result"]
    assert len(routages) == 1, f"une seule issue de nœud : {routages}"
    assert routages[0]["run_id"] == gagnant["id"], routages[0]
    print(f"5. candidat c{gagnant['candidate']} gagne, item routé travail → fini "
          f"par le seul run {gagnant['id']} ✓")

    for k, pgid in pgids.items():
        assert attendre(lambda p=pgid: not vivant(p), 15.0), \
            f"le groupe {pgid} du perdant c{k} a survécu à la réduction"
    print(f"   groupes {sorted(pgids.values())} des perdants morts "
          "(os.killpg lève ProcessLookupError) ✓")

    # 6. la comptabilité est celle de l'étape, pas celle du gagnant
    usages = {r["candidate"]: (r["result"] or {}).get("usage") for r in finaux.values()}
    for k in range(4):
        assert usages[k] == {"input_tokens": JETONS_BASE + k, "total_cost_usd": 0.25}, \
            f"candidat {k} : son usage doit être en base — {usages[k]}"
    total = web._totals(list(finaux.values()))
    attendu = sum(JETONS_BASE + k for k in range(4))
    assert total["input_tokens"] == attendu, (total, attendu)
    par_noeud = web._totals([r for r in finaux.values() if r["node"] == "travail"])
    assert par_noeud == total, "le nœud en fan-out porte tout le prix de l'étape"
    # la table des runs distingue les candidats : sans leur numéro, K lignes
    # identiques — un run sans fan-out, lui, s'affiche comme avant
    assert web._candidate(gagnant) == f" · <small>c{gagnant['candidate']}</small>"
    assert web._candidate({"candidate": None}) == ""
    assert total["total_cost_usd"] == 1.0, total
    assert total["input_tokens"] > max(u["input_tokens"] for u in usages.values()), \
        "le total doit être la somme des K, pas celui du gagnant"
    print(f"6. jetons des 4 candidats en base, total de l'item {attendu} "
          f"= somme des K (gagnant seul : {usages[gagnant['candidate']]['input_tokens']}) ✓")
    return item_id


def resultat_tardif(conn) -> None:
    """7. un résultat de perdant arrivé après la décision est classé, jamais routé."""
    item_id = nouvel_item(conn, bundle_stub(3))
    runs = reserve_tout(conn, item_id)
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
    runs = reserve_tout(conn, item_id)
    assert all(run["id"] in kernel.CLAIMED for run in runs)

    kernel.apply_item(conn, item_id, "wall_deadline", kind="wall")
    apres = etat(conn, item_id)
    assert apres["state"] == "abandon" and apres["terminal_at"] is not None, apres
    version = apres["version"]
    events = len(evenements(conn, item_id))
    classes = runs_de(conn, item_id)
    assert all(run["status"] == "superseded" for run in classes), classes
    assert all(run["finished_at"] is not None for run in classes), classes
    assert not any(run["id"] in kernel.CLAIMED for run in runs)

    conn.execute(
        "UPDATE node_run SET lease_expires_at = now() - interval '1 hour' "
        "WHERE item_id = %s", (item_id,),
    )
    assert kernel.reap(conn) == 0, "le faucheur ne doit pas reprendre un run révoqué"
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


def expiration_wait(conn) -> None:
    """7 ter. WAIT expire atomiquement, puis le ménage se fait hors transaction."""
    item_id = nouvel_item(conn, bundle_wait())
    run = kernel.claim(conn, item_id)
    assert run is not None
    kernel.apply(conn, run["id"], {"outcome": "ok"})
    avant = etat(conn, item_id)
    assert avant["state"] == "attente" and avant["terminal_at"] is None, avant
    events = len(evenements(conn, item_id))

    # Reproduit le résidu observé en production : un ancien run reste en vol
    # alors que l'item attend déjà une réponse.
    conn.execute(
        "UPDATE node_run SET status = 'running', outcome = NULL, result = NULL, "
        "finished_at = NULL, lease_expires_at = now() - interval '1 hour' "
        "WHERE id = %s", (run["id"],),
    )
    kernel.CLAIMED.add(run["id"])
    conn.execute(
        "UPDATE question SET deadline = now() - interval '1 minute' "
        "WHERE item_id = %s AND state = 'open'", (item_id,),
    )

    transactions = []
    vrai_menage = kernel._menage

    def observe(item: int, run_ids: list[int], ranger: bool) -> None:
        transactions.append(conn.info.transaction_status)
        vrai_menage(item, run_ids, ranger)

    kernel._menage = observe
    try:
        assert scheduler._settle_waits(conn) == 1
    finally:
        kernel._menage = vrai_menage

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
    runs = reserve_tout(conn, item_id)
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

    # égalité : c'est le run terminé en premier qui tranche, pas l'ordre des noms
    for premier, second in (("crashed", "stalled"), ("stalled", "crashed")):
        item_id = nouvel_item(conn, bundle_stub(2))
        runs = reserve_tout(conn, item_id)
        assert len(runs) == 2, runs
        kernel.apply(conn, runs[0]["id"], {"outcome": premier})
        kernel.apply(conn, runs[1]["id"], {"outcome": second})
        dernier = evenements(conn, item_id)[-1]
        assert dernier["outcome"] == premier, (dernier["outcome"], premier)
        print(f"   item {item_id} : {premier} et {second} à égalité → "
              f"« {premier} », celle du run terminé en premier ✓")


def keep_n(conn) -> None:
    """9. `keep_n` tranche à n réussites et révoque les candidats en vol."""
    item_id = nouvel_item(conn, bundle_stub(5, keep=2))
    runs = reserve_tout(conn, item_id)
    assert len(runs) == 5, runs
    depart = etat(conn, item_id)

    # Un succès ne suffit pas. Le deuxième tranche sans attendre les trois
    # candidats restants. `_menage` doit recevoir leurs runs pour tuer leurs
    # groupes de processus hors transaction.
    revoques = []
    ancien_revoke = kernel.revoke_orphan
    kernel.revoke_orphan = lambda item, run: revoques.append((item, run))
    try:
        assert kernel.apply(conn, runs[0]["id"], {"outcome": "ok"}) == "applied"
        attente = etat(conn, item_id)
        assert attente["version"] == depart["version"], attente
        assert attente["state"] == "travail", attente["state"]

        assert kernel.apply(conn, runs[1]["id"], {"outcome": "ok"}) == "applied"
    finally:
        kernel.revoke_orphan = ancien_revoke

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

    # Un lot ancien peut déjà porter plus de n réussites au moment où cette
    # règle est déployée. Seules les n premières restent finalistes ; une
    # réussite finie n'est pas un processus à tuer, contrairement aux runs
    # encore en vol.
    item_id = nouvel_item(conn, bundle_stub(5, keep=2))
    runs = reserve_tout(conn, item_id)
    conn.execute(
        "UPDATE node_run SET status = 'applied', outcome = 'ok', finished_at = now() "
        "WHERE id = ANY(%s)", ([r["id"] for r in runs[:3]],),
    )
    ancien = etat(conn, item_id)
    with conn.transaction():
        verdict = kernel._keep_n(conn, ancien, runs[2], 2)
    finaliste, issue, a_tuer = verdict
    classes = {r["candidate"]: r for r in runs_de(conn, item_id)}
    assert finaliste["id"] == runs[0]["id"] and issue == "ok", verdict
    assert [classes[k]["status"] for k in range(5)] == [
        "applied", "applied", "superseded", "superseded", "superseded",
    ], classes
    assert sorted(a_tuer) == sorted(r["id"] for r in runs[3:]), a_tuer
    print(f"   item {item_id} : une réussite tardive de trop est classée, mais seuls "
          "les deux runs encore en vol sont rendus pour terminaison ✓")

    # Moins de n réussites possibles : la course attend sa fin, puis laisse
    # passer les réussites disponibles comme avant la décision anticipée.
    item_id = nouvel_item(conn, bundle_stub(3, keep=2))
    runs = reserve_tout(conn, item_id)
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

    # zéro réussite : rien de neuf, l'issue majoritaire se route comme avant
    item_id = nouvel_item(conn, bundle_stub(2, keep=2))
    runs = reserve_tout(conn, item_id)
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
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-fanout-"))
    blocks.DATA_DIR = workdir
    try:
        with db.connect() as conn:
            sans_fanout(conn)
            course(conn, workdir)
            resultat_tardif(conn)
            transition_sans_run(conn)
            expiration_wait(conn)
            tous_en_echec(conn)
            keep_n(conn)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\nfan-out : OK — K candidats courent, la réduction garde ses finalistes, "
          "et l'item n'a jamais eu qu'une issue")


if __name__ == "__main__":
    main()
