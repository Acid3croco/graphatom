"""Le test de la validation statique : ce qui passe ne peut plus casser.

Scénario, sans base ni ordonnanceur — la validation est pure :

  1. les bundles d'`examples/` se valident tous
  2. un bundle avec une clé `on_kernel` parasite visant un nœud fantôme
     est refusé, avec un message qui nomme la clé
  3. `svg.layers()` ne lève pas de KeyError sur un bundle validé
  4. la file : l'arête réflexive d'un nœud `file` est la seule boucle
     tolérée hors escalade — le drapeau sans arête est refusé, la boucle
     longue qui passe par la file aussi
  5. `solo` reste un booléen interdit sur les nœuds d'attente
  6. `agent.passation` accepte seulement un booléen : les scripts
     déterministes peuvent refuser une passation creuse sans ouvrir le schéma

Usage : uv run python tests/validate_test.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import graph  # noqa: E402
from graphatom import svg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def copie(bundles: dict[str, dict], name: str) -> dict:
    """Une copie profonde d'un exemple : on la casse sans toucher l'original."""
    return json.loads(json.dumps(bundles[name]))


def refuse(bundle: dict, sinon: str, *attendus: str) -> str:
    """La validation refuse le bundle, et son message porte chaque mot attendu.

    `sinon` est le constat d'échec si le bundle passait.
    """
    try:
        graph.validate(bundle)
    except graph.GraphError as e:
        for attendu in attendus:
            assert attendu in str(e), str(e)
        return str(e)
    sys.exit(f"ÉCHEC : {sinon}")


def exemples_valides(bundles: dict[str, dict]) -> None:
    """1. Les exemples se valident."""
    for bundle in bundles.values():
        graph.validate(bundle)
    print(f"1. exemples validés : {', '.join(bundles)} ✓")


def cible_fantome(bundles: dict[str, dict]) -> None:
    """2. Une cible on_kernel fantôme est refusée, quelle que soit la clé."""
    bundle = copie(bundles, "supervision.json")
    bundle["on_kernel"]["notify_to"] = "nowhere"
    e = refuse(bundle, "on_kernel.notify_to = 'nowhere' a passé la validation",
               "notify_to", "nowhere")
    print(f"2. cible parasite refusée : {e} ✓")

    # les deux clés du noyau restent obligatoires
    for key in ("escalate_to", "exhausted_to"):
        bundle = copie(bundles, "supervision.json")
        del bundle["on_kernel"][key]
        refuse(bundle, f"on_kernel sans {key} a passé la validation", key)
    print("   clés du noyau toujours obligatoires ✓")


def placement(bundles: dict[str, dict]) -> None:
    """3. Le placement du graph tient sur tout bundle validé."""
    for name, bundle in bundles.items():
        layers = svg.layers(bundle)
        assert sorted(n for layer in layers for n in layer) == sorted(bundle["nodes"]), name
    print("3. _layers() couvre exactement les nœuds déclarés ✓")


def file_reflexive(bundles: dict[str, dict]) -> None:
    """4. La file tolère son arête réflexive, et rien d'autre. Le drapeau qui
    ne boucle sur rien ment sur le nœud ; la boucle longue, elle, reste
    une boucle que le budget d'escalades devrait borner."""
    bundle = copie(bundles, "code-task.json")
    assert bundle["nodes"]["deploy"]["file"], "deploy n'est plus une file"
    bundle["nodes"]["deploy"]["edges"].pop("waiting")
    e = refuse(bundle, "un nœud `file` qui ne boucle sur rien a passé la validation",
               "file sans arête")
    print(f"4. file sans arête réflexive refusée : {e} ✓")

    bundle = copie(bundles, "code-task.json")
    # l'arête réflexive reste ; c'est la boucle deploy → verify_deploy → deploy
    # que la file ne doit pas dédouaner
    bundle["nodes"]["verify_deploy"]["edges"]["fail"] = "deploy"
    e = refuse(bundle, "une boucle non réflexive par une file a passé la validation",
               "cycle hors escalade")
    print(f"   boucle longue par la file refusée : {e} ✓")


def solo_sur_wait(bundles: dict[str, dict]) -> None:
    """5. solo est un booléen de nœud exécutable, jamais une propriété d'un
    WAIT qui ne réserve précisément aucun run."""
    bundle = copie(bundles, "code-task.json")
    assert bundle["nodes"]["deploy"]["config"]["solo"] is True
    graph.validate(bundle)
    bundle["nodes"]["escalate"]["config"]["solo"] = True
    e = refuse(bundle, "un nœud WAIT solo a passé la validation", "WAIT", "solo")
    print(f"5. solo accepté sur deploy et refusé sur WAIT : {e} ✓")


def contrat_command(bundles: dict[str, dict]) -> None:
    """6. Chaque wagon exécutable dit agent ou commande. Une commande n'a
    aucun faux agent, et les deux formes ne peuvent pas se superposer."""
    bundle = copie(bundles, "code-task.json")
    worktree = bundle["nodes"]["worktree"]["config"]
    assert worktree["execution"]["kind"] == "command"
    assert "agent" not in worktree
    graph.validate(bundle)
    worktree["agent"] = {"prompt": "agent parasite"}
    e = refuse(bundle, "un wagon command avec agent a passé la validation",
               "command", "agent")
    print(f"6. contrat command explicite, faux agent refusé : {e} ✓")


def main() -> None:
    bundles = {p.name: json.loads(p.read_text()) for p in sorted((ROOT / "examples").glob("*.json"))}
    exemples_valides(bundles)
    cible_fantome(bundles)
    placement(bundles)
    file_reflexive(bundles)
    solo_sur_wait(bundles)
    contrat_command(bundles)

    print("\nvalidation : OK — une cible on_kernel fantôme ne se publie plus")


if __name__ == "__main__":
    main()
