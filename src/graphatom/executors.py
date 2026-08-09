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
    effort_env: str | None = None
    timeout_env: str | None = None

    def command(self, model: str | None, effort: str | None = None,
                timeout_s: int | float | None = None) -> str:
        """Construit la commande shell et ses réglages déclarés."""
        parts = []
        if model:
            parts.append(f"{self.model_env}={shlex.quote(model)}")
        if effort and self.effort_env:
            parts.append(f"{self.effort_env}={shlex.quote(effort)}")
        if timeout_s is not None and self.timeout_env:
            parts.append(f"{self.timeout_env}={shlex.quote(str(timeout_s))}")
        parts.extend(("bash", shlex.quote(str(SCRIPTS / self.script))))
        return " ".join(parts)


ADAPTERS = {
    "claude": Adapter("claude", "agent-claude.sh", "CLAUDE_MODEL", "CLAUDE_BIN",
                       timeout_env="CLAUDE_TIMEOUT_S"),
    "codex": Adapter("codex", "agent-codex.sh", "CODEX_MODEL", "CODEX_BIN",
                      "CODEX_REASONING_EFFORT", "CODEX_TIMEOUT_S"),
    "opencode": Adapter("opencode", "agent-opencode.sh", "OPENCODE_MODEL",
                        "OPENCODE_BIN", timeout_env="OPENCODE_TIMEOUT_S"),
}
SUPPORTED_CLIS = frozenset(ADAPTERS)


@dataclass(frozen=True)
class Executor:
    """Les valeurs résolues pour une tentative."""

    cli: str | None
    model: str | None
    cmd: str | None
    effort: str | None = None
    timeout_s: int | float | None = None


def resolve(bundle: dict, node: dict) -> Executor:
    """Résout les valeurs du graph puis du nœud, sans modifier les données."""
    defaults = bundle.get("agent") or {}
    local = (node.get("config") or {}).get("agent") or {}
    return Executor(
        cli=local.get("cli", defaults.get("cli")),
        model=local.get("model", defaults.get("model")),
        cmd=local.get("cmd"),
        effort=local.get("effort", defaults.get("effort")),
        timeout_s=local.get("timeout_s", defaults.get("timeout_s")),
    )


def command(executor: Executor) -> str:
    """Rend la commande effective; ``cmd`` gagne sur l'exécuteur structuré."""
    if executor.cmd is not None:
        return executor.cmd
    if executor.cli not in ADAPTERS:
        raise ValueError(f"CLI d'agent inconnue : {executor.cli!r}")
    return ADAPTERS[executor.cli].command(
        executor.model, executor.effort, executor.timeout_s)


def environment(executor: Executor) -> dict[str, str]:
    """Rend les variables structurées, y compris pour une commande composée."""
    adapter = ADAPTERS.get(executor.cli)
    if adapter is None:
        return {}
    values = ((adapter.model_env, executor.model),
              (adapter.effort_env, executor.effort),
              (adapter.timeout_env, executor.timeout_s))
    env = {name: str(value) for name, value in values
           if name is not None and value is not None}
    env["GRAPHATOM_AGENT_CLI"] = adapter.cli
    return env
