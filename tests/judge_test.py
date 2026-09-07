"""Le test du nœud arbitre : il choisit entre des finalistes, et rien d'autre.

`keep_n` laisse passer plusieurs candidats au lieu d'en élire un ; le nœud
`judge` les départage. Trois issues fermées, et la première est la plus
importante — un finaliste unique ne coûte pas un jeton.

Scénario, sur la base et sur un dépôt git jetable — de vrais candidats qui
committent vraiment dans leur atelier, et un faux modèle qui rend un verdict :

  1. un seul finaliste → issue `sole`, et **aucun appel de modèle** : pas de
     journal d'agent, pas de prompt, pas d'usage, pas un jeton en base ;
     le travail de l'unique finaliste est quand même promu
  2. plusieurs finalistes → issue `chosen` : le verdict du workspace nomme
     l'élu et la raison, le travail de l'élu est sur la branche de l'item,
     celui de l'autre non, et plus un atelier ni une branche de candidat
  3. le juge n'écrit jamais chez les candidats : `HEAD` et `git status` de
     chaque atelier sont inchangés entre le début et la fin de son run
  4. le prompt rendu du juge ne porte ni modèle, ni CLI, ni `label`, ni
     `strategy` : seulement les diffs, sous des lettres
  5. zéro finaliste → issue `none`, retour sur le nœud d'amont, et le budget
     d'escalade se débite comme sur un échec de `validate` : la première
     entrée du passage est gratuite, la re-entrée décompte

Le test ne détruit rien de la production : son dépôt est jetable, et
`GRAPHATOM_REPO_DIR` est épinglé dessus.

Usage : uv run python tests/judge_test.py
"""

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, kernel, views  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

from outils import admettre, ancetre, atelier_item, attendre  # noqa: E402
from outils import bundle_epreuve, data_dir_jetable, depot_git, etat  # noqa: E402
from outils import executer, git, rejoindre, reserver_tout, runs_de  # noqa: E402

# hermétisme : les agents d'ici sont des scripts shell, ils n'ont pas de base
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

# Un candidat commite son travail dans son atelier, puis réussit ou rate. Un
# raté ne rend pas d'issue lisible : c'est `crashed`, une issue du noyau, donc
# un échec — et non une arête du nœud, qui serait une réussite. Le numéro se
# lit sur son workspace, comme partout.
CANDIDAT = """
set -e
k=$(basename "$(pwd)"); k=${k#c}
WT="$GRAPHATOM_WORKTREE"
printf 'travail marqué {marqueur} %s\\n' "$(date +%s%N)" >> "$WT/c$k.txt"
git -C "$WT" add -A
git -C "$WT" commit -qm "un candidat a travaillé"
git -C "$WT" rev-parse HEAD > commit.txt
printf '{"input_tokens": 10, "total_cost_usd": 0.10}' > usage.json
"""
REUSSIT = """
printf '%s\\n' '## Fait' 'Travail committé.' '' '## Appris' 'Rien.' '' \\
    '## Pas fait' 'Rien.' > passation-travail.md
printf '{"outcome": "ok", "summary": "candidat %s"}' "$k" > outcome.json
"""
RATE = "exit 3\n"  # pas d'outcome.json : la tentative est classée `crashed`

# Ce que chaque candidat laisse dans son diff : un mot à lui, qui ne dit ni son
# angle ni son numéro. C'est ce que le juge doit voir — le travail — quand tout
# le reste doit lui rester invisible.
MARQUEURS = ["alizé", "brise", "cyclone"]

# Le faux modèle du juge : il garde son prompt sous la main, écrit son verdict
# et élit la lettre que le test lui a mise dans la config. Entre les deux il
# attend le signal du test — c'est cette fenêtre qui laisse photographier les
# ateliers des candidats pendant que le juge travaille, avant toute promotion.
JUGE = """
cp prompt.md prompt-vu.md
printf 'Comparaison des finalistes, faite par un faux modèle.\\n' > verdict.md
printf '%s\\n' '## Fait' 'Finalistes départagés.' '' '## Appris' 'Rien.' '' \\
    '## Pas fait' 'Rien.' > passation-juge.md
printf '{"input_tokens": 700, "total_cost_usd": 2.50}' > usage.json
while [ ! -f go ]; do sleep 0.05; done
printf '{"outcome": "chosen", "elu": "{elu}", "summary": "{raison}"}' > outcome.json
"""

MODELE = "opus-de-competition"  # le nom que le juge ne doit jamais lire
RAISON = "le diff tient les critères en moins de lignes"


def bundle(issues: list[str], elu: str = "B", keep: int = 2) -> dict:
    """Le graph du jugement : un ACT en keep_n, un JUDGE arbitre derrière.

    Une variante par issue attendue : c'est elle qui décide si le candidat
    réussit ou rate, et donc combien de finalistes le juge recevra.
    """
    variantes = [
        {"label": f"angle-{i}", "strategy": f"stratégie numéro {i}",
         "execution": {"cmd": (CANDIDAT.replace("{marqueur}", MARQUEURS[i])
                               + (REUSSIT if issue == "ok" else RATE))}}
        for i, issue in enumerate(issues)]
    return bundle_epreuve(f"judge-{'-'.join(issues)}-{elu}-{keep}", {
        "travail": {
            "block": "ACT",
            "config": {
                "lease_s": 300,  # le faucheur n'a rien à faire ici
                "execution": {"kind": "agent", "cmd": CANDIDAT + REUSSIT,
                              "timeout_s": 60, "silence_s": 60},
                "agent": {"prompt": "Angle {label} : {strategy}.",
                          "cli": "codex"},
                "fanout": {"variants": variantes, "reduce": "keep_n", "n": keep},
            },
            "edges": {"ok": "juge"},
        },
        "juge": {
            "block": "JUDGE",
            "escalade": True,
            "config": {
                "lease_s": 300,
                "finalists_from": "travail",
                "execution": {
                    "kind": "agent",
                    "cmd": (f"# modèle {MODELE}\n"
                            + JUGE.replace("{elu}", elu).replace("{raison}", RAISON)),
                    "timeout_s": 60, "silence_s": 60,
                },
                "agent": {
                    "prompt": "Départage les finalistes de {subject_key}.",
                    "cli": "codex",
                },
            },
            "edges": {"sole": "fini", "chosen": "fini", "none": "travail"},
        },
    }, escalations=3)


# ------------------------------------------------------------------ outillage


def nouvel_item(conn, repo: Path, spec: dict) -> tuple[int, str, Path]:
    """Un item, sa branche, son atelier — ce que le shell du graph préparerait."""
    item_id = admettre(conn, spec, "judge")
    branche, item_wt = atelier_item(repo, item_id)
    return item_id, branche, item_wt


def courir(conn, item_id: int) -> list[dict]:
    """Réserve la tentative entière et la joue jusqu'au bout, comme l'ordonnanceur."""
    runs = reserver_tout(conn, item_id)
    rejoindre(executer(runs, item_id), 90)
    return runs


def lancer_juge(conn, item_id: int) -> tuple[int, threading.Thread]:
    """Réserve le run du juge et le lance, sans attendre qu'il rende.

    Le juge n'est pas en fan-out : un seul run. Le laisser courir donne la
    fenêtre où l'on peut regarder les ateliers des candidats pendant qu'il
    travaille — c'est là, et pas après, que se voit ce qu'il n'écrit pas.
    """
    run = kernel.claim(conn, item_id)
    assert run is not None and run["candidate"] is None, run
    fil, = executer([run], item_id)
    return run["id"], fil


def photo(ateliers: dict[int, Path]) -> dict[int, tuple[str, str]]:
    """L'état de chaque atelier de candidat : sa tête, et ce qui n'est pas commité."""
    return {k: (git(chemin, "rev-parse", "HEAD"), git(chemin, "status", "--porcelain"))
            for k, chemin in ateliers.items() if chemin.is_dir()}


def run_du_juge(conn, run_id: int) -> dict:
    """La ligne du run du juge, telle que la base la garde."""
    return conn.execute("SELECT * FROM node_run WHERE id = %s", (run_id,)).fetchone()


# -------------------------------------------------------------------- épreuves


def sole(conn, workdir: Path, repo: Path) -> None:
    """1. un finaliste unique : traversé sans un jeton, et promu quand même."""
    item_id, branche, _ = nouvel_item(conn, repo, bundle(["ok", "crash"]))
    workspace = workdir / f"item-{item_id}"
    courir(conn, item_id)

    assert etat(conn, item_id)["state"] == "juge", etat(conn, item_id)["state"]
    commit = (workspace / "c0" / "commit.txt").read_text().strip()

    juge = courir(conn, item_id)
    assert len(juge) == 1 and juge[0]["candidate"] is None, juge
    run = run_du_juge(conn, juge[0]["id"])
    assert run["outcome"] == "sole", run["outcome"]
    assert etat(conn, item_id)["state"] == "fini", etat(conn, item_id)["state"]

    # aucun appel de modèle : ni journal d'agent, ni prompt, ni usage, ni coût
    traces = sorted(p.name for p in workspace.iterdir()
                    if p.name.startswith(("agent-juge", "prompt-juge", "usage-juge")))
    assert not traces, f"le court-circuit a lancé un agent : {traces}"
    assert "usage" not in (run["result"] or {}), run["result"]
    assert views.totals([run]) == {}, views.totals([run])
    assert views.totals(runs_de(conn, item_id, "juge")) == {}, "le nœud juge coûte zéro"
    print(f"1. item {item_id} : un finaliste → issue « sole », aucune trace d'agent "
          f"({', '.join(p.name for p in sorted(workspace.iterdir()))}), coût nul ✓")

    verdict = (workspace / bureau.VERDICT_NAME).read_text()
    assert "finaliste A" in verdict and "seul" in verdict, verdict
    assert "aucun modèle" in verdict, verdict
    assert ancetre(repo, commit, branche), "le travail du finaliste unique est promu"
    assert git(repo, "branch", "--list", f"{branche}-c*") == "", "branches survivantes"
    print(f"   {bureau.VERDICT_NAME} écrit, travail de l'unique finaliste promu, "
          "ateliers rangés ✓")


def finalistes_de(conn, item_id: int) -> list[int]:
    """Les finalistes de `keep_n`, dans l'ordre où ils ont fini.

    C'est aussi celui des lettres que le juge a lues — A au premier terminé,
    B au second.
    """
    finalistes = [r["candidate"] for r in conn.execute(
        "SELECT candidate FROM node_run WHERE item_id = %s AND node = 'travail' "
        "AND status = 'applied' ORDER BY finished_at, id", (item_id,)).fetchall()]
    assert len(finalistes) == 2, f"keep_n devait en laisser deux : {finalistes}"
    return finalistes


def ateliers_intacts(conn, workspace: Path, item_id: int,
                     ateliers: dict[int, Path]) -> dict:
    """2 et 3. le juge tranche entre deux finalistes sans toucher à leurs ateliers.

    La comparaison se fait pendant son run, avant la promotion : après,
    l'élu a rejoint la branche de l'item et les ateliers sont rangés —
    c'est le geste voulu. Rend le run du juge, une fois rendu.
    """
    avant = photo(ateliers)
    assert len(avant) == 2, f"les deux ateliers de finalistes doivent être là : {avant}"

    run_id, fil = lancer_juge(conn, item_id)
    vu = workspace / "prompt-vu.md"
    assert attendre(lambda: vu.is_file(), 60.0), "le faux modèle du juge n'a jamais démarré"
    pendant = photo(ateliers)
    assert pendant == avant, f"le juge a écrit chez un candidat : {avant} → {pendant}"
    (workspace / "go").write_text("")  # le modèle rend son verdict
    rejoindre([fil], 90, "le juge n'est jamais revenu")

    run = run_du_juge(conn, run_id)
    assert run["outcome"] == "chosen", (run["outcome"], run["result"])
    assert etat(conn, item_id)["state"] == "fini", etat(conn, item_id)["state"]
    finalistes = list(ateliers)
    print(f"2. item {item_id} : deux finalistes c{finalistes[0]} et c{finalistes[1]}, "
          f"ateliers inchangés pendant tout le jugement ({len(avant)} têtes "
          "identiques, aucun fichier modifié) ✓")
    return run


def prompt_aveugle(workspace: Path, branche: str, finalistes: list[int]) -> None:
    """4. le prompt rendu ne dit rien de qui a produit quoi."""
    prompt = (workspace / "prompt-vu.md").read_text()
    interdits = [MODELE, "modèle", "--model", "cp prompt.md",  # modèle et CLI
                 "label", "strategy", f"{branche}-c"]  # variante et branche
    interdits += [f"angle-{i}" for i in range(3)]
    interdits += [f"stratégie numéro {i}" for i in range(3)]
    fuites = [mot for mot in interdits if mot in prompt]
    assert not fuites, f"le prompt du juge fuit : {fuites}\n{prompt}"
    assert "Finaliste A" in prompt and "Finaliste B" in prompt, prompt
    assert "Finaliste C" not in prompt, "deux finalistes, deux lettres"
    for k in finalistes:  # le diff, lui, est bien là : c'est tout ce qu'il a
        assert MARQUEURS[k] in prompt, f"le diff de c{k} manque"
    print(f"3. prompt du juge : {len(prompt)} caractères, deux lettres A et B, les "
          f"deux diffs ({', '.join(MARQUEURS[k] for k in finalistes)}), et aucun de "
          f"{len(interdits)} mots interdits ✓")


def verdict_suivi(repo: Path, workspace: Path, item_wt: Path, branche: str,
                  run: dict, elu: int, commits: dict[int, str],
                  ateliers: dict[int, Path]) -> None:
    """2 (suite). le verdict nomme l'élu et la raison, et le travail suit."""
    verdict = (workspace / bureau.VERDICT_NAME).read_text()
    assert "faux modèle" in verdict, "la comparaison du modèle est gardée"
    assert "Élu : finaliste B" in verdict, verdict
    assert RAISON in verdict, verdict
    assert run["result"]["elu"] == "B", run["result"]

    assert attendre(lambda: git(repo, "branch", "--list", f"{branche}-c*") == "", 60.0), \
        "les ateliers des candidats n'ont pas été rangés"
    assert ancetre(repo, commits[elu], branche), \
        f"le commit de l'élu c{elu} n'a pas rejoint {branche}"
    for perdant in set(range(3)) - {elu}:
        assert not ancetre(repo, commits[perdant], branche), \
            f"le commit de c{perdant} a fui sur {branche}"
    assert (item_wt / f"c{elu}.txt").is_file(), "le travail de l'élu n'est pas là"
    restants = [c for c in ateliers.values() if c.exists()]
    assert not restants, f"ateliers de candidats survivants : {restants}"
    branches = git(repo, "branch", "--list", "--format=%(refname:short)",
                   f"{branche}*").splitlines()
    assert branches == [branche], f"l'item doit n'avoir qu'une branche : {branches}"
    print(f"4. verdict : élu B (c{elu}), raison « {RAISON} » ; son commit "
          f"{commits[elu][:7]} est sur {branche}, les autres non, et plus un "
          "atelier ni une branche de candidat ✓")


def chosen(conn, workdir: Path, repo: Path) -> None:
    """2 à 4 : deux finalistes, un élu, des ateliers intacts et un prompt aveugle."""
    item_id, branche, item_wt = nouvel_item(conn, repo, bundle(["ok", "ok", "ok"], elu="B"))
    workspace = workdir / f"item-{item_id}"
    courir(conn, item_id)

    assert etat(conn, item_id)["state"] == "juge", etat(conn, item_id)["state"]
    commits = {k: (workspace / f"c{k}" / "commit.txt").read_text().strip()
               for k in range(3)}
    finalistes = finalistes_de(conn, item_id)
    ateliers = {k: item_wt.with_name(f"{item_wt.name}-c{k}") for k in finalistes}

    run = ateliers_intacts(conn, workspace, item_id, ateliers)
    prompt_aveugle(workspace, branche, finalistes)
    elu = finalistes[1]  # le faux modèle a élu la lettre B, la seconde
    verdict_suivi(repo, workspace, item_wt, branche, run, elu, commits, ateliers)


def none(conn, workdir: Path, repo: Path) -> None:
    """5. zéro finaliste : retour en amont, et le budget d'escalade des règles."""
    item_id, _, _ = nouvel_item(conn, repo, bundle(["ok", "ok"]))
    workspace = workdir / f"item-{item_id}"
    courir(conn, item_id)
    assert etat(conn, item_id)["state"] == "juge", etat(conn, item_id)["state"]

    # la première entrée dans le nœud d'escalade du passage était gratuite
    depart = etat(conn, item_id)
    assert depart["escalations"] == 3, depart["escalations"]

    # les finalistes disparaissent sous le juge — un redémarrage, un faucheur :
    # il arrive alors sur un dossier vide, et c'est le cas qu'on veut voir
    conn.execute("UPDATE node_run SET status = 'superseded' WHERE item_id = %s "
                 "AND node = 'travail'", (item_id,))
    run = run_du_juge(conn, courir(conn, item_id)[0]["id"])
    assert run["outcome"] == "none", (run["outcome"], run["result"])
    assert "usage" not in (run["result"] or {}), "aucun modèle appelé sans finaliste"
    apres = etat(conn, item_id)
    assert apres["state"] == "travail", apres["state"]
    assert apres["escalations"] == depart["escalations"], \
        "sortir d'un nœud d'escalade ne débite rien — c'est y revenir qui débite"
    verdict = (workspace / bureau.VERDICT_NAME).read_text()
    assert "Aucun finaliste" in verdict, verdict
    print(f"5. item {item_id} : zéro finaliste → issue « none », retour sur "
          f"`travail`, budget intact à {apres['escalations']} ✓")

    # la re-entrée, elle, décompte : exactement la règle d'un échec de `validate`
    courir(conn, item_id)
    assert etat(conn, item_id)["state"] == "juge", etat(conn, item_id)["state"]
    rentre = etat(conn, item_id)
    assert rentre["escalations"] == depart["escalations"] - 1, \
        (depart["escalations"], rentre["escalations"])
    print(f"   re-entrée dans `juge` au même passage : budget "
          f"{depart['escalations']} → {rentre['escalations']}, la règle existante ✓")


def main() -> None:
    db.init_db()  # idempotent : ne détruit rien, rattrape juste le schéma
    workdir = data_dir_jetable("graphatom-judge-")
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-judge-repo-"))
    repo = depot_git(tmp, "judge")
    try:
        with db.connect() as conn:
            sole(conn, workdir, repo)
            chosen(conn, workdir, repo)
            none(conn, workdir, repo)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nnœud arbitre : OK — un finaliste ne coûte rien, plusieurs se "
          "départagent à l'aveugle, aucun repart en amont")


if __name__ == "__main__":
    main()
