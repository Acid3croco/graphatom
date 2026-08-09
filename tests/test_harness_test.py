"""Régressions du harnais fermé : sélection, arrêt, 000 et verdict."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "graphatom_test_harness", ROOT / "scripts" / "test_harness.py"
)
HARNESS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(HARNESS)


def main() -> None:
    gates = HARNESS.backend_gates(["src/graphatom/scheduler.py"])
    names = [name for name, _ in gates]
    assert names == ["import", "crash_test.py", "reconnect_test.py"], names
    assert len(names) == len(set(names))
    assert HARNESS.backend_gates(["front/components/item.tsx"]) == []
    assert any(path.startswith(HARNESS.FRONT_PREFIXES)
               for path in ["scripts/test_harness.py"])
    print("1. le diff choisit une liste backend fermée, ordonnée et sans doublon ✓")

    root = Path(tempfile.mkdtemp(prefix="graphatom-harness-"))
    marker = root / "second"
    plan = [
        ("premier", [sys.executable, "-c", "raise SystemExit(7)"]),
        ("second", [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('non')"]),
    ]
    evidence, failure = HARNESS.execute_gates(
        root, root, "backend", plan, time.monotonic() + 10,
    )
    assert failure == ("premier", 7), (failure, evidence)
    assert [item["name"] for item in evidence] == ["premier"]
    assert not marker.exists(), "la commande après l'échec a été lancée"
    print("2. premier code non nul : arrêt terminal, aucune course ni faux succès ✓")

    sleeper = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,time; p=subprocess.Popen(['sleep','60']); "
         "print(p.pid,flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    assert sleeper.stdout is not None
    child = int(sleeper.stdout.readline())
    HARNESS._stop([sleeper])
    assert sleeper.poll() is not None
    time.sleep(0.1)
    assert not Path(f"/proc/{child}").exists(), f"le descendant {child} a survécu"
    port = HARNESS._port()
    assert not HARNESS._ready(f"http://127.0.0.1:{port}/", time.monotonic() + 0.2)
    print("3. processus frontend toujours arrêté ; readiness 000 reste un échec stable ✓")

    verdict = root / "verdict.md"
    verdict.write_text(
        "# Finaliste A\n\n1. **Tenu.** oui\n\n"
        "# Finaliste B\n\n1. **Tenu.** oui\n4. **Raté.** preuve absente\n\n"
        "# Comparaison\n\nB choisi.\n\n## Verdict\n\nÉlu : finaliste B\n"
    )
    assert blocks.elected_failures(verdict) == [4]
    verdict.write_text(verdict.read_text().replace("**Raté.**", "**Tenu.**"))
    assert blocks.elected_failures(verdict) == []
    verdict.write_text(
        "## Finaliste B\n\n1) Tenu. oui\n4) Critère raté : preuve absente\n\n"
        "## Verdict\n\nÉlu : finaliste B\n"
    )
    assert blocks.elected_failures(verdict) == [4]
    verdict.write_text(
        "## Finaliste B\n\n1. Tenu. oui\n- 4. Raté. preuve absente\n\n"
        "## Verdict\n\nÉlu : finaliste B\n"
    )
    assert blocks.elected_failures(verdict) == [4]
    verdict.write_text("Comparaison libre.\n\n## Verdict\n\nÉlu : finaliste B\n")
    assert blocks.elected_failures(verdict) == [0]
    print("4. le faux positif #124 est bloqué tant que le juge élu garde un raté ✓")

    HARNESS._write(root, "backend", ["src/x.py"], evidence, "fail",
                   "premier a échoué", time.monotonic())
    ledger = json.loads((root / "test-evidence.json").read_text())
    assert ledger["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert ledger["commands"][0]["exit_code"] == 7
    assert "Durée totale" in (root / "test_backend.md").read_text()
    shutil.rmtree(root)
    print("5. ledger : commande, code, durée, empreinte et usage nul ✓")

    print("\nharnais de test : OK — déterministe, sériel et fermé")


if __name__ == "__main__":
    main()
