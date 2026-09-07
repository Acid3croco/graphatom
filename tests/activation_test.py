"""L'activation du worker, bord à bord : chaque état écrit une fois, et jamais à tort.

Sur un Postgres jetable, un dépôt git jetable et trois faux outils — un
systemctl qui note ses appels et lit l'état en base au moment où on l'appelle,
un docker qui rend les étiquettes qu'on lui pose, un gh absent :

  1. sans demande durable, rien ; le worker déjà sur le SHA voulu :
     `active`, écrit une fois, muet ensuite
  2. checkout différent : erreur écrite une fois, aucun redémarrage, et le
     tick suivant ne la redit pas ; un dépôt sans git : « checkout inconnu »
  3. services discordants : l'erreur les nomme, le rapport dit ce que chacun
     porte — ou qu'il ne porte rien —, aucun redémarrage
  4. tout concorde : l'état `pending` est en base AVANT l'appel de systemctl,
     appelé une fois ; le tick suivant n'appelle pas dans le délai de reprise
  5. un `attempted_at` illisible vaut une tentative périmée : reprise permise ;
     puis le worker revenu sur le SHA voulu règle la MÊME demande en `active`,
     sans un appel de plus à systemctl
  6. systemctl qui refuse : erreur visible, en base et au rapport
  7. systemctl absent : l'OSError est notée, pas cachée
  8. verrou tenu par une autre session : en attente, rien d'écrit ; libéré,
     la demande passe
  9. demande remplacée sous le verrou : en attente, l'ancienne intouchée
 10. checkout qui bouge sous le verrou : plus en attente, rien d'écrit
 11. la clé du verrou est le `cksum` du chemin du dépôt ; `cksum` en échec,
     muet ou illisible : erreur notée, imprimée, en attente
 12. rapport inaccessible : la ligne est imprimée, l'état reste en base

Après chaque chemin qui échoue — 2, 3, 6, 7, 11 — une autre session prend le
verrou d'activation sur-le-champ : `reconcile` l'a rendu, ou ne l'a jamais pris.

Usage : uv run python tests/activation_test.py
"""

import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import data_dir_jetable, depot_git, git, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-activation")

from graphatom import activation, db, gates  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

from bords_outils import lignes, patche, sortie_capturee  # noqa: E402

RESTART = "--user restart graphatom-worker.service"
NOEUD = "deploy"
AUTRE = "e" * 40  # le SHA que porte ce worker : jamais celui du checkout
LIT_L_ETAT = """
import sys, psycopg
with psycopg.connect(sys.argv[1]) as c:
    print(c.execute("SELECT result->'worker_activation'->>'status' FROM node_run "
                    "WHERE result ? 'worker_activation' ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0])
"""


class Banc:
    """Le banc d'essai : les faux outils, leurs journaux, la base et l'item."""

    def __init__(self, tmp: Path, conn):
        self.conn = conn
        self.repo = depot_git(tmp, "activation")
        self.wanted = git(self.repo, "rev-parse", "HEAD")
        self.journal = tmp / "journal.txt"
        self.vu = tmp / "vu.txt"
        self.code = tmp / "code.txt"
        self.labels = tmp / "labels"
        self.labels.mkdir()
        self.systemctl = tmp / "systemctl"
        self.tentatives = 0
        self.cle = activation._lock_key(self.repo)
        self._outils(tmp)
        self.item = self._item()
        bureau.item_workspace(self.item).mkdir(parents=True)

    def _outils(self, tmp: Path) -> None:
        for fichier in (self.journal, self.vu):
            fichier.write_text("")
        self.code.write_text("0")
        (tmp / "lit.py").write_text(LIT_L_ETAT)
        self.systemctl.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.journal}"\n'
            f'"{sys.executable}" "{tmp / "lit.py"}" "{db.DSN}" >> "{self.vu}"\n'
            f'exit "$(cat "{self.code}")"\n'
        )
        docker = tmp / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            'case "$1 $2" in\n'
            '  "compose ps") echo "id-$4"; exit 0 ;;\n'
            f'  "inspect "*) cat "{self.labels}/${{4#id-}}.txt" 2>/dev/null; exit 0 ;;\n'
            "esac\nexit 1\n"
        )
        for outil in (self.systemctl, docker):
            outil.chmod(0o755)
        os.environ["GRAPHATOM_SYSTEMCTL"] = str(self.systemctl)
        os.environ["GRAPHATOM_DOCKER"] = str(docker)
        os.environ["GRAPHATOM_GH"] = str(tmp / "gh-absent")

    def _item(self) -> int:
        self.conn.execute("INSERT INTO graph_revision (id, name, bundle) "
                          "VALUES ('rev-activation', 'activation', '{}')")
        sujet = self.conn.execute(
            "INSERT INTO subject (graph, subject_key) VALUES ('activation', 'a:1') "
            "RETURNING id").fetchone()["id"]
        return self.conn.execute(
            "INSERT INTO work_item (subject_id, generation, revision, state, "
            "escalations, wall_deadline) VALUES (%s, 1, 'rev-activation', %s, 1, "
            "now() + interval '1 hour') RETURNING id", (sujet, NOEUD)).fetchone()["id"]

    def demande(self, sha: str) -> int:
        """Une release appliquée qui demande `sha` — la plus récente, donc la lue."""
        self.tentatives += 1
        return self.conn.execute(
            "INSERT INTO node_run (item_id, node, attempt, status, fence, "
            "expected_version, lease_expires_at, finished_at, outcome, result) "
            "VALUES (%s, %s, %s, 'applied', 0, 0, now(), now(), 'done', %s) "
            "RETURNING id", (self.item, NOEUD, self.tentatives,
                             Jsonb({"deploy_sha": sha}))).fetchone()["id"]

    def etat(self, run_id: int) -> dict | None:
        return self.conn.execute(
            "SELECT result FROM node_run WHERE id = %s", (run_id,)
        ).fetchone()["result"].get("worker_activation")

    def etiquettes(self, **shas: str) -> None:
        """Ce que chaque service porte ; un service tu n'a aucune étiquette."""
        for name in gates.DEPLOYED_SERVICES:
            (self.labels / f"{name}.txt").unlink(missing_ok=True)
        for name, sha in shas.items():
            (self.labels / f"{name}.txt").write_text(sha)

    def rapport(self) -> list[str]:
        return lignes(bureau.item_workspace(self.item) / f"{NOEUD}.md")

    def redemarrages(self) -> list[str]:
        return lignes(self.journal)

    def verrou_libre(self) -> bool:
        """Une autre session prend-elle le verrou d'activation sur-le-champ ?"""
        with psycopg.connect(db.DSN) as autre:
            pris = autre.execute("SELECT pg_try_advisory_lock(%s)", (self.cle,)).fetchone()[0]
            if pris:
                autre.execute("SELECT pg_advisory_unlock(%s)", (self.cle,))
        return pris


def deja_actif(banc: Banc) -> None:
    """1. le worker porte déjà le SHA voulu : `active`, dit une fois."""
    assert activation.reconcile(banc.conn) is False, "réconcilié sans demande"
    activation.WORKER_SHA = banc.wanted
    run = banc.demande(banc.wanted)
    assert activation.reconcile(banc.conn) is False
    assert banc.etat(run) == {"status": "active", "worker_sha": banc.wanted}
    assert banc.rapport() == [f"worker actif sur le SHA voulu {banc.wanted}"]
    assert activation.reconcile(banc.conn) is False
    assert len(banc.rapport()) == 1, "l'état actif a été redit"
    print("1. worker déjà sur le SHA voulu : active, une seule ligne au rapport ✓")


def checkout_different(banc: Banc) -> None:
    """2. le checkout n'est pas la release : erreur écrite une fois, pas de restart."""
    activation.WORKER_SHA = AUTRE
    run = banc.demande("d" * 40)
    for _ in range(2):
        assert activation.reconcile(banc.conn) is False
    etat = banc.etat(run)
    assert etat["status"] == "error", etat
    assert etat["error"] == f"checkout {banc.wanted} différent", etat
    assert banc.rapport()[-2:] == [f"worker porte {AUTRE} - voulu {'d' * 40}",
                                   f"activation en attente - checkout {banc.wanted} différent"]
    assert len(banc.rapport()) == 3, banc.rapport()
    assert banc.redemarrages() == [], "redémarrage sur un checkout étranger"
    assert banc.verrou_libre(), "verrou gardé après un checkout différent"
    hors_git = banc.repo.with_name("hors-git")
    hors_git.mkdir()
    os.environ["GRAPHATOM_REPO_DIR"] = str(hors_git)
    try:
        assert activation.reconcile(banc.conn) is False
    finally:
        os.environ["GRAPHATOM_REPO_DIR"] = str(banc.repo)
    assert banc.etat(run)["error"] == "checkout inconnu différent", banc.etat(run)
    assert banc.rapport()[-1] == "activation en attente - checkout inconnu différent"
    print("2. checkout différent, puis dépôt sans git : erreur notée une fois sur deux "
          "ticks, checkout inconnu nommé, aucun restart, verrou libre ✓")


def services_discordants(banc: Banc) -> None:
    """3. des services qui ne portent pas la release : nommés, pas de restart."""
    banc.etiquettes(**{"github-sync": banc.wanted, "web": "0" * 40})
    run = banc.demande(banc.wanted)
    for _ in range(2):
        assert activation.reconcile(banc.conn) is False
    etat = banc.etat(run)
    assert etat["error"] == "services discordants : web, front", etat
    assert etat["service_shas"]["front"] == "", etat
    rapport = banc.rapport()
    assert f"web porte finalement {'0' * 40} - voulu {banc.wanted}" in rapport
    assert f"front porte finalement aucun SHA - voulu {banc.wanted}" in rapport
    assert rapport[-1] == "activation en attente - services discordants : web, front"
    assert rapport.count(rapport[-1]) == 1, "l'attente a été redite"
    assert banc.redemarrages() == [], "redémarrage malgré des services discordants"
    assert banc.verrou_libre(), "verrou gardé après des services discordants"
    print("3. web sur un autre SHA, front sans étiquette : nommés, aucun restart, "
          "verrou rendu ✓")


def concordance(banc: Banc) -> int:
    """4. tout concorde : `pending` commis avant systemctl, appelé une seule fois."""
    banc.etiquettes(**{name: banc.wanted for name in gates.DEPLOYED_SERVICES})
    os.environ["GRAPHATOM_ACTIVATION_RETRY_S"] = "60"
    run = banc.demande(banc.wanted)
    assert activation.reconcile(banc.conn) is True
    assert banc.redemarrages() == [RESTART], banc.redemarrages()
    assert lignes(banc.vu) == ["pending"], "systemctl n'a pas vu l'état commis"
    etat = banc.etat(run)
    assert etat["status"] == "pending" and etat["wanted_sha"] == banc.wanted, etat
    dt.datetime.fromisoformat(etat["attempted_at"])
    assert banc.rapport()[-2:] == [f"worker porte {AUTRE} - voulu {banc.wanted}",
                                   "résultat appliqué - activation du worker demandée"]
    assert activation.reconcile(banc.conn) is True
    assert banc.redemarrages() == [RESTART], "redémarrage redemandé dans le délai"
    print("4. concordance : pending commis avant systemctl, un seul appel, "
          "aucun second dans le délai de reprise ✓")
    return run


def tentative_perimee(banc: Banc, run: int) -> None:
    """5. un `attempted_at` illisible ne bloque pas : la tentative est périmée."""
    banc.conn.execute(
        "UPDATE node_run SET result = jsonb_set(result, "
        "'{worker_activation,attempted_at}', '\"pas une date\"') WHERE id = %s", (run,))
    assert activation.reconcile(banc.conn) is True
    assert banc.redemarrages() == [RESTART, RESTART], banc.redemarrages()
    print("5. attempted_at illisible : tentative périmée, redémarrage redemandé ✓")


def worker_revenu(banc: Banc, run: int) -> None:
    """5 bis. redémarré sur le SHA voulu : la même demande passe à `active`, sans systemctl."""
    avant = banc.redemarrages()
    actif = f"worker actif sur le SHA voulu {banc.wanted}"
    with patche(activation, "WORKER_SHA", banc.wanted):
        assert activation.reconcile(banc.conn) is False
        assert banc.etat(run) == {"status": "active", "worker_sha": banc.wanted}, banc.etat(run)
        assert activation.reconcile(banc.conn) is False
    assert banc.redemarrages() == avant, "systemctl rappelé après l'activation"
    assert banc.rapport()[-1] == actif and banc.rapport()[-2] != actif, banc.rapport()[-2:]
    print("5 bis. worker revenu sur le SHA voulu : la demande pending devient active, "
          "dite une fois, aucun appel de plus à systemctl ✓")


def refus_de_systemctl(banc: Banc) -> None:
    """6 et 7. systemctl qui refuse, puis systemctl absent : l'erreur est visible."""
    os.environ["GRAPHATOM_ACTIVATION_RETRY_S"] = "0"
    banc.code.write_text("1")
    run = banc.demande(banc.wanted)
    assert activation.reconcile(banc.conn) is True
    etat = banc.etat(run)
    assert etat["status"] == "error" and etat["error"] == "systemctl code 1", etat
    assert banc.rapport()[-1] == "activation du worker échouée - systemctl code 1"
    assert banc.verrou_libre(), "verrou gardé après un refus de systemctl"
    print("6. systemctl code 1 : état error en base, ligne au rapport, verrou rendu ✓")

    banc.code.write_text("0")
    os.environ["GRAPHATOM_SYSTEMCTL"] = str(banc.systemctl.with_name("absent"))
    run = banc.demande(banc.wanted)
    assert activation.reconcile(banc.conn) is True
    etat = banc.etat(run)
    assert etat["status"] == "error" and "absent" in etat["error"], etat
    assert banc.rapport()[-1].startswith("activation du worker échouée - [Errno")
    assert banc.verrou_libre(), "verrou gardé après un systemctl absent"
    os.environ["GRAPHATOM_SYSTEMCTL"] = str(banc.systemctl)
    print("7. systemctl absent : l'OSError est l'erreur notée, verrou rendu ✓")


def verrou_tenu(banc: Banc) -> None:
    """8. une autre session tient le verrou : en attente, rien d'écrit."""
    cle = banc.cle
    run = banc.demande(banc.wanted)
    avant = banc.redemarrages(), banc.rapport()
    with psycopg.connect(db.DSN) as tenant:
        assert tenant.execute("SELECT pg_try_advisory_lock(%s)", (cle,)).fetchone()[0]
        assert activation.reconcile(banc.conn) is True
        assert banc.etat(run) is None, "un état écrit sous un verrou étranger"
        assert (banc.redemarrages(), banc.rapport()) == avant
    assert activation.reconcile(banc.conn) is True
    assert banc.etat(run)["status"] == "pending", banc.etat(run)
    assert len(banc.redemarrages()) == len(avant[0]) + 1
    print("8. verrou tenu ailleurs : en attente sans rien écrire ; libéré, "
          "la demande passe ✓")


def remplacee_sous_le_verrou(banc: Banc) -> None:
    """9. la demande lue avant le verrou n'est plus la dernière : en attente."""
    ancienne = banc.demande(banc.wanted)
    lectures = []
    reelle = activation._request

    def relue(conn):
        if lectures:  # la seconde lecture, sous le verrou : une release entre-temps
            lectures.append(banc.demande(banc.wanted))
        lectures.append(reelle(conn)["id"])
        return reelle(conn)

    avant = banc.redemarrages()
    with patche(activation, "_request", relue):
        assert activation.reconcile(banc.conn) is True
    assert lectures[0] == ancienne and lectures[-1] != ancienne, lectures
    assert banc.etat(ancienne) is None and banc.etat(lectures[-1]) is None
    assert banc.redemarrages() == avant, "redémarrage pour une demande périmée"
    print("9. demande remplacée sous le verrou : en attente, aucune des deux touchée ✓")


def checkout_mouvant(banc: Banc) -> None:
    """10. le checkout bouge entre la lecture et le verrou : plus en attente."""
    run = banc.demande(banc.wanted)
    avant = banc.redemarrages()
    lectures = iter([banc.wanted, "f" * 40])
    with patche(activation, "_checkout", lambda _repo: next(lectures)):
        assert activation.reconcile(banc.conn) is False
    assert banc.etat(run) is None and banc.redemarrages() == avant
    print("10. checkout mouvant sous le verrou : False, rien d'écrit ✓")


def sans_cksum(banc: Banc) -> None:
    """11. la clé du verrou est le `cksum` du chemin du dépôt ; `cksum` en échec,
    muet ou illisible : erreur notée, imprimée, en attente."""
    cksum = subprocess.run(["cksum"], input=str(banc.repo), capture_output=True,
                           text=True, check=True, timeout=10)
    assert banc.cle == int(cksum.stdout.split()[0]), (banc.cle, cksum.stdout)
    reel = subprocess.run
    for reponse in (dict(returncode=1, stdout=""), dict(returncode=0, stdout=""),
                    dict(returncode=0, stdout="pas un nombre")):
        run = banc.demande(banc.wanted)
        avant = banc.redemarrages()

        def cksum_casse(args, **kwargs):
            if args == ["cksum"]:
                return subprocess.CompletedProcess(args, stderr="", **reponse)
            return reel(args, **kwargs)

        with patche(activation.subprocess, "run", cksum_casse), sortie_capturee() as sortie:
            assert activation.reconcile(banc.conn) is True
        assert "cksum indisponible" in sortie.getvalue(), sortie.getvalue()
        assert banc.etat(run)["error"] == "cksum indisponible", banc.etat(run)
        assert banc.rapport()[-1] == "activation du worker échouée - cksum indisponible"
        assert banc.redemarrages() == avant
        assert banc.verrou_libre(), "verrou gardé sans cksum"
    print("11. clé = cksum du dépôt ; cksum en échec, muet ou illisible : erreur en base, "
          "imprimée, en attente, verrou libre ✓")


def rapport_inaccessible(banc: Banc) -> None:
    """12. le rapport ne s'ouvre pas : la ligne est imprimée, l'état reste en base."""
    rapport = bureau.item_workspace(banc.item) / f"{NOEUD}.md"
    sauve = rapport.read_text()
    rapport.unlink()
    rapport.mkdir()
    run = banc.demande(banc.wanted)
    try:
        with sortie_capturee() as sortie:
            assert activation.reconcile(banc.conn) is True
    finally:
        rapport.rmdir()
        rapport.write_text(sauve)
    assert f"rapport d'activation indisponible pour l'item {banc.item}" in sortie.getvalue()
    assert banc.etat(run)["status"] == "pending", banc.etat(run)
    assert banc.redemarrages()[-1] == RESTART
    print("12. rapport inaccessible : imprimé, l'activation continue en base ✓")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-activation-"))
    workdir = data_dir_jetable("graphatom-activation-data-")
    db.init_db()
    try:
        with db.connect() as conn:
            banc = Banc(tmp, conn)
            deja_actif(banc)
            checkout_different(banc)
            services_discordants(banc)
            run = concordance(banc)
            tentative_perimee(banc, run)
            worker_revenu(banc, run)
            refus_de_systemctl(banc)
            verrou_tenu(banc)
            remplacee_sous_le_verrou(banc)
            checkout_mouvant(banc)
            sans_cksum(banc)
            rapport_inaccessible(banc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)
    print("\nactivation du worker : OK")


if __name__ == "__main__":
    main()
