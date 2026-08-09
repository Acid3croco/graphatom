"""Contrat d'exécution d'un wagon et construction de sa commande.

Un wagon exécutable déclare ``config.execution.kind`` :

``agent``
    Le wagon appelle l'adaptateur de modèle déclaré dans ``config.agent``.
    Une ``execution.cmd`` optionnelle enveloppe cet adaptateur.

``command``
    Le wagon exécute une commande déterministe. Il n'a ni modèle, ni prompt,
    ni passation à fabriquer.

Les anciennes révisions publiées restent lisibles : ``agent.cmd`` et
``harness_cmd`` sont résolus selon leur ancien contrat. Les graphes sources
actuels n'emploient plus ces deux formes.
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
    """Le contrat résolu et complet d'une tentative de wagon."""

    cli: str | None
    model: str | None
    cmd: str | None
    kind: str = "model"
    effort: str | None = None
    timeout_s: int | float | None = None
    silence_s: int | float | None = None
    prompt: str | None = None
    handoff: bool = False

    @property
    def cmd_uses_executor(self) -> bool:
        """Compatibilité de lecture : un wrapper composé appelle le modèle."""
        return self.kind == "composed"


def configured(node: dict) -> bool:
    """Dit si le wagon déclare une exécution réelle, nouvelle ou historique."""
    config = node.get("config") or {}
    return any(key in config for key in ("execution", "harness_cmd", "agent"))


def resolve(bundle: dict, node: dict) -> Executor:
    """Résout l'unique contrat d'exécution, sans modifier le graph."""
    defaults = bundle.get("agent") or {}
    config = node.get("config") or {}
    local = config.get("agent") or {}
    execution = config.get("execution")

    if execution is not None:
        kind = execution["kind"]
        if kind == "command":
            return Executor(
                kind="command",
                cli=None,
                model=None,
                cmd=execution["cmd"],
                timeout_s=execution.get("timeout_s"),
                silence_s=execution.get("silence_s"),
            )
        command = execution.get("cmd")
        return Executor(
            kind="composed" if command is not None else "model",
            cli=local.get("cli", defaults.get("cli")),
            model=local.get("model", defaults.get("model")),
            cmd=command,
            effort=local.get("effort", defaults.get("effort")),
            timeout_s=execution.get(
                "timeout_s", local.get("timeout_s", defaults.get("timeout_s"))
            ),
            silence_s=execution.get("silence_s", local.get("silence_s")),
            prompt=local.get("prompt"),
            handoff=local.get("passation", True),
        )

    # Compatibilité des révisions déjà publiées. Une nouvelle publication
    # qui porte `execution` ne passe jamais par ce chemin implicite.
    harness = config.get("harness_cmd")
    command = harness if harness is not None else local.get("cmd")
    composed = harness is None and local.get("cmd_uses_executor", False)
    return Executor(
        kind=("harness" if harness is not None else
              "composed" if composed else
              "shell" if command is not None else "model"),
        cli=(None if harness is not None
             else local.get("cli", defaults.get("cli"))),
        model=(None if harness is not None
               else local.get("model", defaults.get("model"))),
        cmd=command,
        effort=(None if harness is not None
                else local.get("effort", defaults.get("effort"))),
        timeout_s=local.get("timeout_s", defaults.get("timeout_s")),
        silence_s=local.get("silence_s"),
        prompt=local.get("prompt"),
        handoff=(harness is None and local.get("passation", True)),
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
