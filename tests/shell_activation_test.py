"""L'activation du worker après `deploy` : le résultat durable, puis le restart.

Le nœud `deploy` de `code-task` déclare le SHA qu'il a déployé ; c'est le
résultat appliqué, jamais le HEAD vu pendant le shell, qui commande la
relance du worker. Tout se joue sur un Postgres jetable, un faux docker et
un faux systemctl — aucun modèle, aucun réseau, aucune image. Les autres
preuves shell vivent dans `shell_worktree_test.py`,
`shell_release_test.py` et `shell_deploy_test.py`.

 22. l'activation vient du résultat appliqué et survit au redémarrage
 23. l'import du scheduler reste valide quand l'image n'a pas Git

Usage : uv run python tests/shell_activation_test.py
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import git, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-shell-activation")

from graphatom import activation, db, gates, graph, kernel, scheduler  # noqa: E402
from graphatom import agent as agent_mod, workspace as bureau  # noqa: E402

from shell_outils import BUNDLE, depot, faux_deploiement  # noqa: E402

RESTART = "--user restart graphatom-worker.service"


def faux_systemctl(dossier: Path) -> tuple[Path, Path, Path]:
    """Un systemctl étroit qui note sa commande et rend le code demandé."""
    dossier.mkdir()
    journal = dossier / "journal.txt"
    code = dossier / "code.txt"
    journal.write_text("")
    code.write_text("1")
    outil = dossier / "systemctl"
    outil.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{journal}"\n'
        f'exit "$(cat "{code}")"\n'
    )
    outil.chmod(0o755)
    return outil, journal, code


@contextlib.contextmanager
def environnement_du_worker(cible: Path, outil: Path, deploiement: Path, donnees: Path):
    """Le worker tel que l'activation le voit : ses outils, son clone, ses
    données — remis en place à la sortie, même sur un échec."""
    anciens = {nom: os.environ.get(nom) for nom in (
        "GRAPHATOM_REPO_DIR", "GRAPHATOM_SYSTEMCTL", "GRAPHATOM_DOCKER",
        "GRAPHATOM_GH", "GRAPHATOM_ACTIVATION_RETRY_S")}
    ancien_sha, ancienne_data = activation.WORKER_SHA, bureau.DATA_DIR
    bureau.DATA_DIR = donnees
    os.environ["GRAPHATOM_REPO_DIR"] = str(cible)
    os.environ["GRAPHATOM_SYSTEMCTL"] = str(outil)
    os.environ["GRAPHATOM_DOCKER"] = str(deploiement / "docker")
    os.environ["GRAPHATOM_GH"] = str(deploiement / "gh")
    os.environ["GRAPHATOM_ACTIVATION_RETRY_S"] = "0"
    try:
        yield
    finally:
        activation.WORKER_SHA = ancien_sha
        bureau.DATA_DIR = ancienne_data
        for nom, ancienne in anciens.items():
            if ancienne is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = ancienne


def bundle_activation(nom: str, deploy: dict) -> dict:
    """Un graph d'un seul nœud `deploy`, celui dont l'activation lit le résultat."""
    return {
        "name": f"{nom}-{os.getpid()}",
        "entry": "deploy",
        "budgets": {"escalations": 1, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "abandon", "exhausted_to": "abandon"},
        "nodes": {
            "deploy": {"block": "ACT", **deploy, "edges": {"done": "fini"}},
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


def item_deploye(conn, revision: int, sujet: str, rapport: str) -> tuple[dict, Path]:
    """Un item admis sur `revision`, son workspace avec un `deploy.md`, et le
    run réclamé — prêt à recevoir son résultat."""
    item_id = kernel.admit(conn, revision, f"{sujet}:{os.getpid()}",
                           _allow_parallel_for_test=True)
    workspace = bureau.item_workspace(item_id)
    workspace.mkdir(parents=True)
    (workspace / "deploy.md").write_text(rapport)
    run = kernel.claim(conn, item_id)
    assert run is not None
    return run, workspace


def resultat(conn, run_id: int) -> dict:
    """Le résultat durable d'un run, tel que `node_run` le porte."""
    return conn.execute(
        "SELECT result FROM node_run WHERE id = %s", (run_id,)
    ).fetchone()["result"]


def lignes(journal: Path) -> int:
    """Le nombre de commandes que le faux systemctl a reçues."""
    return len(journal.read_text().splitlines())


def etiquettes(deploiement: Path, sha: str) -> None:
    """Les trois images portent `sha` : c'est ce que le faux docker inspecte."""
    for service in gates.DEPLOYED_SERVICES:
        (deploiement / f"sha-{service}.txt").write_text(sha)


def release_suivante(cible: Path, fichier: str, contenu: str) -> str:
    """Un commit de plus sur le clone de référence : une release plus récente."""
    (cible / fichier).write_text(contenu)
    git(cible, "add", fichier)
    git(cible, "commit", "-qm", f"release {fichier}")
    return git(cible, "rev-parse", "HEAD")


def resultat_durable(conn, run: dict, workspace: Path, wanted: str, journal: Path) -> Path:
    """Le résultat appliqué porte le SHA déclaré, son événement est écrit,
    et rien n'a encore redémarré. Rend l'`outcome.json` brut."""
    brut = workspace / "outcome.json"
    brut.write_text(json.dumps({
        "outcome": "done", "summary": "release prête",
        "deploy_sha": wanted,
    }))
    submitted = agent_mod.read_outcome(brut)
    assert submitted["deploy_sha"] == wanted
    assert kernel.apply(conn, run["id"], submitted) == "applied"
    event = conn.execute(
        "SELECT kind FROM event WHERE run_id = %s", (run["id"],)
    ).fetchone()
    assert event == {"kind": "result"}, event
    assert journal.read_text() == "", "restart avant le résultat durable"
    return brut


def portes_de_verification(conn, item_id: int, run_id: int, cible: Path, wanted: str) -> None:
    """La porte de `verify_deploy` compare le worker et le checkout au SHA
    déclaré, et nomme celui qui diverge."""
    conn.execute(
        "INSERT INTO heartbeat (who, at, worker_sha) "
        "VALUES ('rail', now(), %s) ON CONFLICT (who) DO UPDATE SET "
        "at = now(), worker_sha = EXCLUDED.worker_sha",
        (wanted,),
    )
    assert gates.verify_deploy_error(conn, item_id, cible) is None
    conn.execute("UPDATE heartbeat SET worker_sha = %s WHERE who = 'rail'",
                 ("0" * 40,))
    assert "worker" in gates.verify_deploy_error(conn, item_id, cible)
    conn.execute(
        "UPDATE node_run SET result = jsonb_set(result, "
        "'{deploy_sha}', %s::jsonb) WHERE id = %s",
        (json.dumps("0" * 40), run_id),
    )
    assert "checkout" in gates.verify_deploy_error(conn, item_id, cible)
    conn.execute(
        "UPDATE node_run SET result = jsonb_set(result, "
        "'{deploy_sha}', %s::jsonb) WHERE id = %s",
        (json.dumps(wanted), run_id),
    )
    conn.execute("UPDATE heartbeat SET worker_sha = %s WHERE who = 'rail'",
                 (wanted,))


def images_discordantes(conn, run_id: int, deploiement: Path, wanted: str,
                        journal: Path) -> None:
    """Une image discordante retient le restart, et l'erreur reste durable :
    le tick suivant la rejoue."""
    for service in gates.DEPLOYED_SERVICES:
        label = deploiement / f"sha-{service}.txt"
        label.write_text("0" * 40)
        assert activation.reconcile(conn) == 0
        assert journal.read_text() == "", \
            "restart malgré une image discordante"
        saved = resultat(conn, run_id)
        assert saved["worker_activation"]["error"] == \
            f"services discordants : {service}", saved
        label.write_text(wanted)

    scheduler.tick(conn)  # reprise après le commit, comme au démarrage suivant
    assert journal.read_text().splitlines() == [RESTART]
    saved = resultat(conn, run_id)
    assert saved["worker_activation"]["status"] == "error", saved


def acquittement(conn, run_id: int, workspace: Path, wanted: str,
                 journal: Path, code: Path) -> None:
    """Le restart passe, puis seul le heartbeat du worker neuf acquitte la
    demande — et une seule fois."""
    code.write_text("0")
    assert activation.reconcile(conn) == 1
    assert lignes(journal) == 2, journal.read_text()
    activation.WORKER_SHA = wanted
    assert activation.reconcile(conn) == 0
    assert activation.reconcile(conn) == 0
    assert lignes(journal) == 2, \
        "le worker neuf a relancé une demande déjà acquittée"
    saved = resultat(conn, run_id)
    assert saved["worker_activation"] == {
        "status": "active", "worker_sha": wanted,
    }, saved
    rapport = (workspace / "deploy.md").read_text()
    for service in gates.DEPLOYED_SERVICES:
        assert f"{service} porte finalement {wanted}" in rapport


def seconde_release(conn, revision: int, cible: Path, deploiement: Path,
                    journal: Path) -> None:
    """Deux releases proches : le réconciliateur ne lit que la plus récente
    et converge vers elle sous le même verrou de cible."""
    newest = release_suivante(cible, "seconde.txt", "release plus récente\n")
    etiquettes(deploiement, newest)
    second_run, _ = item_deploye(conn, revision, "activation-plus-recente",
                                 "seconde release rendue\n")
    assert kernel.apply(conn, second_run["id"], {
        "outcome": "done", "summary": "seconde release",
        "deploy_sha": newest,
    }) == "applied"
    before = lignes(journal)
    assert activation.reconcile(conn) == 1
    assert lignes(journal) == before + 1
    activation.WORKER_SHA = newest
    assert activation.reconcile(conn) == 0
    assert activation.reconcile(conn) == 0
    latest = resultat(conn, second_run["id"])
    assert latest["worker_activation"] == {
        "status": "active", "worker_sha": newest,
    }, latest


def chemin_reel(conn, cible: Path, deploiement: Path, journal: Path) -> None:
    """Le chemin complet `_execute` lit outcome.json, applique et route, puis
    seulement appelle le vrai réconciliateur."""
    target = release_suivante(cible, "troisieme.txt", "release du chemin réel\n")
    etiquettes(deploiement, target)
    real_bundle = bundle_activation("activation-reelle", {
        "config": {"lease_s": 30, "activation": True,
                   "execution": {
            "kind": "command",
            "cmd": "printf '%s\\n' '" + json.dumps({
                "outcome": "done", "summary": "chemin réel",
                "deploy_sha": target,
            }) + "' > outcome.json",
            "timeout_s": 10, "silence_s": 10,
        }},
    })
    real_revision = graph.publish(conn, real_bundle)
    real_item = kernel.admit(
        conn, real_revision, f"activation-reelle:{os.getpid()}",
        _allow_parallel_for_test=True,
    )
    real_run = kernel.claim(conn, real_item)
    assert real_run is not None
    observations = []
    real_activation = activation.reconcile

    def observe_after_apply(observed_conn):
        stored = observed_conn.execute(
            "SELECT status, result FROM node_run WHERE id = %s",
            (real_run["id"],),
        ).fetchone()
        event = observed_conn.execute(
            "SELECT kind FROM event WHERE run_id = %s",
            (real_run["id"],),
        ).fetchone()
        observations.append((stored, event))
        return real_activation(observed_conn)

    activation.reconcile = observe_after_apply
    before = lignes(journal)
    try:
        scheduler._execute(real_run["id"], real_item)
    finally:
        activation.reconcile = real_activation
    assert len(observations) == 1, observations
    assert observations[0][0]["status"] == "applied", observations
    assert observations[0][0]["result"]["deploy_sha"] == target
    assert observations[0][1] == {"kind": "result"}, observations
    assert lignes(journal) == before + 1
    activation.WORKER_SHA = target
    assert activation.reconcile(conn) == 0


def sha_tronque(brut: Path, wanted: str) -> None:
    """Un `deploy_sha` tronqué n'est pas un résultat : l'outcome est refusé."""
    brut.write_text(json.dumps({
        "outcome": "done", "summary": "SHA tronqué",
        "deploy_sha": wanted[:12],
    }))
    try:
        agent_mod.read_outcome(brut)
    except ValueError:
        pass
    else:
        raise AssertionError("un deploy_sha tronqué a été accepté")


def preuve_22(tmp: Path) -> None:
    """22. l'activation vient du résultat appliqué, jamais du HEAD vu pendant
    le shell. Une erreur reste durable et le tick suivant la rejoue ; seul
    le heartbeat du worker neuf acquitte la demande."""
    quartier = tmp / "activation"
    quartier.mkdir()
    cible = depot(quartier)
    wanted = git(cible, "rev-parse", "HEAD")
    outil, journal, code = faux_systemctl(quartier / "systemctl")
    deploiement = quartier / "bin"
    faux_deploiement(deploiement)
    etiquettes(deploiement, wanted)
    with environnement_du_worker(cible, outil, deploiement, quartier / "data"):
        with db.connect() as conn:
            revision = graph.publish(conn, bundle_activation("activation", {}))
            run, workspace = item_deploye(conn, revision, "activation",
                                          "résultat du shell rendu\n")
            brut = resultat_durable(conn, run, workspace, wanted, journal)
            portes_de_verification(conn, run["item_id"], run["id"], cible, wanted)
            images_discordantes(conn, run["id"], deploiement, wanted, journal)
            acquittement(conn, run["id"], workspace, wanted, journal, code)
            seconde_release(conn, revision, cible, deploiement, journal)
            chemin_reel(conn, cible, deploiement, journal)
            sha_tronque(brut, wanted)
        assert "systemctl" not in BUNDLE["nodes"]["deploy"]["config"]["execution"]["cmd"]
        print("22. résultat durable puis activation --user, erreur rejouée, "
              "acquittement idempotent par le worker neuf ✓")


def preuve_23() -> None:
    """23. Les images Python de production ne contiennent pas Git.

    Le SHA du worker est une information de diagnostic : son absence ne
    doit jamais empêcher la migration, le web ou le canal GitHub de
    démarrer.
    """
    vraie_execution = activation.subprocess.run

    def sans_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    activation.subprocess.run = sans_git
    try:
        assert activation._worker_sha() == "inconnu"
    finally:
        activation.subprocess.run = vraie_execution
    print("23. activation importable sans Git dans l'image de production ✓")


def main() -> None:
    db.init_db()
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-shell-"))
    preuve_22(tmp)
    preuve_23()

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nnœuds shell, activation : OK — déterministes, et jamais sans outcome")


if __name__ == "__main__":
    main()
