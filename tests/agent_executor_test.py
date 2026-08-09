"""Le contrat structuré des exécuteurs d'agent, testé sans base ni réseau.

Usage : uv run python tests/agent_executor_test.py
"""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, executors, graph  # noqa: E402


class FakeConn:
    """La tentative ne lit ici que le sujet et l'absence de reprise."""

    def execute(self, sql, *args):
        self.sql = sql
        return self

    def fetchone(self):
        if "FROM subject" in self.sql:
            return {"subject_key": "gh:test/graphatom#194"}
        return None


class NoWriteConn:
    """La publication fautive ne doit pas atteindre la base."""

    def execute(self, sql, *args):
        raise AssertionError("la publication a écrit avant de valider")


def node(agent: dict, fanout: dict | None = None) -> dict:
    """Un nœud agent minimal pour la résolution et l'exécution."""
    local = {"prompt": "Réponds à ce test", **agent}
    command = local.pop("cmd", None)
    composed = local.pop("cmd_uses_executor", False)
    local.pop("cmd_reason", None)
    runtime = {key: local.pop(key) for key in ("timeout_s", "silence_s")
               if key in local}
    if command is not None and not composed:
        execution = {"kind": "command", "cmd": command, **runtime}
        config = {"execution": execution}
    else:
        execution = {"kind": "agent", **runtime}
        if command is not None:
            execution["cmd"] = command
        config = {"agent": local, "execution": execution}
    if fanout is not None:
        config["fanout"] = fanout
    return {"block": "ACT", "config": config, "edges": {"done": "fin"}}


def legacy_node(agent: dict) -> dict:
    """Une ancienne révision, réservée aux preuves de compatibilité."""
    return {"block": "ACT",
            "config": {"agent": {"prompt": "ancien prompt", **agent}},
            "edges": {"done": "fin"}}


def bundle(agent: dict, spec: dict | None = None) -> dict:
    """Un graph minimal qui porte les valeurs d'exécuteur par défaut."""
    return {
        "name": "executor-test",
        "entry": "travail",
        "agent": agent,
        "budgets": {"escalations": 1, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "fin", "exhausted_to": "fin"},
        "nodes": {"travail": spec or node({}), "fin": {"terminal": True}},
    }


def resolution() -> None:
    """1. Le wagon agent hérite séparément de la CLI et du modèle."""
    base = bundle({"cli": "codex", "model": "gpt-default"})
    defaults = executors.resolve(base, node({}))
    claude = executors.resolve(base, node({"cli": "claude"}))
    sonnet = executors.resolve(base, node({"model": "sonnet"}))
    assert (defaults.kind, defaults.cli, defaults.model) == (
        "model", "codex", "gpt-default")
    assert (claude.kind, claude.cli, claude.model) == (
        "model", "claude", "gpt-default")
    assert (sonnet.kind, sonnet.cli, sonnet.model) == (
        "model", "codex", "sonnet")

    explicite = executors.resolve(base, node({"cli": "claude", "model": "opus",
                                               "cmd": "printf commande-explicite",
                                               "cmd_uses_executor": True}))
    assert explicite.kind == "composed"
    assert explicite.cmd == "printf commande-explicite"
    assert executors.command(explicite) == "printf commande-explicite"
    print("1. contrats model/composed, héritage et commande explicites ✓")


def executable(path: Path, content: str) -> None:
    """Écrit une fausse CLI locale."""
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


FAKES = {
    "claude": """#!/usr/bin/env bash
printf '%s\n' "$@" > "$CAPTURE"
printf '%s\n' '{"result":"ok","usage":{"input_tokens":11,"output_tokens":5},"total_cost_usd":0.02}'
printf '%s\n' '## Fait' 'Test.' '## Appris' 'Rien.' '## Pas fait' 'Rien.' > "$GRAPHATOM_WORKSPACE/passation-travail.md"
printf '%s\n' '{"outcome":"done","summary":"claude"}' > "$GRAPHATOM_WORKSPACE/outcome.json"
""",
    "codex": """#!/usr/bin/env bash
printf '%s\n' "$@" > "$CAPTURE"
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":6}}'
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}'
printf '%s\n' '## Fait' 'Test.' '## Appris' 'Rien.' '## Pas fait' 'Rien.' > "$GRAPHATOM_WORKSPACE/passation-travail.md"
printf '%s\n' '{"outcome":"done","summary":"codex"}' > "$GRAPHATOM_WORKSPACE/outcome.json"
""",
    "opencode": """#!/usr/bin/env bash
printf '%s\n' "$@" > "$CAPTURE"
printf '%s\n' '{"type":"text","part":{"text":"ok"}}'
printf '%s\n' '{"type":"step_finish","part":{"tokens":{"input":13,"output":7,"reasoning":2,"cache":{"read":1,"write":0},"total":23},"cost":0.03}}'
printf '%s\n' '## Fait' 'Test.' '## Appris' 'Rien.' '## Pas fait' 'Rien.' > "$GRAPHATOM_WORKSPACE/passation-travail.md"
printf '%s\n' '{"outcome":"done","summary":"opencode"}' > "$GRAPHATOM_WORKSPACE/outcome.json"
""",
}


def adapter(cli: str, model: str, expected_input: int, tmp: Path) -> None:
    """Une fausse CLI vérifie son invocation, le prompt et son usage."""
    workspace = tmp / cli
    workspace.mkdir()
    binary = workspace / f"fake-{cli}"
    capture = workspace / "args.txt"
    executable(binary, FAKES[cli])
    env_name = executors.ADAPTERS[cli].binary_env
    old = os.environ.get(env_name)
    os.environ[env_name] = str(binary)
    os.environ["CAPTURE"] = str(capture)
    try:
        effort = "high" if cli == "codex" else None
        spec = node({"cli": cli, "model": model, "effort": effort,
                     "timeout_s": 10,
                     "silence_s": 10})
        paquet = bundle({"cli": "codex", "model": "défaut"}, spec)
        run = {"id": expected_input, "node": "travail", "cycle": 1,
               "attempt": 1, "candidate": None}
        ctx = blocks.Context(FakeConn(), run, {"id": expected_input,
                                               "subject_id": 1}, spec, paquet)
        ctx.workspace = workspace
        ctx.worktree = tmp
        result = blocks.act(ctx)
    finally:
        if old is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = old
        os.environ.pop("CAPTURE", None)

    assert result["outcome"] == "done", result
    assert result["usage"]["input_tokens"] == expected_input, result
    args = capture.read_text().splitlines()
    assert model in args, args
    assert "Réponds à ce test" in "\n".join(args), args
    trace = json.loads(blocks.attempt_command(workspace, run).read_text())
    resolved = executors.resolve(paquet, spec)
    assert trace == {
        "kind": "model",
        "executor": {"cli": cli, "model": model, "effort": effort},
        "command": executors.command(resolved),
    }, trace


def adapters(tmp: Path) -> None:
    """2 et 3. Chaque adaptateur transmet, mesure et laisse sa commande exacte."""
    adapter("claude", "claude-test", 11, tmp)
    adapter("codex", "codex-test", 12, tmp)
    adapter("opencode", "opencode-test", 13, tmp)
    print("2. claude, codex et opencode : invocation, prompt et usage ✓")
    print("3. la commande CLI + modèle effective est tracée hors du journal ✓")


def explicit_command(tmp: Path) -> None:
    """4. Le vrai bloc exécute cmd, sans appeler l'adaptateur hérité."""
    workspace = tmp / "explicite"
    workspace.mkdir()
    commande = "printf commande-explicite; " \
               "printf '{\"outcome\":\"done\",\"summary\":\"shell\"}' > outcome.json"
    spec = node({"cmd": commande, "timeout_s": 10, "silence_s": 10,
                 "passation": False})
    paquet = bundle({"cli": "codex", "model": "ne-doit-pas-tourner"}, spec)
    run = {"id": 40, "node": "travail", "cycle": 1, "attempt": 1,
           "candidate": None}
    ctx = blocks.Context(FakeConn(), run, {"id": 40, "subject_id": 1}, spec, paquet)
    ctx.workspace = workspace
    ctx.worktree = tmp
    result = blocks.act(ctx)
    trace = json.loads(blocks.attempt_command(workspace, run).read_text())
    assert result["outcome"] == "done", result
    assert trace == {"kind": "command", "executor": None,
                     "command": commande}, trace
    assert not workspace.joinpath("prompt.md").exists()
    assert "agent-codex" not in trace["command"]
    assert "ne-doit-pas-tourner" not in trace["command"]
    print("4. le bloc exécute cmd en priorité sur l'adaptateur structuré ✓")


def composed_command(tmp: Path) -> None:
    """4 bis. Une commande composée trace son exécuteur et le texte interpolé."""
    workspace = tmp / "composee"
    workspace.mkdir()
    commande = (
        "printf '%s' '{subject_key}|{label}|{strategy}' > filled.txt; "
        "printf '{\"outcome\":\"done\",\"summary\":\"composed\"}' "
        "> outcome.json"
    )
    spec = node({"cli": "codex", "model": "gpt-5.6-sol", "effort": "high",
                 "cmd": commande, "cmd_uses_executor": True,
                 "timeout_s": 10, "silence_s": 10, "passation": False})
    spec["config"] |= {"label": "minimal Sol", "strategy": "diff minimal"}
    paquet = bundle({"cli": "codex", "model": "défaut"}, spec)
    run = {"id": 41, "node": "travail", "cycle": 2, "attempt": 3,
           "candidate": 1}
    ctx = blocks.Context(FakeConn(), run, {"id": 41, "subject_id": 1}, spec, paquet)
    ctx.workspace = workspace
    ctx.worktree = tmp
    result = blocks.act(ctx)
    trace = json.loads(blocks.attempt_command(workspace, run).read_text())
    filled = commande.replace("{subject_key}", "gh:test/graphatom#194") \
        .replace("{label}", "minimal Sol").replace("{strategy}", "diff minimal")
    assert result["outcome"] == "done", result
    assert workspace.joinpath("filled.txt").read_text() == (
        "gh:test/graphatom#194|minimal Sol|diff minimal"
    )
    assert trace == {
        "kind": "composed",
        "executor": {"cli": "codex", "model": "gpt-5.6-sol", "effort": "high"},
        "command": filled,
    }, trace
    print("4 bis. commande composée : exécuteur, effort et interpolation tracés ✓")


def validation() -> None:
    """5 et 7. La publication refuse une CLI inconnue et les secrets."""
    graph.validate(bundle({"cli": "codex", "model": "gpt"},
                          node({"cmd": "printf explicite"})))
    for mauvais, attendu in (
        ({"cli": "inconnue", "model": "x"}, "inconnue"),
        ({"cli": "codex", "model": "x", "api_key": "secret"}, "api_key"),
        ({"cli": "codex", "model": "x", "token": "secret"}, "token"),
    ):
        try:
            graph.publish(NoWriteConn(), bundle(mauvais))
        except graph.GraphError as exc:
            texte = str(exc)
            assert attendu in texte, texte
        else:
            raise AssertionError(f"configuration acceptée : {mauvais}")
    for mauvais, attendu in (
        ({"cmd": "true", "cmd_uses_executor": "oui"}, "booléen"),
        ({"cmd_uses_executor": True}, "sans cmd"),
    ):
        try:
            graph.validate(bundle({"cli": "codex", "model": "gpt"},
                                  legacy_node(mauvais)))
        except graph.GraphError as exc:
            assert attendu in str(exc), str(exc)
        else:
            raise AssertionError(f"configuration composée acceptée : {mauvais}")
    try:
        graph.validate(bundle({}, legacy_node(
            {"cmd": "true", "cmd_uses_executor": True})))
    except graph.GraphError as exc:
        assert "sans CLI" in str(exc), str(exc)
    else:
        raise AssertionError("commande composée sans CLI acceptée")
    command = node({"cmd": "true"})
    command["config"]["agent"] = {"prompt": "parasite"}
    try:
        graph.validate(bundle({"cli": "codex"}, command))
    except graph.GraphError as exc:
        assert "command" in str(exc) and "agent" in str(exc), str(exc)
    else:
        raise AssertionError("execution command avec agent acceptée")
    for execution, attendu in (
        ({}, "kind"),
        ({"kind": "autre"}, "kind"),
        ({"kind": "command"}, "sans cmd"),
        ({"kind": "command", "cmd": ""}, "sans cmd"),
        ({"kind": "command", "cmd": "true", "silence_s": 0}, "silence_s"),
        ({"kind": "agent", "inconnu": True}, "inconnu"),
    ):
        spec = node({})
        spec["config"]["execution"] = execution
        if execution.get("kind") == "command":
            spec["config"].pop("agent", None)
        try:
            graph.validate(bundle({"cli": "codex"}, spec))
        except graph.GraphError as exc:
            assert attendu in str(exc), (execution, exc)
        else:
            raise AssertionError(f"execution fautive acceptée : {execution}")
    duplicate = node({"timeout_s": 10})
    duplicate["config"]["agent"]["timeout_s"] = 20
    try:
        graph.validate(bundle({"cli": "codex"}, duplicate))
    except graph.GraphError as exc:
        assert "historiques" in str(exc) and "timeout_s" in str(exc), str(exc)
    else:
        raise AssertionError("deux timeout_s concurrents acceptés")
    variant = node({}, {"variants": [{"agent": {"cmd": "true"}}],
                        "reduce": "first_pass"})
    try:
        graph.validate(bundle({"cli": "codex"}, variant))
    except graph.GraphError as exc:
        assert "variants[0]" in str(exc) and "cmd" in str(exc), str(exc)
    else:
        raise AssertionError("commande historique cachée dans une variante")
    print("5. une CLI inconnue est refusée et nommée ✓")
    print("7. execution ferme la forme et refuse les réglages ambigus ✓")


def variantes() -> None:
    """6. Une variante change une seule valeur sans contaminer sa voisine."""
    spec = node({}, {"variants": [{"agent": {"cli": "claude"}},
                                    {"agent": {"model": "gpt-variante"}}],
                     "reduce": "first_pass"})
    paquet = bundle({"cli": "codex", "model": "gpt-default"}, spec)
    premiere = executors.resolve(paquet, graph.candidate_node(spec, 0))
    seconde = executors.resolve(paquet, graph.candidate_node(spec, 1))
    assert (premiere.cli, premiere.model) == ("claude", "gpt-default")
    assert (seconde.cli, seconde.model) == ("codex", "gpt-variante")
    assert executors.command(premiere) != executors.command(seconde)
    print("6. les variantes produisent deux commandes sans contamination ✓")


def main() -> None:
    resolution()
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-executor-"))
    blocks.DATA_DIR = tmp / "data"
    os.environ.pop("GRAPHATOM_REPO_DIR", None)
    adapters(tmp)
    explicit_command(tmp)
    composed_command(tmp)
    validation()
    variantes()
    print("\nexécuteurs structurés : OK")


if __name__ == "__main__":
    main()
