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
    config = {"agent": {"prompt": "Réponds à ce test", **agent}}
    if fanout is not None:
        config["fanout"] = fanout
    return {"block": "ACT", "config": config, "edges": {"done": "fin"}}


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
    """1. Le nœud hérite séparément de la CLI et du modèle du graph."""
    base = bundle({"cli": "codex", "model": "gpt-default"})
    assert executors.resolve(base, node({})) == executors.Executor(
        cli="codex", model="gpt-default", cmd=None)
    assert executors.resolve(base, node({"cli": "claude"})) == executors.Executor(
        cli="claude", model="gpt-default", cmd=None)
    assert executors.resolve(base, node({"model": "sonnet"})) == executors.Executor(
        cli="codex", model="sonnet", cmd=None)

    explicite = executors.resolve(base, node({"cli": "claude", "model": "opus",
                                               "cmd": "printf commande-explicite"}))
    assert explicite.cmd == "printf commande-explicite"
    assert executors.command(explicite) == "printf commande-explicite"
    print("1. héritage complet, surcharges partielles et priorité de cmd ✓")


def executable(path: Path, content: str) -> None:
    """Écrit une fausse CLI locale."""
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


FAKES = {
    "claude": """#!/usr/bin/env bash
printf '%s\n' "$@" > "$CAPTURE"
printf '%s\n' '{"result":"ok","usage":{"input_tokens":11,"output_tokens":5},"total_cost_usd":0.02}'
printf '%s\n' '{"outcome":"done","summary":"claude"}' > "$GRAPHATOM_WORKSPACE/outcome.json"
""",
    "codex": """#!/usr/bin/env bash
printf '%s\n' "$@" > "$CAPTURE"
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":6}}'
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}'
printf '%s\n' '{"outcome":"done","summary":"codex"}' > "$GRAPHATOM_WORKSPACE/outcome.json"
""",
    "opencode": """#!/usr/bin/env bash
printf '%s\n' "$@" > "$CAPTURE"
printf '%s\n' '{"type":"text","part":{"text":"ok"}}'
printf '%s\n' '{"type":"step_finish","part":{"tokens":{"input":13,"output":7,"reasoning":2,"cache":{"read":1,"write":0},"total":23},"cost":0.03}}'
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
        spec = node({"cli": cli, "model": model, "timeout_s": 10,
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
    log = blocks.attempt_log(workspace, run).read_text()
    assert cli in log and model in log, log


def adapters(tmp: Path) -> None:
    """2 et 3. Chaque adaptateur transmet et mesure; le bloc journalise."""
    adapter("claude", "claude-test", 11, tmp)
    adapter("codex", "codex-test", 12, tmp)
    adapter("opencode", "opencode-test", 13, tmp)
    print("2. claude, codex et opencode : invocation, prompt et usage ✓")
    print("3. la commande CLI + modèle effectivement exécutée est journalisée ✓")


def explicit_command(tmp: Path) -> None:
    """4. Le vrai bloc exécute cmd, sans appeler l'adaptateur hérité."""
    workspace = tmp / "explicite"
    workspace.mkdir()
    commande = "printf commande-explicite; " \
               "printf '{\"outcome\":\"done\",\"summary\":\"shell\"}' > outcome.json"
    spec = node({"cmd": commande, "timeout_s": 10, "silence_s": 10})
    paquet = bundle({"cli": "codex", "model": "ne-doit-pas-tourner"}, spec)
    run = {"id": 40, "node": "travail", "cycle": 1, "attempt": 1,
           "candidate": None}
    ctx = blocks.Context(FakeConn(), run, {"id": 40, "subject_id": 1}, spec, paquet)
    ctx.workspace = workspace
    ctx.worktree = tmp
    result = blocks.act(ctx)
    log = blocks.attempt_log(workspace, run).read_text()
    assert result["outcome"] == "done", result
    assert "commande-explicite" in log, log
    assert "agent-codex" not in log and "ne-doit-pas-tourner" not in log, log
    print("4. le bloc exécute cmd en priorité sur l'adaptateur structuré ✓")


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
    print("5. une CLI inconnue est refusée et nommée ✓")
    print("7. les réglages structurés refusent toute clé sensible ou inconnue ✓")


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
    validation()
    variantes()
    print("\nexécuteurs structurés : OK")


if __name__ == "__main__":
    main()
