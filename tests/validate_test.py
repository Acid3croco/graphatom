"""Le test de la validation statique : ce qui passe ne peut plus casser.

Scénario, sans base ni ordonnanceur — la validation est pure :

  1. les bundles d'`examples/` se valident tous
  2. un bundle avec une clé `on_kernel` parasite visant un nœud fantôme
     est refusé, avec un message qui nomme la clé
  3. `web._layers()` ne lève pas de KeyError sur un bundle validé
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

from graphatom import graph, web  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    bundles = {p.name: json.loads(p.read_text()) for p in sorted((ROOT / "examples").glob("*.json"))}

    # 1. les exemples se valident
    for bundle in bundles.values():
        graph.validate(bundle)
    print(f"1. exemples validés : {', '.join(bundles)} ✓")

    # 2. une cible on_kernel fantôme est refusée, quelle que soit la clé
    bundle = json.loads(json.dumps(bundles["supervision.json"]))
    bundle["on_kernel"]["notify_to"] = "nowhere"
    try:
        graph.validate(bundle)
    except graph.GraphError as e:
        assert "notify_to" in str(e) and "nowhere" in str(e), str(e)
        print(f"2. cible parasite refusée : {e} ✓")
    else:
        sys.exit("ÉCHEC : on_kernel.notify_to = 'nowhere' a passé la validation")

    # les deux clés du noyau restent obligatoires
    for key in ("escalate_to", "exhausted_to"):
        bundle = json.loads(json.dumps(bundles["supervision.json"]))
        del bundle["on_kernel"][key]
        try:
            graph.validate(bundle)
        except graph.GraphError as e:
            assert key in str(e), str(e)
        else:
            sys.exit(f"ÉCHEC : on_kernel sans {key} a passé la validation")
    print("   clés du noyau toujours obligatoires ✓")

    # 3. le placement du graph tient sur tout bundle validé
    for name, bundle in bundles.items():
        layers = web._layers(bundle)
        assert sorted(n for layer in layers for n in layer) == sorted(bundle["nodes"]), name
    print("3. _layers() couvre exactement les nœuds déclarés ✓")

    # 4. la file tolère son arête réflexive, et rien d'autre. Le drapeau qui
    #    ne boucle sur rien ment sur le nœud ; la boucle longue, elle, reste
    #    une boucle que le budget d'escalades devrait borner
    bundle = json.loads(json.dumps(bundles["code-task.json"]))
    assert bundle["nodes"]["deploy"]["file"], "deploy n'est plus une file"
    bundle["nodes"]["deploy"]["edges"].pop("waiting")
    try:
        graph.validate(bundle)
    except graph.GraphError as e:
        assert "file sans arête" in str(e), str(e)
        print(f"4. file sans arête réflexive refusée : {e} ✓")
    else:
        sys.exit("ÉCHEC : un nœud `file` qui ne boucle sur rien a passé la validation")

    bundle = json.loads(json.dumps(bundles["code-task.json"]))
    # l'arête réflexive reste ; c'est la boucle deploy → verify_deploy → deploy
    # que la file ne doit pas dédouaner
    bundle["nodes"]["verify_deploy"]["edges"]["fail"] = "deploy"
    try:
        graph.validate(bundle)
    except graph.GraphError as e:
        assert "cycle hors escalade" in str(e), str(e)
        print(f"   boucle longue par la file refusée : {e} ✓")
    else:
        sys.exit("ÉCHEC : une boucle non réflexive par une file a passé la validation")

    # 5. solo est un booléen de nœud exécutable, jamais une propriété d'un
    #    WAIT qui ne réserve précisément aucun run.
    bundle = json.loads(json.dumps(bundles["code-task.json"]))
    assert bundle["nodes"]["deploy"]["config"]["solo"] is True
    graph.validate(bundle)
    bundle["nodes"]["escalate"]["config"]["solo"] = True
    try:
        graph.validate(bundle)
    except graph.GraphError as e:
        assert "WAIT" in str(e) and "solo" in str(e), str(e)
        print(f"5. solo accepté sur deploy et refusé sur WAIT : {e} ✓")
    else:
        sys.exit("ÉCHEC : un nœud WAIT solo a passé la validation")

    # 6. les agents de modèle écrivent une passation par défaut. Un script
    #    déterministe peut la désactiver explicitement, avec un booléen.
    bundle = json.loads(json.dumps(bundles["code-task.json"]))
    assert bundle["nodes"]["worktree"]["config"]["agent"]["passation"] is False
    graph.validate(bundle)
    bundle["nodes"]["worktree"]["config"]["agent"]["passation"] = "non"
    try:
        graph.validate(bundle)
    except graph.GraphError as e:
        assert "passation" in str(e) and "booléen" in str(e), str(e)
        print(f"6. passation false acceptée, valeur non booléenne refusée : {e} ✓")
    else:
        sys.exit("ÉCHEC : agent.passation non booléen a passé la validation")

    print("\nvalidation : OK — une cible on_kernel fantôme ne se publie plus")


if __name__ == "__main__":
    main()
