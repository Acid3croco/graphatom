"""Les bords de l'exécuteur d'agent, de son groupe de processus et des adaptateurs.

Sans base ni ordonnanceur — le bloc agent, le module des processus et la
résolution des commandes, poussés là où un test nominal ne va jamais :

  1. un `usage.json` illisible ou vide ne rend aucun usage ; un usage plein passe
  2. un fichier qui disparaît pendant la marche des mtimes est toléré, et un
     journal encore absent compte pour zéro octet
  3. l'attente du quota ne mange pas le budget : la même commande, sans son
     marqueur, tombe sous le couperet
  4. l'agent reçoit sa base jetable, jamais celle du rail — qui reste celle du
     quota et du heartbeat
  5. un bloc interrompu révoque son groupe avant de remonter l'erreur
  6. un `deploy_sha` sans `activation` déclarée est un crash, pas une demande
  7. la porte déclarée rétrograde un `pass` ; sans faute elle se tait, et la
     passation valide passe
  8. `read_outcome` refuse une issue ou un résumé qui n'est pas une chaîne,
     et un `deploy_sha` mal formé
  9. une panne de fournisseur sans fournisseur ni raison ne vaut rien
 10. `kill_group` : un chef déjà parti n'est pas une erreur ; un groupe qui
     ignore SIGTERM tombe au SIGKILL après la grâce
 11. `revoke_residual_group` tue au SIGKILL ce qui survit à la grâce
 12. `agent_state` : un `/proc` illisible, un boot_id absent ou vide sont
     indécidables ; le même boot et la même naissance disent vivant
 13. `revoke_orphan` refuse une trace qui nomme le groupe du faucheur lui-même
 14. les adaptateurs : ni modèle ni effort absents dans la commande, l'effort
     sans variable n'est pas émis, une CLI inconnue est refusée

Usage : uv run python tests/agent_edges_test.py
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# hermétisme : ni atelier git ni instance jetable dans l'environnement
os.environ.pop("GRAPHATOM_REPO_DIR", None)
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

from graphatom import agent, blocks, db, executors, process  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

from bords_outils import (NOEUD, agent as noeud_agent, attend, commande,  # noqa: E402
                          contexte, issue, passation, patche)
from outils import data_dir_jetable, groupe_vivant  # noqa: E402

MARQUEUR = agent.WAIT_FILE
TETU = "trap '' TERM; echo pret > {temoin}; sleep 30"  # un groupe qui ignore SIGTERM
STAT = "4242 (agent) " + " ".join(["S"] + ["0"] * 18 + ["7"])  # naissance : 7


def usage(workdir: Path) -> None:
    """1. illisible ou vide, aucun usage ; un dictionnaire plein est rendu tel quel."""
    espace = workdir / "usage"
    espace.mkdir()
    (espace / bureau.USAGE_NAME).mkdir()  # un répertoire : illisible
    assert agent._usage(espace) == {}
    (espace / bureau.USAGE_NAME).rmdir()
    (espace / bureau.USAGE_NAME).write_text("{}")
    assert agent._usage(espace) == {}
    (espace / bureau.USAGE_NAME).write_text('{"input_tokens": 3}')
    assert agent._usage(espace) == {"usage": {"input_tokens": 3}}
    print("1. usage.json illisible ou vide : rien ; plein : rendu tel quel ✓")


def marche_des_mtimes(workdir: Path) -> None:
    """2. un fichier disparu pendant la marche ne fait pas tomber le relevé."""
    arbre = workdir / "arbre"
    arbre.mkdir()
    (arbre / "present.txt").write_text("là")
    (arbre / "disparu.txt").symlink_to(arbre / "jamais-ecrit.txt")  # stat échoue
    assert agent._latest_mtime(arbre) == (arbre / "present.txt").stat().st_mtime
    releve = agent.mark(arbre / "journal-absent.log", arbre, None)
    assert releve[0] == 0 and releve[2] == 0.0, releve
    print("2. fichier disparu pendant la marche toléré ; journal absent = 0 octet ✓")


def attente_du_quota() -> None:
    """3. le marqueur d'attente du quota suspend le budget ; sans lui, il tombe."""
    travail = f"touch {MARQUEUR}; sleep 1.6; rm {MARQUEUR}; {issue()}"
    depart = time.monotonic()
    resultat = blocks.act(contexte(commande(travail, timeout_s=1.0), node="quota"))
    duree = time.monotonic() - depart
    assert resultat == {"outcome": "ok", "summary": "fait"}, resultat
    assert duree >= 1.6, duree
    temoin = blocks.act(contexte(commande(f"sleep 1.6; {issue()}", timeout_s=1.0),
                                 node="sans-quota"))
    assert temoin["outcome"] == "stalled" and temoin["timeout"] is True, temoin
    print(f"3. 1.6 s sous le marqueur du quota avec 1.0 s de budget : ok en "
          f"{duree:.1f} s ; la même attente sans marqueur : couperet ✓")


def base_jetable() -> None:
    """4. la base de l'agent est la sienne ; quota et heartbeat gardent celle du rail."""
    travail = "env | grep '^GRAPHATOM_.*DSN=' > dsn.txt; " + issue()
    with patche(db, "agent_dsn", lambda item_id: f"postgresql://jetable/item{item_id}"):
        resultat = blocks.act(contexte(commande(travail), node="dsn", item_id=9))
    assert resultat["outcome"] == "ok", resultat
    vues = dict(ligne.split("=", 1) for ligne in
                (bureau.item_workspace(9) / "dsn.txt").read_text().splitlines())
    assert vues["GRAPHATOM_DSN"] == "postgresql://jetable/item9", vues
    assert vues["GRAPHATOM_QUOTA_DSN"] == db.DSN, vues
    assert vues["GRAPHATOM_HEARTBEAT_DSN"] == db.DSN, vues
    print("4. GRAPHATOM_DSN de l'agent : sa base jetable ; quota et heartbeat : "
          "celle du rail ✓")


def interruption(workdir: Path) -> None:
    """5. le bloc interrompu révoque son groupe, puis seulement remonte l'erreur."""
    pgid_txt = bureau.item_workspace(0) / "pgid.txt"

    def interrompu(*_args):
        assert attend(pgid_txt.exists), "l'agent n'a pas noté son groupe"
        raise KeyboardInterrupt("le worker s'arrête")

    ctx = contexte(commande("ps -o pgid= -p $$ > pgid.tmp; mv pgid.tmp pgid.txt; sleep 30"), node="coupe")
    with patche(agent, "_wait", interrompu):
        try:
            blocks.act(ctx)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("l'interruption n'est pas remontée")
    pgid = int(pgid_txt.read_text())
    assert attend(lambda: not groupe_vivant(pgid)), f"le groupe {pgid} survit"
    assert not (ctx.workspace / bureau.PGID_FILE).exists(), "trace de pgid laissée"
    assert (workdir / "item-0" / "agent-coupe-1-1.log").exists(), "journal perdu"
    print("5. bloc interrompu : groupe révoqué, trace effacée, erreur remontée ✓")


def capacite_activation() -> None:
    """6. `deploy_sha` est une capacité : sans `activation`, c'est un crash."""
    sha = "a" * 40
    sans = blocks.act(contexte(commande(issue("done", deploy_sha=sha)), node="sans"))
    assert sans["outcome"] == "crashed", sans
    assert "activation" in sans["error"], sans
    declare = commande(issue("done", deploy_sha=sha))
    declare["config"]["activation"] = True
    avec = blocks.act(contexte(declare, node="avec"))
    assert avec.get("deploy_sha") == sha, avec
    print("6. deploy_sha sans activation déclarée : crashed ; déclarée : demande ✓")


def porte_et_passation() -> None:
    """7. la porte déclarée rétrograde un `pass` ; muette sinon, passation exigée."""
    verdict = bureau.item_workspace(0) / bureau.VERDICT_NAME
    verdict.write_text("## Finaliste B\n\n1. Tenu. oui\n2. Raté. preuve absente\n\n"
                       "## Verdict\n\nÉlu : finaliste B\n")
    noeud = noeud_agent(f"{passation()}; {issue('pass')}", passation=True,
                        gate="elected_verdict")
    ferme = blocks.act(contexte(noeud, node=NOEUD))
    assert ferme["outcome"] == "fail" and "2" in ferme["summary"], ferme
    verdict.write_text(verdict.read_text().replace("Raté", "Tenu"))
    ouvert = blocks.act(contexte(noeud, node=NOEUD, attempt=2))
    assert ouvert == {"outcome": "pass", "summary": "fait"}, ouvert
    sans = blocks.act(contexte(noeud_agent(issue("pass"), passation=True), attempt=3))
    assert sans["outcome"] == "crashed" and "passation" in sans["error"], sans
    print("7. porte elected_verdict : pass → fail sur un raté, muette sans ; "
          "passation valide acceptée, absente refusée ✓")


def contrat_d_issue(workdir: Path) -> None:
    """8. le contrat ne prend que des chaînes, et un SHA canonique complet."""
    fichier = workdir / "issue.json"
    refus = []
    for contenu in ({"outcome": 1}, {"outcome": "ok", "summary": ["x"]},
                    {"outcome": "ok", "deploy_sha": "abc"},
                    {"outcome": "ok", "deploy_sha": 42}):
        fichier.write_text(json.dumps(contenu))
        try:
            agent.read_outcome(fichier)
        except (TypeError, ValueError) as exc:
            refus.append(type(exc).__name__)
        else:
            raise AssertionError(f"accepté : {contenu}")
    assert refus == ["TypeError", "TypeError", "ValueError", "ValueError"], refus
    print("8. issue ou résumé non chaîne : TypeError ; deploy_sha tronqué ou "
          "non chaîne : ValueError ✓")


def panne_vide(workdir: Path) -> None:
    """9. une panne déclarée sans fournisseur, ou sans raison, ne vaut rien."""
    fichier = workdir / bureau.STARVED_NAME
    for contenu in ({"provider": " ", "reason": "quota"},
                    {"provider": "codex", "reason": ""},
                    {"provider": 3, "reason": "quota"}):
        fichier.write_text(json.dumps(contenu))
        assert agent._starved(fichier) is None, contenu
    fichier.write_text(json.dumps({"provider": "codex", "reason": "quota"}))
    assert agent._starved(fichier)["outcome"] == "starved"
    print("9. fournisseur ou raison vides : aucune panne reconnue ✓")


def groupe_tetu(workdir: Path, nom: str) -> subprocess.Popen:
    """Un groupe dont le chef et son descendant ignorent SIGTERM, une fois armés."""
    temoin = workdir / f"tetu-{nom}.txt"
    proc = subprocess.Popen(["sh", "-c", TETU.format(temoin=temoin)],
                            start_new_session=True)
    assert attend(temoin.exists), "le piège TERM n'est jamais armé"
    return proc


def revocation_du_groupe(workdir: Path) -> None:
    """10. chef déjà parti : rien ; groupe qui ignore SIGTERM : SIGKILL après la grâce."""
    parti = subprocess.Popen(["true"], start_new_session=True)
    parti.wait()
    process.kill_group(parti)  # ProcessLookupError absorbé
    assert parti.returncode == 0

    tetu = groupe_tetu(workdir, "chef")
    depart = time.monotonic()
    with patche(process, "GRACE_S", 0.5):
        process.kill_group(tetu)
    duree = time.monotonic() - depart
    assert tetu.returncode == -signal.SIGKILL, tetu.returncode
    assert duree >= 0.5, duree
    assert attend(lambda: not groupe_vivant(tetu.pid)), "un descendant survit"
    print(f"10. chef parti : rien à révoquer ; groupe têtu : SIGKILL après "
          f"{duree:.1f} s de grâce ✓")


def residus(workdir: Path) -> None:
    """11. ce qui survit à la grâce du groupe résiduel tombe au SIGKILL."""
    tetu = groupe_tetu(workdir, "residu")
    depart = time.monotonic()
    with patche(process, "GRACE_S", 0.5):
        process.revoke_residual_group(tetu.pid)
    duree = time.monotonic() - depart
    tetu.wait()
    assert tetu.returncode == -signal.SIGKILL, tetu.returncode
    assert duree >= 0.5, duree
    assert attend(lambda: not groupe_vivant(tetu.pid)), "un descendant survit"
    print(f"11. groupe résiduel têtu : SIGKILL après {duree:.1f} s ✓")


def faux_proc(workdir: Path, stat: str | None, boot: str | None) -> Path:
    """Un `/proc` factice : le stat du pid 4242 et le boot_id, ou leur absence."""
    proc = workdir / "proc"
    for chemin in (proc / "4242", proc / "sys/kernel/random"):
        chemin.mkdir(parents=True, exist_ok=True)
    (proc / "4242/stat").unlink(missing_ok=True)
    if stat is not None:
        (proc / "4242/stat").write_text(stat)
    (proc / "sys/kernel/random/boot_id").unlink(missing_ok=True)
    if boot is not None:
        (proc / "sys/kernel/random/boot_id").write_text(boot)
    return proc


def etat_de_l_agent(workdir: Path) -> None:
    """12. identité indécidable ou décidée, selon ce que `/proc` laisse lire."""
    (bureau.item_workspace(0) / bureau.PGID_FILE).write_text(json.dumps({
        "run": 1, "pgid": 4242, "cmd": "sleep",
        "identity": {"boot": "boot-a", "starttime": 7},
    }))
    cas = [(STAT, "boot-a\n", process.AGENT_ALIVE),
           (STAT, "boot-b\n", process.AGENT_DEAD),
           ("4242 (agent) S", "boot-a", process.AGENT_UNKNOWN),  # stat amputé
           (STAT, None, process.AGENT_UNKNOWN),  # boot_id absent
           (STAT, "", process.AGENT_UNKNOWN),  # boot_id vide
           (None, "boot-a", process.AGENT_UNKNOWN)]  # pid absent, /proc absent
    for stat, boot, attendu in cas:
        with patche(process, "PROC", faux_proc(workdir, stat, boot)):
            assert process.agent_state(0, 1) == attendu, (stat, boot, attendu)
    proc = faux_proc(workdir, None, "boot-a")
    (proc / "self").mkdir()
    (proc / "self/stat").write_text(STAT)
    (proc / "4242/stat").mkdir()  # un stat illisible autrement qu'absent
    with patche(process, "PROC", proc):
        assert process.agent_state(0, 1) == process.AGENT_UNKNOWN
        (proc / "4242/stat").rmdir()
        assert process.agent_state(0, 1) == process.AGENT_DEAD
    print("12. stat amputé, boot_id absent ou vide, stat illisible : unknown ; "
          "pid absent sous un /proc vivant : dead ; identité égale : alive ✓")


def faucheur_intouchable() -> None:
    """13. une trace qui nomme le groupe du faucheur ne tue personne."""
    trace = bureau.item_workspace(0) / bureau.PGID_FILE
    pgid = os.getpgid(0)
    trace.write_text(json.dumps({"run": 1, "pgid": pgid, "cmd": "moi",
                                 "identity": process.identity(pgid)}))
    assert process.revoke_orphan(0, 1) is None
    assert not trace.exists(), "la trace aberrante a survécu"
    print("13. trace aberrante sur notre propre pgid : refusée, effacée ✓")


def adaptateurs() -> None:
    """14. la commande ne porte que ce qui est déclaré ; une CLI inconnue est refusée."""
    claude = executors.ADAPTERS["claude"]
    nue = claude.command(None)
    assert "CLAUDE_MODEL" not in nue and nue.startswith("bash "), nue
    assert "CLAUDE_MODEL=opus" in claude.command("opus", timeout_s=3), "modèle absent"
    sans_effort = claude.command("opus", effort="high")
    assert "high" not in sans_effort, sans_effort  # claude n'a pas de variable d'effort
    assert "CODEX_REASONING_EFFORT=high" in executors.ADAPTERS["codex"].command(
        "o3", effort="high")
    inconnue = executors.Executor(cli="gemini", model="m", cmd=None)
    try:
        executors.command(inconnue)
    except ValueError as exc:
        assert "gemini" in str(exc), exc
    else:
        raise AssertionError("une CLI inconnue a rendu une commande")
    assert executors.environment(inconnue) == {}
    print("14. modèle et effort absents non émis ; effort sans variable ignoré ; "
          "CLI inconnue refusée ✓")


def main() -> None:
    workdir = data_dir_jetable("graphatom-agent-edges-")
    bureau.item_workspace(0).mkdir(parents=True)
    usage(workdir)
    marche_des_mtimes(workdir)
    attente_du_quota()
    base_jetable()
    interruption(workdir)
    capacite_activation()
    porte_et_passation()
    contrat_d_issue(workdir)
    panne_vide(workdir)
    revocation_du_groupe(workdir)
    residus(workdir)
    etat_de_l_agent(workdir)
    faucheur_intouchable()
    adaptateurs()
    print("\nbords de l'agent : OK")


if __name__ == "__main__":
    main()
