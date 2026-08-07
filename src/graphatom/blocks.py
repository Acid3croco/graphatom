"""L'interface de bloc, les six blocs stub, et l'exécuteur d'agent.

Un bloc reçoit un contexte et retourne {"outcome": ...} plus ce qu'il veut.
Il ne touche jamais la base : le noyau réserve avant, applique après.
Les stubs simulent le travail via la config du nœud.

Un nœud ACT / CHECK / JUDGE peut déclarer `config.agent` : un vrai agent
CLI fait alors le travail. Le contrat est minuscule et agnostique — le
bloc écrit `prompt.md` dans le workspace, lance la commande configurée
(claude, codex, pi, n'importe quoi), et lit `outcome.json`. Pas de
fichier d'issue valide = crashed, et le noyau route comme d'habitude.

L'agent tourne dans son propre groupe de processus : au timeout, c'est
tout le groupe qui est révoqué — un descendant ne survit pas au bail.
Le pgid est aussi persisté dans le workspace : si c'est le worker qui
meurt, le faucheur du suivant y trouve de quoi tuer l'orphelin.

Une tentative crashée rend son autopsie : code de sortie, queue du log et
flag timeout. Le post-mortem se lit dans le résultat du run, pas en
fouillant le workspace à la main.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import psycopg

DATA_DIR = Path("data")  # les tests le font pointer sur un répertoire temporaire
OUTBOX_NAME = "effects_outbox.log"  # sous DATA_DIR, résolu au moment de l'effet
GRACE_S = 5.0  # entre le SIGTERM et le SIGKILL du groupe de l'agent
PGID_FILE = "agent.pgid"  # la trace qui survit au worker, écrasée à chaque tentative
TAIL_LINES = 20  # queue du log rendue dans l'autopsie d'une tentative crashée
TAIL_CHARS = 2000


def item_workspace(item_id: int) -> Path:
    """Le répertoire de travail d'un item — connu du bloc comme du faucheur."""
    return DATA_DIR / f"item-{item_id}"


class Context:
    def __init__(self, conn: psycopg.Connection, run: dict, item: dict,
                 node: dict, bundle: dict):
        self.conn = conn
        self.run = run
        self.item = item
        self.node = node
        self.bundle = bundle
        self.config = node.get("config") or {}
        self.workspace = item_workspace(item["id"])
        self.workspace.mkdir(parents=True, exist_ok=True)

    def simulate_work(self) -> None:
        time.sleep(float(self.config.get("duration_s", 0)))


def _agent(ctx: Context) -> dict:
    """Exécute l'agent CLI configuré. Contrat : prompt.md → outcome.json."""
    cfg = ctx.config["agent"]
    outcomes = sorted(ctx.node.get("edges") or {})
    subject = ctx.conn.execute(
        "SELECT subject_key FROM subject WHERE id = %s", (ctx.item["subject_id"],)
    ).fetchone()["subject_key"]

    workspace = ctx.workspace.resolve()
    outcome_path = workspace / "outcome.json"
    outcome_path.unlink(missing_ok=True)
    prompt = os.path.expandvars(
        cfg["prompt"].replace("{subject_key}", subject)
    ) + (
        "\n\n--- Contrat GraphAtom ---\n"
        f"Tu es le bloc « {ctx.run['node']} » d'un rail d'exécution. "
        f"Ton workspace : {workspace}\n"
        f"Avant de terminer, écris impérativement {outcome_path} : "
        f'{{"outcome": <une valeur parmi {outcomes}>, "summary": "<une phrase>"}}\n'
        "Sans ce fichier, ta tentative est classée crashed et sera retentée."
    )
    (workspace / "prompt.md").write_text(prompt)

    env = os.environ | {"GRAPHATOM_WORKSPACE": str(workspace)}
    if "GRAPHATOM_AGENT_DSN" in env:
        # l'agent ne voit jamais la base du rail : sa DSN est une base jetable
        env["GRAPHATOM_DSN"] = env["GRAPHATOM_AGENT_DSN"]
    log = workspace / f"agent-{ctx.run['attempt']}.log"
    pgid_file = workspace / PGID_FILE
    with log.open("w") as out:
        # session dédiée : l'agent est chef de son groupe, ses descendants aussi
        proc = subprocess.Popen(
            cfg["cmd"], shell=True, cwd=workspace, env=env, start_new_session=True,
            stdout=out, stderr=subprocess.STDOUT,
        )
        try:
            _write_pgid(pgid_file, proc, cfg["cmd"], ctx.run["id"])
            proc.wait(timeout=float(cfg.get("timeout_s", 570)))
        except subprocess.TimeoutExpired as exc:
            _kill_group(proc)  # le bail expire : le groupe entier est révoqué
            return _autopsy(proc, log, exc, timeout=True)
        except BaseException:  # interruption du bloc : on révoque, puis on remonte
            _kill_group(proc)
            raise  # l'erreur remonte après la révocation : tentative crashed
        finally:  # le worker a tenu jusqu'au bout : la trace n'a plus d'usage
            pgid_file.unlink(missing_ok=True)

    try:
        data = json.loads(outcome_path.read_text())
        outcome, summary = data["outcome"], data.get("summary", "")
    except (OSError, ValueError, KeyError, TypeError) as exc:  # pas d'issue valide
        return _autopsy(proc, log, exc, timeout=False)
    return {"outcome": outcome, "summary": summary}


def _autopsy(proc: subprocess.Popen, log: Path, exc: BaseException,
             timeout: bool) -> dict:
    """Le post-mortem d'une tentative crashée, dans le résultat du run.

    Le code de sortie est celui du processus agent — négatif, c'est le
    signal qui l'a tué (-9 pour le SIGKILL de la révocation).
    """
    return {"outcome": "crashed", "error": f"{type(exc).__name__}: {exc}",
            "timeout": timeout, "exit_code": proc.returncode, "log_tail": _tail(log)}


def _tail(log: Path) -> str:
    """Les dernières lignes du journal de l'agent, bornées en taille."""
    try:
        lines = log.read_text(errors="replace").splitlines()[-TAIL_LINES:]
    except OSError as exc:  # un log illisible est lui-même une information
        return f"[journal illisible : {exc}]"
    return "\n".join(lines)[-TAIL_CHARS:]


def _kill_group(proc: subprocess.Popen) -> None:
    """Révoque le groupe entier : SIGTERM, une grâce, puis SIGKILL.

    L'expiration d'un bail révoque l'autorité — donc aussi celle des
    descendants. Tuer le seul shell laisserait des orphelins (chromium,
    serveurs de test, sous-agents) travailler sans autorité.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:  # déjà parti, rien à révoquer
        return
    _signal_group(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=GRACE_S)  # le chef de groupe part le premier
    except subprocess.TimeoutExpired:
        pass
    _signal_group(pgid, signal.SIGKILL)
    proc.wait()  # récolte le chef de groupe, pas de zombie


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:  # plus personne dans le groupe
        pass


def _write_pgid(path: Path, proc: subprocess.Popen, cmd: str, run_id: int) -> None:
    """Persiste de quoi révoquer l'agent sans handle Popen.

    `start_new_session` fait de l'agent le chef de son groupe : son pid est
    donc le pgid de tout ce qu'il lancera. L'identité dit lequel, le run dit
    à qui la trace appartient, la commande est pour l'humain qui lit le
    fichier. Écriture atomique : le faucheur ne lit jamais une demi-trace.
    """
    trace = {"run": run_id, "pgid": proc.pid, "cmd": cmd,
             "identity": _identity(proc.pid)}
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(trace))
    tmp.replace(path)


def _identity(pid: int) -> dict | None:
    """Ce qui distingue un processus d'un homonyme : son boot et sa naissance.

    Un pid se recycle ; ni la date de naissance (en tops d'horloge depuis le
    boot) ni le boot lui-même ne suivent. La ligne de commande, elle, ne dit
    rien de sûr : elle est encore vide au retour de Popen, et le shell la
    réécrit s'il s'exec-optimise. Pas de /proc, pas d'identité — donc None.
    """
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, IndexError):
        return None
    return {"boot": boot, "starttime": int(fields[19])}  # champ 22 de proc(5)


def revoke_orphan(item_id: int, run_id: int) -> int | None:
    """Tue le groupe de l'agent d'un run fauché. Rend le pgid tué.

    Le faucheur n'a pas de handle Popen — il n'a que le `agent.pgid` laissé
    dans le workspace de l'item. Révoquer l'autorité en base ne suffit pas :
    l'orphelin continue d'écrire dans le checkout et le workspace.

    Trois garde-fous, parce que tuer un innocent est pire qu'un orphelin :
    la trace doit être celle du run fauché (une tentative suivante l'écrase),
    le chef du groupe doit toujours être celui qu'on a lancé — un pid se
    recycle, pas une identité — et le faucheur ne se fauche jamais lui-même.
    """
    path = item_workspace(item_id) / PGID_FILE
    try:
        trace = json.loads(path.read_text())
        run, pgid, who = trace["run"], trace["pgid"], trace["identity"]
    except (OSError, ValueError, KeyError):  # pas d'agent en vol, ou trace illisible
        return None
    if run != run_id:  # trace d'une tentative plus fraîche : pas la nôtre
        return None
    path.unlink(missing_ok=True)  # une trace ne sert qu'à une révocation

    if who is None or _identity(pgid) != who:
        return None  # groupe éteint, ou pid déjà repris par un innocent
    if pgid == os.getpgid(0):
        return None  # trace aberrante : un faucheur qui se tue ne fauche plus rien

    _signal_group(pgid, signal.SIGTERM)
    deadline = time.time() + GRACE_S
    while time.time() < deadline and _identity(pgid) == who:
        time.sleep(0.1)
    # le pgid n'est pas recyclable tant que le groupe a un membre : sans chef,
    # le SIGKILL reste pour les descendants ou ne trouve plus personne
    _signal_group(pgid, signal.SIGKILL)
    print(f"orphelin révoqué : groupe {pgid} ({trace.get('cmd', '?')})", flush=True)
    return pgid


def fetch(ctx: Context) -> dict:
    ctx.simulate_work()
    evidence = ctx.workspace / f"evidence-{ctx.run['node']}-{ctx.run['attempt']}.json"
    evidence.write_text(json.dumps({"materialized": ctx.config.get("materialize", [])}))
    return {"outcome": "ok", "evidence": evidence.name}


def judge(ctx: Context) -> dict:
    if "agent" in ctx.config:
        return _agent(ctx)
    ctx.simulate_work()
    # le stub répond ce que la config scripte ; un vrai JUDGE appelle un agent
    return {"outcome": ctx.config["stub_outcome"]}


def act(ctx: Context) -> dict:
    if "agent" in ctx.config:
        return _agent(ctx)
    ctx.simulate_work()
    checkpoint = ctx.workspace / f"checkpoint-{ctx.run['attempt']}.txt"
    checkpoint.write_text(f"travail de la tentative {ctx.run['attempt']}\n")
    return {"outcome": "ok", "checkpoint": checkpoint.name}


def check(ctx: Context) -> dict:
    if "agent" in ctx.config:
        return _agent(ctx)
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
    outbox = DATA_DIR / OUTBOX_NAME
    outbox.parent.mkdir(parents=True, exist_ok=True)
    outbox.touch()
    if logical_key not in outbox.read_text():
        ctx.simulate_work()
        with outbox.open("a") as f:
            f.write(f"{logical_key}\t{row['intent']}\n")

    with conn.transaction():
        conn.execute(
            "UPDATE effect SET observation = 'applied' WHERE op_id = %s", (row["op_id"],)
        )
    return {"outcome": "applied", "op_id": row["op_id"]}


BLOCKS = {"FETCH": fetch, "JUDGE": judge, "ACT": act, "CHECK": check, "EFFECT": effect}
# WAIT n'a pas de bloc : le noyau arme la question, le waiter route la réponse.
