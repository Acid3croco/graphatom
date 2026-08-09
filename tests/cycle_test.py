"""Un cycle entier du profil `code-task`, de l'admission à `close`.

Les autres tests prennent le fan-out, l'arbitre ou la réduction séparément.
Celui-ci prend le graph de production **tel qu'il est publié** —
`examples/code-task.json` — et le fait traverser par le vrai ordonnanceur,
d'un bout à l'autre : `implement` en fan-out réduit par `keep_n`, le nœud
`judge` derrière, puis les tests, la revue, le retrait, et `close`.

Ce qui est joué pour de vrai : le graph, ses arêtes, ses budgets, la
réduction `keep_n` et son `n`, le nœud arbitre et sa source, l'ordonnanceur,
la base, git — les ateliers des candidats sont de vrais worktrees et leurs
commits de vrais commits. Ce qui est remplacé : les seules `cmd` qui
appellent un modèle ou sortent sur le réseau — celle du nœud, et celles que
ses variantes surchargent, car une variante qui nomme sa propre CLI en
appelle un aussi. Un cycle qui paie tous les candidats et un juge ne se joue
pas dans une suite de tests ; ce qui route un item, en revanche, ce sont les
arêtes et les issues, et elles sont ici d'origine.

Le chemin attendu, celui du profil :

    ingest → worktree → scope → implement (les candidats du profil, keep_n = 2)
           → judge (2 finalistes → chosen) → test_backend → test_frontend
           → validate → review (l'humain répond `merger`) → release
           → deploy → verify_deploy → cleanup → close

Rien de la production n'est touché : le dépôt est jetable, son `origin` est
un dépôt nu du même répertoire temporaire, et l'ordonnanceur tourne dans un
`cwd` jetable — les workspaces qu'il crée ne voient pas le `data/` du dépôt.

Usage : uv run python tests/cycle_test.py
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import channel, db, graph, kernel  # noqa: E402

from outils import etat, git  # noqa: E402

# La base mère ne porte que la création de l'instance jetable. Les agents du
# cycle, eux, sont des scripts shell et ne reçoivent pas cette capacité.
ORIGINAL_AGENT_DSN = os.environ.get("GRAPHATOM_AGENT_DSN")
AGENT_INSTANCE = ORIGINAL_AGENT_DSN or db.DSN
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

ROOT = Path(__file__).resolve().parents[1]
PROFIL = ROOT / "examples" / "code-task.json"
# Un échec peut laisser son item actif dans la base de test. Un numéro neuf
# rend le scénario rejouable sans effacer ce diagnostic ni toucher un voisin.
NUM = int(time.time_ns() % 1_000_000_000)
TIMEOUT_S = 180.0
TEST_DB_ID = 970000 + os.getpid() % 10000

# Le chemin que l'item doit suivre, nœud par nœud. C'est la liste qu'on
# vérifie à la fin : pas seulement « il est arrivé », mais « il est passé
# par là » — un cycle qui atteindrait `close` en sautant l'arbitre ne
# prouverait rien de ce ticket.
ATTENDU = ["ingest", "worktree", "scope", "implement", "judge", "test_backend",
           "test_frontend", "validate", "review", "release", "deploy",
           "verify_deploy", "cleanup", "close"]

# Combien de candidats `implement` fait courir — le profil le dit, et ce
# n'est pas au test de le figer : ce qu'il vérifie, c'est que `keep_n` en
# garde deux quel qu'en soit le nombre.
CANDIDATS = len(graph.fanout_variants(
    json.loads(PROFIL.read_text())["nodes"]["implement"]))
COUT_CANDIDAT = 0.50  # ce que la doublure `CANDIDAT` déclare dans son usage.json
PASSATION = ("printf '%s\\n' '## Fait' 'Doublure exécutée.' '' '## Appris' "
             "'Rien.' '' '## Pas fait' 'Rien.'")

# Un candidat de `implement` : il commite pour de vrai dans son atelier, avec
# un mot à lui, puis rend `done`. Autant de candidats que le profil en
# déclare, autant de diffs différents — de quoi donner au juge quelque chose
# à départager.
CANDIDAT = """
set -e
K=$(basename "$(pwd)")
printf 'le candidat %s a travaillé\\n' "$K" >> "$GRAPHATOM_WORKTREE/travail-$K.txt"
git -C "$GRAPHATOM_WORKTREE" add -A
git -C "$GRAPHATOM_WORKTREE" commit -qm "implémentation du candidat $K"
printf '%s\\n' '## Fait' 'Implémentation committée.' '' '## Appris' 'Rien.' '' \\
    '## Pas fait' 'Rien.' > passation-implement.md
printf '{"input_tokens": 1000, "total_cost_usd": 0.50}' > usage.json
printf '{"outcome": "done", "summary": "candidat %s"}' "$K" > outcome.json
"""

# L'arbitre : il écrit son verdict et élit la première lettre. Ce qu'il
# coûte est dit, et c'est bien plus que ce qu'un candidat coûte — c'est
# l'haltère, et le front doit pouvoir montrer les deux bouts.
JUGE = """
printf '%s\\n' '# Finaliste A' '' '1. **Tenu.** Tous les critères sont tenus.' '' \\
    '# Finaliste B' '' '1. **Tenu.** Tous les critères sont tenus.' '' \\
    '# Comparaison' '' 'A le fait en moins de lignes.' > verdict.md
printf '%s\\n' '## Fait' 'Finalistes départagés.' '' '## Appris' 'A est plus court.' '' \\
    '## Pas fait' 'Rien.' > passation-judge.md
printf '{"input_tokens": 50000, "total_cost_usd": 4.00}' > usage.json
printf '{"outcome": "chosen", "elu": "A", "summary": "le diff le plus court"}' \\
    > outcome.json
"""

DEPLOIEMENT = f"""
SHA=$(git -C "$GRAPHATOM_REPO_DIR" rev-parse HEAD)
{PASSATION} > passation-deploy.md
printf '{{"outcome": "done", "summary": "doublure de test", "deploy_sha": "%s"}}' \
    "$SHA" > outcome.json
"""


def stub(node: str, outcome: str) -> str:
    """Un nœud qui rend sa passation et son issue — ni modèle, ni réseau."""
    return (f"{PASSATION} > passation-{node}.md\n"
            f"printf '{{\"outcome\": \"{outcome}\", "
            f"\"summary\": \"doublure de test\"}}' > outcome.json\n")


# Les nœuds dont la `cmd` appelle un modèle, sort sur le réseau ou dépend des
# scripts du dépôt traité, et l'issue qu'on leur fait rendre. Tout le reste du
# bundle — `ingest`, `worktree`, `review`, et surtout les arêtes et le
# `fanout` — reste d'origine. Le dépôt jetable ne contient que son fichier
# socle : son cleanup doit donc être doublé comme les autres commandes.
DOUBLURES = {
    "scope": stub("scope", "ready"),
    "implement": CANDIDAT,
    "judge": JUGE,
    "test_backend": stub("test_backend", "pass"),
    "test_frontend": stub("test_frontend", "pass"),
    "validate": stub("validate", "pass"),
    "release": stub("release", "done"),
    "deploy": DEPLOIEMENT,
    "verify_deploy": stub("verify_deploy", "pass"),
    "cleanup": stub("cleanup", "done"),
}


def bundle() -> dict:
    """Le profil de production, toutes ses exécutions remplacées par des stubs."""
    spec = json.loads(PROFIL.read_text())
    spec["name"] = "code-task-cycle-test"
    for node, cmd in DOUBLURES.items():
        config = spec["nodes"][node]["config"]
        config.pop("agent", None)
        config["execution"] = {
            "kind": "command", "cmd": cmd,
            "timeout_s": 60, "silence_s": 60,
        }
        # les budgets d'origine sont ceux d'un vrai modèle : un quart d'heure
        # d'attente pour une doublure qui rend en 50 ms n'apprend rien, et
        # allongerait le test d'autant le jour où l'une se pend
    fanout = spec["nodes"]["implement"]["config"]["fanout"]
    assert fanout["reduce"] == "keep_n", fanout  # le ticket, en une ligne
    assert fanout["n"] == 2, fanout
    # une variante peut surcharger l'agent du nœud pour courir sur une autre
    # CLI : c'est encore un modèle, et un réseau. On lui retire sa surcharge,
    # elle retombe sur la doublure du nœud — le nombre de candidats, lui, ne
    # bouge pas, et c'est lui que la réduction départage.
    for variant in fanout["variants"]:
        variant.pop("agent", None)
    assert graph.judge_source(spec["nodes"]["judge"]) == "implement"
    return spec


# ------------------------------------------------------------------ outillage


def depot(tmp: Path) -> Path:
    """Un clone jetable et son `origin` : ce dont `worktree` a besoin, pas plus.

    Le nœud `worktree` du profil est joué tel quel — il fetch `origin` et
    part d'`origin/main`. Il lui faut donc un vrai remote, et un dépôt nu du
    même répertoire temporaire fait l'affaire.
    """
    nu = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(nu)], check=True)
    amorce = tmp / "amorce"
    subprocess.run(["git", "clone", "-q", str(nu), str(amorce)], check=True)
    git(amorce, "config", "user.email", "cycle@test.invalid")
    git(amorce, "config", "user.name", "cycle")
    (amorce / "socle.txt").write_text("le commit de départ\n")
    git(amorce, "add", "-A")
    git(amorce, "commit", "-qm", "socle")
    git(amorce, "push", "-q", "origin", "main")

    repo = tmp / "repo"
    subprocess.run(["git", "clone", "-q", str(nu), str(repo)], check=True)
    git(repo, "config", "user.email", "cycle@test.invalid")
    git(repo, "config", "user.name", "cycle")
    return repo


def ordonnanceur(work: Path) -> subprocess.Popen:
    """Le vrai rail, dans son groupe de processus, sur un `cwd` jetable."""
    return subprocess.Popen([str(ROOT / ".venv" / "bin" / "graphatom"), "run"],
                            cwd=work, start_new_session=True)


def services_deployes(tmp: Path, sha: str) -> tuple[Path, Path]:
    """Deux outils étroits qui exposent quatre services sur le SHA du test."""
    docker = tmp / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "for ARG in \"$@\"; do LAST=\"$ARG\"; done\n"
        "if [ \"$1\" = inspect ]; then printf '%s\\n' \"$GRAPHATOM_TEST_SHA\"; exit 0; fi\n"
        "if [ \"$2\" = ps ]; then printf 'id-%s\\n' \"$LAST\"; exit 0; fi\n"
        "exit 1\n"
    )
    docker.chmod(0o755)
    gh = tmp / "gh"
    gh.write_text("#!/bin/sh\nprintf 'jeton-de-test\\n'\n")
    gh.chmod(0o755)
    return docker, gh


def tuer(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    proc.wait()


def attendre(predicat, seconds: float = TIMEOUT_S):
    """Attend qu'un fait devienne vrai — un cycle n'est pas synchrone."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if (valeur := predicat()):
            return valeur
        time.sleep(0.2)
    return predicat()


def repondre(conn, item_id: int, option: str) -> None:
    """Ce que l'humain fait sur la revue : il répond, le rail reprend."""
    question = attendre(lambda: conn.execute(
        "SELECT * FROM question WHERE item_id = %s AND state = 'open'",
        (item_id,)).fetchone())
    assert question is not None, f"aucune question ouverte sur l'item {item_id}"
    assert question["node"] == "review", question["node"]
    motif = channel.record_answer(conn, question["id"], option, "cycle-test")
    assert motif is None, motif
    print(f"   revue : question [{question['id']}] répondue « {option} » ✓")


# -------------------------------------------------------------------- épreuve


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-cycle-"))
    work = tmp / "work"
    work.mkdir()
    repo = depot(tmp)
    os.environ["GRAPHATOM_REPO_DIR"] = str(repo)
    saved_worker_sha = os.environ.get("GRAPHATOM_WORKER_SHA")
    worker_sha = git(repo, "rev-parse", "HEAD")
    os.environ["GRAPHATOM_WORKER_SHA"] = worker_sha
    docker, gh = services_deployes(tmp, worker_sha)
    saved_deploy_env = {
        name: os.environ.get(name) for name in
        ("GRAPHATOM_DOCKER", "GRAPHATOM_GH", "GRAPHATOM_TEST_SHA")
    }
    os.environ["GRAPHATOM_DOCKER"] = str(docker)
    os.environ["GRAPHATOM_GH"] = str(gh)
    os.environ["GRAPHATOM_TEST_SHA"] = worker_sha

    saved_dsn = db.DSN
    saved_dsn_env = os.environ.get("GRAPHATOM_DSN")
    os.environ["GRAPHATOM_AGENT_DSN"] = AGENT_INSTANCE
    test_dsn = db.agent_dsn(TEST_DB_ID)
    assert test_dsn, "aucune instance jetable : impossible d'isoler le cycle"
    os.environ.pop("GRAPHATOM_AGENT_DSN", None)
    db.DSN = test_dsn
    os.environ["GRAPHATOM_DSN"] = test_dsn
    db.init_db()
    proc = None
    try:
        with db.connect() as conn:
            rev = graph.publish(conn, bundle())
            item_id = kernel.admit(conn, rev, f"gh:exemple/depot#{NUM}")
            print(f"1. item {item_id} admis sur le profil code-task, "
                  f"atelier jetable {repo}")

            proc = ordonnanceur(work)
            repondre(conn, item_id, "merger")

            fini = attendre(lambda: etat(conn, item_id)["terminal_at"] is not None)
            item = etat(conn, item_id)
            if not fini:
                sys.exit(f"ÉCHEC : l'item est resté sur « {item['state']} » "
                         f"après {TIMEOUT_S} s")
            if item["state"] != "close":
                sys.exit(f"ÉCHEC : terminal sur « {item['state']} », attendu « close »")
            # `escalations` est un reste, pas un compte : le budget d'origine
            # intact dit qu'un cycle nominal ne paie aucune escalade
            budget = graph.load_bundle(conn, item["revision"])["budgets"]["escalations"]
            assert item["escalations"] == budget, item["escalations"]
            print(f"2. item terminal sur « close » en v{item['version']}, "
                  f"passage {item['cycle']}, budget d'escalade intact "
                  f"({budget}) ✓")

            verifier(conn, item_id, work)
    finally:
        if proc is not None:
            tuer(proc)
        os.environ["GRAPHATOM_AGENT_DSN"] = AGENT_INSTANCE
        db.drop_agent_db()
        db.DSN = saved_dsn
        if saved_dsn_env is None:
            os.environ.pop("GRAPHATOM_DSN", None)
        else:
            os.environ["GRAPHATOM_DSN"] = saved_dsn_env
        if ORIGINAL_AGENT_DSN is None:
            os.environ.pop("GRAPHATOM_AGENT_DSN", None)
        else:
            os.environ["GRAPHATOM_AGENT_DSN"] = ORIGINAL_AGENT_DSN
        if saved_worker_sha is None:
            os.environ.pop("GRAPHATOM_WORKER_SHA", None)
        else:
            os.environ["GRAPHATOM_WORKER_SHA"] = saved_worker_sha
        for name, value in saved_deploy_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(tmp, ignore_errors=True)

    print("\ncycle : OK — le profil code-task traverse keep_n et judge, "
          "et l'item atteint close")


def verifier(conn, item_id: int, work: Path) -> None:
    """Le chemin parcouru, les finalistes, et le prix du jugement à part."""
    chemin = [e["to_state"] for e in conn.execute(
        "SELECT to_state FROM event WHERE item_id = %s ORDER BY item_version",
        (item_id,))]
    if chemin != ATTENDU:
        sys.exit(f"ÉCHEC : chemin {chemin}\n         attendu {ATTENDU}")
    print(f"3. chemin d'origine tenu, {len(chemin)} nœuds : "
          f"{' → '.join(chemin)} ✓")

    runs = conn.execute(
        "SELECT node, candidate, status, outcome, result FROM node_run "
        "WHERE item_id = %s ORDER BY id", (item_id,)).fetchall()
    candidats = [r for r in runs if r["node"] == "implement"]
    finalistes = [r for r in candidats if r["status"] == "applied"]
    recales = [r for r in candidats if r["status"] == "superseded"]
    assert len(candidats) == CANDIDATS, candidats
    assert len(finalistes) == 2, finalistes  # `n` = 2, et pas un de plus
    assert len(recales) == CANDIDATS - 2, recales
    print(f"4. {len(candidats)} candidats courus, {len(finalistes)} finalistes "
          f"transmis, {len(recales)} recalé{'s' if len(recales) > 1 else ''} ✓")

    arbitre = [r for r in runs if r["node"] == "judge"]
    assert len(arbitre) == 1 and arbitre[0]["outcome"] == "chosen", arbitre
    print("   l'arbitre a couru une fois et a élu ✓")

    deploy = [r for r in runs if r["node"] == "deploy"]
    assert len(deploy) == 1, deploy
    deploy_result = deploy[0]["result"]
    assert re.fullmatch(r"[0-9a-f]{40}", deploy_result["deploy_sha"])
    assert deploy_result["worker_activation"] == {
        "status": "active", "worker_sha": deploy_result["deploy_sha"],
    }, deploy_result
    print("   le vrai lecteur du bloc conserve deploy_sha jusqu'au résultat "
          "appliqué et à l'acquittement du worker ✓")

    workspace = work / "data" / f"item-{item_id}"
    passations = [workspace / "passation-scope.md",
                  workspace / "passation-judge.md",
                  workspace / "passation-test_backend.md",
                  workspace / "passation-validate.md"]
    for path in passations:
        texte = path.read_text()
        assert all(section in texte for section in
                   ("## Fait", "## Appris", "## Pas fait")), path
    assert len(list(workspace.glob("c*/passation-implement.md"))) >= 2
    print("   passations fraîches de scope, implement, judge, test_backend et "
          "validate, toutes à trois sections ✓")

    # le prix du jugement, à part de celui de la génération : c'est ce que la
    # page de l'item montre, et c'est mesurable ici sur les mêmes lignes
    prix = {r["part"]: r["cout"] for r in conn.execute(
        "SELECT CASE WHEN node = 'judge' THEN 'jugement' ELSE 'candidats' END AS part, "
        "sum((result->'usage'->>'total_cost_usd')::float) AS cout FROM node_run "
        "WHERE item_id = %s AND node IN ('judge', 'implement') GROUP BY 1",
        (item_id,))}
    assert prix.get("jugement") == 4.0, prix
    factures = [r for r in candidats if ((r.get("result") or {}).get("usage") or {})
                .get("total_cost_usd") is not None]
    assert all(r["result"]["usage"]["total_cost_usd"] == COUT_CANDIDAT
               for r in factures), factures
    # `keep_n` révoque dès deux réussites. Un troisième candidat peut avoir
    # rendu son usage juste avant la révocation, ou être coupé avant : le
    # prix compte tous les usages rendus, et n'en invente aucun pour celui
    # qui n'en a pas rendu.
    assert prix.get("candidats") == len(factures) * COUT_CANDIDAT, (prix, factures)
    print(f"5. prix du jugement {prix['jugement']} $ face aux candidats "
          f"{prix['candidats']} $ ({len(factures)}/{CANDIDATS} usages rendus) — "
          "deux parts, pas un total ✓")


if __name__ == "__main__":
    main()
