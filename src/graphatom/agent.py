"""L'exécuteur d'agent : une tentative de wagon, de la commande à l'issue.

Un nœud ACT / CHECK / JUDGE déclare `config.execution` : `agent` appelle la
CLI configurée et reçoit `prompt.md`, `command` exécute un programme
déterministe sans faux prompt ni fausse passation. Les deux écrivent
`outcome.json`, que le bloc relit. Pas de fichier d'issue valide = crashed,
et le noyau route comme d'habitude.

Une extension optionnelle : si la tentative laisse un `usage.json`, le
bloc le fusionne dans le résultat du run — les types de tokens tels que
l'agent les rapporte, sans que personne ici les interprète.

Les traces d'une tentative sont auditables après coup : le journal porte
le nœud, le passage et la tentative dans son nom, et le prompt comme
l'usage y sont rangés à la fin. Rien de l'histoire d'un item n'est jamais
réécrit — seul `outcome.json` reste transitoire.

L'agent travaille dans son propre univers : sa `GRAPHATOM_DSN` est la base
jetable de son item, jamais celle du rail ni celle du voisin. Le bloc n'a
jamais à deviner où est son checkout : `GRAPHATOM_WORKTREE` le lui dit.

Un agent muet ne consomme plus son budget entier. Un chien de garde relève
trois signaux mécaniques — la taille du journal, le mtime le plus récent du
workspace, celui du worktree — et coupe dès que les trois n'ont pas bougé
pendant `silence_s`. Au couperet, c'est le progrès constaté qui fait
l'issue : un agent qui avait produit déborde de son budget, `timed_out` ;
un agent qui n'avait rien produit était pendu, `stalled`.
"""

import json
import os
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from . import db, executors, gates, prompt
from .context import Context
from .failure import autopsy, is_failure_outcome
from .policy import LEASE_SECONDS, agent_timeout_s
from .process import kill_group, revoke_residual_group, write_pgid
from .quota import WAIT_FILE
from .workspace import (EVENT_NAMES, OUTCOME_NAME, PGID_FILE, PROMPT_NAME,
                        STARVED_NAME, USAGE_NAME, attempt_command, attempt_log,
                        attempt_name)

AGENT_SILENCE_S = 180  # silence toléré par le chien de garde, quand le nœud n'en dit rien
PROBE_S = 5.0  # granularité du relevé de progrès, resserrée par un silence court
POLL_S = 0.2  # granularité de l'attente du process : ce qui borne le budget total
PRUNED_DIRS = {".git", "node_modules", ".next", "__pycache__", ".venv"}
SHA = re.compile(r"[0-9a-f]{40}")


def run_agent(ctx: Context) -> dict:
    """Exécute l'agent CLI configuré, et range ses traces sous son nom.

    L'usage de tokens rejoint le résultat quelle que soit l'issue : une
    tentative crashée a consommé, elle aussi, et c'est là qu'on veut le
    voir. L'archivage, lui, a lieu même si le bloc est interrompu.
    """
    workspace = ctx.workspace.resolve()
    try:
        return attempt(ctx, workspace) | _usage(workspace)
    finally:
        archive(workspace, attempt_name(ctx.run))


def _usage(workspace: Path) -> dict:
    """L'usage de tokens de la tentative, si l'agent en a laissé un.

    Le format est celui que l'agent rapporte : personne ici ne l'interprète.
    Pas de fichier, fichier illisible ou vide : rien du tout.
    """
    try:
        usage = json.loads((workspace / USAGE_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return {"usage": usage} if isinstance(usage, dict) and usage else {}


def archive(workspace: Path, name: str) -> None:
    """Range les traces de nom fixe sous le nom de la tentative.

    Le prompt, l'usage et les événements du fournisseur sont l'interface
    avec le `cmd` : des noms fixes, que le nœud suivant écraserait. La
    tentative finie, ils deviennent des pièces d'archive à côté du journal
    déjà nommé ainsi.
    """
    for fixed in (PROMPT_NAME, USAGE_NAME, *EVENT_NAMES):
        path = workspace / fixed
        if path.is_file():
            path.replace(path.with_stem(f"{path.stem}-{name}"))


# ------------------------------------------------------------------ progrès


def _latest_mtime(root: Path | None) -> float:
    """Le mtime le plus récent d'une arborescence, machinerie exclue.

    `.git`, `node_modules` et les caches de build bougent sans qu'un agent y
    soit pour rien — un index git rafraîchi par le voisin n'est pas du
    travail.
    """
    if root is None:
        return 0.0
    latest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS]
        for name in filenames:
            try:
                latest = max(latest, os.stat(Path(dirpath) / name).st_mtime)
            except OSError:  # fichier disparu pendant la marche : il a bougé, tant pis
                pass
    return latest


def mark(log: Path, workspace: Path, worktree: Path | None) -> tuple:
    """Les trois signaux du progrès d'un agent, en un relevé."""
    try:
        size = log.stat().st_size
    except OSError:  # journal pas encore là : zéro octet, comme un journal vide
        size = 0
    return size, _latest_mtime(workspace), _latest_mtime(worktree)


def progress(before: tuple, fresh: tuple) -> bool:
    """L'agent a-t-il produit quoi que ce soit avant le couperet ?

    Deux signaux, et un seul suffit : des octets dans le journal de la
    tentative, ou un fichier touché depuis le relevé de départ. Le journal
    se juge sur son contenu et non sur sa croissance — un agent qui écrit sa
    première ligne avant même le relevé de départ a produit.
    """
    return fresh[0] > 0 or fresh[1:] != before[1:]


def _wait(proc: subprocess.Popen, watched: tuple, before: tuple,
          budget_s: float, silence_s: float) -> None:
    """Attend l'agent jusqu'au premier des deux couperets.

    Le budget total borne la tentative ; le silence toléré borne
    l'inactivité. Rend la main quand le process meurt de sa belle mort ;
    lève `TimeoutExpired` quand un couperet tombe, comme `proc.wait`.
    """
    start = time.monotonic()
    deadline, probe_s = start + budget_s, min(PROBE_S, silence_s / 4)
    quiet_since, next_probe = start, start + probe_s
    last = start
    while True:
        try:
            proc.wait(timeout=POLL_S)
            return  # mort de sa belle mort : l'issue se lit dans outcome.json
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        # Une commande lourde qui attend le quota n'a pas encore commencé.
        # Cette durée ne mange donc ni le couperet total ni celui du silence.
        if (watched[1] / WAIT_FILE).exists():
            deadline += now - last
            quiet_since = now
        last = now
        if now >= next_probe:
            next_probe = now + probe_s
            fresh = mark(*watched)
            if fresh != before:
                before, quiet_since = fresh, now
            elif now - quiet_since >= silence_s:
                raise subprocess.TimeoutExpired(
                    "chien de garde : ni un octet ni un fichier touché", silence_s)
        if now >= deadline:
            raise subprocess.TimeoutExpired(proc.args, budget_s)


# ---------------------------------------------------------------- tentative


def _subject(ctx: Context) -> str:
    return ctx.conn.execute(
        "SELECT subject_key FROM subject WHERE id = %s", (ctx.item["subject_id"],)
    ).fetchone()["subject_key"]


def _environment(ctx: Context, workspace: Path, subject: str,
                 resolved: executors.Executor) -> dict[str, str]:
    env = os.environ | {"GRAPHATOM_WORKSPACE": str(workspace),
                        "GRAPHATOM_SUBJECT_KEY": subject,
                        "GRAPHATOM_ITEM_ID": str(ctx.item["id"]),
                        "GRAPHATOM_RUN_ID": str(ctx.run["id"]),
                        "GRAPHATOM_LEASE_S": str(ctx.config.get("lease_s", LEASE_SECONDS)),
                        "GRAPHATOM_HEARTBEAT_DSN": db.DSN,
                        # Copiée avant que la base jetable de l'item remplace
                        # GRAPHATOM_DSN : toutes les places vivent ici.
                        "GRAPHATOM_QUOTA_DSN": db.DSN}
    if ctx.worktree is not None:
        # le bloc ne devine pas son checkout : celui d'un candidat n'est pas
        # celui de son item, et aucune convention de nom ne l'en déduit
        env["GRAPHATOM_WORKTREE"] = str(ctx.worktree)
    dsn = db.agent_dsn(ctx.item["id"])
    if dsn:
        # l'agent ne voit jamais la base du rail : la sienne est jetable
        env["GRAPHATOM_DSN"] = dsn
    env.update(executors.environment(resolved))
    return env


def _record_command(workspace: Path, run: dict, resolved: executors.Executor,
                    cmd: str) -> None:
    uses_executor = resolved.kind in {"model", "composed"}
    attempt_command(workspace, run).write_text(json.dumps({
        "kind": resolved.kind,
        "executor": ({"cli": resolved.cli, "model": resolved.model,
                      "effort": resolved.effort} if uses_executor else None),
        "command": cmd,
    }, ensure_ascii=False, indent=2) + "\n")


def _purge(workspace: Path, node: str, text: str | None) -> Path:
    """Rien de la tentative précédente ; le prompt neuf posé. Rend la passation."""
    handoff_path = workspace / f"passation-{node}.md"
    for transient in (workspace / OUTCOME_NAME, workspace / STARVED_NAME,
                      workspace / USAGE_NAME, handoff_path):
        transient.unlink(missing_ok=True)
    prompt_path = workspace / PROMPT_NAME
    if text is None:
        prompt_path.unlink(missing_ok=True)
    else:
        prompt_path.write_text(text)
    return handoff_path


def _run(ctx: Context, workspace: Path, cmd: str, env: dict, log: Path,
         resolved: executors.Executor) -> tuple[subprocess.Popen, dict | None]:
    """Lance l'agent dans son groupe et l'attend. Rend le process et l'échec."""
    pgid_file = workspace / PGID_FILE
    watched = (log, workspace, ctx.worktree)
    with log.open("w") as out:
        # session dédiée : l'agent est chef de son groupe, ses descendants aussi
        proc = subprocess.Popen(
            cmd, shell=True, cwd=workspace, env=env, start_new_session=True,
            stdout=out, stderr=subprocess.STDOUT,
        )
        try:
            write_pgid(pgid_file, proc, cmd, ctx.run["id"])
            before = mark(*watched)  # nos traces sont écrites : la suite est de l'agent
            _wait(proc, watched, before, float(resolved.timeout_s),
                  float(resolved.silence_s or AGENT_SILENCE_S))
            # L'adaptateur peut avoir fini alors qu'un de ses descendants
            # travaille encore (GNU timeout en mode --foreground). Révoque
            # ces membres avant d'effacer la seule trace qui les retrouve.
            revoke_residual_group(proc.pid)
        except subprocess.TimeoutExpired as exc:
            # le relevé se prend avant la révocation : ce que le SIGTERM
            # arrache à l'agent n'est pas du travail qu'il a fait
            moved = progress(before, mark(*watched))
            kill_group(proc)  # le bail expire : le groupe entier est révoqué
            return proc, autopsy(proc, log, exc, timeout=True, progress=moved)
        except BaseException:  # interruption du bloc : on révoque, puis on remonte
            kill_group(proc)
            raise  # l'erreur remonte après la révocation : tentative crashed
        finally:  # le worker a tenu jusqu'au bout : la trace n'a plus d'usage
            pgid_file.unlink(missing_ok=True)
    return proc, None


def _verdict(ctx: Context, workspace: Path, proc: subprocess.Popen, log: Path,
             resolved: executors.Executor, handoff_path: Path) -> dict:
    """Relit le contrat, joue la porte déclarée, exige la passation."""
    try:
        result = read_outcome(workspace / OUTCOME_NAME)
    except (OSError, ValueError, KeyError, TypeError) as exc:  # pas d'issue valide
        return _starved(workspace / STARVED_NAME) or autopsy(proc, log, exc, timeout=False)
    # un deploy_sha est une capacité, pas un champ libre : seul un nœud qui
    # déclare `activation` peut demander un redémarrage du worker
    if "deploy_sha" in result and not ctx.config.get("activation"):
        return autopsy(proc, log, ValueError(
            "deploy_sha rendu par un nœud sans activation déclarée"), timeout=False)
    # la porte déclarée du nœud rejoue sa preuve mécanique sur le résultat :
    # elle peut rétrograder un succès, jamais l'inverse — voir `gates`
    gate = ctx.config.get("gate")
    if gate and (rewritten := gates.apply(gate, ctx, workspace, result)):
        result = rewritten
    if resolved.handoff and not is_failure_outcome(result["outcome"]):
        if erreur := prompt.passation_invalide(handoff_path):
            return autopsy(proc, log, ValueError(erreur), timeout=False)
    return result


def attempt(ctx: Context, workspace: Path) -> dict:
    """Une tentative de wagon. Contrat : exécution → outcome.json."""
    resolved = executors.resolve(ctx.bundle, ctx.node)
    subject = _subject(ctx)
    # Le prompt se construit avant la purge : une relance du même nœud doit
    # recevoir la passation de sa tentative précédente. Le process, lui,
    # démarre après la purge et ne peut donc réussir avec ce vieux fichier.
    text = (prompt.build(ctx, workspace, subject, resolved)
            if resolved.prompt is not None else None)
    handoff_path = _purge(workspace, ctx.run["node"], text)
    if resolved.timeout_s is None:
        resolved = replace(resolved, timeout_s=agent_timeout_s(ctx.config))
    env = _environment(ctx, workspace, subject, resolved)
    cmd = prompt.fill(ctx, executors.command(resolved), subject)
    _record_command(workspace, ctx.run, resolved, cmd)
    log = attempt_log(workspace, ctx.run)
    proc, failed = _run(ctx, workspace, cmd, env, log, resolved)
    if failed is not None:
        return failed
    return _verdict(ctx, workspace, proc, log, resolved, handoff_path)


def read_outcome(path: Path) -> dict:
    """Lit le contrat d'un bloc et conserve ses champs noyau explicites."""
    data = json.loads(path.read_text())
    outcome, summary = data["outcome"], data.get("summary", "")
    if not isinstance(outcome, str) or not isinstance(summary, str):
        raise TypeError("outcome et summary doivent être des chaînes")
    result = {"outcome": outcome, "summary": summary}
    if "deploy_sha" in data:
        deploy_sha = data["deploy_sha"]
        if not isinstance(deploy_sha, str) or not SHA.fullmatch(deploy_sha):
            raise ValueError("deploy_sha doit être un SHA git canonique complet")
        result["deploy_sha"] = deploy_sha
    return result


def _starved(path: Path) -> dict | None:
    """La panne de fournisseur déclarée par l'adaptateur, si elle est valide.

    Le bloc ne reconnaît aucun message de CLI. Il ne fait que lire le contrat
    de fichier : deux chaînes non vides, le fournisseur et sa raison exacte.
    Un fichier absent ou mal formé ne change rien au verdict `crashed`.
    """
    try:
        data = json.loads(path.read_text())
        provider, reason = data["provider"], data["reason"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(value, str) and value.strip()
               for value in (provider, reason)):
        return None
    return {"outcome": "starved", "provider": provider, "reason": reason}
