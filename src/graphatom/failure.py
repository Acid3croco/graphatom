"""La trace stable du dernier échec d'un item, et l'autopsie d'une tentative.

Agent, adaptateur de fournisseur et script shell ont tous le même contrat
de bloc. Le rail connaît donc le nœud et l'issue du run, le journal de sa
tentative et le dernier compte rendu Markdown écrit pendant cette
tentative. Le post-mortem se lit dans le résultat du run, pas en fouillant
le workspace à la main.
"""

import json
import subprocess
from pathlib import Path

from .bundle import KERNEL_OUTCOMES
from .workspace import (PROMPT_NAME, attempt_log, attempt_name, failure_path,
                        run_workspace)

TAIL_LINES = 20  # queue du log rendue dans l'autopsie d'une tentative crashée
TAIL_CHARS = 2000
REPORT_CHARS = 4000  # queue du compte rendu citée dans la trace d'échec
# Le noyau a ses propres issues d'échec. Le domaine garde les noms négatifs
# employés par les graphes. Une issue n'est un échec que si elle est dans l'un
# de ces deux ensembles : une réussite, même non terminale, ne laisse donc pas
# de fausse trace.
DOMAIN_FAILURE_OUTCOMES = {
    "abandon", "abandonner", "conflict", "expired", "fail", "failed",
    "none", "raise", "reformuler", "retry", "revise", "stuck",
    "uncertain", "unclear",
}


def is_failure_outcome(outcome: str) -> bool:
    """Une issue d'échec est une issue du noyau ou un nom négatif du domaine."""
    return outcome in KERNEL_OUTCOMES or outcome in DOMAIN_FAILURE_OUTCOMES


def tail(log: Path) -> str:
    """Les dernières lignes du journal de l'agent, bornées en taille."""
    try:
        lines = log.read_text(errors="replace").splitlines()[-TAIL_LINES:]
    except OSError as exc:  # un log illisible est lui-même une information
        return f"[journal illisible : {exc}]"
    return "\n".join(lines)[-TAIL_CHARS:]


def autopsy(proc: subprocess.Popen, log: Path, exc: BaseException,
            timeout: bool, progress: bool = True) -> dict:
    """Le post-mortem d'une tentative ratée, dans le résultat du run.

    Le code de sortie est celui du processus agent — négatif, c'est le
    signal qui l'a tué (-9 pour le SIGKILL de la révocation).

    L'issue dit lequel des trois ratés c'était :

    - `crashed` : la tentative est allée au bout sans rendre d'issue lisible ;
    - `timed_out` : un couperet est tombé sur un agent qui avait produit —
      la tâche déborde vraiment de son budget ;
    - `stalled` : un couperet est tombé sur un agent qui n'avait rien
      produit — il était pendu, c'est de l'infra et pas de la tâche.

    Le post-mortem, lui, est le même des trois côtés : `error`, `exit_code`
    et `log_tail` y sont toujours, et le flag `timeout` redit en clair que
    c'est un couperet qui a tranché.
    """
    if not timeout:
        outcome = "crashed"
    else:
        outcome = "timed_out" if progress else "stalled"
    return {"outcome": outcome,
            "error": f"{type(exc).__name__}: {exc}",
            "timeout": timeout, "exit_code": proc.returncode, "log_tail": tail(log)}


def lease_autopsy(item_id: int, run: dict, alive: bool, orphaned: bool) -> dict:
    """Le post-mortem d'une tentative fauchée par son bail.

    Même forme que `autopsy`, sans code de sortie : le faucheur n'a pas de
    handle Popen. Ce qu'il sait, lui, c'est si l'agent travaillait encore à
    l'expiration du bail — un budget dépassé, donc `timed_out` — ou s'il
    était déjà mort — une panne, donc `crashed`. Le journal de la tentative
    se retrouve par le nom de ses traces.

    `orphaned` : le worker qui a réservé ce run n'est plus là — il a
    redémarré, et le run est resté `running` derrière lui. L'issue est la
    même, la cause probable non, et c'est elle qu'on lit : un redémarrage
    emporte tous les fils en vol d'un coup, et leurs agents avec. Le dire
    évite de chercher des agents instables quand c'est le worker qui tombe.
    """
    agent = "encore vivant" if alive else "déjà mort"
    return {"outcome": "timed_out" if alive else "crashed",
            "error": (f"run emporté par un redémarrage du worker — agent orphelin, "
                      f"{agent} : le worker est tombé, pas l'agent"
                      if orphaned else f"bail expiré, agent {agent}"),
            "timeout": alive, "exit_code": None,
            "log_tail": tail(attempt_log(run_workspace(item_id, run), run))}


def _attempt_marker(workspace: Path, run: dict) -> Path | None:
    """La trace qui date le début de la tentative, archivée ou encore active."""
    archived = workspace / f"prompt-{attempt_name(run)}.md"
    if archived.is_file():
        return archived
    active = workspace / PROMPT_NAME
    return active if active.is_file() else None


def _attempt_report(workspace: Path, run: dict) -> dict | None:
    """Le compte rendu Markdown écrit pendant cette tentative, s'il existe.

    Le nom du rapport n'est pas une convention du noyau : `deploy` écrit
    `deploy.md`, mais `implement` peut écrire `implementation.md` puis
    `portes.md`. Le prompt de la tentative sert donc de borne temporelle, et
    le dernier Markdown touché après lui est le rapport rendu par ce nœud.
    Les prompts archivés et la trace stable ne sont jamais des candidats.
    """
    marker = _attempt_marker(workspace, run)
    if marker is None:
        return None
    try:
        started = marker.stat().st_mtime_ns
        reports = [
            path for path in workspace.glob("*.md")
            if not path.name.startswith("prompt-")
            and path.name != PROMPT_NAME
            and path.stat().st_mtime_ns >= started
        ]
    except OSError:
        return None
    if not reports:
        return None
    report = max(reports, key=lambda path: (path.stat().st_mtime_ns, path.name))
    try:
        content = report.read_text(errors="replace")[-REPORT_CHARS:]
    except OSError as exc:
        content = f"[compte rendu illisible : {exc}]"
    return {"name": report.name, "content": content}


def write_failure_trace(item_id: int, run: dict, outcome: str) -> Path | None:
    """Remplace la trace stable quand un run rend une issue d'échec.

    Journal et rapport sont bornés par `TAIL_LINES`/`TAIL_CHARS` et
    `REPORT_CHARS`. Pour `starved`, la trace garde aussi le fournisseur et
    sa raison.

    Une réussite ne touche rien. Un item neuf ne reçoit ainsi aucune trace, et
    une réussite ultérieure conserve bien le dernier échec observé. Chaque
    nouvel échec remplace le JSON entier au même chemin, sans accumulation.
    """
    if not is_failure_outcome(outcome):
        return None
    workspace = run_workspace(item_id, run)
    path = failure_path(item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    trace = {
        "node": run["node"],
        "outcome": outcome,
        "log_tail": tail(attempt_log(workspace, run)),
        "report": _attempt_report(workspace, run),
    }
    result = run.get("result") or {}
    if isinstance(result.get("retry"), str):
        trace["retry"] = result["retry"]
    if outcome == "starved":
        for name in ("provider", "reason"):
            value = result.get(name)
            if isinstance(value, str) and value.strip():
                trace[name] = value
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
    return path
