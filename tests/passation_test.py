"""Le test de la passation : ce qu'un nœud a appris, ou laissé tomber, circule.

Un graph est une chaîne, mais le savoir n'y circulait pas : chaque nœud
démarrait en contexte neuf et redécouvrait ce que le précédent avait payé.
Pire, un renoncement que personne ne nommait ne laissait aucune trace. Ce
test fige le contrat des deux côtés — ce qu'un nœud écrit, ce que le suivant
lit — sans base ni ordonnanceur : seul le prompt du bloc agent est en jeu.

  1. le contrat de tout nœud agent demande sa passation, en trois sections
     nommées, dans son propre workspace
  1 bis. une réussite sans nouvelle passation valide est refusée : le vieux
     fichier est purgé, les trois sections sont obligatoires et non vides
  2. le prompt porte la passation de son prédécesseur, son issue et la queue
     de son journal
  3. un nœud d'entrée ne reçoit rien : ni bloc, ni mention creuse — pas plus
     qu'un nœud atteint par une réponse humaine, dont l'événement n'a pas de run
  4. le contenu transmis est borné : la passation à `PASSATION_CHARS`, le
     journal à `TAIL_LINES` lignes
  5. et il ne remonte pas au-delà du prédécesseur immédiat : sur une chaîne
     longue, le dernier nœud ne voit que l'avant-dernier
  6. une relance du même nœud lit la tentative précédente de ce nœud-là
  7. en fan-out, la passation lue est celle du candidat que l'événement
     porte, dans son `c<k>/` — pas celle d'un voisin ; et le nœud arbitre,
     lui, ne reçoit rien : son dossier reste anonyme
  8. un prédécesseur qui n'a rendu aucune passation le dit, et ne fait
     tomber personne

Usage : uv run python tests/passation_test.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks  # noqa: E402

# hermétisme : ni dépôt ni instance jetable dans l'environnement, sinon le
# bloc irait lire l'atelier d'un item qui existe pour de vrai
os.environ.pop("GRAPHATOM_REPO_DIR", None)
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

ITEM = 4  # l'item du contexte de test : jamais un item de la base
SUJET = "gh:test/passation#170"
SECTIONS = ("## Fait", "## Appris", "## Pas fait")
ROOT = Path(__file__).resolve().parents[1]
BUNDLE = json.loads((ROOT / "examples" / "code-task.json").read_text())


class FauxConn:
    """La base réduite à ce que le prompt lui demande.

    Trois questions, et pas une de plus : la clé du sujet, l'événement qui a
    fait entrer l'item dans son état, et le run que cet événement nomme. La
    quatrième — la tentative antérieure du nœud courant — reste sans réponse :
    aucune reprise ne vient brouiller le bloc qu'on regarde.
    """

    def __init__(self, events: list[dict], runs: dict[int, dict]):
        self.events = events  # dans l'ordre des versions d'item
        self.runs = runs
        self.sql, self.args = "", ()

    def execute(self, sql, args=()):
        self.sql, self.args = sql, args
        return self

    def fetchone(self) -> dict | None:
        if "FROM subject" in self.sql:
            return {"subject_key": SUJET}
        if "FROM event" in self.sql:  # le dernier événement menant à ce nœud
            menant = [e for e in self.events if e["to_state"] == self.args[1]]
            return {"run_id": menant[-1]["run_id"]} if menant else None
        if "WHERE id = %s" in self.sql:  # le run que l'événement nomme
            return self.runs.get(self.args[0])
        return None  # aucune tentative antérieure : pas de bloc de reprise


def run(run_id: int, node: str, outcome: str, candidate: int | None = None) -> dict:
    return {"id": run_id, "node": node, "cycle": 1, "attempt": 1,
            "candidate": candidate, "outcome": outcome}


def contexte(workdir: Path, node: str, events: list[dict],
             runs: dict[int, dict]) -> blocks.Context:
    blocks.DATA_DIR = workdir
    spec = {"block": "ACT", "edges": {"done": "suite"},
            "config": {"agent": {"cmd": "true", "prompt": "fais le travail"}}}
    return blocks.Context(
        FauxConn(events, runs),
        {"id": 99, "node": node, "cycle": 1, "attempt": 1},
        {"id": ITEM, "subject_id": 1},
        spec,
        {"name": "passation"},
    )


def prompt(workdir: Path, node: str, events: list[dict],
           runs: dict[int, dict]) -> str:
    ctx = contexte(workdir, node, events, runs)
    return blocks._prompt(ctx, ctx.workspace.resolve(), SUJET)


def ecrire(workdir: Path, run: dict, passation: str, journal: str = "") -> None:
    """Ce qu'un nœud laisse dans son workspace : sa passation, son journal."""
    workspace = blocks.run_workspace(ITEM, run)
    workspace.mkdir(parents=True, exist_ok=True)
    blocks.passation_path(ITEM, run).write_text(passation)
    if journal:
        blocks.attempt_log(workspace, run).write_text(journal)


def demande(workdir: Path) -> None:
    """1. tout nœud agent est nommément prié de laisser sa passation."""
    texte = prompt(workdir, "scope", [], {})
    for section in SECTIONS:
        assert section in texte, section
    assert "passation-scope.md" in texte, texte
    assert "même quand tu réussis" in texte, texte
    print("1. le contrat demande passation-scope.md et ses trois sections "
          f"{list(SECTIONS)} ✓")


def passation_obligatoire(workdir: Path) -> None:
    """1 bis. une réussite porte une passation neuve, complète et bornée."""
    ctx = contexte(workdir, "scope", [], {})
    fichier = blocks.passation_path(ITEM, ctx.run)
    fichier.write_text("ancienne passation sans sections")
    ctx.config["agent"]["cmd"] = (
        "printf '%s' '{\"outcome\":\"done\",\"summary\":\"trop tôt\"}' "
        "> outcome.json"
    )
    resultat = blocks._attempt(ctx, ctx.workspace.resolve())
    assert resultat["outcome"] == "crashed", resultat
    assert "passation absente" in resultat["error"], resultat
    assert not fichier.exists(), "la passation de la tentative précédente a survécu"

    ctx.config["agent"]["cmd"] = (
        "printf '%s\\n' '## Fait' 'travail fait' '' '## Appris' 'rien' '' "
        "'## Pas fait' 'rien' > passation-scope.md; "
        "printf '%s' '{\"outcome\":\"done\",\"summary\":\"complet\"}' "
        "> outcome.json"
    )
    resultat = blocks._attempt(ctx, ctx.workspace.resolve())
    assert resultat == {"outcome": "done", "summary": "complet"}, resultat
    assert blocks._passation_invalide(fichier) is None

    fichier.write_text("## Fait\noui\n\n## Appris\n\n## Pas fait\nrien\n")
    assert "section '## Appris' vide" == blocks._passation_invalide(fichier)
    print("1 bis. vieille passation purgée ; succès refusé sans trois sections "
          "neuves, ordonnées, remplies et bornées ✓")


def portes_de_pertinence(workdir: Path) -> None:
    """1 ter. Les portes shell rendent le contrat complet sans modèle."""
    repo = workdir / "repo"
    atelier = repo / ".worktrees" / f"rail-item-{ITEM}"
    scripts = atelier / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "agent-declared.sh",
                 scripts / "agent-declared.sh")
    (scripts / "agent-codex.sh").write_text(
        "printf appelé > \"$GRAPHATOM_WORKSPACE/agent-called\"\n"
        "printf '%s\\n' '{\"outcome\":\"pass\",\"summary\":\"agent appelé\"}' "
        "> outcome.json\n"
    )
    outils = workdir / "bin"
    outils.mkdir()
    faux_git = outils / "git"
    faux_git.write_text("#!/bin/sh\nprintf '%s\\n' \"${FAKE_DIFF:-}\"\n")
    faux_git.chmod(0o755)

    ancien_repo = os.environ.get("GRAPHATOM_REPO_DIR")
    ancien_path = os.environ["PATH"]
    os.environ["GRAPHATOM_REPO_DIR"] = str(repo)
    os.environ["PATH"] = f"{outils}:{ancien_path}"
    try:
        for node in ("test_backend", "test_frontend"):
            spec = BUNDLE["nodes"][node]
            run = {"id": 70, "node": node, "cycle": 1, "attempt": 1,
                   "candidate": None}
            ctx = blocks.Context(FauxConn([], {}), run,
                                 {"id": ITEM, "subject_id": 1}, spec, BUNDLE)
            ctx.worktree = atelier
            rapport = ctx.workspace / f"{node}.md"
            passation = ctx.workspace / f"passation-{node}.md"
            appel = ctx.workspace / "agent-called"

            rapport.write_text("ancien rapport\n")
            appel.unlink(missing_ok=True)
            os.environ["FAKE_DIFF"] = "README.md"
            resultat = blocks._attempt(ctx, ctx.workspace.resolve())
            assert resultat["outcome"] == "pass", (node, resultat)
            assert not appel.exists(), node
            assert blocks._passation_invalide(passation) is None, node
            assert "modèle n'a pas été appelé" in passation.read_text(), node
            assert "README.md" in passation.read_text(), node
            assert "non concerné" in rapport.read_text(), node
            assert "ancien rapport" not in rapport.read_text(), node

            os.environ["FAKE_DIFF"] = ""
            resultat = blocks._attempt(ctx, ctx.workspace.resolve())
            assert resultat["outcome"] == "fail", (node, resultat)
            assert "diff vide" in resultat["summary"], (node, resultat)
            assert "diff vide" in rapport.read_text(), node
            assert blocks._passation_invalide(passation) is None, node

            os.environ["FAKE_DIFF"] = (
                "src/graphatom/web.py" if node == "test_frontend"
                else "src/graphatom/kernel.py"
            )
            appel.unlink(missing_ok=True)
            resultat = blocks._attempt(ctx, ctx.workspace.resolve())
            assert appel.read_text() == "appelé", node
            assert resultat["outcome"] == "crashed", (node, resultat)
            assert "passation absente" in resultat["error"], (node, resultat)
    finally:
        os.environ.pop("FAKE_DIFF", None)
        os.environ["PATH"] = ancien_path
        if ancien_repo is None:
            os.environ.pop("GRAPHATOM_REPO_DIR", None)
        else:
            os.environ["GRAPHATOM_REPO_DIR"] = ancien_repo

    print("1 ter. backend et frontend : court-circuit complet sans modèle, "
          "diff vide lisible et chemin pertinent soumis à la passation ✓")


def transmission(workdir: Path) -> None:
    """2. le prompt du suivant porte passation, issue et queue de journal."""
    amont = run(11, "implement", "done")
    ecrire(workdir, amont, "## Fait\nle code\n\n## Appris\nle port 8850 est pris\n\n"
                           "## Pas fait\ncritère 4 non vérifié, faute de base\n",
           journal="ligne oubliée\ndernière ligne du journal d'implement\n")
    events = [{"to_state": "implement", "run_id": None},
              {"to_state": "test_backend", "run_id": 11}]
    texte = prompt(workdir, "test_backend", events, {11: amont})

    assert "Passation de « implement »" in texte, texte
    assert "l'issue « done »" in texte, texte
    assert "le port 8850 est pris" in texte, texte
    assert "critère 4 non vérifié" in texte, texte
    assert "dernière ligne du journal d'implement" in texte, texte
    assert "ne rejuge pas son issue" in texte, texte
    print("2. test_backend lit la passation d'implement, son issue « done » et "
          "la queue de son journal ✓")


def noeud_d_entree(workdir: Path) -> None:
    """3. sans prédécesseur, aucun bloc — et aucune mention creuse."""
    for cas, events in (("aucun événement", []),
                        ("événement sans run", [{"to_state": "worktree",
                                                 "run_id": None}])):
        texte = prompt(workdir, "worktree", events, {})
        assert "Passation de" not in texte, (cas, texte)
        assert "aucune passation" not in texte, (cas, texte)
        assert "prédécesseur" not in texte, (cas, texte)
        print(f"3. {cas} : le prompt du nœud d'entrée ne porte aucun bloc ✓")


def bornes(workdir: Path) -> None:
    """4. la passation et le journal transmis sont bornés en taille."""
    amont = run(21, "implement", "done")
    ecrire(workdir, amont, "A" * (blocks.PASSATION_CHARS * 3),
           journal="".join(f"ligne {n}\n" for n in range(500)))
    events = [{"to_state": "test_backend", "run_id": 21}]
    texte = prompt(workdir, "test_backend", events, {21: amont})

    assert "A" * blocks.PASSATION_CHARS in texte, "la passation n'est pas passée"
    assert "A" * (blocks.PASSATION_CHARS + 1) not in texte, "borne dépassée"
    assert "… (tronqué)" in texte, texte
    assert "ligne 499" in texte, texte
    assert "ligne 400" not in texte, "le journal remonte au-delà de sa queue"
    print(f"4. passation coupée à {blocks.PASSATION_CHARS} caractères, journal "
          f"réduit à ses {blocks.TAIL_LINES} dernières lignes ✓")


def profondeur(workdir: Path) -> None:
    """5. un cycle long ne fait pas remonter l'histoire de l'item."""
    chaine = [run(31, "scope", "ready"), run(32, "implement", "done"),
              run(33, "judge", "chosen"), run(34, "test_backend", "pass"),
              run(35, "test_frontend", "pass")]
    for r in chaine:
        ecrire(workdir, r, f"## Fait\ntravail de {r['node']}\n\n## Appris\n"
                           f"secret de {r['node']}\n\n## Pas fait\nrien\n")
    events = [{"to_state": "scope", "run_id": None}]
    events += [{"to_state": suivant, "run_id": r["id"]}
               for r, suivant in zip(chaine, ["implement", "judge", "test_backend",
                                              "test_frontend", "validate"])]
    texte = prompt(workdir, "validate", events, {r["id"]: r for r in chaine})

    assert "secret de test_frontend" in texte, texte
    for oublie in ("scope", "implement", "judge", "test_backend"):
        assert f"secret de {oublie}" not in texte, oublie
    print("5. cinq nœuds en amont, un seul dans le prompt du dernier : "
          "test_frontend, son prédécesseur immédiat ✓")


def relance(workdir: Path) -> None:
    """6. relancé, un nœud lit sa propre tentative précédente."""
    avant = {"id": 41, "node": "implement", "cycle": 1, "attempt": 1,
             "candidate": None, "outcome": "crashed"}
    ecrire(workdir, avant, "## Fait\nrien\n\n## Appris\nle test veut une base\n\n"
                           "## Pas fait\ntout\n")
    events = [{"to_state": "implement", "run_id": None},
              {"to_state": "implement", "run_id": 41}]
    texte = prompt(workdir, "implement", events, {41: avant})

    assert "Passation de « implement »" in texte, texte
    assert "le test veut une base" in texte, texte
    print("6. relance d'implement : la passation lue est celle de sa tentative "
          "précédente ✓")


def fan_out(workdir: Path) -> None:
    """7. en fan-out, seul le run que l'événement porte est lu."""
    elu = run(51, "implement", "done", candidate=1)
    voisin = run(52, "implement", "done", candidate=0)
    ecrire(workdir, elu, "## Fait\nle travail de l'élu\n")
    ecrire(workdir, voisin, "## Fait\nle travail du voisin recalé\n")
    assert blocks.passation_path(ITEM, elu).parent.name == "c1", "workspace du candidat"
    events = [{"to_state": "test_backend", "run_id": 51}]
    texte = prompt(workdir, "test_backend", events, {51: elu, 52: voisin})

    assert "le travail de l'élu" in texte, texte
    assert "voisin recalé" not in texte, "le prompt lit un candidat qu'il ne doit pas"
    print("7. fan-out : la passation lue est celle du candidat c1, porté par "
          "l'événement — celle de c0 reste dans son coin ✓")

    # le nœud arbitre, lui, ne reçoit rien : sa seule lecture est le dossier
    # anonyme, et la passation d'un finaliste nommerait sa CLI et son modèle
    ctx = contexte(workdir, "judge", events, {51: elu})
    ctx.node["config"]["finalists_from"] = "implement"
    ctx.node["block"] = "JUDGE"
    arbitre = blocks._prompt(ctx, ctx.workspace.resolve(), SUJET)
    assert "Passation de" not in arbitre, arbitre
    assert "le travail de l'élu" not in arbitre, "l'arbitre lit un finaliste nommé"
    assert "passation-judge.md" in arbitre, "l'arbitre en écrit une, lui"
    print("7 bis. nœud arbitre : aucune passation reçue — il juge le travail, "
          "pas son auteur ✓")


def sans_passation(workdir: Path) -> None:
    """8. un prédécesseur muet le dit, et rien ne tombe."""
    muet = run(61, "worktree", "done")
    blocks.run_workspace(ITEM, muet).mkdir(parents=True, exist_ok=True)
    texte = prompt(workdir, "scope", [{"to_state": "scope", "run_id": 61}], {61: muet})

    assert "Passation de « worktree »" in texte, texte
    assert "(aucune passation rendue)" in texte, texte
    print("8. prédécesseur sans passation : le bloc le dit en clair ✓")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-passation-"))
    try:
        demande(workdir)
        passation_obligatoire(workdir)
        portes_de_pertinence(workdir)
        transmission(workdir)
        noeud_d_entree(workdir)
        bornes(workdir)
        profondeur(workdir)
        relance(workdir)
        fan_out(workdir)
        sans_passation(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\npassation : OK — ce qu'un nœud a appris, ou laissé tomber, arrive au suivant")


if __name__ == "__main__":
    main()
