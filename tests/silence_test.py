"""Le test du chien de garde : un agent muet ne coûte plus son budget entier.

Un timeout recouvrait deux morts très différentes — la tâche qui déborde de
son budget, et le processus qui n'a jamais démarré. La première a produit, la
relancer à l'identique rebrûlerait le même budget ; la seconde n'a rien
produit, et seule la relance l'a jamais sauvée. Ce test fige les deux, du
couperet jusqu'au commentaire qui pose la question à l'humain :

  1. un agent muet est coupé à `silence_s`, bien avant son `timeout_s`
  2. un agent qui écrit régulièrement traverse `silence_s` sans être coupé —
     il ne meurt qu'à son budget total
  3. coupé sans avoir rien produit : `stalled`, et le noyau relance sur
     place jusqu'à MAX_ATTEMPTS, puis escalade
  4. coupé après avoir produit : `timed_out`, et le noyau escalade tout de
     suite — aucune relance, quel que soit le compteur
  5. le prompt d'une reprise porte l'état déjà là — le `git diff` du worktree
     de l'item et la liste des fichiers de son workspace —, y compris à la
     tentative 1 d'un passage neuf ; sans rien à reprendre, aucun bloc, et le
     bloc nomme la mort dont il reprend le travail
  6. la question d'escalade dit laquelle des deux morts s'est produite
  7. `error`, `exit_code` et `log_tail` restent au post-mortem des deux côtés

Le test ne détruit rien : il crée ses sujets et ses items, et laisse la base
en place — il peut tourner à côté d'un rail vivant.

Usage : uv run python tests/silence_test.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, github_sync as gs, graph, kernel  # noqa: E402
from graphatom.kernel import MAX_ATTEMPTS  # noqa: E402

from outils import etat, git  # noqa: E402

# hermétisme : pas d'instance jetable dans l'environnement, donc aucun bloc
# d'ici ne dérive la base d'un item qui existe pour de vrai
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

SILENCE_S = 2.0       # le silence toléré des agents factices : de quoi couper vite
BUDGET_S = 20.0       # leur budget total : assez loin pour que le couperet se voie
COURT_S = 6.0         # le budget de l'agent bavard, qui doit aller jusqu'au bout
MUET = "sleep 60"                                   # pendu : pas un octet, pas un fichier
BAVARD = "while :; do echo tic; sleep 0.4; done"    # au travail, et qui le montre
PRECOCE = "echo 'un mot, puis plus rien'; sleep 60"  # a produit, puis s'est tu
NOEUD = "travail"
ITEM_FACTICE = 0      # l'item des contextes de bloc : jamais un item de la base
ITEM_PROPRE = 1       # l'item du worktree sans travail dedans : rien à reprendre
REPO = f"test-{uuid.uuid4().hex[:8]}/silence"  # le dépôt du canal, à ce test seul
LIGNE = "l'agent en était là quand le couperet est tombé"
JOURNAL = f"avant-dernière ligne\n{LIGNE}\n"


def bundle(nom: str) -> dict:
    """Un graph minuscule : un nœud de travail à budget, un nœud d'escalade."""
    return {
        "name": nom,
        "entry": NOEUD,
        "budgets": {"escalations": 5, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {
            NOEUD: {
                "block": "ACT",
                "config": {"agent": {"cmd": "true", "prompt": "ne fais rien",
                                     "timeout_s": BUDGET_S, "silence_s": SILENCE_S}},
                "edges": {"ok": "fini"},
            },
            "escalate": {
                "block": "WAIT",
                "escalade": True,
                "config": {"question": "Le nœud est mort. On retente ?",
                           "options": ["retry", "abandon"],
                           "owner": "test", "deadline_minutes": 60},
                "edges": {"retry": NOEUD, "abandon": "abandon",
                          "expired": "abandon"},
            },
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


class FauxProc:
    """Un processus déjà mort : l'autopsie n'en lit que le code de sortie."""

    returncode = -9


class FauxConn:
    """Le bloc interroge la base pour la clé du sujet, et pour la tentative
    antérieure dont il reprendrait le travail — que le test lui donne."""

    def __init__(self, precedente: dict | None = None):
        self.precedente = precedente
        self.sql = ""

    def execute(self, sql, *args):
        self.sql = sql
        return self

    def fetchone(self) -> dict | None:
        if "FROM subject" in self.sql:
            return {"subject_key": f"gh:{REPO}#112"}
        return self.precedente


def contexte(workdir: Path, cmd: str, node: str, budget_s: float = BUDGET_S,
             attempt: int = 1, cycle: int = 1, item_id: int = ITEM_FACTICE,
             precedente: dict | None = None) -> blocks.Context:
    blocks.DATA_DIR = workdir
    spec = {"block": "ACT", "edges": {"ok": "fini"},
            "config": {"agent": {"cmd": cmd, "prompt": "fais le travail",
                                 "timeout_s": budget_s, "silence_s": SILENCE_S}}}
    return blocks.Context(
        FauxConn(precedente),
        {"id": 1, "node": node, "cycle": cycle, "attempt": attempt},
        {"id": item_id, "subject_id": 1},
        spec,
        {"name": "silence"},
    )


def worktree_factice(tmp: Path) -> Path:
    """Le worktree de l'item, avec un travail en cours dedans.

    `item_worktree` le cherche sous `$GRAPHATOM_REPO_DIR/.worktrees/
    rail-item-<N>` : le test fabrique donc les deux, et y laisse un fichier
    modifié et un fichier neuf — exactement ce qu'une tentative coupée en
    plein vol laisse derrière elle.
    """
    repo = tmp / "repo"
    worktree = repo / ".worktrees" / f"rail-item-{ITEM_FACTICE}"
    worktree.mkdir(parents=True)
    git(worktree, "init", "-q", "-b", "main")
    git(worktree, "config", "user.email", "silence@test.invalid")
    git(worktree, "config", "user.name", "silence")
    (worktree / "socle.txt").write_text("la ligne d'origine\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "socle")
    (worktree / "socle.txt").write_text("la ligne réécrite par la tentative\n")
    (worktree / "neuf.txt").write_text("un fichier que la tentative a créé\n")
    os.environ["GRAPHATOM_REPO_DIR"] = str(repo)
    worktree_propre(repo)
    return worktree


def worktree_propre(repo: Path) -> Path:
    """Le worktree d'un autre item, celui-là sans rien dedans.

    Même dépôt de référence, même convention de nom : un worktree commité et
    propre, dont le workspace n'a jamais rien reçu. C'est le cas où il n'y a
    rien à reprendre, et où aucun bloc ne doit être posé.
    """
    worktree = repo / ".worktrees" / f"rail-item-{ITEM_PROPRE}"
    worktree.mkdir(parents=True)
    git(worktree, "init", "-q", "-b", "main")
    git(worktree, "config", "user.email", "silence@test.invalid")
    git(worktree, "config", "user.name", "silence")
    (worktree / "socle.txt").write_text("la ligne d'origine\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "socle")
    return worktree


def dernier_event(conn, item_id: int) -> dict:
    return conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def runs_du_noeud(conn, item_id: int, node: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM node_run WHERE item_id = %s AND node = %s",
        (item_id, node),
    ).fetchone()["n"]


def item_neuf(conn, revision: str, prefixe: str) -> int:
    return kernel.admit(
        conn, revision, f"{prefixe}:{uuid.uuid4().hex[:8]}", _allow_parallel_for_test=True
    )


def route(conn, item_id: int, outcome: str, attempt: int) -> str:
    """Route une issue noyau sur le nœud courant, à un compteur donné."""
    item = etat(conn, item_id)
    paquet = graph.load_bundle(conn, item["revision"])
    run = {"node": item["state"], "id": None, "attempt": attempt}
    with conn.transaction():
        kernel._route(conn, item, paquet, run, outcome, kind="result")
    return etat(conn, item_id)["state"]


def post_mortem(post: dict, outcome: str) -> None:
    """Le post-mortem conservé : les trois champs, quelle que soit la mort."""
    assert post["outcome"] == outcome, post
    assert post["timeout"] is True, post
    assert "TimeoutExpired" in post["error"], post
    assert post["exit_code"] < 0, post  # négatif : le signal qui l'a tué
    assert set(post) >= {"error", "exit_code", "log_tail"}, set(post)


def couperet_du_silence(workdir: Path) -> None:
    """1. un agent muet est coupé à `silence_s`, pas à `timeout_s`."""
    depart = time.monotonic()
    result = blocks.act(contexte(workdir, MUET, node=f"{NOEUD}-muet"))
    mort = time.monotonic() - depart

    assert result["outcome"] == "stalled", result
    assert mort < BUDGET_S, f"le budget total a tranché ({mort:.1f} s), pas le silence"
    assert mort >= SILENCE_S, mort
    print(f"1. agent muet : coupé après {mort:.1f} s — silence {SILENCE_S} s, "
          f"budget {BUDGET_S} s jamais atteint ✓")


def agent_bavard(workdir: Path) -> None:
    """2. écrire régulièrement traverse le silence : seul le budget tranche."""
    depart = time.monotonic()
    result = blocks.act(
        contexte(workdir, BAVARD, node=f"{NOEUD}-bavard", budget_s=COURT_S))
    mort = time.monotonic() - depart

    assert mort >= COURT_S, f"le chien de garde a mordu à {mort:.1f} s"
    assert mort > SILENCE_S * 2, mort  # il a traversé le silence plus d'une fois
    assert result["outcome"] == "timed_out", result
    print(f"2. agent bavard : {SILENCE_S} s de silence toléré traversés, mort à "
          f"{mort:.1f} s sur son budget de {COURT_S} s ✓")


def regle_du_progres() -> None:
    """3 et 4, la règle nue : des octets au journal, ou un mtime qui a bougé.

    Le journal se juge sur son contenu, pas sur sa croissance. Un agent qui
    écrit sa première ligne avant que le relevé de départ soit pris a produit
    — et le relevé de départ est pris juste après le lancement, donc c'est
    une course que l'ordonnanceur arbitre. La juger sur le delta rendrait
    l'issue tirée au sort entre `stalled` et `timed_out` ; la juger sur les
    octets présents la rend mécanique.
    """
    depart = (0, 10.0, 20.0)
    assert blocks._progress(depart, (0, 10.0, 20.0)) is False, "rien n'a bougé"
    assert blocks._progress(depart, (7, 10.0, 20.0)) is True, "des octets au journal"
    assert blocks._progress(depart, (0, 11.0, 20.0)) is True, "un fichier du workspace"
    assert blocks._progress(depart, (0, 10.0, 21.0)) is True, "un fichier du worktree"
    # la course : le journal était déjà non vide au relevé de départ
    assert blocks._progress((7, 10.0, 20.0), (7, 10.0, 20.0)) is True, "journal non vide"
    print("3-4 bis. _progress : 0 octet et aucun mtime bougé, et rien d'autre, "
          "vaut une pendaison ✓")


def deux_morts(workdir: Path) -> None:
    """3, 4 et 7. le progrès constaté fait l'issue, le post-mortem reste entier."""
    pendu = blocks.act(contexte(workdir, MUET, node=f"{NOEUD}-pendu"))
    log = blocks.attempt_log(blocks.item_workspace(ITEM_FACTICE),
                             {"node": f"{NOEUD}-pendu", "cycle": 1, "attempt": 1})
    trace = blocks.attempt_command(
        blocks.item_workspace(ITEM_FACTICE),
        {"node": f"{NOEUD}-pendu", "cycle": 1, "attempt": 1},
    )
    assert log.stat().st_size == 0, "le pendu a écrit quelque chose ?"
    assert json.loads(trace.read_text())["command"] == MUET
    post_mortem(pendu, "stalled")
    print(f"3. commande tracée, journal à 0 octet → {pendu['outcome']} ✓")

    # un mot écrit puis le silence : le chien de garde coupe aussi, mais
    # l'agent avait démarré — c'est un débordement, pas une pendaison
    depart = time.monotonic()
    produit = blocks.act(contexte(workdir, PRECOCE, node=f"{NOEUD}-precoce"))
    mort = time.monotonic() - depart
    assert mort < BUDGET_S, mort  # bien le chien de garde, pas le budget
    post_mortem(produit, "timed_out")
    assert "un mot" in produit["log_tail"], produit
    print(f"4. journal non vide → {produit['outcome']} après {mort:.1f} s, "
          "coupé par le même chien de garde ✓")

    attendu = {"outcome", "error", "timeout", "exit_code", "log_tail"}
    for post in (pendu, produit):
        assert set(post) == attendu, set(post)
    print(f"7. post-mortem identique des deux côtés : {sorted(attendu)} ✓")


def relances_et_escalade(conn, revision: str) -> tuple[int, int]:
    """3 bis et 4 bis. `stalled` relance sur place, `timed_out` escalade."""
    # cet item-là est le sujet d'une issue : sa question sera lue au point 6
    pendu = kernel.admit(conn, revision, f"gh:{REPO}#112", _allow_parallel_for_test=True)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        run = kernel.claim(conn, pendu)
        assert run["attempt"] == attempt, run
        kernel.apply(conn, run["id"], {
            "outcome": "stalled", "timeout": True, "exit_code": -9,
            "error": "TimeoutExpired: Command 'chien de garde : ni un octet ni un "
                     "fichier touché' timed out after 2.0 seconds",
            "log_tail": "",
        })
        attendu = NOEUD if attempt < MAX_ATTEMPTS else "escalate"
        assert etat(conn, pendu)["state"] == attendu, (attempt, etat(conn, pendu))
    assert runs_du_noeud(conn, pendu, NOEUD) == MAX_ATTEMPTS, "relances perdues"
    assert dernier_event(conn, pendu)["outcome"] == "stalled"
    print(f"3 bis. item {pendu} : {MAX_ATTEMPTS} relances sur place, puis escalade ✓")

    for attempt in (0, 1, MAX_ATTEMPTS, MAX_ATTEMPTS + 1):
        autre = item_neuf(conn, revision, "stalled-compteur")
        cible = NOEUD if attempt < MAX_ATTEMPTS else "escalate"
        assert route(conn, autre, "stalled", attempt) == cible, attempt

    deborde = kernel.admit(conn, revision, f"gh:{REPO}#113", _allow_parallel_for_test=True)
    run = kernel.claim(conn, deborde)
    assert run["attempt"] == 1, run
    kernel.apply(conn, run["id"], {
        "outcome": "timed_out", "timeout": True, "exit_code": -9,
        "error": "TimeoutExpired: Command 'claude …' timed out after 20.0 seconds",
        "log_tail": JOURNAL,
    })
    assert etat(conn, deborde)["state"] == "escalate", etat(conn, deborde)
    assert runs_du_noeud(conn, deborde, NOEUD) == 1, "le nœud a été rouvert"
    print(f"4 bis. item {deborde} : timed_out dès la tentative 1 → escalate, "
          "aucune relance ✓")
    return pendu, deborde


def prompt_de_reprise(workdir: Path, worktree: Path) -> None:
    """5. une reprise repart de l'état laissé, jamais d'une page blanche.

    La reprise ne se décide pas sur le numéro de la tentative : un `retry`
    d'escalade ouvre un passage neuf, donc une tentative 1, sur le worktree
    que le passage d'avant a rempli. Ce qui se demande, c'est s'il y a
    quelque chose à reprendre.
    """
    ctx = contexte(workdir, "true", node=NOEUD, attempt=1, cycle=2,
                   precedente={"cycle": 1, "attempt": 1, "outcome": "timed_out"})
    workspace = ctx.workspace.resolve()
    (workspace / "implementation.md").write_text("ce que la tentative 1 a écrit\n")
    debordee = blocks._prompt(ctx, workspace, f"gh:{REPO}#112")

    assert "Reprise de la tentative 1 du passage 1" in debordee, debordee
    assert "ne recommence pas de zéro" in debordee, debordee
    # le git diff du worktree : le fichier modifié, sa ligne, et le fichier neuf
    assert str(worktree) in debordee, debordee
    assert "$ git status --short" in debordee, debordee
    assert "$ git diff HEAD" in debordee, debordee
    assert "-la ligne d'origine" in debordee, debordee
    assert "+la ligne réécrite par la tentative" in debordee, debordee
    assert "?? neuf.txt" in debordee, debordee
    # et la liste des fichiers déjà écrits dans le workspace
    assert "- implementation.md" in debordee, debordee
    print("5. tentative 1 d'un passage neuf, worktree sale : le prompt porte "
          "git status, git diff et les fichiers du workspace ✓")

    # rien dans le worktree, rien dans le workspace : rien à reprendre
    vierge = contexte(workdir, "true", node=NOEUD, attempt=1, cycle=2,
                      item_id=ITEM_PROPRE,
                      precedente={"cycle": 1, "attempt": 1, "outcome": "timed_out"})
    blocks.attempt_command(vierge.workspace.resolve(), {
        "node": NOEUD, "cycle": 1, "attempt": 1,
    }).write_text('{"kind":"shell","executor":null,"command":"true"}\n')
    rien = blocks._prompt(vierge, vierge.workspace.resolve(), f"gh:{REPO}#112")
    assert "Reprise" not in rien, rien
    print("5 bis. worktree propre et workspace vide : aucun bloc de reprise — "
          "une reprise ne s'invente pas ✓")

    # la mort d'avant est nommée, et les deux morts ne se disent pas pareil
    pendue = blocks._prompt(
        contexte(workdir, "true", node=NOEUD, attempt=2, cycle=2,
                 precedente={"cycle": 2, "attempt": 1, "outcome": "stalled"}),
        workspace, f"gh:{REPO}#112")
    assert "dépassé son budget" in debordee, debordee
    assert "pendue" not in debordee, debordee
    assert "est restée pendue" in pendue, pendue
    assert "dépassé son budget" not in pendue, pendue
    assert pendue != debordee, "les deux morts donnent le même prompt"
    print("5 ter. le motif est nommé : budget dépassé d'un côté, pendaison de "
          "l'autre — deux textes distincts ✓")


def questions_d_escalade(conn, pendu: int, deborde: int) -> None:
    """6. le commentaire dit laquelle des deux morts a escaladé."""
    gh = gs.GitHub(REPO, "jeton")  # aucun appel sortant : on ne construit que le corps
    corps = {}
    for q in gs._gh_questions(conn, gh):
        if q["item_id"] in (pendu, deborde):
            corps[q["item_id"]] = gs._question_body(conn, q, "http://127.0.0.1:9")
    assert set(corps) == {pendu, deborde}, sorted(corps)

    assert gs.HUNG_TITLE in corps[pendu], corps[pendu]
    assert gs.TIMEOUT_TITLE not in corps[pendu], corps[pendu]
    assert "sans avoir écrit un octet" in corps[pendu], corps[pendu]
    assert f"{MAX_ATTEMPTS} tentatives" in corps[pendu], corps[pendu]

    assert gs.TIMEOUT_TITLE in corps[deborde], corps[deborde]
    assert gs.HUNG_TITLE not in corps[deborde], corps[deborde]
    assert f"`timeout_s` de {BUDGET_S:.0f} s" in corps[deborde], corps[deborde]
    assert LIGNE in corps[deborde], corps[deborde]
    print("6. deux questions, deux messages : pendaison sans progrès d'un côté, "
          "budget dépassé de l'autre ✓")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-silence-"))
    workdir = tmp / "data"
    workdir.mkdir()
    blocks.DATA_DIR = workdir  # les blocs écrivent là, jamais dans data/
    repo_dir = os.environ.get("GRAPHATOM_REPO_DIR")
    worktree = worktree_factice(tmp)
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    try:
        couperet_du_silence(workdir)
        agent_bavard(workdir)
        regle_du_progres()
        deux_morts(workdir)
        prompt_de_reprise(workdir, worktree)
        with db.connect() as conn:
            revision = graph.publish(conn, bundle(f"silence-{REPO[5:13]}"))
            pendu, deborde = relances_et_escalade(conn, revision)
            questions_d_escalade(conn, pendu, deborde)
    finally:
        if repo_dir is None:
            os.environ.pop("GRAPHATOM_REPO_DIR", None)
        else:
            os.environ["GRAPHATOM_REPO_DIR"] = repo_dir
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nchien de garde : OK — un pendu se relance, un débordement escalade")


if __name__ == "__main__":
    main()
