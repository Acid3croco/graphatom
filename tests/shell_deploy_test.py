"""`deploy` et `verify_deploy` de `code-task` : les portes, puis la file.

Les `cmd` du graph sont joués tels quels sur un dépôt jetable, avec un
faux docker qui note ses `up` — aucun modèle, aucun réseau, aucune image.
Un Postgres jetable porte le verrou de `deploy` et le heartbeat que
`verify_deploy` relit. Les autres preuves shell vivent dans
`shell_worktree_test.py`, `shell_release_test.py` et
`shell_activation_test.py`.

 11. une porte de `verify_deploy` attend un service qui met des secondes à
     écouter, au lieu de conclure sur le refus de connexion
 12. un service qui ne répond jamais la fait échouer, et le rapport comme
     l'outcome disent combien de temps elle a attendu
 13. une réponse fausse — un 500, un corps sans `_next` — la fait échouer
     tout de suite, sans consommer le budget d'attente
 14. ce budget tient dans le couperet du nœud, donc dans son bail, et le
     README le justifie
 15. deux `deploy` lancés en même temps sur la même cible déploient l'un
     après l'autre, tous deux en succès : la concurrence est une file
 16. le second voit que le SHA visé est déjà déployé et conclut sans
     relancer `docker compose up` — son rapport porte la comparaison
 17. le verrou d'une session morte ne bloque personne, et rien sur disque
     ne survit à cette mort : le verrou n'est pas un fichier
 18. l'attente est bornée, et son dépassement rend `waiting` — que le
     graph renvoie sur `deploy`, jamais sur `escalate`
 19. le conteneur bâtard d'un `up` interrompu est retiré, et le `up` rejoué
 20. les garde-fous du verrou : seul `deploy` le prend, son attente tient
     dans le couperet du nœud, et le README comme le prompt le justifient
 21. le `python3` du PATH n'a pas psycopg — celui du clone de référence
     l'a, et c'est lui qui prend le verrou

Usage : uv run python tests/shell_deploy_test.py
"""

import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import git, provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-shell-deploy")

from graphatom import db, policy  # noqa: E402
from graphatom.policy import AGENT_TIMEOUT_S  # noqa: E402

from shell_outils import (  # noqa: E402
    BUNDLE, ROOT, VERROU_DSN, Serveur, cible_deployable, cle_verrou, dans_le_path,
    depot, faux_deploiement, faux_docker, joue, montees, tenant,
)

DEPLOIEMENT = BUNDLE["nodes"]["deploy"]["config"]["execution"]["cmd"]


def attente(rapport: str, porte: int) -> int:
    """Le temps qu'une porte dit avoir attendu, lu dans `verify_deploy.md`."""
    vu = re.search(rf"porte {porte} - conclue après (\d+) s", rapport)
    assert vu, f"la porte {porte} ne dit pas son attente :\n{rapport}"
    return int(vu.group(1))


def heartbeat(requete: str, *params) -> None:
    """Une requête sur la table `heartbeat`, celle que `verify_deploy` relit."""
    with db.connect() as conn:
        conn.execute(requete, params)


def budget_tenu(noeud: str, variable: str) -> tuple[int, float]:
    """Le budget d'attente d'un nœud, lu dans son `cmd`, tient dans son
    couperet, donc dans son bail — et le README dit pourquoi cette valeur.

    Rend le budget et le couperet, pour la ligne imprimée.
    """
    config = BUNDLE["nodes"][noeud]["config"]
    cmd = config["execution"]["cmd"]
    budget = int(re.search(rf"{variable}:-(\d+)", cmd).group(1))
    couperet = policy.agent_timeout_s(config, AGENT_TIMEOUT_S)
    assert budget < couperet < config["lease_s"], config
    readme = (ROOT / "README.md").read_text()
    assert variable in readme, f"le README ne justifie pas {variable}"
    assert f"{budget} s d'attente" in readme, "le README ne dit pas le budget retenu"
    return budget, couperet


class Portes:
    """La cible de `verify_deploy` : son clone, son workspace, son faux docker,
    et le serveur de secours qui répond `{}` pour le web."""

    def __init__(self, tmp: Path):
        self.workspace = tmp / "portes"
        self.workspace.mkdir()
        self.cible = depot(tmp / "cible-portes")
        self.sha = git(self.cible, "rev-parse", "HEAD")
        self.docker = faux_docker(tmp, "docker-en-marche", "github-sync\nweb\nfront\n")
        self.secours = Serveur(corps="{}")
        self.secours.start()

    def verifie(self, front: str, **plus: str) -> dict:
        """`verify_deploy` joué contre `front`, le heartbeat lu dans la base."""
        return joue("verify_deploy", self.cible, self.workspace, plus={
            "PATH": self.docker, "GRAPHATOM_FRONT_URL": front,
            "GRAPHATOM_WEB_URL": self.secours.url,
            "GRAPHATOM_HEARTBEAT_DSN": VERROU_DSN} | plus)

    def rapport(self) -> str:
        return (self.workspace / "verify_deploy.md").read_text()


def preuve_11(portes: Portes) -> None:
    """11. les portes de verify_deploy attendent.

    `docker compose up` rend la main avant que les services écoutent : un
    front reconstruit ouvre son port quelques secondes plus tard, et la
    porte doit le voir arriver au lieu de conclure sur le refus de
    connexion. Puis le worker absent ou faux, et chaque étiquette
    discordante, font échouer la vérification.
    """
    heartbeat(
        "INSERT INTO heartbeat (who, at, worker_sha, worker_started_at) "
        "VALUES ('rail', now(), %s, now()) ON CONFLICT (who) DO UPDATE "
        "SET at = now(), worker_sha = EXCLUDED.worker_sha, "
        "worker_started_at = EXCLUDED.worker_started_at",
        portes.sha,
    )
    lent = Serveur(delai=4.0)
    lent.start()
    outcome = portes.verifie(lent.url, GRAPHATOM_PORTES_DELAI_S="30")
    assert outcome["outcome"] == "pass", outcome
    rapport = portes.rapport()
    assert attente(rapport, 2) >= 3, rapport  # elle a bien attendu le front

    heartbeat("DELETE FROM heartbeat WHERE who = 'rail'")
    outcome = portes.verifie(lent.url)
    assert outcome["outcome"] == "fail" and "worker" in outcome["summary"], outcome
    heartbeat(
        "INSERT INTO heartbeat (who, at, worker_sha, worker_started_at) "
        "VALUES ('rail', now(), 'mauvais-sha', now())"
    )
    outcome = portes.verifie(lent.url)
    assert outcome["outcome"] == "fail" and "worker" in outcome["summary"], outcome
    heartbeat("UPDATE heartbeat SET worker_sha = %s WHERE who = 'rail'", portes.sha)
    for service in ("github-sync", "web", "front"):
        outcome = portes.verifie(lent.url, GRAPHATOM_TEST_BAD_SERVICE=service)
        assert outcome["outcome"] == "fail" and service in outcome["summary"], outcome
    lent.stop()
    print("11. concordance complète, worker absent ou faux et quatre étiquettes "
          "discordantes contrôlés ✓")


def preuve_12(portes: Portes) -> None:
    """12. le service qui ne répond jamais : la porte échoue, mais après avoir
    épuisé le budget, et elle dit combien de temps elle a tenu."""
    sourd = Serveur()  # jamais démarré : le port est réservé, rien n'écoute
    outcome = portes.verifie(sourd.url, GRAPHATOM_PORTES_DELAI_S="6")
    assert outcome["outcome"] == "fail", outcome
    assert "porte 2" in outcome["summary"], outcome
    tenu = re.search(r"après (\d+) s d'attente", outcome["summary"])
    assert tenu and int(tenu.group(1)) >= 6, outcome
    assert attente(portes.rapport(), 2) >= 6
    sourd.stop()
    print(f"12. {outcome['summary']} ✓")


def preuve_13(portes: Portes) -> None:
    """13. la réponse fausse ne s'améliore pas en attendant.

    Un 500, ou un corps sans `_next`, fait échouer la porte tout de suite.
    Le budget est mis à 60 s exprès — si la porte le consommait, on le
    verrait.
    """
    for nom, serveur, marque in (
            ("un 500", Serveur(code=500, corps="erreur"), "500"),
            ("le stdlib", Serveur(corps="<html>le web stdlib</html>"), "_next")):
        serveur.start()
        depart = time.monotonic()
        outcome = portes.verifie(serveur.url, GRAPHATOM_PORTES_DELAI_S="60")
        ecoule = time.monotonic() - depart
        assert outcome["outcome"] == "fail", outcome
        assert "porte 2" in outcome["summary"] and marque in outcome["summary"], outcome
        assert ecoule < 15, f"{nom} a consommé {ecoule:.0f} s du budget de 60 s"
        assert attente(portes.rapport(), 2) < 15
        serveur.stop()
    portes.secours.stop()
    print("13. un 500 et un corps sans _next : échec immédiat, budget intact ✓")


def preuve_14() -> None:
    """14. le budget d'attente tient dans le couperet du nœud, donc dans son
    bail — et le README dit pourquoi cette valeur-là."""
    budget, couperet = budget_tenu("verify_deploy", "GRAPHATOM_PORTES_DELAI_S")
    lease = BUNDLE["nodes"]["verify_deploy"]["config"]["lease_s"]
    print(f"14. budget d'attente {budget} s < couperet {couperet:.0f} s < bail {lease} s ✓")


def preuve_15(tmp: Path) -> None:
    """15. la cible du deploy est unique : deux items qui l'atteignent en
    même temps doivent déployer l'un après l'autre.

    Le faux docker met 2 s à monter et note l'intervalle de chaque `up` —
    si les deux se recouvraient, un vrai docker refuserait le second sur un
    nom pris.
    """
    file = tmp / "file"
    file.mkdir()
    concurrent = depot(file)
    docker = faux_deploiement(file / "bin", duree="2", etiquette=False)
    rendus: dict[str, dict] = {}
    fils = []
    for nom in ("premier", "second"):
        atelier = file / nom
        atelier.mkdir()
        fils.append(threading.Thread(
            target=lambda n=nom, a=atelier: rendus.__setitem__(
                n, joue("deploy", concurrent, a, plus={"PATH": docker,
                                                       "GRAPHATOM_VERROU_DELAI_S": "60"}))))
    for fil in fils:
        fil.start()
    for fil in fils:
        fil.join()
    for nom, outcome in rendus.items():
        assert outcome["outcome"] == "done", (nom, outcome)
    vus = montees(file / "bin")
    assert len(vus) == 2, vus
    assert vus[0][1] <= vus[1][0], f"les deux `up` se sont recouverts : {vus}"
    print(f"15. deux deploy simultanés : deux `up` successifs, "
          f"{(vus[1][0] - vus[0][1]) / 1e9:.1f} s entre les deux, aucun conflit ✓")


def preuve_16(tmp: Path) -> None:
    """16. rejoué sur le même `main`, le deploy ne reconstruit rien : les
    conteneurs portent déjà le SHA visé, et le rapport le dit."""
    memes = tmp / "deja"
    memes.mkdir()
    stable = depot(memes)
    docker = faux_deploiement(memes / "bin")
    for nom in ("un", "deux"):
        atelier = memes / nom
        atelier.mkdir()
        outcome = joue("deploy", stable, atelier, plus={"PATH": docker})
        assert outcome["outcome"] == "done", outcome
    assert len(montees(memes / "bin")) == 1, montees(memes / "bin")
    assert "déjà déployé" in outcome["summary"], outcome
    sha = git(stable, "rev-parse", "origin/main")
    rapport = (memes / "deux" / "deploy.md").read_text()
    assert f"front porte {sha} - voulu {sha}" in rapport, rapport
    print(f"16. {outcome['summary']} ✓")


def preuve_17(tmp: Path) -> None:
    """17. le verrou meurt avec sa session : celui d'un shell tué en plein
    vol ne bloque pas le suivant.

    Rien sur disque ne lui survit — un verrou qui survit à un crash serait
    pire que pas de verrou du tout.
    """
    orphelin, shim, atelier = cible_deployable(tmp, "session-morte")
    tueur = tenant(VERROU_DSN, cle_verrou(orphelin))
    tueur.kill()
    tueur.wait()
    outcome = joue("deploy", orphelin, atelier, plus={"PATH": dans_le_path(shim),
                                                     "GRAPHATOM_VERROU_DELAI_S": "20"})
    assert outcome["outcome"] == "done", outcome
    assert len(montees(shim)) == 1, montees(shim)
    for interdit in ("flock", ".lock", "mkdir"):
        assert interdit not in DEPLOIEMENT, f"le verrou passe par {interdit}"
    print("17. le verrou d'une session tuée est rendu par postgres, "
          "et rien sur disque ne le porte ✓")


def preuve_18(tmp: Path) -> None:
    """18. l'attente est bornée : le verrou tenu par une session vivante
    fait rendre `waiting` — que le graph renvoie sur `deploy`, pas sur
    `escalate`. Jamais d'escalade pour cause de concurrence."""
    occupe, shim, atelier = cible_deployable(tmp, "attente")
    tient = tenant(VERROU_DSN, cle_verrou(occupe))
    depart = time.monotonic()
    outcome = joue("deploy", occupe, atelier, plus={"PATH": dans_le_path(shim),
                                                   "GRAPHATOM_VERROU_DELAI_S": "3"})
    ecoule = time.monotonic() - depart
    tient.kill()
    tient.wait()
    assert outcome["outcome"] == "waiting", outcome
    assert 3 <= ecoule < 30, f"l'attente a duré {ecoule:.0f} s"
    assert montees(shim) == [], "un `up` est passé sans le verrou"
    assert BUNDLE["nodes"]["deploy"]["edges"]["waiting"] == "deploy", "waiting n'attend pas"
    print(f"18. {outcome['summary']} ✓")


def preuve_19(tmp: Path) -> None:
    """19. le conteneur bâtard d'un `up` interrompu est rattrapé au
    déploiement suivant : le message nomme le coupable, le shell le retire
    et rejoue — pas d'humain dans la boucle."""
    reste = ("741ae7f1a135_imperative-crunching-conway-init-1")
    coince, shim, atelier = cible_deployable(tmp, "conflit-docker", conflit=(
        "Error response from daemon: Conflict. The container name "
        f'"/{reste}" is already in use by container "86fea9f768f3".\n'))
    outcome = joue("deploy", coince, atelier, plus={"PATH": dans_le_path(shim)})
    assert outcome["outcome"] == "done", outcome
    assert len(montees(shim)) == 2, montees(shim)
    assert f"retiré {reste}" in (shim / "journal.txt").read_text()
    assert reste in (atelier / "deploy.md").read_text()
    print(f"19. conteneur bâtard {reste[:20]}… retiré, `up` rejoué ✓")


def preuve_20() -> None:
    """20. les garde-fous du verrou, relus dans le bundle à chaque tour :
    lui seul se sérialise, son attente tient dans le couperet du nœud, et
    l'arbitrage entre exclusion mutuelle et attente bornée est écrit."""
    for nom, node in BUNDLE["nodes"].items():
        shell = node.get("config", {}).get("execution", {}).get("cmd", "")
        if nom != "deploy":
            assert "advisory_lock" not in shell, f"{nom} se sérialise lui aussi"
    config = BUNDLE["nodes"]["deploy"]["config"]
    verrou, couperet = budget_tenu("deploy", "GRAPHATOM_VERROU_DELAI_S")
    assert "agent" not in config, "deploy prétend encore être un agent"
    readme = (ROOT / "README.md").read_text()
    for marque in ("exclusion mutuelle", "attente bornée", "pg_advisory_lock"):
        assert marque in readme.lower(), f"le README ne dit pas « {marque} »"
    print(f"20. seul deploy prend le verrou, attente {verrou} s < couperet "
          f"{couperet:.0f} s < bail {config['lease_s']} s ✓")


def preuve_21(tmp: Path) -> None:
    """21. le worker du rail n'a pas son venv dans son PATH : le `python3`
    ambiant y est celui du système, sans psycopg.

    Le nœud doit trouver l'interprète du clone de référence — sinon le
    verrou se tairait, et la file ne serait qu'un commentaire dans le bundle.
    """
    cible, shim, atelier = cible_deployable(tmp, "python-nu")
    venv = cible / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python3").symlink_to(sys.executable)
    sourd = shim / "python3"  # celui du PATH ne sait pas importer psycopg
    sourd.write_text("#!/bin/sh\nexit 1\n")
    sourd.chmod(0o755)
    tient = tenant(VERROU_DSN, cle_verrou(cible))
    outcome = joue("deploy", cible, atelier, plus={"PATH": dans_le_path(shim),
                                                  "GRAPHATOM_VERROU_DELAI_S": "3"})
    tient.kill()
    tient.wait()
    assert outcome["outcome"] == "waiting", \
        f"le verrou s'est tu faute d'interprète : {outcome}"
    assert montees(shim) == [], "un `up` est passé sans le verrou"
    print("21. le python du PATH n'a pas psycopg : celui du clone de "
          "référence prend le verrou quand même ✓")


def main() -> None:
    db.init_db()
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-shell-"))
    portes = Portes(tmp)
    preuve_11(portes)
    preuve_12(portes)
    preuve_13(portes)
    preuve_14()
    preuve_15(tmp)
    preuve_16(tmp)
    preuve_17(tmp)
    preuve_18(tmp)
    preuve_19(tmp)
    preuve_20()
    preuve_21(tmp)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nnœuds shell, deploy : OK — déterministes, et jamais sans outcome")


if __name__ == "__main__":
    main()
