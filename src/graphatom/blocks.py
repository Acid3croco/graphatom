"""L'interface de bloc et les six blocs : stub par défaut, agent sur demande.

Un bloc reçoit un contexte et retourne {"outcome": ...} plus ce qu'il veut.
Il ne touche jamais la base : le noyau réserve avant, applique après.
Les stubs simulent le travail via la config du nœud ; un nœud qui déclare
`config.execution` joue l'agent, voir `agent`. Un JUDGE qui déclare
`finalists_from` départage des finalistes, voir `arbitrage`.
"""

import json

from . import effects, executors, workspace
from .agent import run_agent
from .arbitrage import arbitrate
from .bundle import judge_source
from .context import Context
from .workspace import OUTBOX_NAME, attempt_name


def fetch(ctx: Context) -> dict:
    ctx.simulate_work()
    evidence = ctx.workspace / f"evidence-{attempt_name(ctx.run)}.json"
    evidence.write_text(json.dumps({"materialized": ctx.config.get("materialize", [])}))
    return {"outcome": "ok", "evidence": evidence.name}


def judge(ctx: Context) -> dict:
    if judge_source(ctx.node):  # un JUDGE qui départage des finalistes
        return arbitrate(ctx)
    if executors.configured(ctx.node):
        return run_agent(ctx)
    ctx.simulate_work()
    # le stub répond ce que la config scripte ; un vrai JUDGE appelle un agent
    return {"outcome": ctx.config["stub_outcome"]}


def act(ctx: Context) -> dict:
    if executors.configured(ctx.node):
        return run_agent(ctx)
    ctx.simulate_work()
    checkpoint = ctx.workspace / f"checkpoint-{attempt_name(ctx.run)}.txt"
    checkpoint.write_text(
        f"travail de la tentative {ctx.run['attempt']} du passage {ctx.run['cycle']}\n")
    return {"outcome": "ok", "checkpoint": checkpoint.name}


def check(ctx: Context) -> dict:
    if executors.configured(ctx.node):
        return run_agent(ctx)
    ctx.simulate_work()
    return {"outcome": ctx.config.get("stub_outcome", "pass")}


def effect(ctx: Context) -> dict:
    """La passerelle en miniature : intention commise avant l'action,
    clé logique au périmètre du sujet, relecture avant ré-exécution."""
    conn, item = ctx.conn, ctx.item
    subject = conn.execute(
        "SELECT graph, subject_key FROM subject WHERE id = %s", (item["subject_id"],)
    ).fetchone()
    logical_key = f"{subject['graph']}:{subject['subject_key']}:{ctx.run['node']}"
    target_uri = ctx.config.get("target", "stub://outbox")

    # l'intention existe avant tout accès au monde
    row = effects.intend(conn, item["id"], ctx.run["id"], logical_key, target_uri,
                         {"intent": ctx.config.get("intent", "noop")})
    if row["observation"] == "applied":  # déjà fait par une tentative passée
        return {"outcome": "applied", "op_id": row["op_id"]}

    # « interroger la cible par la clé logique » — la cible stub est l'outbox,
    # une ligne par effet, la clé entière avant la tabulation : une clé qui
    # en préfixe une autre n'est pas déjà appliquée
    outbox = workspace.DATA_DIR / OUTBOX_NAME
    outbox.parent.mkdir(parents=True, exist_ok=True)
    outbox.touch()
    applied = {line.partition("\t")[0] for line in outbox.read_text().splitlines()}
    if logical_key not in applied:
        ctx.simulate_work()
        with outbox.open("a") as f:
            f.write(f"{logical_key}\t{row['intent']}\n")

    with conn.transaction():
        effects.mark_applied(conn, op_id=row["op_id"])
    return {"outcome": "applied", "op_id": row["op_id"]}


BLOCKS = {"FETCH": fetch, "JUDGE": judge, "ACT": act, "CHECK": check, "EFFECT": effect}
# WAIT n'a pas de bloc : le noyau arme la question, le waiter route la réponse.
