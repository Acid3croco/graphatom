"""Le test d'hermétisme : ce qu'un agent lance ne touche jamais la production.

L'incident : un agent de test a lancé un ordonnanceur sur la base jetable
*partagée*, avec le `GRAPHATOM_REPO_DIR` de la production dans son
environnement — le cleanup d'un item de test a retiré le worktree d'un vrai
item portant le même numéro. Trois barrières, vérifiées ici :

  1. une base jetable par item : `agent_dsn` la crée à la volée, deux fois
     de suite sans broncher, et ne rend jamais la base de l'instance
  2. sans instance jetable dans l'environnement, aucune base n'est dérivée
  3. `drop-agent-db` refuse tout ce qui ne porte pas le préfixe jetable
  4. …et drop la base de l'item quand c'est bien la sienne
  5. le cleanup du graph ne touche pas un worktree dont la branche n'est pas
     celle de son sujet — et rend `done` quand même
  6. ni un chemin que son propre REPO_DIR n'enregistre pas
  7. sur sa vraie cible, il retire worktree et branche
  8. les scripts de test épinglent `GRAPHATOM_REPO_DIR` sur leur dépôt

Usage : uv run python tests/hermetic_test.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from graphatom import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# l'instance jetable : celle de l'agent qui nous lance, sinon la nôtre
INSTANCE = os.environ.get("GRAPHATOM_AGENT_DSN") or db.DSN
os.environ["GRAPHATOM_AGENT_DSN"] = INSTANCE
# un numéro d'item que le rail n'atteindra pas, et le pid pour que deux
# exécutions concurrentes ne se disputent pas la même base
ITEM_ID = 900000 + os.getpid() % 10000
PRODUCTION = "postgresql://graphatom:graphatom@127.0.0.1:5432/graphatom"


def bases() -> list[str]:
    with psycopg.connect(INSTANCE, autocommit=True) as conn:
        return [r[0] for r in conn.execute("SELECT datname FROM pg_database")]


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def depot(tmp: Path) -> Path:
    """Un dépôt jetable avec le worktree d'un item, comme en produirait le rail."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "hermetic@test.invalid")
    git(repo, "config", "user.name", "hermetic")
    (repo / "socle.txt").write_text("le commit de départ\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "socle")
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "rail-item-42"),
        "-b", "rail/issue-77")
    return repo


def cleanup(repo: Path, workspace: Path, subject: str) -> dict:
    """Le cmd de cleanup du graph, joué tel quel. Rend son outcome.json."""
    bundle = json.loads((ROOT / "examples" / "code-task.json").read_text())
    cmd = bundle["nodes"]["cleanup"]["config"]["agent"]["cmd"]
    subprocess.run(
        cmd, shell=True, cwd=workspace, check=False,
        env=os.environ | {"GRAPHATOM_REPO_DIR": str(repo),
                          "GRAPHATOM_WORKSPACE": str(workspace),
                          "GRAPHATOM_SUBJECT_KEY": subject},
    )
    return json.loads((workspace / "outcome.json").read_text())


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-hermetic-"))
    workspace = tmp / "item-42"  # le nom du workspace fait celui du worktree
    workspace.mkdir()
    nom = f"{db.AGENT_DB_PREFIX}{ITEM_ID}"

    # 1. la base de l'item est créée à la volée, et le rester est idempotent
    dsn = db.agent_dsn(ITEM_ID)
    assert db.agent_dsn(ITEM_ID) == dsn, "deux appels ne rendent pas la même base"
    assert nom in bases(), nom
    assert f"dbname={nom}" in dsn, dsn
    print(f"1. base jetable {nom} créée à la volée, dérivation idempotente ✓")

    # 2. sans instance jetable, rien n'est dérivé : l'agent garde sa DSN
    del os.environ["GRAPHATOM_AGENT_DSN"]
    assert db.agent_dsn(ITEM_ID + 1) is None
    assert f"{db.AGENT_DB_PREFIX}{ITEM_ID + 1}" not in bases()
    os.environ["GRAPHATOM_AGENT_DSN"] = INSTANCE
    print("2. sans instance jetable : aucune base dérivée ✓")

    # 3. le drop refuse tout ce qui n'est pas une base jetable
    vrai = db.DSN
    try:
        db.DSN = PRODUCTION
        try:
            db.drop_agent_db()
        except ValueError as err:
            print(f"3. drop refusé sur une base de production : {err} ✓")
        else:
            sys.exit("ÉCHEC : le drop a accepté une base de production")

        # 4. …et drop la sienne
        db.DSN = dsn
        assert db.drop_agent_db() == nom
        assert nom not in bases(), nom
        print(f"4. base {nom} droppée ✓")
    finally:
        db.DSN = vrai

    repo = depot(tmp)
    worktree = repo / ".worktrees" / "rail-item-42"

    # 5. la branche ne correspond pas au sujet : on ne touche à rien
    outcome = cleanup(repo, workspace, "gh:Acid3croco/graphatom#12")
    assert outcome["outcome"] == "done", outcome  # un cleanup ne bloque jamais la sortie
    assert worktree.is_dir(), "un worktree étranger au sujet a été détruit"
    assert "rail/issue-77" in git(repo, "branch", "--list", "rail/issue-77")
    assert "on ne touche à rien" in (workspace / "cleanup.md").read_text()
    print(f"5. branche ≠ sujet : worktree intact, outcome {outcome['outcome']} ✓")

    # 6. le chemin n'est pas enregistré dans ce REPO_DIR-là : idem
    etranger = tmp / "etranger"
    (etranger / ".worktrees" / "rail-item-42").mkdir(parents=True)
    outcome = cleanup(etranger, workspace, "gh:Acid3croco/graphatom#77")
    assert outcome["outcome"] == "done", outcome
    assert (etranger / ".worktrees" / "rail-item-42").is_dir(), "chemin étranger détruit"
    assert worktree.is_dir(), "le worktree du vrai dépôt a été détruit"
    print("6. chemin non enregistré dans le REPO_DIR : rien détruit ✓")

    # 7. sa vraie cible, en revanche, part bien — worktree et branche
    outcome = cleanup(repo, workspace, "gh:Acid3croco/graphatom#77")
    assert outcome["outcome"] == "done", outcome
    assert not worktree.exists(), "le worktree de l'item aurait dû partir"
    assert git(repo, "branch", "--list", "rail/issue-77") == ""
    print(f"7. cible à soi : {outcome['summary']} ✓")

    # 8. la barrière du REPO_DIR tient en une ligne par script : facile à
    # oublier en écrivant un test, et c'est un worktree de production qui paie
    for script in ("crash_test.py", "seed.py", "reconnect_test.py"):
        source = (ROOT / "tests" / script).read_text()
        assert 'os.environ["GRAPHATOM_REPO_DIR"] = str(ROOT)' in source, script
    print("8. les scripts de test épinglent GRAPHATOM_REPO_DIR sur leur dépôt ✓")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nhermétisme : OK — ni la base ni le dépôt de la production ne fuient")


if __name__ == "__main__":
    main()
