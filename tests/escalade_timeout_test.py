"""Le test de l'escalade sur timeout : un dépassement ne se relance plus.

Un timeout n'est pas une panne transitoire : c'est le signal que la tâche
déborde de son budget. La relancer à l'identique brûle un cycle complet de
plus pour retomber au même endroit. Ce test fige la règle, de l'autopsie du
bloc jusqu'au commentaire qui pose la question à l'humain :

  1. `_autopsy` : `timed_out` quand le budget a sauté, `crashed` sinon —
     même post-mortem des deux côtés (error, timeout, exit_code, log_tail)
  2. un `cmd` qui dort au-delà de son `timeout_s` rend `timed_out`, qu'il
     soit le nœud à modèle ou le nœud shell : c'est le même `_attempt`
  3. `_route` envoie `timed_out` sur `escalate_to` dès la tentative 1, et
     quel que soit le compteur — jamais un retour sur le nœud
  4. `crashed` et `invalid_result` gardent leur seconde chance : retry sur
     place tant qu'il reste de la marge, puis escalade
  5. le faucheur tranche mécaniquement : bail expiré avec le groupe de
     l'agent encore vivant → `timed_out` et escalade ; groupe déjà mort →
     `crashed` et retry sur place
  6. la révocation ne bouge pas : fence +1 des deux côtés, orphelin tué
  7. la question d'escalade née d'un timeout dit le mot, le budget dépassé
     et la queue du journal

Le test ne détruit rien : il crée ses sujets et ses items, et laisse la base
en place — il peut tourner à côté d'un rail vivant.

Usage : uv run python tests/escalade_timeout_test.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, github_sync as gs, graph, kernel  # noqa: E402
from graphatom.kernel import MAX_ATTEMPTS  # noqa: E402

# hermétisme : pas d'instance jetable dans l'environnement, donc aucun bloc
# d'ici ne dérive la base d'un item qui existe pour de vrai
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

BUDGET_S = 7          # le `timeout_s` du nœud de travail — ce que la question doit dire
COURT_S = 2.0         # le budget des agents factices : de quoi déborder vite
MODELE = "echo 'claude -p …'; sleep 30"   # un nœud à modèle qui n'en finit pas
SHELL = "echo 'git push'; sleep 30"       # un nœud shell qui n'en finit pas
JOURNAL = "avant-dernière ligne\nl'agent en était là quand le bail a sauté\n"
LIGNE = "l'agent en était là quand le bail a sauté"
ITEM_FACTICE = 0      # l'item des contextes de bloc : jamais un item de la base
REPO = f"test-{uuid.uuid4().hex[:8]}/escalade"  # le dépôt du canal, à ce test seul


def bundle(nom: str) -> dict:
    """Un graph minuscule : un nœud de travail à budget, un nœud d'escalade."""
    return {
        "name": nom,
        "entry": "travail",
        "budgets": {"escalations": 5, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {
                "block": "ACT",
                "config": {"agent": {"cmd": "true", "prompt": "ne fais rien",
                                     "timeout_s": BUDGET_S}},
                "edges": {"ok": "fini"},
            },
            "escalate": {
                "block": "WAIT",
                "escalade": True,
                "config": {"question": "Le nœud a débordé. On retente ?",
                           "options": ["retry", "abandon"],
                           "owner": "test", "deadline_minutes": 60},
                "edges": {"retry": "travail", "abandon": "abandon",
                          "expired": "abandon"},
            },
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


class FauxProc:
    """Un processus déjà mort : l'autopsie n'en lit que le code de sortie."""

    returncode = -9


class FauxConn:
    """Le bloc n'interroge la base que pour la clé du sujet."""

    def execute(self, *args):
        return self

    def fetchone(self) -> dict:
        return {"subject_key": "escalade:test"}


def contexte(workdir: Path, cmd: str, node: str) -> blocks.Context:
    blocks.DATA_DIR = workdir
    spec = {"block": "ACT", "edges": {"ok": "fini"},
            "config": {"agent": {"cmd": cmd, "prompt": "ne fais rien",
                                 "timeout_s": COURT_S}}}
    return blocks.Context(
        FauxConn(),
        {"id": 1, "node": node, "cycle": 1, "attempt": 1},
        {"id": ITEM_FACTICE, "subject_id": 1},
        spec,
        {"name": "escalade-timeout"},
    )


def groupe_vivant(pgid: int) -> list[int]:
    """Les pids vivants du groupe — zombies exclus, ils ne travaillent plus."""
    vivants = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:  # une course avec /proc n'est pas un échec du test
            champs = stat.read_text().rsplit(") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(champs[2]) == pgid and champs[0] != "Z":
            vivants.append(int(stat.parent.name))
    return vivants


def survivants(pgid: int, secondes: float = 3.0) -> list[int]:
    """Ce qui reste du groupe, le temps laissé à init pour récolter."""
    echeance = time.time() + secondes
    while time.time() < echeance and groupe_vivant(pgid):
        time.sleep(0.1)
    return groupe_vivant(pgid)


def etat(conn, item_id: int) -> dict:
    return conn.execute(
        "SELECT * FROM work_item WHERE id = %s", (item_id,)
    ).fetchone()


def dernier_event(conn, item_id: int) -> dict:
    return conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def item_neuf(conn, revision: str, prefixe: str) -> int:
    return kernel.admit(conn, revision, f"{prefixe}:{uuid.uuid4().hex[:8]}")


def route(conn, item_id: int, outcome: str, attempt: int) -> str:
    """Route une issue noyau sur le nœud courant, à un compteur donné.

    Le run est celui d'`apply_item` : un nœud, un compteur, pas de ligne en
    base — c'est bien le seul compteur qu'on veut faire varier ici.
    """
    item = etat(conn, item_id)
    paquet = graph.load_bundle(conn, item["revision"])
    run = {"node": item["state"], "id": None, "attempt": attempt}
    with conn.transaction():
        kernel._route(conn, item, paquet, run, outcome, kind="result")
    return etat(conn, item_id)["state"]


def runs_du_noeud(conn, item_id: int, node: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM node_run WHERE item_id = %s AND node = %s",
        (item_id, node),
    ).fetchone()["n"]


def en_vol(conn, revision: str, prefixe: str) -> tuple[int, dict]:
    """Un item avec un run réservé, dont le bail vient d'expirer."""
    item_id = item_neuf(conn, revision, prefixe)
    run = kernel.claim(conn, item_id)
    assert run is not None, "rien à réserver"
    conn.execute(
        "UPDATE node_run SET lease_expires_at = now() - interval '1 second' "
        "WHERE id = %s", (run["id"],),
    )
    return item_id, run


def autopsies(workdir: Path) -> None:
    """1. les deux issues d'`_autopsy`, et le même post-mortem des deux côtés."""
    log = workdir / "journal.log"
    log.write_text(JOURNAL)
    debord = blocks._autopsy(
        FauxProc(), log, subprocess.TimeoutExpired("cmd", BUDGET_S), timeout=True)
    panne = blocks._autopsy(FauxProc(), log, ValueError("pas d'outcome"), timeout=False)

    assert debord["outcome"] == "timed_out", debord
    assert debord["timeout"] is True, debord
    assert panne["outcome"] == "crashed", panne
    assert panne["timeout"] is False, panne
    attendu = {"outcome", "error", "timeout", "exit_code", "log_tail"}
    assert set(debord) == attendu, set(debord)
    assert set(panne) == attendu, set(panne)
    for post in (debord, panne):
        assert post["exit_code"] == FauxProc.returncode, post
        assert LIGNE in post["log_tail"], post
    print(f"1. _autopsy : timeout → {debord['outcome']}, sortie ratée → "
          f"{panne['outcome']}, mêmes clés {sorted(attendu)} ✓")


def tentatives_qui_debordent(workdir: Path) -> None:
    """2. un `cmd` qui dort trop longtemps : même issue des deux côtés."""
    for genre, cmd in (("à modèle", MODELE), ("shell", SHELL)):
        depart = time.time()
        result = blocks.act(contexte(workdir, cmd, node=f"travail-{genre[0]}"))
        assert result["outcome"] == "timed_out", result
        assert result["timeout"] is True, result
        assert result["exit_code"] < 0, result  # négatif : le signal qui l'a tué
        assert "TimeoutExpired" in result["error"], result
        print(f"2. nœud {genre} : {COURT_S} s de budget, timed_out après "
              f"{time.time() - depart:.1f} s ✓")


def escalade_directe(conn, revision: str) -> int:
    """3. `timed_out` escalade dès la première tentative, et toujours."""
    # cet item-là est le sujet d'une issue : sa question d'escalade sera lue
    # par le canal GitHub au point 7
    item_id = kernel.admit(conn, revision, f"gh:{REPO}#111")
    run = kernel.claim(conn, item_id)
    assert run["attempt"] == 1, run
    kernel.apply(conn, run["id"], {
        "outcome": "timed_out", "timeout": True, "exit_code": -9,
        "error": "TimeoutExpired: Command 'claude …' timed out after 7 seconds",
        "log_tail": JOURNAL,
    })
    item = etat(conn, item_id)
    event = dernier_event(conn, item_id)
    assert item["state"] == "escalate", item["state"]
    assert (event["to_state"], event["outcome"]) == ("escalate", "timed_out"), event
    assert runs_du_noeud(conn, item_id, "travail") == 1, "le nœud a été rouvert"
    print(f"3. item {item_id} : tentative 1 en timeout → escalate, "
          "aucun re-run du nœud ✓")

    for attempt in (0, 1, MAX_ATTEMPTS, MAX_ATTEMPTS + 1):
        autre = item_neuf(conn, revision, "timeout-compteur")
        assert route(conn, autre, "timed_out", attempt) == "escalate", attempt
        assert dernier_event(conn, autre)["outcome"] == "timed_out"
        assert runs_du_noeud(conn, autre, "travail") == 0, "le nœud a été rouvert"
    print(f"3 bis. _route : timed_out → escalate aux compteurs "
          f"0, 1, {MAX_ATTEMPTS}, {MAX_ATTEMPTS + 1} ✓")
    return item_id


def seconde_chance(conn, revision: str) -> None:
    """4. une panne et une sortie malformée gardent leur retry sur place."""
    for outcome in ("crashed", "invalid_result"):
        item_id = item_neuf(conn, revision, f"{outcome}-marge")
        assert route(conn, item_id, outcome, attempt=0) == "travail", outcome
        assert route(conn, item_id, outcome, attempt=MAX_ATTEMPTS - 1) == "travail"
        assert route(conn, item_id, outcome, attempt=MAX_ATTEMPTS) == "escalate"
        print(f"4. {outcome} : retry sur place jusqu'à {MAX_ATTEMPTS}, "
              "puis escalade ✓")


def faucheur(conn, revision: str) -> None:
    """5 et 6. le pgid tranche l'issue, et la révocation ne bouge pas."""
    vif, run_vif = en_vol(conn, revision, "faucheur-vivant")
    espace = blocks.item_workspace(vif)
    espace.mkdir(parents=True, exist_ok=True)
    blocks.attempt_log(espace, run_vif).write_text(JOURNAL)
    dormeur = subprocess.Popen(["sleep", "300"], start_new_session=True)
    blocks._write_pgid(espace / blocks.PGID_FILE, dormeur, "sleep 300", run_vif["id"])
    assert blocks.agent_alive(vif, run_vif["id"]) is True
    assert dormeur.pid != os.getpgid(0), "le cobaye doit avoir sa propre session"

    mort, run_mort = en_vol(conn, revision, "faucheur-mort")
    assert blocks.agent_alive(mort, run_mort["id"]) is False, "pas de trace, pas d'agent"

    fences = {vif: etat(conn, vif)["fence"], mort: etat(conn, mort)["fence"]}
    kernel.reap(conn)

    attendu = {vif: ("timed_out", "escalate"), mort: ("crashed", "travail")}
    for item_id, (outcome, cible) in attendu.items():
        run = conn.execute(
            "SELECT * FROM node_run WHERE item_id = %s ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        item = etat(conn, item_id)
        assert (run["status"], run["outcome"]) == ("faulted", outcome), run
        assert run["result"]["timeout"] is (outcome == "timed_out"), run["result"]
        assert item["state"] == cible, (item["state"], cible)
        assert dernier_event(conn, item_id)["outcome"] == outcome
        assert item["fence"] == fences[item_id] + 1, (item["fence"], fences[item_id])
    print(f"5. faucheur : item {vif} groupe vivant → timed_out → escalate ; "
          f"item {mort} groupe mort → crashed → retry sur place ✓")

    restes = survivants(dormeur.pid)
    if restes:
        for pid in restes:
            os.kill(pid, 9)
        sys.exit(f"ÉCHEC : l'orphelin a survécu au faucheur : {restes}")
    assert not (espace / blocks.PGID_FILE).exists(), "la trace consommée doit disparaître"
    tail = conn.execute(
        "SELECT result FROM node_run WHERE item_id = %s ORDER BY id DESC LIMIT 1",
        (vif,),
    ).fetchone()["result"]["log_tail"]
    assert LIGNE in tail, tail
    print(f"6. groupe {dormeur.pid} révoqué, trace effacée, fence +1 des deux "
          "côtés, queue du journal au post-mortem ✓")


def question_d_escalade(conn, item_id: int) -> None:
    """7. la question dit le timeout, le budget dépassé et la queue du log."""
    gh = gs.GitHub(REPO, "jeton")  # aucun appel sortant : on ne construit que le corps
    q = next(q for q in gs._gh_questions(conn, gh) if q["item_id"] == item_id)
    corps = gs._question_body(conn, q, "http://127.0.0.1:9")

    assert "timeout" in corps.lower(), corps
    assert f"`timeout_s` de {BUDGET_S} s" in corps, corps
    assert LIGNE in corps, corps
    print(f"7. question {q['id']} : « timeout », budget {BUDGET_S} s et queue "
          "du journal dans le commentaire ✓")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-escalade-timeout-"))
    blocks.DATA_DIR = workdir  # les blocs et le faucheur écrivent là, jamais dans data/
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    try:
        autopsies(workdir)
        tentatives_qui_debordent(workdir)
        with db.connect() as conn:
            revision = graph.publish(conn, bundle(f"escalade-timeout-{REPO[5:13]}"))
            escalade = escalade_directe(conn, revision)
            seconde_chance(conn, revision)
            faucheur(conn, revision)
            question_d_escalade(conn, escalade)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\nescalade timeout : OK — un dépassement escalade, une panne retente")


if __name__ == "__main__":
    main()
