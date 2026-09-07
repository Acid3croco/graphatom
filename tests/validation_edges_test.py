"""Les bornes de la validation, une par une — ce que les mutants ont montré nu.

Chaque preuve ici a été écrite parce qu'un mutant de `validation.py` lui
survivait : la règle existait, rien ne la tenait.

  1. un `timeout_s` d'agent non positif, ou un booléen, est refusé nommément,
     et un nœud ne porte pas ce réglage : il vient du graph ou de l'exécution
  2. la borne est stricte : 0 refusé, 1 accepté, 0.5 accepté — pour l'agent
     du graph et pour l'exécution
  3. sans `repeat`, chaque variante fait exactement un candidat : huit
     variantes passent, neuf sont refusées, et quatre variantes répétées
     deux fois passent
  4. `execution.silence_s` suit la même borne stricte
  5. une variante posée sur la config : un objet fusionne avec un objet, un
     scalaire remplace, une clé neuve s'ajoute, le reste est hérité

Usage : uv run python tests/validation_edges_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import bundle as forme, graph  # noqa: E402

PROMPT = "fais le travail"


def bundle(agent: dict | None = None, node_agent: dict | None = None,
           execution: dict | None = None, fanout: dict | None = None) -> dict:
    """Un graph à un nœud ACT, dont on ne varie que les réglages sous test."""
    config = {"execution": execution or {"kind": "agent"},
              "agent": {"prompt": PROMPT, **(node_agent or {})}}
    if fanout is not None:
        config["fanout"] = fanout
    found = {
        "name": "bornes", "entry": "travail",
        "budgets": {"escalations": 1, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {"block": "ACT", "config": config, "edges": {"ok": "fini"}},
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }
    found["agent"] = {"cli": "codex", **(agent or {})}
    return found


def refused(candidate: dict, fragment: str) -> None:
    try:
        graph.validate(candidate)
    except graph.GraphError as exc:
        assert fragment in str(exc), (fragment, str(exc))
        return
    raise AssertionError(f"accepté à tort, attendu « {fragment} »")


def timeouts_invalides() -> None:
    for value in (0, -3, True, False, "12"):
        refused(bundle(agent={"timeout_s": value}), "timeout d'agent invalide")
    refused(bundle(node_agent={"timeout_s": 5}), "réglage agent inconnu ['timeout_s']")
    print("1. timeout_s d'agent non positif ou booléen → refusé nommément ; "
          "un nœud ne le porte pas ✓")


def borne_stricte() -> None:
    for value in (1, 0.5, 600):
        graph.validate(bundle(agent={"timeout_s": value}))
        graph.validate(bundle(execution={"kind": "agent", "timeout_s": value}))
    refused(bundle(execution={"kind": "agent", "timeout_s": 0}),
            "execution.timeout_s invalide")
    print("2. 0 refusé, 1 et 0.5 acceptés — agent du graph et exécution ✓")


def variantes_sans_repeat() -> None:
    def fanout(n: int, repeat: int | None = None) -> dict:
        found = {"variants": [{"label": f"v{i}"} for i in range(n)],
                 "reduce": "first_pass"}
        if repeat is not None:
            found["repeat"] = repeat
        return found

    graph.validate(bundle(fanout=fanout(8)))
    refused(bundle(fanout=fanout(9)), "9 variantes × 1 = 9 candidats")
    graph.validate(bundle(fanout=fanout(4, repeat=2)))
    refused(bundle(fanout=fanout(3, repeat=3)), "3 variantes × 3 = 9 candidats")
    # l'énumération des candidats suit la même arithmétique, sans repeat comme avec
    spec = bundle(fanout=fanout(8))["nodes"]["travail"]
    assert len(forme.fanout_variants(spec)) == 8
    spec = bundle(fanout=fanout(4, repeat=2))["nodes"]["travail"]
    assert [v["label"] for v in forme.fanout_variants(spec)] == \
        ["v0", "v0", "v1", "v1", "v2", "v2", "v3", "v3"]
    print("3. sans repeat, une variante = un candidat : 8 passent, 9 sont refusés ✓")


def overlay_objets() -> None:
    """Une variante fusionne un objet avec un objet, et remplace tout le reste."""
    spec = bundle(fanout={"variants": [{"agent": {"model": "gros"}, "label": "a"},
                                       {"execution": "brut", "nouveau": {"k": 1}}],
                          "reduce": "first_pass"})["nodes"]["travail"]
    premier = forme.candidate_node(spec, 0)["config"]
    assert premier["agent"] == {"prompt": PROMPT, "model": "gros"}, premier
    assert premier["execution"] == {"kind": "agent"} and premier["label"] == "a"
    second = forme.candidate_node(spec, 1)["config"]
    assert second["execution"] == "brut", "un scalaire posé sur un objet le remplace"
    assert second["nouveau"] == {"k": 1}, "un objet posé sur une clé absente est pris tel quel"
    assert second["agent"] == {"prompt": PROMPT}, "ce que la variante tait est hérité"
    print("5. overlay : objet sur objet fusionne, scalaire remplace, clé neuve s'ajoute ✓")


def silence_strict() -> None:
    graph.validate(bundle(execution={"kind": "agent", "silence_s": 1}))
    refused(bundle(execution={"kind": "agent", "silence_s": 0}),
            "execution.silence_s invalide")
    refused(bundle(execution={"kind": "agent", "silence_s": True}),
            "execution.silence_s invalide")
    print("4. execution.silence_s : 1 accepté, 0 et True refusés ✓")


def main() -> None:
    timeouts_invalides()
    borne_stricte()
    variantes_sans_repeat()
    silence_strict()
    overlay_objets()
    print("\nbornes de validation : OK — chaque seuil tient sa preuve")


if __name__ == "__main__":
    main()
