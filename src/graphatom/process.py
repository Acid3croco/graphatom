"""Le groupe de processus d'un agent : sa trace, son identité, sa révocation.

L'agent tourne dans son propre groupe de processus : au timeout, c'est
tout le groupe qui est révoqué — un descendant ne survit pas au bail.
Le pgid est aussi persisté dans le workspace : si c'est le worker qui
meurt, le faucheur du suivant y trouve de quoi tuer l'orphelin.

Un pid se recycle ; ni la date de naissance (en tops d'horloge depuis le
boot) ni le boot lui-même ne suivent. C'est cette identité-là qui sépare
« l'agent qu'on a lancé » d'un homonyme innocent.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .workspace import PGID_FILE, item_workspace

GRACE_S = 5.0  # entre le SIGTERM et le SIGKILL du groupe de l'agent
AGENT_ALIVE = "alive"
AGENT_DEAD = "dead"
AGENT_UNKNOWN = "unknown"
PROC = Path("/proc")


def kill_group(proc: subprocess.Popen) -> None:
    """Révoque le groupe entier : SIGTERM, une grâce, puis SIGKILL.

    L'expiration d'un bail révoque l'autorité — donc aussi celle des
    descendants. Tuer le seul shell laisserait des orphelins (chromium,
    serveurs de test, sous-agents) travailler sans autorité.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:  # déjà parti, rien à révoquer
        return
    signal_group(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=GRACE_S)  # le chef de groupe part le premier
    except subprocess.TimeoutExpired:
        pass
    signal_group(pgid, signal.SIGKILL)
    proc.wait()  # récolte le chef de groupe, pas de zombie


def revoke_residual_group(pgid: int) -> None:
    """Révoque les descendants restés après la sortie normale du chef.

    ``Popen.wait`` a déjà récolté le chef. Il ne reste donc pas de handle à
    attendre, mais le groupe garde son pgid tant qu'un descendant vit.
    """
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + GRACE_S
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    signal_group(pgid, signal.SIGKILL)


def signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:  # plus personne dans le groupe
        pass


def write_pgid(path: Path, proc: subprocess.Popen, cmd: str, run_id: int) -> None:
    """Persiste de quoi révoquer l'agent sans handle Popen.

    `start_new_session` fait de l'agent le chef de son groupe : son pid est
    donc le pgid de tout ce qu'il lancera. L'identité dit lequel, le run dit
    à qui la trace appartient, la commande est pour l'humain qui lit le
    fichier. Écriture atomique : le faucheur ne lit jamais une demi-trace.
    """
    trace = {"run": run_id, "pgid": proc.pid, "cmd": cmd,
             "identity": identity(proc.pid)}
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(trace))
    tmp.replace(path)


def _starttime(pid: int) -> int:
    """La naissance du processus, champ 22 de proc(5), en tops depuis le boot."""
    fields = (PROC / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
    return int(fields[19])


def _boot() -> str:
    return (PROC / "sys/kernel/random/boot_id").read_text().strip()


def identity(pid: int) -> dict | None:
    """Ce qui distingue un processus d'un homonyme : son boot et sa naissance.

    La ligne de commande, elle, ne dit rien de sûr : elle est encore vide au
    retour de Popen, et le shell la réécrit s'il s'exec-optimise. Pas de
    /proc, pas d'identité — donc None.
    """
    try:
        return {"boot": _boot(), "starttime": _starttime(pid)}
    except (OSError, IndexError, ValueError):
        return None


def trace(item_id: int, run_id: int) -> tuple[Path, dict] | None:
    """La trace de pgid de ce run, et où elle est. None si elle n'y est pas.

    Le workspace de l'item porte la sienne, et chaque candidat de fan-out la
    sienne dans son sous-répertoire : le faucheur n'a qu'un numéro de run, il
    les regarde donc toutes et suit celle qui le nomme. Une trace illisible
    ou amputée n'en est pas une — elle ne fait tomber personne.
    """
    workspace = item_workspace(item_id)
    for path in (workspace / PGID_FILE, *sorted(workspace.glob(f"c*/{PGID_FILE}"))):
        try:
            found = json.loads(path.read_text())
            run, _, _ = found["run"], found["pgid"], found["identity"]
        except (OSError, ValueError, KeyError):
            continue
        if run == run_id:
            return path, found
    return None


def _well_formed(found: dict) -> bool:
    """Une trace dont l'identité et le pgid ont la forme qu'on a écrite."""
    who = found["identity"]
    return (isinstance(who, dict) and isinstance(who.get("boot"), str)
            and type(who.get("starttime")) is int
            and type(found["pgid"]) is int and found["pgid"] > 0)


def agent_state(item_id: int, run_id: int) -> str:
    """État certain de l'agent : vivant, mort, ou identité indécidable.

    Une trace absente ou invalide ne prouve rien. Un `/proc` absent, ou une
    identité différente pour le même pid, prouve au contraire que l'agent
    enregistré n'existe plus.
    """
    found = trace(item_id, run_id)
    if found is None or not _well_formed(found[1]):
        return AGENT_UNKNOWN
    who, pgid = found[1]["identity"], found[1]["pgid"]
    try:
        starttime = _starttime(pgid)
    except FileNotFoundError:
        return AGENT_DEAD if (PROC / "self/stat").exists() else AGENT_UNKNOWN
    except (OSError, IndexError, ValueError):
        return AGENT_UNKNOWN
    try:
        boot = _boot()
    except OSError:
        return AGENT_UNKNOWN
    if not boot:
        return AGENT_UNKNOWN
    return AGENT_ALIVE if {"boot": boot, "starttime": starttime} == who else AGENT_DEAD


def revoke_orphan(item_id: int, run_id: int) -> int | None:
    """Tue le groupe de l'agent d'un run fauché. Rend le pgid tué.

    Le faucheur n'a pas de handle Popen — il n'a que le `agent.pgid` laissé
    dans le workspace de l'item. Révoquer l'autorité en base ne suffit pas :
    l'orphelin continue d'écrire dans le checkout et le workspace.

    Trois garde-fous, parce que tuer un innocent est pire qu'un orphelin :
    la trace doit être celle du run fauché (une tentative suivante l'écrase,
    et un candidat voisin a la sienne), le chef du groupe doit toujours être
    celui qu'on a lancé — un pid se recycle, pas une identité — et le
    faucheur ne se fauche jamais lui-même.
    """
    found = trace(item_id, run_id)
    if found is None:  # pas d'agent en vol, trace illisible, ou trace d'un autre run
        return None
    path, found = found
    pgid, who = found["pgid"], found["identity"]
    path.unlink(missing_ok=True)  # une trace ne sert qu'à une révocation

    if who is None or identity(pgid) != who:
        return None  # groupe éteint, ou pid déjà repris par un innocent
    if pgid == os.getpgid(0):
        return None  # trace aberrante : un faucheur qui se tue ne fauche plus rien

    signal_group(pgid, signal.SIGTERM)
    deadline = time.time() + GRACE_S
    while time.time() < deadline and identity(pgid) == who:
        time.sleep(0.1)
    # le pgid n'est pas recyclable tant que le groupe a un membre : sans chef,
    # le SIGKILL reste pour les descendants ou ne trouve plus personne
    signal_group(pgid, signal.SIGKILL)
    print(f"orphelin révoqué : groupe {pgid} ({found.get('cmd', '?')})", flush=True)
    return pgid
