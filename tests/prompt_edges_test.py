"""Les bords du prompt, de la trace d'échec et de la signature de relance.

Sans base : une base réduite à ses réponses, un dépôt jetable, et les trois
modules poussés là où un test nominal ne va jamais :

  1. une exécution agent sans prompt résolu est refusée, pas devinée
  2. la passation : absente, trop longue, mal sectionnée, précédée de texte
     ou à section vide — chaque défaut est nommé ; la bonne passe
  3. le prédécesseur : un événement sans run ne pose aucun bloc ; avec un run,
     le bloc porte son issue, sa passation bornée et la queue de son journal ;
     un nœud arbitre n'en reçoit jamais
  4. la mort d'avant est dite : fournisseur et raison quand ils sont là,
     la formule générique sinon
  5. sans worktree, rien n'est inventé : pas de reprise sans fichier, et le
     prompt dit « aucun worktree » quand il y en a une
  6. `origin/main` absent, ou aucune avance dessus : aucun journal de
     commits ; des commits d'avance sont cités, et l'état git est borné
  7. la trace d'échec : un rapport dont le stat échoue ne casse rien, un
     rapport illisible est dit tel quel, aucun rapport vaut None
  8. la signature : un lien symbolique, un fichier trop grand ou illisible
     rendent la tentative incomparable
  9. un fichier supprimé du worktree signe « absent », et change la signature
 10. trop de fichiers, trop d'entrées, un workspace disparu : incomparable
 11. un atelier qui n'est pas un dépôt git, ou un échec structuré trop lourd :
     incomparable ; sans atelier du tout, la signature existe

Usage : uv run python tests/prompt_edges_test.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.pop("GRAPHATOM_REPO_DIR", None)

from graphatom import executors, failure, prompt, signature  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

from bords_outils import (NOEUD, SUJET, FauxConn, agent as noeud_agent,  # noqa: E402
                          commande, contexte, patche)
from outils import atelier_item, data_dir_jetable, depot_git, git  # noqa: E402

PASSATION = "## Fait\nTout.\n\n## Appris\nRien.\n\n## Pas fait\nRien.\n"
PRECEDENT = {"id": 5, "node": "amont", "cycle": 1, "attempt": 1,
             "candidate": None, "outcome": "done"}
SANS_ATELIER = 40  # un item sans worktree : rien à reprendre côté git
AVEC_ATELIER = 41  # l'item dont le dépôt jetable porte l'atelier


def sans_prompt() -> None:
    """1. une exécution agent sans prompt résolu est une erreur, pas un prompt vide."""
    ctx = contexte(commande("true"))
    try:
        prompt.build(ctx, ctx.workspace, SUJET, executors.resolve({}, ctx.node))
    except ValueError as exc:
        assert NOEUD in str(exc) and "sans prompt" in str(exc), exc
    else:
        raise AssertionError("un prompt a été rendu sans prompt")
    print("1. exécution agent sans prompt résolu : ValueError nommant le nœud ✓")


def passations(workdir: Path) -> None:
    """2. chaque défaut de passation est nommé ; la bonne passe."""
    fichier = workdir / "passation-x.md"
    cas = [
        (None, "passation absente"),
        ("## Fait\n" + "x" * prompt.PASSATION_CHARS, "passation trop longue"),
        ("## Fait\nx\n## Pas fait\ny\n", "trois sections exactes attendues"),
        ("Préambule\n" + PASSATION, "précédées de texte"),
        ("## Fait\n\n## Appris\nx\n## Pas fait\ny\n", "section '## Fait' vide"),
        ("  ## Fait\nTout.\n## Appris\nRien.\n## Pas fait\nRien.", None),
    ]
    for texte, attendu in cas:
        fichier.unlink(missing_ok=True)
        if texte is not None:
            fichier.write_text(texte)
        verdict = prompt.passation_invalide(fichier)
        assert (verdict is None) if attendu is None else (attendu in verdict), \
            (texte, verdict)
    print("2. absente, trop longue, mal sectionnée, précédée de texte, section "
          "vide : nommées ; la passation valide passe ✓")


def predecesseur() -> None:
    """3. sans run dans l'événement, aucun bloc ; avec, l'issue, la passation, le journal."""
    sans_run = contexte(noeud_agent("true"), FauxConn({"FROM event": {"run_id": None}}))
    texte = prompt.build(sans_run, sans_run.workspace, SUJET)
    assert "Passation de" not in texte, texte

    conn = FauxConn({"FROM event": {"run_id": 5}, "FROM node_run WHERE id": PRECEDENT})
    ctx = contexte(noeud_agent("true"), conn)
    bureau.passation_path(0, PRECEDENT).write_text(
        "## Fait\n" + "détail " * prompt.PASSATION_CHARS)
    bureau.attempt_log(ctx.workspace, PRECEDENT).write_text("ligne 1\nfin du journal\n")
    texte = prompt.build(ctx, ctx.workspace, SUJET)
    assert "Passation de « amont »" in texte and "issue « done »" in texte, texte
    assert "… (tronqué)" in texte and "fin du journal" in texte, texte

    juge = contexte(noeud_agent("true", finalists_from="amont"), conn, node="juge")
    assert "Passation de" not in prompt.build(juge, juge.workspace, SUJET)
    print("3. événement sans run : aucun bloc ; avec run : issue, passation "
          "bornée, queue du journal ; nœud arbitre : rien ✓")


def morts() -> None:
    """4. la mort d'avant : le fournisseur et sa raison, sinon la formule générique."""
    nommee = prompt.death({"outcome": "starved",
                           "result": {"provider": "codex", "reason": "0 jeton"}})
    assert "codex : 0 jeton" in nommee, nommee
    muette = prompt.death({"outcome": "starved", "result": {"provider": "codex"}})
    assert "a rendu « starved »" in muette, muette
    inconnue = prompt.death({"outcome": "bizarre"})
    assert "a rendu « bizarre »" in inconnue, inconnue
    assert "dépassé son budget" in prompt.death({"outcome": "timed_out"})
    print("4. starved avec fournisseur et raison : nommés ; sans : formule "
          "générique, comme une issue inconnue ✓")


def sans_worktree() -> None:
    """5. sans atelier, pas de reprise inventée ; avec un fichier, le prompt le dit."""
    conn = FauxConn({"FROM node_run WHERE item_id": {
        "cycle": 1, "attempt": 1, "outcome": "crashed", "result": {}}})
    ctx = contexte(noeud_agent("true"), conn, item_id=SANS_ATELIER, attempt=2)
    assert ctx.worktree is None
    assert "Reprise" not in prompt.build(ctx, ctx.workspace, SUJET)
    (ctx.workspace / "brouillon.txt").write_text("un fichier d'agent\n")
    texte = prompt.build(ctx, ctx.workspace, SUJET)
    assert "Reprise de la tentative 1" in texte, texte
    assert "aucun worktree pour cet item" in texte, texte
    assert "- brouillon.txt" in texte, texte
    print("5. sans worktree ni fichier : aucune reprise ; un fichier : reprise "
          "qui dit « aucun worktree » ✓")


def commits_d_avance(tmp: Path) -> None:
    """6. `origin/main` absent : rien ; présent : les commits d'avance, bornés."""
    isole = tmp / "isole"
    isole.mkdir()
    git(isole, "init", "-q", "-b", "main")
    git(isole, "commit", "-q", "--allow-empty", "-m", "seul")
    assert prompt._committed(isole) == "", "un journal sans origin/main"

    repo = depot_git(tmp, "prompt")
    _, atelier = atelier_item(repo, AVEC_ATELIER)
    assert "$ git log" not in prompt._git_state(atelier), "un journal sans avance"
    (atelier / "avance.txt").write_text("commité, pas fusionné\n")
    git(atelier, "add", "-A")
    git(atelier, "commit", "-qm", "une avance sur origin/main")
    conn = FauxConn({"FROM node_run WHERE item_id": {
        "cycle": 1, "attempt": 1, "outcome": "timed_out", "result": {}}})
    ctx = contexte(noeud_agent("true"), conn, item_id=AVEC_ATELIER, attempt=2)
    assert ctx.worktree == atelier, ctx.worktree
    texte = prompt.build(ctx, ctx.workspace, SUJET)
    assert "$ git log --oneline origin/main..HEAD" in texte, texte
    assert "une avance sur origin/main" in texte, texte
    with patche(prompt, "GIT_CHARS", 40):
        borne = prompt.build(ctx, ctx.workspace, SUJET)
    assert "… (tronqué)" in borne and "une avance" not in borne, borne
    print("6. sans origin/main : aucun journal ; avec : les commits d'avance "
          "cités, et l'état git borné à GIT_CHARS ✓")


def rapport(item_id: int, run: dict) -> dict | None:
    """Le rapport que la trace d'échec retient pour ce run."""
    return json.loads(failure.write_failure_trace(item_id, run, "crashed")
                      .read_text())["report"]


def trace_d_echec() -> None:
    """7. stat qui échoue : rien ; rapport illisible : dit tel quel ; aucun : None."""
    run = {"node": NOEUD, "cycle": 1, "attempt": 1}
    espace = bureau.item_workspace(50)
    espace.mkdir(parents=True)
    (espace / bureau.PROMPT_NAME).write_text("le prompt de la tentative\n")
    assert rapport(50, run) is None, "un rapport sans Markdown"
    (espace / "fantome.md").symlink_to(espace / "jamais-la.md")
    assert rapport(50, run) is None, "un stat qui échoue a rendu un rapport"
    (espace / "fantome.md").unlink()
    (espace / "compte-rendu.md").mkdir()
    illisible = rapport(50, run)
    assert illisible["name"] == "compte-rendu.md", illisible
    assert "[compte rendu illisible" in illisible["content"], illisible
    print("7. Markdown au stat impossible : aucun rapport ; rapport illisible : "
          "nommé et dit illisible ; sans Markdown : None ✓")


def commande_de(item_id: int, run: dict) -> Path:
    """La commande tracée de la tentative, ce que toute signature exige."""
    trace = bureau.attempt_command(bureau.run_workspace(item_id, run), run)
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text('{"command": "true"}\n')
    return trace


def fichiers_bornes(tmp: Path) -> None:
    """8. lien symbolique, fichier trop grand ou illisible : incomparable."""
    run = {"node": NOEUD, "cycle": 1, "attempt": 1}
    trace = commande_de(SANS_ATELIER, run)
    assert signature.retry_signature(SANS_ATELIER, run, "crashed") is not None
    trace.unlink()
    trace.symlink_to(tmp / "ailleurs.json")
    assert signature.retry_signature(SANS_ATELIER, run, "crashed") is None, "lien"
    trace.unlink()
    trace.write_text("x" * (signature.SIGNATURE_BYTES + 1))
    assert signature.retry_signature(SANS_ATELIER, run, "crashed") is None, "taille"
    assert signature._file_digest(Path("/proc/self/mem")) is None, "lecture"
    print("8. commande en lien symbolique, trop grande ou illisible : aucune "
          "signature ✓")


def fichier_supprime(atelier: Path) -> None:
    """9. un fichier supprimé du worktree signe « absent », et ça change tout."""
    run = {"node": NOEUD, "cycle": 1, "attempt": 1}
    commande_de(AVEC_ATELIER, run)
    avant = signature.retry_signature(AVEC_ATELIER, run, "crashed")
    (atelier / "socle.txt").unlink()
    apres = signature.retry_signature(AVEC_ATELIER, run, "crashed")
    assert avant is not None and apres is not None and avant != apres
    (atelier / "lourd.bin").write_bytes(b"\0" * (signature.SIGNATURE_BYTES + 1))
    assert signature.retry_signature(AVEC_ATELIER, run, "crashed") is None
    (atelier / "lourd.bin").unlink()
    git(atelier, "checkout", "--", "socle.txt")
    print("9. fichier supprimé : signé absent, signature changée ; fichier "
          "trop grand dans l'atelier : incomparable ✓")


def workspace_borne(tmp: Path) -> None:
    """10. trop de fichiers durables, trop d'entrées, workspace disparu."""
    run = {"node": NOEUD, "cycle": 1, "attempt": 1}
    espace = bureau.item_workspace(60)
    commande_de(60, run)
    for i in range(signature.SIGNATURE_FILES + 1):
        (espace / f"durable-{i}.txt").write_text("travail")
    assert signature.retry_signature(60, run, "crashed") is None, "fichiers"
    for chemin in espace.glob("durable-*.txt"):
        chemin.unlink()
    for i in range(signature.SIGNATURE_ENTRIES):
        (espace / f"agent-vieux-{i}.log").write_text("")  # ignoré, mais compté
    assert signature.retry_signature(60, run, "crashed") is None, "entrées"
    assert signature._files_digest(
        tmp, [f"f{i}" for i in range(signature.SIGNATURE_FILES + 1)]) is None
    assert signature._workspace_digest(tmp / "disparu") is None
    print("10. plus de 100 fichiers durables, plus de 256 entrées, workspace "
          "disparu : incomparable ✓")


def atelier_borne(repo: Path) -> None:
    """11. atelier sans git, échec structuré trop lourd : incomparable ; sans
    atelier, la signature existe."""
    run = {"node": NOEUD, "cycle": 1, "attempt": 1,
           "result": {"error": "x" * (signature.SIGNATURE_BYTES + 1)}}
    commande_de(SANS_ATELIER, run)
    assert signature.retry_signature(SANS_ATELIER, run, "crashed") is None, "lourd"
    run["result"] = {"error": "léger"}
    assert signature.retry_signature(SANS_ATELIER, run, "crashed") is not None
    faux = repo / ".worktrees" / f"rail-item-{SANS_ATELIER}"
    faux.mkdir(parents=True)
    (faux / ".git").write_text("gitdir: /nulle/part\n")  # un renvoi cassé : git échoue
    try:
        assert signature.retry_signature(SANS_ATELIER, run, "crashed") is None, "git"
    finally:
        shutil.rmtree(faux)
    print("11. échec structuré trop lourd ou atelier sans git : incomparable ; "
          "sans atelier : signée ✓")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-prompt-edges-"))
    workdir = data_dir_jetable("graphatom-prompt-edges-data-")
    bureau.item_workspace(0).mkdir(parents=True)
    try:
        sans_prompt()
        passations(workdir)
        predecesseur()
        morts()
        sans_worktree()
        commits_d_avance(tmp)
        trace_d_echec()
        fichiers_bornes(tmp)
        repo = Path(os.environ["GRAPHATOM_REPO_DIR"])
        fichier_supprime(repo / ".worktrees" / f"rail-item-{AVEC_ATELIER}")
        workspace_borne(tmp)
        atelier_borne(repo)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)
    print("\nbords du prompt, de la trace et de la signature : OK")


if __name__ == "__main__":
    main()
