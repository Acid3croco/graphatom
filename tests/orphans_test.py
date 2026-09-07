"""Le test des orphelins : ni le bail ni la mort du worker ne laissent d'agent.

Scénario, sans base ni ordonnanceur — seuls le bloc agent et la révocation
par le faucheur sont en jeu :

  1. un agent factice qui laisse un sous-processus derrière lui est fauché
     au timeout, et la tentative est classée timed_out avec son autopsie
  2. plus aucun processus vivant de son groupe après la tentative
  3. les traces sont nommées par nœud, passage et tentative — journal
     `agent-<nœud>-<passage>-<tentative>.log` et prompt archivé à côté
  4. un agent qui répond normalement passe toujours, et son `usage.json`
     rejoint le résultat du run — un agent qui n'en laisse pas aussi
  5. un agent qui sort en erreur sans `outcome.json` rend son autopsie :
     code de sortie, queue de log, et pas de flag timeout
  6. worker tué en plein vol : l'agent survit, et sa trace `agent.pgid` aussi
  7. `revoke_orphan` — ce que fait le faucheur — tue le groupe et efface la trace
  8. garde-fou : une trace d'une autre tentative n'est ni suivie ni effacée
 9. garde-fou : une identité qui ne colle plus (naissance ou boot) ne tue personne
10. une trace illisible ou amputée ne fait pas tomber le faucheur
11. les trois adaptateurs gardent leur CLI dans le groupe suivi par le worker
12. le timeout interne exact de codex ne laisse aucun descendant vivant

Usage : uv run python tests/orphans_test.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks  # noqa: E402
from graphatom import context as contexte, process as processus, workspace  # noqa: E402

# le shell note le pgid de son groupe, puis laisse un sous-processus derrière lui
FACTICE = "ps -o pgid= -p $$ > pgid.txt; sleep 300 & sleep 300"
SANS_USAGE = """
printf '%s\n' '## Fait' 'Travail terminé.' '' '## Appris' 'Rien.' '' \
    '## Pas fait' 'Rien.' > passation-travail.md
printf '{"outcome": "ok", "summary": "fait"}' > outcome.json
"""
USAGE = {"input_tokens": 12, "output_tokens": 3456}  # ce que l'agent veut bien dire
REPOND = f"{SANS_USAGE}\nprintf '{json.dumps(USAGE)}' > usage.json"
ECHOUE = "echo 'Execution error' >&2; exit 42"  # sort mal, sans outcome.json
# hermétisme : sans instance jetable dans l'environnement, le bloc ne dérive
# aucune base — les agents factices d'ici n'en ont pas besoin, et l'item 7
# du test ne va pas créer la base d'un item 7 qui existe pour de vrai
os.environ.pop("GRAPHATOM_AGENT_DSN", None)
TIMEOUT_S = 2
ITEM_ID = 7  # l'item du contexte de test : son workspace porte la trace
RUN_ID = 1  # le run du contexte de test : la trace lui appartient
CYCLE = 1  # le passage du contexte de test : il nomme les traces de l'agent
NODE = "travail"  # le nœud du contexte de test : il les nomme aussi
ROOT = Path(__file__).resolve().parents[1]


class FakeConn:
    """Le bloc interroge la base pour la clé du sujet, et pour la tentative
    antérieure dont il reprendrait le travail — ici, il n'y en a aucune."""

    def execute(self, sql, *_args):  # noqa — le contrat de la base, lu par le bloc
        self.sql = sql
        return self

    def fetchone(self) -> dict | None:  # noqa — idem
        return {"subject_key": "orphelins:test"} if "FROM subject" in self.sql else None


def context(workdir: Path, cmd: str, attempt: int,
            timeout_s: float = TIMEOUT_S) -> contexte.Context:
    workspace.DATA_DIR = workdir  # noqa — les workspaces du test
    node = {"block": "ACT", "edges": {"ok": "done"},
            "config": {"execution": {"kind": "agent", "cmd": cmd,
                                     "timeout_s": timeout_s},
                       "agent": {"prompt": "ne fais rien"}}}
    return contexte.Context(
        FakeConn(),
        {"id": RUN_ID, "node": NODE, "cycle": CYCLE, "attempt": attempt},
        {"id": ITEM_ID, "subject_id": 1},
        node,
        {"name": "orphelins"},
    )


def group_alive(pgid: int) -> list[int]:
    """Les pids vivants du groupe — zombies exclus, ils ne travaillent plus."""
    alive = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:  # une course avec /proc n'est pas un échec du test
            fields = stat.read_text().rsplit(") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) == pgid and fields[0] != "Z":
            alive.append(int(stat.parent.name))
    return alive


def wait_dead(pgid: int, seconds: float = 2.0) -> list[int]:
    """Les survivants du groupe, une fois laissé à init le temps de récolter."""
    deadline = time.time() + seconds
    while time.time() < deadline and group_alive(pgid):
        time.sleep(0.1)
    return group_alive(pgid)


def exige_groupe_mort(pgid: int, echec: str) -> None:
    """Le groupe doit être vide ; sinon on achève ses survivants et on sort."""
    survivants = wait_dead(pgid)
    if survivants:
        for pid in survivants:
            os.kill(pid, 9)
        sys.exit(f"ÉCHEC : {echec} : {survivants}")


def attendre_fichier(path: Path, seconds: float, pas: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not path.exists():
        time.sleep(pas)


def worker(workdir: Path) -> None:
    """Le bloc agent dans un processus jetable — le test tue ce worker-là."""
    blocks.act(context(workdir, FACTICE, attempt=4, timeout_s=300))


def foreign(pgid: int) -> bool:
    """Le groupe est-il bien étranger au test ? Rien ne se révoque sans ça.

    Le test tourne lui-même dans un groupe de processus, parfois celui d'un
    agent. Un cobaye qui partagerait ce groupe ferait d'une révocation
    réussie un suicide silencieux : plus de sortie, plus de verdict.
    """
    return pgid != os.getpgid(0)


# -------------------------------------------------------------------- preuves


def preuve_timeout(workdir: Path) -> contexte.Context:
    """1. l'agent factice est fauché au timeout, et rend son autopsie."""
    ctx = context(workdir, FACTICE, attempt=1)
    started = time.time()
    result = blocks.act(ctx)
    assert result["outcome"] == "timed_out", result  # un dépassement, pas une panne
    assert result["timeout"] is True, result
    # négatif = signal : SIGTERM s'il part à la grâce, SIGKILL sinon
    assert result["exit_code"] in (-signal.SIGTERM, -signal.SIGKILL), result
    assert "TimeoutExpired" in result["error"], result
    print(f"1. agent fauché au timeout après {time.time() - started:.1f}s, "
          f"autopsie {result['exit_code']} ✓")
    return ctx


def preuve_groupe_revoque(ctx: contexte.Context) -> None:
    """2. le groupe entier est révoqué, descendants compris."""
    pgid = int((ctx.workspace / "pgid.txt").read_text())
    assert foreign(pgid), "l'agent doit avoir sa propre session, pas celle du test"
    exige_groupe_mort(pgid, f"survivants du groupe {pgid}")
    print(f"2. groupe {pgid} entièrement révoqué ✓")


def preuve_traces(ctx: contexte.Context) -> None:
    """3. les traces de la tentative portent son nom, et rien n'est écrasé."""
    log = ctx.workspace / f"agent-{NODE}-{CYCLE}-1.log"
    prompt = ctx.workspace / f"prompt-{NODE}-{CYCLE}-1.md"
    assert log.exists(), log
    assert prompt.exists(), prompt
    assert not (ctx.workspace / workspace.PROMPT_NAME).exists(), "prompt.md doit être rangé"
    print(f"3. traces {log.name} et {prompt.name} écrites ✓")


def preuve_nominal(workdir: Path, ctx: contexte.Context, trace_path: Path) -> None:
    """4. non-régression : un agent qui répond passe toujours, et son usage suit."""
    result = blocks.act(context(workdir, REPOND, attempt=2))
    assert result == {"outcome": "ok", "summary": "fait", "usage": USAGE}, result
    assert json.loads(
        (ctx.workspace / f"usage-{NODE}-{CYCLE}-2.json").read_text()) == USAGE
    assert not trace_path.exists(), "une exécution collectée n'a plus de trace"
    print(f"4. agent nominal appliqué, usage fusionné : {result} ✓")

    # 4 bis. un agent sans usage.json reste un citoyen de première classe
    result = blocks.act(context(workdir, SANS_USAGE, attempt=5))
    assert result == {"outcome": "ok", "summary": "fait"}, result
    print("4 bis. agent sans usage.json : résultat inchangé ✓")


def preuve_crash(workdir: Path) -> None:
    """5. l'agent qui sort en erreur rend son autopsie, sans flag timeout."""
    result = blocks.act(context(workdir, ECHOUE, attempt=3))
    assert result["outcome"] == "crashed", result
    assert result["exit_code"] == 42, result
    assert result["timeout"] is False, result
    assert "Execution error" in result["log_tail"], result
    print(f"5. autopsie du crash : sortie {result['exit_code']}, "
          f"queue « {result['log_tail'].strip()} » ✓")


def preuve_orphelin(workdir: Path, trace_path: Path) -> dict:
    """6. le worker meurt en plein vol : l'agent et sa trace lui survivent."""
    # session dédiée pour le cobaye : ni lui ni son agent ne partagent le
    # groupe du test, que la révocation de l'étape 7 balaiera
    proc = subprocess.Popen([sys.executable, __file__, "--worker", str(workdir)],
                            stdout=subprocess.DEVNULL, start_new_session=True)
    attendre_fichier(trace_path, 10, 0.1)
    proc.kill()  # SIGKILL du seul worker : l'agent, lui, a sa propre session
    proc.wait()
    if not trace_path.exists():
        sys.exit("ÉCHEC : le bloc n'a pas persisté agent.pgid")
    trace = json.loads(trace_path.read_text())
    if not foreign(trace["pgid"]):
        sys.exit("ÉCHEC : l'orphelin est dans le groupe du test — révocation refusée")
    if not group_alive(trace["pgid"]):
        sys.exit("ÉCHEC : l'agent n'a pas survécu à son worker — rien à révoquer")
    print(f"6. worker tué, agent orphelin toujours vivant : groupe {trace['pgid']} ✓")
    return trace


def preuve_faucheur(trace: dict, trace_path: Path) -> None:
    """7. le faucheur révoque l'orphelin à partir de la seule trace."""
    tue = processus.revoke_orphan(ITEM_ID, RUN_ID)
    assert tue == trace["pgid"], tue
    exige_groupe_mort(trace["pgid"], "l'orphelin a survécu au faucheur")
    assert not trace_path.exists(), "la trace consommée doit disparaître"
    print("7. orphelin révoqué par le faucheur, trace effacée ✓")


def preuve_autre_tentative(trace_path: Path) -> tuple[subprocess.Popen, dict]:
    """8. garde-fou : la trace d'une autre tentative ne regarde pas ce run."""
    innocent = subprocess.Popen(["sleep", "300"], start_new_session=True)
    identite = processus.identity(innocent.pid)
    vivant = {"run": RUN_ID + 1, "pgid": innocent.pid, "cmd": "sleep 300",
              "identity": identite}
    trace_path.write_text(json.dumps(vivant))
    assert processus.agent_state(ITEM_ID, RUN_ID) == processus.AGENT_UNKNOWN
    assert processus.revoke_orphan(ITEM_ID, RUN_ID) is None
    assert innocent.poll() is None, "l'agent d'une autre tentative doit vivre"
    assert trace_path.exists(), "sa trace doit rester armée pour son propre run"
    print("8. trace d'une autre tentative : ni suivie ni effacée ✓")
    return innocent, vivant


def preuve_identite_perimee(trace_path: Path, innocent: subprocess.Popen,
                            vivant: dict) -> None:
    """9. garde-fou : une identité qui ne colle plus ne tue personne."""
    identite = vivant["identity"]
    for faux in (identite | {"starttime": -1}, identite | {"boot": "un-autre-boot"}):
        trace_path.write_text(json.dumps(vivant | {"run": RUN_ID, "identity": faux}))
        assert processus.agent_state(ITEM_ID, RUN_ID) == processus.AGENT_DEAD
        assert processus.revoke_orphan(ITEM_ID, RUN_ID) is None
        assert innocent.poll() is None, "un pid recyclé ne doit jamais être tué"
        assert not trace_path.exists(), "la trace périmée, elle, doit disparaître"
    innocent.kill()
    innocent.wait()
    trace_path.write_text(json.dumps(vivant | {"run": RUN_ID}))
    assert processus.agent_state(ITEM_ID, RUN_ID) == processus.AGENT_DEAD
    trace_path.unlink()
    print("9. identité périmée (naissance, boot) : le faucheur ne tue personne ✓")


def preuve_trace_abimee(trace_path: Path) -> None:
    """10. une trace abîmée ne fait pas tomber le tick du faucheur."""
    for abime in ("{ceci n'est pas du json", json.dumps({"run": RUN_ID})):
        trace_path.write_text(abime)
        assert processus.agent_state(ITEM_ID, RUN_ID) == processus.AGENT_UNKNOWN
        assert processus.revoke_orphan(ITEM_ID, RUN_ID) is None
    print("10. trace illisible ou amputée : le faucheur passe son chemin ✓")


def preuve_timeout_foreground(workdir: Path) -> Path:
    """11. GNU timeout crée sinon un groupe enfant que la trace agent.pgid ne
    voit pas. --foreground garde la CLI dans le groupe suivi et révoqué."""
    for name in ("agent-claude.sh", "agent-codex.sh", "agent-opencode.sh"):
        script = (ROOT / "scripts" / name).read_text()
        assert "timeout --foreground -k 5" in script, name
    probe = workdir / "timeout-foreground"
    probe.mkdir()
    child_pgid = probe / "child.pgid"
    command = (
        "timeout --foreground -k 5 30 sh -c "
        "'ps -o pgid= -p $$ > child.pgid; sleep 30'"
    )
    process = subprocess.Popen(["bash", "-c", command], cwd=probe,
                               start_new_session=True)
    attendre_fichier(child_pgid, 5, 0.05)
    assert child_pgid.exists(), "la CLI factice n'a pas écrit son groupe"
    assert int(child_pgid.read_text().strip()) == os.getpgid(process.pid)
    os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=5)
    print("11. timeout --foreground : la CLI reste dans le groupe suivi ✓")
    return probe


def preuve_timeout_codex(workdir: Path, probe: Path, trace_path: Path) -> None:
    """12. Le vrai adaptateur codex laisse GNU timeout atteindre sa borne.
    La CLI factice lance un enfant long : après le retour du bloc, le
    groupe doit être vide et la trace déjà désarmée."""
    fake = probe / "codex-factice"
    fake.write_text(
        "#!/bin/sh\n"
        "ps -o pgid= -p $$ > \"$CODEX_FAKE_PGID\"\n"
        "sleep 300 &\n"
        "wait\n"
    )
    fake.chmod(0o755)
    fake_pgid = probe / "codex-timeout.pgid"
    adapter = ROOT / "scripts" / "agent-codex.sh"
    command = (
        f'CODEX_BIN="{fake}" CODEX_FAKE_PGID="{fake_pgid}" '
        f'CODEX_TIMEOUT_S=0.3 bash "{adapter}"'
    )
    result = blocks.act(context(workdir, command, attempt=6, timeout_s=10))
    assert result["outcome"] == "crashed", result
    assert result["exit_code"] == 4, result
    assert fake_pgid.exists(), "la CLI codex factice n'a pas écrit son groupe"
    pgid = int(fake_pgid.read_text().strip())
    exige_groupe_mort(pgid, "enfants après le timeout codex")
    assert not trace_path.exists(), "le timeout codex laisse une trace désarmée"
    print("12. timeout interne codex : aucun descendant ne survit ✓")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-orphans-"))

    ctx = preuve_timeout(workdir)
    preuve_groupe_revoque(ctx)
    preuve_traces(ctx)
    trace_path = ctx.workspace / workspace.PGID_FILE
    preuve_nominal(workdir, ctx, trace_path)
    preuve_crash(workdir)
    trace = preuve_orphelin(workdir, trace_path)
    preuve_faucheur(trace, trace_path)
    innocent, vivant = preuve_autre_tentative(trace_path)
    preuve_identite_perimee(trace_path, innocent, vivant)
    preuve_trace_abimee(trace_path)
    probe = preuve_timeout_foreground(workdir)
    preuve_timeout_codex(workdir, probe, trace_path)

    shutil.rmtree(workdir, ignore_errors=True)
    print("\norphelins : OK — ni le bail ni la mort du worker ne laissent d'agent")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        worker(Path(sys.argv[2]))
    else:
        main()
