"""Le graph SVG d'un item : la géométrie est le contrat, sans base ni serveur.

  1. couches BFS : entry et cibles on_kernel en couche 0, un nœud rejoint
     par deux chemins garde la plus courte
  2. arêtes fusionnées, retours en voies séparées, étiquettes décalées,
     couleurs des nœuds, escalade en pointillé, libellés échappés
  3. une arête entre deux nœuds de la même colonne contourne par le bas,
     et sa voie compte dans la hauteur — un trait droit traverserait les boîtes

Usage : uv run python tests/svg_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import svg  # noqa: E402

BUNDLE = {
    "name": "svg", "entry": "a", "on_kernel": {"escalate_to": "b"},
    "nodes": {
        "a": {"block": "ACT", "edges": {"ok": "c", "ko": "c", "far": "d"}},
        "b": {"block": "JUDGE&", "edges": {"ok": "c"}},
        "c": {"block": "CHECK", "edges": {"done": "e", "back": "a"}},
        "d": {"block": "WAIT", "escalade": True, "edges": {"retry": "b", "expired": "e"}},
        "e": {"terminal": True},
    },
}

# a → b, deux nœuds de la couche 0 : b est cible on_kernel, donc placé sous a
COLONNE = {
    "name": "colonne", "entry": "a", "on_kernel": {"escalate_to": "b"},
    "nodes": {"a": {"block": "ACT", "edges": {"ok": "b"}}, "b": {"terminal": True}},
}


def couches() -> None:
    """1. BFS depuis entry et les cibles on_kernel ; la profondeur la plus courte gagne."""
    assert svg.layers(BUNDLE) == [["a", "b"], ["c", "d"], ["e"]], "la cible on_kernel en couche 0"
    # e est à deux pas de a par c comme par d, et c est rejoint par a et par b : une seule place
    assert svg.layers(COLONNE) == [["a", "b"]], "b, cible on_kernel, reste en couche 0 malgré a → b"
    print("1. couches : entry et on_kernel en tête, la profondeur la plus courte gagne ✓")


def dessin_complet() -> None:
    """2. le SVG : placement, arêtes, étiquettes, couleurs."""
    dessin = svg.graph_svg(BUNDLE, "c")
    assert dessin.startswith("<svg viewBox='0 0 567 178' ") and dessin.endswith("</svg>")
    assert "<path d='M144,29.0 C184,29.0 157,29.0 197,29.0' fill='none'" in dessin, "a → c en avant"
    assert "y='24.0' font-size='9' fill='#777' text-anchor='middle'>ok, ko</text>" in dessin, \
        "deux issues vers la même cible, un seul trait"
    assert dessin.count("x='170.5' y='56.0'") == 1 and dessin.count("x='170.5' y='46.0'") == 1, \
        "a → d et b → c ont le même milieu : la seconde étiquette est décalée, pas empilée"
    assert "<path d='M197,29.0 C137,172 204,172 144,29.0'" in dessin, "c → a contourne par le bas"
    assert "<path d='M197,93.0 C137,158 204,158 144,93.0'" in dessin, "d → b, une voie plus haut"
    assert "y='168'" in dessin and "y='154'" in dessin, "une étiquette par voie"
    assert dessin.count("marker-end='url(#arr)'") == 7, "sept couples (source, cible)"
    assert ("<rect x='197' y='12' width='132' height='34' rx='7' fill='#ffb74d' stroke='#888'/>"
            "<text x='263.0' y='33.0' font-size='11' text-anchor='middle'>c · CHECK</text>") in dessin
    assert dessin.count("fill='#ffb74d'") == 1, "un seul état courant"
    assert "fill='#e3ecf7' stroke='#888'/><text x='78.0' y='33.0' font-size='11' " \
           "text-anchor='middle'>a · ACT</text>" in dessin
    assert "fill='#e3ecf7' stroke='#888' stroke-dasharray='4 2'/>" in dessin, "l'escalade en pointillé"
    assert "fill='#eee' stroke='#888'/><text x='448.0' y='33.0' font-size='11' " \
           "text-anchor='middle'>e</text>" in dessin, "un terminal : gris, sans bloc"
    assert ">b · JUDGE&amp;</text>" in dessin, "les libellés sont échappés"
    print("2. SVG : couches, arêtes fusionnées, voies de retour, étiquettes décalées, couleurs ✓")


def meme_colonne() -> None:
    """3. a → b dans une même colonne : un retour par le bas, avec sa voie."""
    dessin = svg.graph_svg(COLONNE, "a")
    assert dessin.startswith("<svg viewBox='0 0 197 164' "), "12 + 2 × 64 + 10, plus une voie de 14"
    assert "<path d='M12,29.0 C-48,158 204,158 144,93.0' fill='none'" in dessin, \
        "du bord gauche de a au bord droit de b, par la voie du bas — jamais à travers les boîtes"
    assert "x='78.0' y='154' font-size='9'" in dessin, "l'étiquette sur la voie"
    print("3. même colonne : l'arête contourne par le bas et sa voie compte dans la hauteur ✓")


def main() -> None:
    couches()
    dessin_complet()
    meme_colonne()
    print("\nsvg : OK — la géométrie tenue au pixel")


if __name__ == "__main__":
    main()
