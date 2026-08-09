"""Le test des portes déterministes d'un candidat d'`implement`.

`implement` est une course : trois candidats concurrents, et `keep_n`
garde deux finalistes dont les issues de succès ont passé leurs portes. Sans
porte, on sélectionnerait les plus rapides à *prétendre*, pas les plus
corrects. Le `cmd` du nœud lance donc `scripts/portes.sh` après l'agent, et
retire son `outcome.json` quand une porte lâche. Le scénario réduit la course
par `first_pass` pour pouvoir trancher avec un seul candidat sain.

Les portes sont celles de tout le monde, le modèle n'y change rien : deux
candidats passent par `scripts/agent-codex.sh`, le troisième par
`scripts/agent-opencode.sh` sur un modèle gratuit, et le même jeu de portes
tranche les trois.

Le `cmd` joué ici est celui d'`examples/code-task.json`, tel quel : seule la
ligne d'appel du modèle — Codex pour deux stratégies, OpenCode pour la
variante gratuite — est remplacée par un agent factice qui écrit dans
l'atelier du candidat ce que le test veut y voir. Tout ce qui suit — les
portes et le retrait de l'issue — est le vrai.

Scénario, sur la base et sur une copie jetable du dépôt :

  1. les trois variantes donnent trois prompts différents, chacun portant
     sa stratégie ; aucun `{label}` ni `{strategy}` littéral ne survit au
     rendu
  2. le candidat qui casse un module du paquet : la porte d'import lâche,
     son `outcome.json` disparaît, et son `node_run` porte `crashed` — pas
     l'issue de succès du nœud. Le candidat gratuit y passe comme les
     autres : sa commande n'est pas la leur, ses portes le sont
  3. le candidat qui casse un test concerné par son diff : la porte de la
     suite lâche de la même façon, après que la sélection l'a choisie
  4. le candidat sain passe ses portes, garde son `outcome.json`, et gagne
     la course ; son diff sans code ne fait jouer que la porte d'import
  5. aucun test de porte ne parle à une base de données : le script coupe les
     DSN d'entrée. Seul le fournisseur du quota garde la base commune, pour
     son verrou de session et sans lire les données d'un item

Le test ne détruit rien de la production : son dépôt est une copie jetable,
et `GRAPHATOM_REPO_DIR` est épinglé dessus.

Usage : uv run python tests/portes_test.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, graph, kernel, scheduler  # noqa: E402

from outils import git  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REEL = json.loads((ROOT / "examples" / "code-task.json").read_text())["nodes"]["implement"]
VARIANTES = REEL["config"]["fanout"]["variants"]

# hermétisme : les agents d'ici sont des scripts shell, ils n'ont pas de base
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

# ce que la copie du dépôt n'emporte pas : la machinerie, jamais le travail
INUTILE = shutil.ignore_patterns(".git", ".venv", "node_modules", ".next",
                                 "__pycache__", "data", ".worktrees", "*.egg-info")

# L'agent factice, à la place du seul appel de modèle du `cmd`. Le numéro du
# candidat se lit sur son workspace, comme partout ailleurs ; c'est le test
# qui lui impose son geste, pas la stratégie de sa variante.
#
#   c0 — une ligne de documentation : rien de cassé, et un diff sans code
#   c1 — un module du paquet rendu illisible : la porte d'import doit tomber
#   c2 — le candidat gratuit rend un test illisible : sa commande passe par
#        l'adaptateur opencode, ses portes non
#
# Les trois se déclarent finis : c'est aux portes de les départager.
FAUX_AGENT = """WS=$(pwd)
K=$(basename "$WS"); K=${K#c}
printf '%s\\n' '{"type":"result","result":"agent factice",\
"usage":{"input_tokens":7},"total_cost_usd":0.0}' > agent.jsonl
printf '%s\\n' '{"input_tokens":7,"total_cost_usd":0.0}' > usage.json
case "$K" in
  0) printf '\\nUne ligne de documentation, et rien de plus.\\n' \
>> "$GRAPHATOM_WORKTREE/README.md" ;;
  1) printf '\\ndef casse(:\\n' >> "$GRAPHATOM_WORKTREE/src/graphatom/channel.py" ;;
  2) printf '\\ndef casse(:\\n' >> "$GRAPHATOM_WORKTREE/tests/validate_test.py" ;;
esac
git -C "$GRAPHATOM_WORKTREE" add -A
git -C "$GRAPHATOM_WORKTREE" commit -qm "candidat $K"
printf '{"outcome": "done", "summary": "candidat %s : je me déclare fini"}' "$K" \
> outcome.json
RC=0"""

# La ligne d'appel du modèle, dans le `cmd` d'un candidat : codex pour deux
# stratégies, opencode pour la variante gratuite. C'est elle, et elle seule,
# que l'agent factice remplace.
APPEL = re.compile(r"^(?:claude |OPENCODE_\w+=|CODEX_\w+=).*$", re.M)


def cmd_teste(cmd: str) -> str:
    """Un `cmd` réel d'`implement`, son seul appel de modèle remplacé.

    Le remplacement passe par une fonction et non par une chaîne : une
    chaîne de remplacement relit les échappements, et le shell en porte.
    """
    cmd, n = APPEL.subn(lambda _: FAUX_AGENT, cmd, count=1)
    assert n == 1, f"ce `cmd` d'`implement` n'appelle plus de modèle : {cmd[:120]}"
    assert "portes.sh" in cmd, "ce `cmd` d'`implement` ne lance plus ses portes"
    return cmd


def bundle_portes() -> dict:
    """Le graph de la course : le nœud `implement` réel, ses arêtes raccourcies."""
    node = json.loads(json.dumps(REEL))  # une copie : le nœud réel ne bouge pas
    node["config"]["agent"]["cmd"] = cmd_teste(node["config"]["agent"]["cmd"])
    for variante in node["config"]["fanout"]["variants"]:
        # une variante qui joue sa propre commande — la gratuite — se fait
        # remplacer la sienne : sans ça, elle appellerait un vrai modèle
        propre = (variante.get("agent") or {}).get("cmd")
        if propre is not None:
            variante["agent"]["cmd"] = cmd_teste(propre)
    node["config"]["fanout"]["reduce"] = "first_pass"
    node["config"]["fanout"].pop("n", None)
    node["config"]["agent"]["timeout_s"] = 300  # l'agent est factice, les portes non
    node["config"]["lease_s"] = 600
    node["edges"] = {"done": "fini"}
    return {
        "name": "portes-test",
        "entry": "implement",
        "budgets": {"escalations": 2, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {
            "implement": node,
            "escalate": {
                "block": "WAIT", "escalade": True,
                "config": {"question": "On retente ?", "options": ["retry"],
                           "owner": "test", "deadline_minutes": 60},
                "edges": {"retry": "implement", "expired": "abandon"},
            },
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


# ------------------------------------------------------------------ outillage


def depot(tmp: Path) -> Path:
    """Une copie jetable du dépôt, avec son `origin/main`.

    Les portes jouent de vrais `uv run` sur de vrais tests : il faut donc un
    vrai checkout du projet, pas un dépôt d'une ligne. La référence
    `origin/main` est posée à la main — c'est elle que le calcul du diff
    interroge, et un dépôt jetable n'a pas de remote.
    """
    repo = tmp / "repo"
    shutil.copytree(ROOT, repo, ignore=INUTILE, symlinks=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "portes@test.invalid")
    git(repo, "config", "user.name", "portes")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "socle")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    os.environ["GRAPHATOM_REPO_DIR"] = str(repo)
    return repo


def nouvel_item(conn, repo: Path) -> int:
    """Un item, son atelier, sa branche — ce que le shell du graph préparerait."""
    rev = graph.publish(conn, bundle_portes())
    item_id = kernel.admit(conn, rev, f"portes:{uuid.uuid4().hex[:8]}")
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / f"rail-item-{item_id}"),
        "-b", f"rail/issue-{item_id}")
    return item_id


def runs_de(conn, item_id: int) -> dict[int, dict]:
    rows = conn.execute("SELECT * FROM node_run WHERE item_id = %s", (item_id,)).fetchall()
    return {r["candidate"]: r for r in rows}


# -------------------------------------------------------------------- épreuves


def course(conn, workdir: Path, repo: Path) -> None:
    """1 à 4 : trois candidats, deux recalés par leurs portes, un gagnant."""
    item_id = nouvel_item(conn, repo)
    runs = {}
    while (run := kernel.claim(conn, item_id)) is not None:
        runs[run["candidate"]] = run
        assert len(runs) <= graph.FANOUT_MAX_CANDIDATES, "claim ne s'arrête plus"
    tous = list(range(len(VARIANTES)))
    assert sorted(runs) == tous, f"{len(VARIANTES)} variantes, un candidat chacune : {sorted(runs)}"

    # les recalés d'abord, le sain en dernier : un succès révoque ses frères
    # encore en vol, et on veut lire ce que les portes ont fait des leurs
    for k in tous[1:] + [0]:
        scheduler._execute(runs[k]["id"], item_id)

    workspace = workdir / f"item-{item_id}"
    prompts = {k: (workspace / f"c{k}" / "prompt-implement-1-1.md").read_text()
               for k in tous}
    portes = {k: (workspace / f"c{k}" / "portes.md").read_text() for k in tous}
    finaux = runs_de(conn, item_id)

    # 1. un prompt par candidat, une stratégie chacun, aucun jeton resté littéral
    assert len(set(prompts.values())) == len(tous), "deux candidats ont reçu le même prompt"
    for k, variante in enumerate(VARIANTES):
        assert variante["strategy"] in prompts[k], \
            f"c{k} : sa stratégie manque à son prompt — {prompts[k][:300]}"
        assert variante["label"] in prompts[k], f"c{k} : son label manque à son prompt"
        for jeton in ("{label}", "{strategy}", "{subject_key}"):
            assert jeton not in prompts[k], f"c{k} : {jeton} est resté littéral"
        for autre in tous:  # chacun porte la sienne, pas celle d'un voisin
            assert autre == k or VARIANTES[autre]["strategy"] not in prompts[k], \
                f"c{k} porte aussi la stratégie de c{autre}"
    print(f"1. {len(tous)} prompts distincts, chacun avec sa stratégie et son label, "
          f"aucun jeton littéral : {[v['label'] for v in VARIANTES]} ✓")

    # 2 et 3. les recalés : pas d'outcome.json, pas d'issue de succès
    for point, k, porte, casse in ((2, 1, "import", "src/graphatom/channel.py"),
                                   (3, 2, "tests/validate_test.py",
                                    "tests/validate_test.py")):
        cw = workspace / f"c{k}"
        assert not (cw / blocks.OUTCOME_NAME).exists(), \
            f"c{k} garde un outcome.json alors que ses portes ont lâché"
        assert f"ÉCHEC — porte « {porte} »" in portes[k], portes[k][-600:]
        run = finaux[k]
        assert run["outcome"] not in (REEL["edges"] or {}), \
            f"c{k} a rendu l'issue de succès du nœud : {run['outcome']}"
        assert run["outcome"] in graph.KERNEL_OUTCOMES, run["outcome"]
        assert run["outcome"] == "crashed", run["outcome"]
        assert (run["result"] or {}).get("usage"), "ses jetons comptent quand même"
        print(f"{point}. c{k} « {VARIANTES[k]['label']} » casse {casse} : porte "
              f"« {porte} » lâchée, outcome.json retiré, "
              f"node_run « {run['outcome']} » ✓")

    # 4. le sain passe, garde son issue, et gagne
    gagnant = finaux[0]
    assert gagnant["status"] == "applied" and gagnant["outcome"] == "done", gagnant
    issue = json.loads((workspace / "c0" / blocks.OUTCOME_NAME).read_text())
    assert issue["outcome"] == "done", issue
    assert "PORTES OK" in portes[0], portes[0]
    assert "suite de tests non concernée" in portes[0], portes[0]
    item = conn.execute("SELECT * FROM work_item WHERE id = %s", (item_id,)).fetchone()
    assert item["state"] == "fini", item["state"]
    assert all(finaux[k]["status"] == "applied" for k in tous), finaux
    print(f"4. c0 passe ses portes (diff sans code : import seul), garde son "
          f"outcome.json, et l'item part sur « {item['state']} » ✓")


def portes_sans_base() -> None:
    """5. Les tests sont sans base ; seul le fournisseur garde son DSN."""
    source = (ROOT / "scripts" / "portes.sh").read_text()
    assert "unset GRAPHATOM_DSN GRAPHATOM_AGENT_DSN" in source, \
        "le script ne coupe plus les DSN de l'agent"
    assert "GRAPHATOM_QUOTA_DSN" in source and "build-quota" in source, \
        "la suite lourde ne passe plus par la base de contrôle du quota"
    assert 'export GRAPHATOM_REPO_DIR="$WT"' in source, \
        "le script ne pointe plus le dépôt du candidat sur son propre atelier"

    bloc = re.search(r'^TESTS="(.*?)"$', source, re.M | re.S)
    assert bloc, "le script ne nomme plus sa liste de tests"
    choisies = bloc.group(1).split()
    assert len(choisies) >= 5, choisies
    assert "tests/crash_test.py" not in choisies, \
        "crash_test.py détruit la base nommée par GRAPHATOM_DSN : jamais en porte"
    for nom in choisies:
        chemin = ROOT / nom
        assert chemin.is_file(), f"{nom} : porte nommée, test introuvable"
        code = chemin.read_text()
        for ouvre in ("db.connect(", "db.init_db(", "psycopg.connect("):
            assert ouvre not in code, f"{nom} ouvre une base ({ouvre}) : pas une porte"
    print(f"5. {len(choisies)} portes de test sans base, DSN de données coupées, "
          "fournisseur du quota présent et dépôt épinglé sur l'atelier ✓")


def main() -> None:
    portes_sans_base()
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-portes-"))
    blocks.DATA_DIR = workdir
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-portes-repo-"))
    try:
        repo = depot(tmp)
        with db.connect() as conn:
            course(conn, workdir, repo)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nportes : OK — un candidat ne gagne qu'après avoir prouvé, "
          "et ses portes ne gênent aucun de ses frères")


if __name__ == "__main__":
    main()
