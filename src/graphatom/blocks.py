"""L'interface de bloc, et les six blocs en version stub.

Un bloc reçoit un contexte et retourne {"outcome": ...} plus ce qu'il veut.
Il ne touche jamais la base : le noyau réserve avant, applique après.
Les stubs simulent le travail via la config du nœud — assez pour exercer
le rail et le crash-test, sans modèle ni monde extérieur.
"""

import json
import time
from pathlib import Path

import psycopg

DATA_DIR = Path("data")
OUTBOX = DATA_DIR / "effects_outbox.log"


class Context:
    def __init__(self, conn: psycopg.Connection, run: dict, item: dict,
                 node: dict, bundle: dict):
        self.conn = conn
        self.run = run
        self.item = item
        self.node = node
        self.bundle = bundle
        self.config = node.get("config") or {}
        self.workspace = DATA_DIR / f"item-{item['id']}"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def simulate_work(self) -> None:
        time.sleep(float(self.config.get("duration_s", 0)))


def fetch(ctx: Context) -> dict:
    ctx.simulate_work()
    evidence = ctx.workspace / f"evidence-{ctx.run['node']}-{ctx.run['attempt']}.json"
    evidence.write_text(json.dumps({"materialized": ctx.config.get("materialize", [])}))
    return {"outcome": "ok", "evidence": evidence.name}


def judge(ctx: Context) -> dict:
    ctx.simulate_work()
    # le stub répond ce que la config scripte ; un vrai JUDGE appellerait un modèle
    return {"outcome": ctx.config["stub_outcome"]}


def act(ctx: Context) -> dict:
    ctx.simulate_work()
    checkpoint = ctx.workspace / f"checkpoint-{ctx.run['attempt']}.txt"
    checkpoint.write_text(f"travail de la tentative {ctx.run['attempt']}\n")
    return {"outcome": "ok", "checkpoint": checkpoint.name}


def check(ctx: Context) -> dict:
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

    with conn.transaction():  # l'intention existe avant tout accès au monde
        conn.execute(
            "INSERT INTO effect (item_id, run_id, logical_key, target_uri, intent) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (target_uri, logical_key) DO NOTHING",
            (item["id"], ctx.run["id"], logical_key, target_uri,
             json.dumps({"intent": ctx.config.get("intent", "noop")})),
        )
    row = conn.execute(
        "SELECT * FROM effect WHERE target_uri = %s AND logical_key = %s",
        (target_uri, logical_key),
    ).fetchone()
    if row["observation"] == "applied":  # déjà fait par une tentative passée
        return {"outcome": "applied", "op_id": row["op_id"]}

    # « interroger la cible par la clé logique » — la cible stub est l'outbox
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    OUTBOX.touch()
    if logical_key not in OUTBOX.read_text():
        ctx.simulate_work()
        with OUTBOX.open("a") as f:
            f.write(f"{logical_key}\t{row['intent']}\n")

    with conn.transaction():
        conn.execute(
            "UPDATE effect SET observation = 'applied' WHERE op_id = %s", (row["op_id"],)
        )
    return {"outcome": "applied", "op_id": row["op_id"]}


BLOCKS = {"FETCH": fetch, "JUDGE": judge, "ACT": act, "CHECK": check, "EFFECT": effect}
# WAIT n'a pas de bloc : le noyau arme la question, le waiter route la réponse.
