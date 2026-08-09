"""Résolution des exécuteurs d'agent et construction de leur commande.

Le graph fixe les valeurs par défaut dans ``agent``. Le nœud surcharge
``config.agent.cli`` ou ``config.agent.model`` séparément. Une commande
``config.agent.cmd`` reste l'échappatoire explicite et a toujours priorité.
"""

from dataclasses import dataclass
from pathlib import Path
import shlex


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@dataclass(frozen=True)
class Adapter:
    """Le pont unique entre une CLI et le contrat du workspace."""

    cli: str
    script: str
    model_env: str
    binary_env: str

    def command(self, model: str | None) -> str:
        """Construit la commande shell de l'adaptateur et son modèle."""
        parts = []
        if model:
            parts.append(f"{self.model_env}={shlex.quote(model)}")
        parts.extend(("bash", shlex.quote(str(SCRIPTS / self.script))))
        return " ".join(parts)


ADAPTERS = {
    "claude": Adapter("claude", "agent-claude.sh", "CLAUDE_MODEL", "CLAUDE_BIN"),
    "codex": Adapter("codex", "agent-codex.sh", "CODEX_MODEL", "CODEX_BIN"),
    "opencode": Adapter("opencode", "agent-opencode.sh", "OPENCODE_MODEL",
                        "OPENCODE_BIN"),
}
SUPPORTED_CLIS = frozenset(ADAPTERS)


@dataclass(frozen=True)
class Executor:
    """Les valeurs résolues pour une tentative."""

    cli: str | None
    model: str | None
    cmd: str | None


def resolve(bundle: dict, node: dict) -> Executor:
    """Résout les valeurs du graph puis du nœud, sans modifier les données."""
    defaults = bundle.get("agent") or {}
    local = (node.get("config") or {}).get("agent") or {}
    return Executor(
        cli=local.get("cli", defaults.get("cli")),
        model=local.get("model", defaults.get("model")),
        cmd=local.get("cmd"),
    )


def command(executor: Executor) -> str:
    """Rend la commande effective; ``cmd`` gagne sur l'exécuteur structuré."""
    if executor.cmd is not None:
        return executor.cmd
    if executor.cli not in ADAPTERS:
        raise ValueError(f"CLI d'agent inconnue : {executor.cli!r}")
    return ADAPTERS[executor.cli].command(executor.model)
