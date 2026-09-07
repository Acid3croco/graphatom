"""Chaque refus de la validation, nommé : une malformation, un message, sa place.

Un graph publié ne peut plus échouer sur sa structure — à condition que
chaque forme fautive soit refusée avant, et refusée en nommant l'endroit.
Une table : la malformation appliquée à un graph valide, et le fragment
que le message doit porter.

  1. la forme du bundle : champ manquant, entry inconnu, aucun terminal
  2. les agents : objet exigé, modèle, effort, passation, CLI structurée
  3. les exécutions : jamais sur un terminal ni un FETCH, objet exigé,
     agent sans config.agent, sans prompt, cmd invalide
  4. les portes : bloc exécutable, registre fermé, activation booléenne
  5. les nœuds : terminal sans arête, bloc inconnu, arête obligatoire,
     issue noyau interdite, cible déclarée, WAIT complet, solo booléen
  6. le graph entier : tout nœud atteignable

Usage : uv run python tests/validation_refusals_test.py
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import graph  # noqa: E402


def valide() -> dict:
    """Un graph qui passe : FETCH, ACT agent, WAIT d'escalade, deux terminaux."""
    return {
        "name": "refus", "entry": "lire", "agent": {"cli": "codex"},
        "budgets": {"escalations": 1, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "demande", "exhausted_to": "abandon"},
        "nodes": {
            "lire": {"block": "FETCH", "edges": {"ok": "travail"}},
            "travail": {"block": "ACT",
                        "config": {"execution": {"kind": "agent"},
                                   "agent": {"prompt": "fais"}},
                        "edges": {"ok": "fini"}},
            "demande": {"block": "WAIT", "escalade": True,
                        "config": {"question": "On retente ?", "options": ["retry"],
                                   "owner": "test", "deadline_minutes": 60},
                        "edges": {"retry": "travail", "expired": "abandon"}},
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


def refuse(mutation, fragment: str) -> None:
    bundle = valide()
    mutation(bundle)
    try:
        graph.validate(bundle)
    except graph.GraphError as exc:
        assert fragment in str(exc), (fragment, str(exc))
        return
    raise AssertionError(f"accepté à tort, attendu « {fragment} »")


def travail(bundle: dict) -> dict:
    return bundle["nodes"]["travail"]["config"]


def forme() -> None:
    refuse(lambda b: b.pop("budgets"), "champ manquant : budgets")
    refuse(lambda b: b.update(entry="fantome"), "entry inconnu : fantome")
    refuse(lambda b: [b["nodes"][n].pop("terminal") for n in ("fini", "abandon")],
           "aucun nœud terminal")
    print("1. champ manquant, entry inconnu, aucun terminal : refusés et nommés ✓")


def agents() -> None:
    refuse(lambda b: b.update(agent="codex"), "graph : agent n'est pas un objet")
    refuse(lambda b: b["agent"].update(model=""), "graph : modèle d'agent invalide ''")
    refuse(lambda b: b["agent"].update(effort=3), "graph : effort d'agent invalide 3")
    refuse(lambda b: travail(b)["agent"].update(passation="oui"),
           "travail : passation doit être un booléen, vu 'oui'")
    refuse(lambda b: b.pop("agent"), "travail : agent sans CLI structurée")
    print("2. agent objet, modèle, effort, passation, CLI structurée : refusés ✓")


def executions() -> None:
    refuse(lambda b: b["nodes"]["fini"].update(
        config={"execution": {"kind": "command", "cmd": "true"}}),
        "fini : un terminal ne peut pas avoir execution")
    refuse(lambda b: b["nodes"]["lire"].update(
        config={"execution": {"kind": "command", "cmd": "true"}}),
        "lire : execution refusée sur un bloc FETCH")
    refuse(lambda b: travail(b).update(execution="agent"),
           "travail : execution n'est pas un objet")
    refuse(lambda b: travail(b).pop("agent"), "travail : execution agent sans config.agent")
    refuse(lambda b: travail(b)["agent"].pop("prompt"),
           "travail : execution agent sans prompt")
    refuse(lambda b: travail(b)["execution"].update(cmd=" "),
           "travail : execution agent avec cmd invalide")
    print("3. exécution sur terminal ou FETCH, non-objet, sans agent, sans prompt, "
          "cmd vide : refusées ✓")


def portes() -> None:
    refuse(lambda b: b["nodes"]["lire"].update(config={"gate": "elected_verdict"}),
           "lire : gate sur un bloc FETCH")
    refuse(lambda b: travail(b).update(gate="inconnue"),
           "travail : porte inconnue 'inconnue' — livrées")
    refuse(lambda b: travail(b).update(activation="oui"),
           "travail : activation doit être un booléen, vu 'oui'")
    print("4. porte sur FETCH, porte inconnue, activation non booléenne : refusées ✓")


def noeuds() -> None:
    refuse(lambda b: b["nodes"]["fini"].update(edges={"ok": "abandon"}),
           "fini : un terminal n'a pas d'arêtes")
    refuse(lambda b: b["nodes"]["lire"].update(block="PENSER"), "lire : bloc inconnu PENSER")
    refuse(lambda b: b["nodes"]["lire"].pop("edges"),
           "lire : aucun nœud non-terminal sans arête")
    refuse(lambda b: b["nodes"]["lire"]["edges"].update(crashed="fini"),
           "lire.crashed : issue noyau dans l'espace domaine")
    refuse(lambda b: b["nodes"]["lire"]["edges"].update(ok="nulle-part"),
           "lire.ok → nœud non déclaré nulle-part")
    refuse(lambda b: b["nodes"]["demande"]["config"].pop("owner"),
           "demande : WAIT sans owner")
    refuse(lambda b: b["nodes"]["demande"]["config"]["options"].append("plus-tard"),
           "demande : options sans arête : {'plus-tard'}")
    refuse(lambda b: b["nodes"]["demande"]["edges"].pop("expired"),
           "demande : l'expiration d'un WAIT doit nommer son arête")
    refuse(lambda b: travail(b).update(solo="oui"),
           "travail : solo doit être un booléen, vu 'oui'")
    print("5. terminal avec arête, bloc inconnu, sans arête, issue noyau, cible "
          "fantôme, WAIT incomplet, solo non booléen : refusés ✓")


def atteignable() -> None:
    def isole(b: dict) -> None:
        b["nodes"]["perdu"] = copy.deepcopy(b["nodes"]["lire"])
    refuse(isole, "nœuds inatteignables : {'perdu'}")
    graph.validate(valide())
    print("6. un nœud que rien n'atteint est refusé ; le graph de départ passe ✓")


def main() -> None:
    forme()
    agents()
    executions()
    portes()
    noeuds()
    atteignable()
    print("\nrefus de validation : OK — chaque malformation a son message et sa place")


if __name__ == "__main__":
    main()
