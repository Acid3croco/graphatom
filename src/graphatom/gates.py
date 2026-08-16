"""Les portes déterministes qu'un nœud déclare — jamais un nom de nœud codé.

Une porte rejoue une preuve mécanique APRÈS le bloc, sur son résultat : elle
peut rétrograder un succès en échec, jamais l'inverse. Le moteur ne connaît
aucun nom de nœud : le nœud qui veut une porte la nomme dans sa config
(``"gate": "<nom>"``), la validation ferme l'ensemble des noms possibles, et
`blocks._attempt` applique ce qui est déclaré.

Deux portes livrées :

``deploy_verification``
    Refuse un `pass` si le code actif n'est pas la release durablement
    demandée : SHA du run de déploiement, checkout, worker, labels des
    services docker.

``elected_verdict``
    Refuse un `pass` quand le verdict du juge élu garde des critères ratés —
    le nœud de validation ne peut pas blanchir ce que le juge a compté.
"""

import os
import re
import subprocess
from pathlib import Path

import psycopg

GIT_TIMEOUT_S = 20
DEPLOY_PROBE_TIMEOUT_S = 5
# les services dont le déploiement pose l'étiquette com.graphatom.sha
DEPLOYED_SERVICES = ("github-sync", "web", "front")


def apply(name: str, ctx, workspace: Path, result: dict) -> dict | None:
    """Joue la porte nommée ; rend le résultat rétrogradé, ou None si rien."""
    return GATES[name](ctx, workspace, result)


def _report(workspace: Path, node: str, line: str) -> None:
    """Une ligne durable dans le rapport du nœud — le résultat porte déjà tout."""
    try:
        with (workspace / f"{node}.md").open("a") as report:
            report.write(line + "\n")
    except OSError:
        pass


def _deploy_verification(ctx, workspace: Path, result: dict) -> dict | None:
    if result.get("outcome") != "pass":
        return None
    repo = Path(os.environ.get(
        "GRAPHATOM_REPO_DIR", Path(__file__).resolve().parents[2],
    ))
    error = verify_deploy_error(ctx.conn, ctx.item["id"], repo)
    if not error:
        return None
    _report(workspace, ctx.run["node"], f"porte du noyau - {error}")
    return {"outcome": "fail", "summary": error}


def _elected_verdict(ctx, workspace: Path, result: dict) -> dict | None:
    if result.get("outcome") != "pass":
        return None
    failures = elected_failures(workspace / "verdict.md")
    if not failures:
        return None
    malformed = failures == [0]
    listed = ("format du verdict" if malformed else
              ", ".join(str(number) for number in failures))
    _report(workspace, ctx.run["node"],
            f"\n- [ ] {listed} : preuve du juge élu insuffisante.")
    return {
        "outcome": "fail",
        "summary": (("la section du finaliste élu ne donne aucun "
                     "statut numéroté Tenu/Raté") if malformed else
                    ("le finaliste élu garde des critères ratés par le "
                     f"juge : {listed}; un nouveau cycle doit les prouver")),
    }


GATES = {
    "deploy_verification": _deploy_verification,
    "elected_verdict": _elected_verdict,
}


def verify_deploy_error(conn: psycopg.Connection, item_id: int,
                        repo: Path) -> str | None:
    """Refuse un faux succès si le code actif n'est pas la release demandée.

    Le shell contrôle les services et les URLs. Cette porte du noyau contrôle
    les deux identités qu'un shell ne doit jamais deviner : la demande durable
    du déploiement, puis le SHA réellement chargé par le worker hôte.
    """
    deployed = conn.execute(
        "SELECT result->>'deploy_sha' AS sha FROM node_run "
        "WHERE item_id = %s AND status = 'applied' "
        "AND outcome = 'done' AND result->>'deploy_sha' IS NOT NULL "
        "ORDER BY finished_at DESC, id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    expected = deployed["sha"] if deployed else ""
    worker = conn.execute(
        "SELECT worker_sha FROM heartbeat WHERE who = 'rail'"
    ).fetchone()
    worker_sha = worker["worker_sha"] if worker else ""
    checkout_sha = _command_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=GIT_TIMEOUT_S,
    )
    service_shas = deployed_service_shas(repo)

    errors = []
    if not re.fullmatch(r"[0-9a-f]{40}", expected or ""):
        errors.append("SHA de déploiement durable absent ou invalide")
    if checkout_sha != expected:
        errors.append(f"checkout {checkout_sha or 'inconnu'} différent de {expected or 'inconnu'}")
    if worker_sha != expected:
        errors.append(f"worker {worker_sha or 'inconnu'} différent de {expected or 'inconnu'}")
    wrong = [name for name in DEPLOYED_SERVICES if service_shas[name] != expected]
    if wrong:
        errors.append("services discordants : " + ", ".join(wrong))
    return "; ".join(errors) if errors else None


def _command_output(args: list[str], *, cwd: Path | None = None,
                    env: dict | None = None, timeout: float) -> str:
    """Rend stdout, ou une valeur vide pour toute commande locale en échec."""
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True,
            check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def deployed_service_shas(repo: Path) -> dict[str, str]:
    """Lit sans repli les labels des services réellement lancés."""
    gh = os.environ.get("GRAPHATOM_GH", "gh")
    token = _command_output(
        [gh, "auth", "token"], timeout=DEPLOY_PROBE_TIMEOUT_S,
    )
    env = os.environ | {"GITHUB_TOKEN": token}
    docker = os.environ.get("GRAPHATOM_DOCKER", "docker")
    seen = {}
    for service in DEPLOYED_SERVICES:
        containers = _command_output(
            [docker, "compose", "ps", "-q", service], cwd=repo, env=env,
            timeout=DEPLOY_PROBE_TIMEOUT_S,
        ).splitlines()
        seen[service] = (_command_output(
            [docker, "inspect", "--format",
             '{{ index .Config.Labels "com.graphatom.sha" }}', containers[0]],
            timeout=DEPLOY_PROBE_TIMEOUT_S,
        ) if containers else "")
    return seen


def elected_failures(path: Path) -> list[int]:
    """Rend les critères explicitement ratés dans la section du finaliste élu."""
    try:
        verdict = path.read_text()
    except OSError:
        return []
    choices = re.findall(r"Élu\s*:\s*finaliste\s+([A-Z])", verdict)
    if not choices:
        return []
    letter = choices[-1]
    section = re.search(
        rf"(?ms)^#{{1,6}}\s+Finaliste\s+{re.escape(letter)}\s*$\n"
        rf"(.*?)(?=^#{{1,6}}\s+(?:Finaliste|Comparaison|Verdict)\b|\Z)",
        verdict,
    )
    if not section:
        return [0]
    content = section.group(1)
    entries = re.findall(
        r"(?im)^\s*(?:[-*+]\s+)?(\d+)[.)]\s+(.+)$", content
    )
    if not entries:
        return [0]
    failed = {int(number) for number, status in entries
              if re.search(r"\brat(?:é|ée|e|ee)\b", status, re.IGNORECASE)}
    for line in content.splitlines():
        if not re.search(r"\brat(?:é|ée|e|ee)\b", line, re.IGNORECASE):
            continue
        number = re.search(r"(?i)(?:critère\s*)?(\d+)", line)
        if number is None:
            return [0]
        failed.add(int(number.group(1)))
    return sorted(failed)
