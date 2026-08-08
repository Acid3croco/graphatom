"""Le test de la déclaration `fanout` : sa forme, et son refus à la publication.

Rien ne s'exécute différemment ici — le fan-out se déclare, et une config
fautive est refusée avant tout run, pas découverte au milieu.

Scénario, sans base ni ordonnanceur — la validation est pure, la connexion
une doublure :

  1. un `fanout` bien formé sur un nœud non-WAIT, `reduce: "first_pass"` et
     un K sous la limite, se publie : `graph.publish` rend une révision
  2. un `fanout` sur un nœud WAIT est refusé, et le message nomme le nœud
  3. la table « type de bloc → réductions permises » est explicite, et une
     réduction hors du jeu du bloc — un nom inconnu, ou `best_by` qui n'est
     livrée nulle part — est refusée, message à l'appui
  4. variantes × repeat au-delà de la limite dure est refusé, et le message
     cite la limite
  5. un `fanout` malformé — `variants` absent ou vide, `repeat` non entier
     ou < 1, `reduce` absent — est refusé, cas par cas
  6. un bundle sans `fanout` nulle part se valide et se publie comme avant

Usage : uv run python tests/fanout_config_test.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import graph  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# quatre candidats : deux stratégies, jouées deux fois
BON = {
    "variants": [
        {"label": "minimal", "strategy": "diff minimal : touche le moins de lignes possible"},
        {"label": "réécrit", "strategy": "réécris le composant concerné en entier"},
    ],
    "repeat": 2,
    "reduce": "first_pass",
}


class FakeConn:
    """La base : `publish` n'y écrit qu'une ligne, on ne lui demande rien d'autre."""

    def __init__(self):
        self.rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()):
        self.rows.append(params)
        return None


def bundle_nu() -> dict:
    """Le bundle d'exemple, tel quel — aucun `fanout` nulle part."""
    return json.loads((ROOT / "examples" / "code-task.json").read_text())


def avec_fanout(node: str, fanout) -> dict:
    """Le même bundle, avec `fanout` posé dans la config d'un nœud."""
    bundle = bundle_nu()
    bundle["nodes"][node].setdefault("config", {})["fanout"] = fanout
    return bundle


def refuse(quoi: str, bundle: dict, *attendus: str) -> str:
    """La validation refuse le bundle, et son message porte chaque mot attendu."""
    try:
        graph.validate(bundle)
    except graph.GraphError as e:
        for mot in attendus:
            assert mot in str(e), f"{quoi} : {mot!r} absent du message : {e}"
        return str(e)
    sys.exit(f"ÉCHEC : {quoi} a passé la validation")


def main() -> None:
    # 1. un fan-out bien formé sur un ACT se publie
    bundle = avec_fanout("implement", BON)
    graph.validate(bundle)
    conn = FakeConn()
    rev = graph.publish(conn, bundle)
    assert rev == graph.content_hash(bundle), rev
    assert len(rev) == 64 and len(conn.rows) == 1, (rev, conn.rows)
    print(f"1. fan-out bien formé sur `implement` publié : révision {rev[:12]}… ✓")

    # 2. un WAIT ne peut pas être en fan-out
    e = refuse("un fan-out sur le WAIT `clarify`",
               avec_fanout("clarify", BON), "clarify", "WAIT")
    print(f"2. fan-out sur un WAIT refusé : {e} ✓")

    # 3. la table des réductions est explicite, et seule `first_pass` est livrée
    table = graph.FANOUT_REDUCERS
    assert set(table) == graph.BLOCK_KINDS, set(table) ^ graph.BLOCK_KINDS
    assert table["WAIT"] == set(), table["WAIT"]
    livrees = set().union(*table.values())
    assert livrees == {"first_pass"}, livrees
    print(f"3. table bloc → réductions : {livrees} livrée, WAIT au jeu vide ✓")

    for reduce in ("vote", "best_by", "keep_n"):
        e = refuse(f"la réduction {reduce!r}",
                   avec_fanout("implement", BON | {"reduce": reduce}),
                   "implement", reduce, "ACT")
        print(f"   réduction {reduce!r} refusée : {e} ✓")

    # 4. la limite dure borne variantes × repeat
    limite = graph.FANOUT_MAX_CANDIDATES
    assert isinstance(limite, int) and limite >= 1, limite
    e = refuse("cinq répétitions de deux variantes",
               avec_fanout("implement", BON | {"repeat": 5}),
               "implement", "10", str(limite))
    print(f"4. au-delà de la limite dure ({limite}) refusé : {e} ✓")

    # la limite elle-même passe : c'est un plafond, pas un interdit
    graph.validate(avec_fanout("implement", BON | {"repeat": limite // 2}))
    print(f"   {limite} candidats pile passent ✓")

    # 5. les formes malades, une par une
    malades = {
        "variants absent": {"repeat": 2, "reduce": "first_pass"},
        "variants vide": BON | {"variants": []},
        "variants pas une liste": BON | {"variants": {"label": "minimal"}},
        "variante pas un objet": BON | {"variants": ["minimal"]},
        "repeat non entier": BON | {"repeat": "2"},
        "repeat booléen": BON | {"repeat": True},
        "repeat nul": BON | {"repeat": 0},
        "repeat négatif": BON | {"repeat": -1},
        "reduce absent": {"variants": BON["variants"], "repeat": 2},
        "fanout pas un objet": ["minimal", "réécrit"],
    }
    for quoi, fanout in malades.items():
        e = refuse(quoi, avec_fanout("implement", fanout), "implement")
        print(f"5. {quoi} → {e} ✓")

    # repeat absent vaut une fois : ce n'est pas une forme malade
    graph.validate(avec_fanout("implement", {k: v for k, v in BON.items() if k != "repeat"}))
    print("   repeat absent vaut une fois ✓")

    # 6. sans fan-out, rien ne change
    nu = bundle_nu()
    assert "fanout" not in json.dumps(nu), "l'exemple ne déclare aucun fan-out"
    graph.validate(nu)
    assert graph.publish(FakeConn(), nu) == graph.content_hash(nu)
    for path in sorted((ROOT / "examples").glob("*.json")):
        graph.validate(json.loads(path.read_text()))
    print("6. les bundles d'examples/ se valident et se publient comme avant ✓")

    print("\nfan-out : OK — la déclaration existe, et une config fautive ne se publie pas")


if __name__ == "__main__":
    main()
