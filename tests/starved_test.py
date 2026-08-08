"""Le contrat sans base d'une tentative privée de fournisseur.

Les sorties de fournisseur ci-dessous sont des enregistrements minimaux :
elles gardent la forme du flux de chaque CLI et le message exact que
l'adaptateur doit reconnaître. Le test tranche quatre propriétés :

  1. le bloc rend `starved`, fournisseur et raison, sans interpréter la CLI
  2. sans fichier valide, il garde `crashed`
  3. `starved.json` est purgé avant la tentative suivante
  4. les adaptateurs opencode et codex reconnaissent leurs motifs fermés

Usage : uv run python tests/starved_test.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    "opencode": ROOT / "scripts" / "agent-opencode.sh",
    "codex": ROOT / "scripts" / "agent-codex.sh",
}
SORTIES = {
    "opencode": ("Quota exceeded for this account",
                 '{"type":"error","error":{"message":"Quota exceeded for this account"}}'),
    "codex": ("You have 0 weighted tokens left",
              '{"type":"error","message":"You have 0 weighted tokens left"}'),
}


class FauxConn:
    """La seule lecture du bloc : la clé du sujet, puis aucune reprise."""

    def execute(self, sql, *args):
        self.sql = sql
        return self

    def fetchone(self) -> dict | None:
        if "FROM subject" in self.sql:
            return {"subject_key": "test:starved"}
        return None


def contexte(workdir: Path, cmd: str, attempt: int) -> blocks.Context:
    """Un bloc agent minimal, sans base ni atelier git."""
    blocks.DATA_DIR = workdir
    spec = {
        "block": "ACT",
        "edges": {"ok": "fini"},
        "config": {"agent": {"cmd": cmd, "prompt": "fais le travail",
                             "timeout_s": 5, "silence_s": 5}},
    }
    return blocks.Context(
        FauxConn(),
        {"id": attempt, "node": "travail", "cycle": 1, "attempt": attempt},
        {"id": 0, "subject_id": 1},
        spec,
        {"name": "starved"},
    )


def contrat_du_bloc(workdir: Path) -> None:
    """1 à 3. Le fichier valide gagne sur le crash, et lui seul."""
    blocks.DATA_DIR = workdir
    workspace = blocks.item_workspace(0)
    workspace.mkdir(parents=True)
    stale = workspace / blocks.STARVED_NAME
    stale.write_text(json.dumps({"provider": "ancien", "reason": "ancien quota"}))

    crashed = blocks.act(contexte(workdir, "exit 7", 1))
    assert crashed["outcome"] == "crashed", crashed
    assert not stale.exists(), "le starved.json de la tentative précédente a survécu"
    print("1. starved.json antérieur purgé, absence nouvelle → crashed ✓")

    reason = SORTIES["codex"][0]
    cmd = ("printf '%s' "
           f"'{json.dumps({'provider': 'codex', 'reason': reason})}' "
           "> starved.json; exit 9")
    starved = blocks.act(contexte(workdir, cmd, 2))
    assert starved == {"outcome": "starved", "provider": "codex", "reason": reason}, starved
    print("2. starved.json valide sans outcome.json → starved, raison intacte ✓")

    invalide = blocks.act(contexte(
        workdir, "printf '{\"provider\": \"codex\"}' > starved.json; exit 9", 3))
    assert invalide["outcome"] == "crashed", invalide

    priorite = blocks.act(contexte(
        workdir,
        "printf '{\"provider\":\"codex\",\"reason\":\"quota\"}' > starved.json; "
        "printf '{\"outcome\":\"ok\",\"summary\":\"fait\"}' > outcome.json",
        4,
    ))
    assert priorite == {"outcome": "ok", "summary": "fait"}, priorite
    print("3. fichier starved mal formé → crashed ; outcome valide prioritaire ✓")


def faux_cli(path: Path, sortie: str, stderr: bool = False) -> None:
    """Une CLI qui rejoue une sortie enregistrée, puis sort en erreur."""
    cible = " >&2" if stderr else ""
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{sortie}'{cible}\nexit 1\n")
    path.chmod(0o755)


def joue_adaptateur(workdir: Path, provider: str) -> None:
    """L'adaptateur reconnaît son message dans JSON ou dans stderr brut."""
    workspace = workdir / provider
    workspace.mkdir()
    (workspace / "prompt.md").write_text("fais le travail\n")
    reason, sortie = SORTIES[provider]
    cli = workspace / f"faux-{provider}"
    faux_cli(cli, sortie if provider == "codex" else reason,
             stderr=provider == "opencode")
    env = os.environ | {
        f"{provider.upper()}_BIN": str(cli),
        f"{provider.upper()}_DIR": str(workspace),
    }
    if provider == "codex":
        env["CODEX_MODEL"] = "modele-test"
    done = subprocess.run(
        ["bash", str(ADAPTERS[provider])], cwd=workspace, env=env,
        capture_output=True, text=True,
    )

    assert done.returncode == 5, (done.returncode, done.stdout, done.stderr)
    result = json.loads((workspace / blocks.STARVED_NAME).read_text())
    assert result == {"provider": provider, "reason": reason}, result
    assert reason in done.stderr, done.stderr
    assert not (workspace / blocks.OUTCOME_NAME).exists()
    print(f"4. {provider} : sortie enregistrée → starved.json, raison intacte ✓")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-starved-"))
    os.environ.pop("GRAPHATOM_REPO_DIR", None)
    os.environ.pop("GRAPHATOM_AGENT_DSN", None)
    try:
        contrat_du_bloc(workdir)
        for provider in ("opencode", "codex"):
            joue_adaptateur(workdir, provider)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\nstarved : OK — l'adaptateur détecte, le bloc lit, la raison reste exacte")


if __name__ == "__main__":
    main()
