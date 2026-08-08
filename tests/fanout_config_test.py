"""Le test de la déclaration `fanout` : sa forme, et son refus à la publication.

Rien ne s'exécute différemment ici — le fan-out se déclare, et une config
fautive est refusée avant tout run, pas découverte au milieu.

Scénario, sans base ni ordonnanceur — la validation est pure, la connexion
une doublure :

  1. un `fanout` bien formé sur un nœud non-WAIT, `reduce: "first_pass"` et
     un K sous la limite, se publie : `graph.publish` rend une révision
  2. un `fanout` sur un nœud WAIT est refusé, et le message nomme le nœud
  3. la table « type de bloc → réductions permises » est explicite, et une
     réduction hors du jeu du bloc — un nom inconnu, `best_by` qui n'est
     livrée nulle part, ou `keep_n` sur un bloc qui n'a rien à départager —
     est refusée, message à l'appui
  4. variantes × repeat au-delà de la limite dure est refusé, et le message
     cite la limite
  5. un `fanout` malformé — `variants` absent ou vide, `repeat` non entier
     ou < 1, `reduce` absent — est refusé, cas par cas
  6. le même bundle privé de son `fanout` se valide et se publie comme avant,
     et tous ceux d'`examples/` — celui de `code-task`, qui déclare la course
     d'`implement`, compris — passent la validation

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


def bundle_livre() -> dict:
    """Le bundle d'exemple, tel qu'il est livré — `implement` en keep_n, `judge`."""
    return json.loads((ROOT / "examples" / "code-task.json").read_text())


def bundle_nu() -> dict:
    """Le même, ramené à l'avant fan-out : aucun `fanout`, aucun arbitre.

    C'est le socle des cas malades — on y pose un `fanout` et on regarde la
    validation refuser. Retirer le juge va avec : il n'existe que pour la
    réduction `keep_n` d'`implement`, et sans elle il n'a plus de source.
    """
    bundle = bundle_livre()
    del bundle["nodes"]["judge"]
    del bundle["nodes"]["implement"]["config"]["fanout"]
    bundle["nodes"]["implement"]["edges"] = {"done": "test_backend"}
    return bundle


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

    # 3. la table des réductions est explicite, et chacune n'est permise que
    #    là où elle a un sens : `first_pass` partout, `keep_n` sur un ACT seul
    table = graph.FANOUT_REDUCERS
    assert set(table) == graph.BLOCK_KINDS, set(table) ^ graph.BLOCK_KINDS
    assert table["WAIT"] == set(), table["WAIT"]
    livrees = set().union(*table.values())
    assert livrees == {"first_pass", "keep_n"}, livrees
    assert {k for k, v in table.items() if "keep_n" in v} == {"ACT"}, table
    print(f"3. table bloc → réductions : {sorted(livrees)} livrées, keep_n sur ACT "
          "seul, WAIT au jeu vide ✓")

    for reduce in ("vote", "best_by"):
        e = refuse(f"la réduction {reduce!r}",
                   avec_fanout("implement", BON | {"reduce": reduce}),
                   "implement", reduce, "ACT")
        print(f"   réduction {reduce!r} refusée : {e} ✓")

    # `keep_n` là où il n'y a rien à départager : un constat, une lecture, un
    # effet — le message nomme le bloc et les réductions qu'il permet
    for node, kind in (("test_backend", "CHECK"), ("scope", "JUDGE")):
        e = refuse(f"keep_n sur le {kind} `{node}`",
                   avec_fanout(node, BON | {"reduce": "keep_n", "n": 2}),
                   node, "keep_n", kind, "first_pass")
        print(f"   keep_n sur un {kind} refusée : {e} ✓")

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

    # 6. sans fan-out, rien ne change — l'exemple en déclare un sur `implement`
    # depuis que la course y est en production, et `bundle_nu` le retire déjà
    # pour retrouver un bundle nu, plutôt que de faire semblant qu'il n'y en a
    # jamais eu. Le constat reste dit ici, sur le bundle livré.
    assert bundle_livre()["nodes"]["implement"]["config"].get("fanout"), \
        "`implement` ne déclare plus de fan-out dans l'exemple"
    nu = bundle_nu()
    assert "fanout" not in json.dumps(nu), "aucun autre nœud n'en déclare"
    graph.validate(nu)
    assert graph.publish(FakeConn(), nu) == graph.content_hash(nu)
    for path in sorted((ROOT / "examples").glob("*.json")):
        graph.validate(json.loads(path.read_text()))
    print("6. les bundles d'examples/ se valident et se publient comme avant ✓")

    # 7. le `n` de keep_n : obligatoire, entier, et dans une borne dure
    borne = (graph.FANOUT_KEEP_MIN, graph.FANOUT_KEEP_MAX)
    assert borne == (2, 3), borne
    KEEP = BON | {"reduce": "keep_n"}
    graph.validate(avec_fanout("implement", KEEP | {"n": 2}))
    graph.validate(avec_fanout("implement", KEEP | {"n": 3}))
    print(f"7. keep_n avec n = {borne[0]}..{borne[1]} passe ✓")

    for quoi, n in (("n absent", None), ("n = 1", 1), ("n = 4", 4), ("n = 0", 0),
                    ("n négatif", -2), ("n non entier", "2"), ("n booléen", True)):
        fanout = KEEP if n is None else KEEP | {"n": n}
        e = refuse(f"keep_n avec {quoi}", avec_fanout("implement", fanout),
                   "implement", str(borne[0]), str(borne[1]))
        print(f"   keep_n, {quoi} → {e} ✓")

    # 8. le nœud arbitre : son bloc, sa source, ses trois issues
    graph.validate(bundle_livre())
    arbitre = bundle_livre()["nodes"]["judge"]
    assert graph.judge_source(arbitre) == "implement", arbitre
    assert set(graph.JUDGE_OUTCOMES) <= set(arbitre["edges"]), arbitre["edges"]
    print("8. l'exemple livré porte son arbitre : JUDGE, source `implement`, "
          f"issues {list(graph.JUDGE_OUTCOMES)} ✓")

    malades = {
        "arbitre sur un bloc ACT": (
            lambda b: b["nodes"]["judge"].update(block="ACT"), ("judge", "JUDGE")),
        "source non déclarée": (
            lambda b: b["nodes"]["judge"]["config"].update(finalists_from="fantome"),
            ("judge", "fantome")),
        "source qui ne réduit pas par keep_n": (
            lambda b: b["nodes"]["implement"]["config"]["fanout"].update(
                reduce="first_pass"), ("judge", "implement", "keep_n")),
        "arbitre sans arête `none`": (
            lambda b: b["nodes"]["judge"]["edges"].pop("none"), ("judge", "none")),
        "arbitre sans arête `sole`": (
            lambda b: b["nodes"]["judge"]["edges"].pop("sole"), ("judge", "sole")),
    }
    for quoi, (casser, attendus) in malades.items():
        bundle = bundle_livre()
        casser(bundle)
        e = refuse(quoi, bundle, *attendus)
        print(f"   {quoi} → {e} ✓")

    print("\nfan-out : OK — la déclaration existe, et une config fautive ne se publie pas")


if __name__ == "__main__":
    main()
