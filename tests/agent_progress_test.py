"""Le progrès d'un agent et son univers : ce que le bloc relève, ce qu'il transmet.

Sans base, sans dépôt, sans process — les fonctions pures du bloc agent,
poussées sur les bords que le chien de garde et l'environnement tiennent :

  1. l'agent reçoit `GRAPHATOM_WORKTREE` égal au chemin de son atelier ;
     sans atelier, la variable est absente — jamais la chaîne « None »
  2. le relevé ignore la machinerie : un fichier touché sous chaque
     répertoire de `PRUNED_DIRS` laisse le relevé intact ; un fichier de
     `src/` touché le change
  3. le progrès se lit sur le contenu du journal, pas sur sa croissance :
     un octet suffit avec des mtimes identiques ; rien de neuf, c'est faux ;
     un fichier touché avec un journal vide, c'est vrai

Usage : uv run python tests/agent_progress_test.py
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# hermétisme : ni atelier git, ni instance jetable, ni atelier hérité
for variable in ("GRAPHATOM_REPO_DIR", "GRAPHATOM_AGENT_DSN", "GRAPHATOM_WORKTREE"):
    os.environ.pop(variable, None)

from graphatom import agent, context, executors  # noqa: E402

from bords_outils import SUJET, commande, contexte  # noqa: E402
from outils import data_dir_jetable  # noqa: E402

VARIABLE = "GRAPHATOM_WORKTREE"


def univers(workdir: Path) -> None:
    """1. l'atelier est dit à l'agent par son chemin ; sans atelier, rien n'est dit."""
    ctx = contexte(commande("true"), node="univers")
    resolu = executors.resolve(ctx.bundle, ctx.node)
    atelier = workdir / "atelier"
    ctx.worktree = atelier
    avec = agent._environment(ctx, ctx.workspace, SUJET, resolu)
    assert avec[VARIABLE] == str(atelier), avec.get(VARIABLE)
    ctx.worktree = None
    sans = agent._environment(ctx, ctx.workspace, SUJET, resolu)
    assert VARIABLE not in sans, sans.get(VARIABLE)
    print("1. GRAPHATOM_WORKTREE : le chemin de l'atelier ; sans atelier, absente ✓")


def avancer(chemin: Path, secondes: int) -> None:
    """Pousse le mtime d'un fichier de `secondes` dans le futur."""
    instant = chemin.stat().st_mtime + secondes
    os.utime(chemin, (instant, instant))


def machinerie(workdir: Path) -> None:
    """2. la machinerie touchée ne bouge pas le relevé ; un fichier de src, si."""
    atelier = workdir / "arbre"
    for machine in agent.PRUNED_DIRS:
        (atelier / machine).mkdir(parents=True)
        (atelier / machine / "index").write_text("machinerie")
    (atelier / "src").mkdir()
    (atelier / "src" / "x.py").write_text("travail")
    journal = workdir / "journal.log"
    journal.write_text("")
    repere = agent.mark(journal, workdir / "bureau-vide", atelier)
    for machine in agent.PRUNED_DIRS:
        avancer(atelier / machine / "index", 60)
        assert agent.mark(journal, workdir / "bureau-vide", atelier) == repere, machine
    avancer(atelier / "src" / "x.py", 60)
    assert agent.mark(journal, workdir / "bureau-vide", atelier) != repere, "src ignoré"
    print(f"2. {len(agent.PRUNED_DIRS)} répertoires de machinerie touchés : relevé "
          "intact ; src/x.py touché : relevé changé ✓")


def progres() -> None:
    """3. un octet de journal suffit ; rien de neuf est faux ; un fichier touché suffit."""
    depart = (0, 10.0, 20.0)
    assert agent.progress(depart, (1, 10.0, 20.0)) is True, "un octet ignoré"
    assert agent.progress(depart, (0, 10.0, 20.0)) is False, "progrès inventé"
    assert agent.progress(depart, (0, 11.0, 20.0)) is True, "fichier touché ignoré"
    print("3. un octet avec les mêmes mtimes : progrès ; relevé identique : aucun ; "
          "un fichier touché sans octet : progrès ✓")


def stub_instantane(workdir: Path) -> None:
    """4. Un stub sans `duration_s` ne dort pas : la simulation vaut zéro par défaut.

    Un nœud de stub qui déclare une durée l'attend ; sans elle, le bloc rend
    la main tout de suite — les preuves du noyau en jouent des centaines.
    """
    run = {"id": 1, "node": "travail", "cycle": 1, "attempt": 1, "candidate": None}
    sans = context.Context(None, run, {"id": 1}, {"block": "ACT"}, {})
    debut = time.monotonic()
    sans.simulate_work()
    assert time.monotonic() - debut < 0.5, "le stub sans durée doit rendre la main aussitôt"
    avec = context.Context(None, run, {"id": 1},
                           {"block": "ACT", "config": {"duration_s": 0.3}}, {})
    debut = time.monotonic()
    avec.simulate_work()
    assert time.monotonic() - debut >= 0.3, "duration_s déclaré est bien attendu"
    assert (workdir / "item-1").is_dir(), "le contexte ouvre le bureau de l'item"
    print("4. stub sans duration_s : instantané ; avec : la durée déclarée ✓")


def main() -> None:
    workdir = data_dir_jetable("graphatom-agent-progress-")
    univers(workdir)
    machinerie(workdir)
    progres()
    stub_instantane(workdir)
    print("\nprogrès et univers de l'agent : OK")


if __name__ == "__main__":
    main()
