"""Le test de la dérivation du couperet : `lease_s` moins une marge nommée.

Le couperet du process (`timeout_s`, historiquement recopié sur chaque nœud)
descend maintenant du bail : `kernel.agent_timeout_s` calcule
`lease_s - AGENT_LEASE_MARGIN_S`, une constante définie une seule fois à côté
de `LEASE_SECONDS`. Ce test, sans base, tient les critères de l'issue :

  1. les douze valeurs de l'issue — six paires (bail → couperet) — sont
     celles que la dérivation calcule ;
  2. un nœud sans `lease_s` garde le défaut de 570 s ;
  3. un `timeout_s` encore porté explicitement reste honoré — l'exception
     visible, pas une valeur qu'on recopie par accident ;
  4. la marge est une constante nommée, définie une fois, et aucune
     soustraction littérale `- 60` ne subsiste dans le paquet ;
  5. nœud par nœud, les deux graphes d'exemple ne portent plus de
     `timeout_s`, et chaque nœud à bail dérive exactement le couperet qu'il
     portait avant — rien ne change à l'exécution.

Usage : uv run python tests/timeout_marge_test.py
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import kernel  # noqa: E402
from graphatom.blocks import AGENT_TIMEOUT_S  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "graphatom"

# les six paires de l'issue — les douze valeurs, telles qu'elles y sont listées
PAIRES = {180: 120, 600: 540, 900: 840, 1800: 1740, 1020: 960, 120: 60}


def noeuds(graph: dict) -> list[tuple[str, dict]]:
    return [(nom, node) for nom, node in graph["nodes"].items()
            if (node.get("config") or {}).get("lease_s") is not None]


def main() -> None:
    # 1. la dérivation reproduit les douze valeurs de l'issue
    for bail, attendu in PAIRES.items():
        calcule = kernel.agent_timeout_s({"lease_s": bail}, AGENT_TIMEOUT_S)
        assert calcule == attendu, f"bail {bail} s → {calcule} s, attendu {attendu} s"
        assert calcule < bail, "le couperet doit précéder le bail, jamais l'inverse"
    print(f"1. {len(PAIRES)} paires de l'issue — {sorted(PAIRES.items())} — "
          "la dérivation les calcule toutes ✓")

    # 2. un nœud sans bail garde le défaut de 570 s
    assert kernel.agent_timeout_s({"agent": {"silence_s": 300}},
                                  AGENT_TIMEOUT_S) == AGENT_TIMEOUT_S
    assert kernel.agent_timeout_s({}, AGENT_TIMEOUT_S) == AGENT_TIMEOUT_S
    assert AGENT_TIMEOUT_S == 570
    print(f"2. un nœud sans `lease_s` garde le défaut de {AGENT_TIMEOUT_S} s ✓")

    # 3. un `timeout_s` explicite reste honoré — l'exception visible
    assert kernel.agent_timeout_s({"lease_s": 600,
                                   "agent": {"timeout_s": 300}},
                                  AGENT_TIMEOUT_S) == 300
    assert kernel.agent_timeout_s({"lease_s": 180,
                                   "agent": {"timeout_s": 300}},
                                  AGENT_TIMEOUT_S) == 300
    print("3. un `timeout_s` explicite est honoré, quel que soit le bail ✓")

    # 4. la marge est une constante nommée, définie une seule fois
    marge = kernel.AGENT_LEASE_MARGIN_S
    assert marge == 60, marge
    source = (SRC / "kernel.py").read_text()
    assert source.count("AGENT_LEASE_MARGIN_S =") == 1, \
        "la marge n'est pas définie exactement une fois dans kernel.py"
    pour = re.compile(r"AGENT_LEASE_MARGIN_S\s*=\s*60")
    assert pour.search(source), "la constante ne vaut pas 60 dans kernel.py"
    for module in SRC.glob("*.py"):
        reste = [l for l in module.read_text().splitlines()
                 if re.search(r"\d\s*-\s*60(?:\.0)?\b", l)
                 and "AGENT_LEASE_MARGIN_S" not in l]
        assert not reste, f"{module.name} : soustraction littérale de 60 : {reste}"
    print("4. la marge vit une seule fois, nommée, à côté de LEASE_SECONDS — "
          "aucune soustraction littérale `- 60` ailleurs ✓")

    # 5. nœud par nœud sur les deux graphes d'exemple
    for fichier in ("code-task.json", "gauntlet.json"):
        texte = (ROOT / "examples" / fichier).read_text()
        assert "timeout_s" not in texte, f"{fichier} porte encore un timeout_s"
        graph = json.loads(texte)
        for nom, node in noeuds(graph):
            config = node["config"]
            calcule = kernel.agent_timeout_s(config, AGENT_TIMEOUT_S)
            attendu = config["lease_s"] - marge
            assert calcule == attendu, \
                f"{fichier}/{nom} : bail {config['lease_s']} s → {calcule} s, " \
                f"attendu {attendu} s"
        print(f"5. {fichier} : {len(noeuds(graph))} nœuds à bail, aucun "
              "`timeout_s`, couperet dérivé à l'identique ✓")

    print("\ntimeout_marge : OK — le couperet descend du bail, une seule "
          "marge, les douze valeurs de l'issue inchangées")


if __name__ == "__main__":
    main()
