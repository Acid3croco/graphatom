"""Le runner de mutants, prouvé sur un module jouet : ses quatre verdicts.

Un runner qui compterait mal ses mutants rendrait la règle « zéro
survivant » creuse. On le joue donc sur un module minuscule et des preuves
minuscules, dans un dépôt jetable qui a la forme du vrai :

  1. les opérateurs énumèrent chaque site une fois, nommé par ligne et
     colonne — deux mutants d'une même ligne ne se confondent pas, un
     argument nommé de pure forme n'est pas muté, une comparaison chaînée
     donne un mutant par maillon
  2. un mutant vu par une preuve est `killed`, un mutant que rien ne voit
     est `survived`, un mutant qui boucle est `timeout` — et le verdict vient
     bien du source muté que la preuve importe en sous-processus
  3. un équivalent inscrit avec sa raison est `equivalent` ; sans raison, le
     fichier est refusé
  4. un worker qui lève arrête la campagne au lieu d'inventer un verdict

Usage : uv run python tests/mutants_test.py
"""

import ast
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mutants", ROOT / "scripts" / "mutants.py")
mutants = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mutants)

JOUET = '''\
def borne(n: int) -> bool:
    return 0 < n <= 3


def ordre(a, b) -> bool:
    return a < b and not (a == b)


def rendu(valeur: dict) -> str:
    import json
    return json.dumps(valeur, sort_keys=True, indent=2)


def boucle(n: int) -> int:
    total = 0
    while n > 0:
        total += n
        n -= 1
    return total
'''

PREUVE = '''\
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import jouet
assert jouet.borne(1) and jouet.borne(3) and not jouet.borne(0) and not jouet.borne(4)
assert jouet.ordre(1, 2) and not jouet.ordre(2, 2)
assert jouet.boucle(3) == 6
'''


def sites() -> None:
    found = mutants.mutants(JOUET)
    names = [m.name for m in found]
    assert len(names) == len(set(names)), "deux mutants portent le même nom"
    chained = [n for n in names if n.startswith("2:") and "compare" in n]
    assert len(chained) == 2, f"une comparaison chaînée donne un mutant par maillon : {chained}"
    assert not any(n.startswith("11:") and "bool" in n for n in names), \
        "sort_keys=True est de pure forme : non muté"
    assert any("int" in n for n in names) and any("augassign" in n for n in names)
    print("1. sites énumérés une fois, nommés ligne:colonne, forme exclue, chaîne éclatée ✓")


def depot_jouet(tmp: Path) -> Path:
    """Un dépôt de la forme du vrai : src/, tests/, un pyproject minimal."""
    repo = tmp / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "scripts").mkdir()
    (repo / "src" / "jouet.py").write_text(JOUET)
    (repo / "tests" / "jouet_test.py").write_text(PREUVE)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "jouet"\nversion = "0"\nrequires-python = ">=3.12"\n')
    return repo


def verdicts(tmp: Path) -> None:
    repo = depot_jouet(tmp)
    mutants.ROOT = repo
    mutants.EQUIVALENTS = repo / "scripts" / "mutants_equivalents.txt"
    mutants.TIMEOUT_FLOOR_S = 8.0
    report = mutants.campaign(Path("src/jouet.py"), ["tests/jouet_test.py"], workers=2)
    found = report["verdicts"]
    line_of = {m.name: m.line for m in mutants.mutants(JOUET)}
    # `0 < n` en `0 <= n` : borne(0) passe — vu ; `total += n` en `-=` — vu
    assert all(v == "killed" for n, v in found.items() if line_of[n] == 2), found
    assert all(v == "killed" for n, v in found.items() if line_of[n] == 17), found
    # `n > 0` muté en `n >= 0` : boucle(3) rend encore 6 — que rien ne voit
    survivants = [n for n, v in found.items() if v == "survived"]
    assert any(line_of[n] == 16 for n in survivants), (survivants, found)
    # `n -= 1` en `n += 1` : la boucle ne finit plus — dépassement, pas tué
    assert any(v == "timeout" and line_of[n] == 18 for n, v in found.items()), found
    assert report["timeout"] and report["survived"]
    print("2. killed / survived / timeout rendus depuis le source muté du sous-processus ✓")


def equivalents(tmp: Path) -> None:
    repo = mutants.ROOT
    survivant = next(n for n, v in mutants.campaign(
        Path("src/jouet.py"), ["tests/jouet_test.py"], workers=1)["verdicts"].items()
        if v == "survived")
    mutants.EQUIVALENTS.write_text(f"jouet:{survivant}  # n vaut 0 à la sortie : >= ou > idem\n")
    report = mutants.campaign(Path("src/jouet.py"), ["tests/jouet_test.py"], workers=1)
    assert report["verdicts"][survivant] == "equivalent", report["verdicts"]
    mutants.EQUIVALENTS.write_text(f"jouet:{survivant}\n")
    try:
        mutants.equivalents()
    except SystemExit as exc:
        assert "sans raison" in str(exc), exc
    else:
        raise AssertionError("un équivalent sans raison doit être refusé")
    mutants.EQUIVALENTS.unlink()
    assert repo == mutants.ROOT
    print("3. équivalent inscrit avec sa raison honoré ; sans raison, refusé ✓")


def worker_en_erreur() -> None:
    original = mutants._judge

    def casse(*_args, **_kwargs):
        raise RuntimeError("worker cassé")

    mutants._judge = casse
    try:
        mutants.campaign(Path("src/jouet.py"), ["tests/jouet_test.py"], workers=2)
    except RuntimeError as exc:
        assert "worker cassé" in str(exc)
    else:
        raise AssertionError("une erreur du runner doit arrêter la campagne")
    finally:
        mutants._judge = original
    print("4. un worker qui lève arrête la campagne : aucun verdict inventé ✓")


def main() -> None:
    sites()
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-mutants-test-"))
    try:
        verdicts(tmp)
        equivalents(tmp)
        worker_en_erreur()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nrunner de mutants : OK — quatre verdicts, aucun inventé")


if __name__ == "__main__":
    main()
