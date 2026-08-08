"""Le test des nœuds shell de `code-task` : du déterminisme, pas du prompt.

Les `cmd` du graph sont joués tels quels sur un dépôt jetable — aucun
modèle, aucun réseau, aucun docker. Ce qu'on vérifie, c'est la promesse
qui autorise un nœud sans agent : il écrit toujours son `outcome.json`.

  1. `worktree` crée le worktree de l'item sur `rail/issue-<num>` depuis
     `origin/main`, et le dit dans `worktree.md`
  2. rejoué, il reprend celui du cycle en cours au lieu d'en refaire un
  3. la branche restée d'un cycle passé est reprise telle quelle
  4. un sujet sans numéro échoue — proprement, sans toucher au dépôt
  5. `deploy` et `verify_deploy` sans environnement : `failed` / `fail`,
     jamais un nœud coincé
  6. `deploy` accepte un clone de référence qui est un worktree, où `.git`
     est un fichier de renvoi et non un répertoire
  7. `deploy` passe aussi quand ce worktree n'a pas `main` : la branche est
     tenue par le checkout principal, et la tête détachée ne la dispute pas
  8. `scripts/release.sh` nomme le pas qui a lâché et sort sur son code
  9. son pas de rapprochement traverse les quatre cas : déjà à jour, merge
     automatique, conflit (code 9, worktree laissé propre), fetch en échec,
     et deux fois de plus sur le worktree sale que le rail a vraiment
 10. la frontière tient dans le bundle : un nœud mécanique ne lance aucun
     modèle, un nœud à modèle rend son `usage.json`, les trois nœuds de
     retrait sont le même shell, et aucun ne teste `.git` comme un chemin
 11. une porte de `verify_deploy` attend un service qui met des secondes à
     écouter, au lieu de conclure sur le refus de connexion
 12. un service qui ne répond jamais la fait échouer, et le rapport comme
     l'outcome disent combien de temps elle a attendu
 13. une réponse fausse — un 500, un corps sans `_next` — la fait échouer
     tout de suite, sans consommer le budget d'attente
 14. ce budget tient dans le `timeout_s` du nœud, donc dans son bail, et le
     README le justifie

Usage : uv run python tests/shell_test.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = json.loads((ROOT / "examples" / "code-task.json").read_text())
SUJET = "gh:Acid3croco/graphatom#77"


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def depot(tmp: Path) -> Path:
    """Un clone de référence et son `origin`, comme en a le worker du rail."""
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    source = tmp / "source"
    # cloner un dépôt vide avertit sur stderr : le bruit n'est pas une panne
    subprocess.run(["git", "clone", "-q", str(origin), str(source)],
                   check=True, capture_output=True)
    git(source, "config", "user.email", "shell@test.invalid")
    git(source, "config", "user.name", "shell")
    (source / "socle.txt").write_text("le commit de départ\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "socle")
    git(source, "push", "-q", "origin", "main")
    repo = tmp / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    return repo


def reference_worktree(tmp: Path) -> Path:
    """Un clone de référence qui est lui-même un worktree git.

    C'est la forme du clone du worker : son `.git` est un fichier de renvoi,
    pas un répertoire. L'hôte garde `main` libre pour que le worktree l'ait.
    """
    hote = tmp / "hote"
    subprocess.run(["git", "clone", "-q", str(tmp / "origin.git"), str(hote)], check=True)
    git(hote, "switch", "-q", "-c", "garage")
    reference = tmp / "reference"
    git(hote, "worktree", "add", "-q", str(reference), "main")
    return reference


def reference_worktree_main_prise(tmp: Path) -> Path:
    """Le même worktree, mais `main` est tenue par le checkout principal.

    C'est la forme observée en prod : l'hôte reste sur `main`, donc aucun
    worktree ne peut la reprendre. Seule une tête détachée passe.
    """
    hote = tmp / "hote-sur-main"
    subprocess.run(["git", "clone", "-q", str(tmp / "origin.git"), str(hote)], check=True)
    reference = tmp / "reference-main-prise"
    git(hote, "worktree", "add", "-q", "--detach", str(reference), "main")
    return reference


def sha_deploye(workspace: Path) -> str:
    """Le SHA sur lequel `deploy.md` dit s'être détaché."""
    marque = "HEAD détachée sur origin/main - "
    for ligne in (workspace / "deploy.md").read_text().splitlines():
        if ligne.startswith(marque):
            return ligne[len(marque):]
    raise AssertionError("deploy.md ne dit pas sur quel SHA il s'est détaché")


def sans_gh(tmp: Path) -> str:
    """Un PATH où `gh` échoue : le deploy s'arrête au jeton, avant docker."""
    binaires = tmp / "bin"
    binaires.mkdir(exist_ok=True)
    faux = binaires / "gh"
    faux.write_text("#!/bin/sh\nexit 1\n")
    faux.chmod(0o755)
    return f"{binaires}:{os.environ['PATH']}"


def faux_docker(tmp: Path, nom: str, services: str, log: str = "sync au repos\n") -> str:
    """Un PATH où `docker` répond sans docker, pour les portes 1 et 4.

    Le shim ne connaît que les deux sous-commandes que `verify_deploy`
    lance : `compose ps` rend la liste des services, `compose logs` rend le
    journal du sync. Tout le reste sort en erreur, comme un vrai docker
    devant une commande qu'il ne comprend pas.
    """
    binaires = tmp / nom
    binaires.mkdir()
    (binaires / "services.txt").write_text(services)
    (binaires / "log.txt").write_text(log)
    faux = binaires / "docker"
    faux.write_text(
        "#!/bin/sh\n"
        f"ICI={binaires}\n"
        'for MOT in "$@"; do\n'
        '  case "$MOT" in\n'
        '    ps) cat "$ICI/services.txt"; exit 0 ;;\n'
        '    logs) cat "$ICI/log.txt"; exit 0 ;;\n'
        '  esac\n'
        "done\n"
        'echo "commande inconnue : $*" >&2\n'
        "exit 1\n"
    )
    faux.chmod(0o755)
    return f"{binaires}:{os.environ['PATH']}"


class Serveur(threading.Thread):
    """Un serveur HTTP jetable qui n'ouvre son écoute qu'après `delai`.

    Le port est réservé dès la construction, mais la file d'écoute n'ouvre
    qu'au démarrage du thread : d'ici là toute connexion est refusée, et
    `curl` rend `000`. C'est l'état d'un conteneur qui vient d'être
    reconstruit — celui que la porte doit attendre au lieu de le condamner.
    Jamais démarré, le serveur reste un port qui ne répondra pas.
    """

    def __init__(self, code: int = 200, corps: str = "<html>/_next/ok</html>",
                 delai: float = 0.0):
        super().__init__(daemon=True)
        self.delai = delai

        class Poignee(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - le nom vient de la stdlib
                page = corps.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *args):
                pass  # le journal du serveur n'est pas la preuve cherchée

        self.httpd = HTTPServer(("127.0.0.1", 0), Poignee, bind_and_activate=False)
        self.httpd.server_bind()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def run(self) -> None:
        time.sleep(self.delai)
        self.httpd.server_activate()
        self.httpd.serve_forever(poll_interval=0.1)

    def stop(self) -> None:
        if self.is_alive():
            self.httpd.shutdown()
        self.httpd.server_close()


def joue(node: str, repo: Path, workspace: Path, subject: str = SUJET,
         plus: dict | None = None) -> dict:
    """Le `cmd` d'un nœud du graph, joué tel quel. Rend son outcome.json.

    Les deux URL des portes visent par défaut le port 9, où rien n'écoute :
    aucun test ne doit tomber sur le déploiement réel de la machine. Le
    budget d'attente, lui, est ramené à quelques secondes — les 60 s de
    production se vérifient en lisant le `cmd`, pas en les subissant.
    """
    (workspace / "outcome.json").unlink(missing_ok=True)
    subprocess.run(
        BUNDLE["nodes"][node]["config"]["agent"]["cmd"],
        shell=True, cwd=workspace, check=False, capture_output=True,
        env=os.environ | {"GRAPHATOM_REPO_DIR": str(repo),
                          "GRAPHATOM_WORKSPACE": str(workspace),
                          "GRAPHATOM_SUBJECT_KEY": subject,
                          "GRAPHATOM_WEB_URL": "http://127.0.0.1:9",
                          "GRAPHATOM_FRONT_URL": "http://127.0.0.1:9",
                          "GRAPHATOM_PORTES_DELAI_S": "4"} | (plus or {}),
    )
    return json.loads((workspace / "outcome.json").read_text())


def attente(rapport: str, porte: int) -> int:
    """Le temps qu'une porte dit avoir attendu, lu dans `verify_deploy.md`."""
    vu = re.search(rf"porte {porte} - conclue après (\d+) s", rapport)
    assert vu, f"la porte {porte} ne dit pas son attente :\n{rapport}"
    return int(vu.group(1))


def release(repo: Path, workspace: Path, subject: str,
            plus: dict | None = None) -> int:
    """`scripts/release.sh` du worktree, lancé comme le ferait l'agent."""
    out = subprocess.run(
        [str(ROOT / "scripts" / "release.sh")], capture_output=True, text=True,
        env=os.environ | {"GRAPHATOM_REPO_DIR": str(repo),
                          "GRAPHATOM_WORKSPACE": str(workspace),
                          "GRAPHATOM_SUBJECT_KEY": subject} | (plus or {}),
    )
    return out.returncode


def cas_release(tmp: Path, nom: str) -> tuple[Path, Path, Path]:
    """Un dépôt jetable neuf, son workspace, et le worktree de l'item dedans.

    Chaque cas de rapprochement fait avancer `main` à sa façon : ils ne
    peuvent pas se partager un `origin`.
    """
    base = tmp / nom
    base.mkdir()
    repo = depot(base)
    git(repo, "config", "user.email", "shell@test.invalid")
    git(repo, "config", "user.name", "shell")
    workspace = base / "item-42"
    workspace.mkdir()
    assert joue("worktree", repo, workspace)["outcome"] == "done"
    return repo, workspace, repo / ".worktrees" / "rail-item-42"


def avance_main(repo: Path, fichier: str, contenu: str) -> str:
    """Un commit de plus sur `origin/main`, comme un item voisin qui merge."""
    source = repo.parent / "source"
    (source / fichier).write_text(contenu)
    git(source, "add", "-A")
    git(source, "commit", "-qm", f"main avance sur {fichier}")
    git(source, "push", "-q", "origin", "main")
    return git(source, "rev-parse", "--short", "HEAD")


def travaille(worktree: Path, fichier: str, contenu: str) -> None:
    """Un commit sur la branche de l'item, pour qu'elle diverge vraiment.

    Sans lui le rapprochement serait un simple avance-rapide, et le commit
    de merge que le cas 3 attend n'existerait pas.
    """
    (worktree / fichier).write_text(contenu)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", f"l'item touche {fichier}")


def merge_en_cours(worktree: Path) -> bool:
    """Vrai si un merge est resté ouvert dans le worktree."""
    vu = subprocess.run(["git", "-C", str(worktree), "rev-parse", "-q",
                         "--verify", "MERGE_HEAD"], capture_output=True)
    return vu.returncode == 0


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-shell-"))
    workspace = tmp / "item-42"  # le nom du workspace fait celui du worktree
    workspace.mkdir()
    repo = depot(tmp)
    worktree = repo / ".worktrees" / "rail-item-42"

    # 1. le worktree part d'origin/main, sur la branche du sujet
    outcome = joue("worktree", repo, workspace)
    assert outcome["outcome"] == "done", outcome
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "rail/issue-77"
    assert git(worktree, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")
    assert "créé depuis origin/main" in (workspace / "worktree.md").read_text()
    print(f"1. {outcome['summary']} ✓")

    # 2. rejoué, il reprend : le travail du cycle en cours ne se perd pas
    (worktree / "travail.txt").write_text("l'implémentation en cours\n")
    outcome = joue("worktree", repo, workspace)
    assert outcome["outcome"] == "done", outcome
    assert "repris" in outcome["summary"], outcome
    assert (worktree / "travail.txt").exists(), "le worktree a été recréé sous le travail"
    print(f"2. {outcome['summary']} ✓")

    # 3. le worktree est parti, la branche est restée : on la reprend
    git(repo, "worktree", "remove", "--force", str(worktree))
    outcome = joue("worktree", repo, workspace)
    assert outcome["outcome"] == "done", outcome
    assert "déjà là" in outcome["summary"], outcome
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "rail/issue-77"
    print(f"3. {outcome['summary']} ✓")

    # 4. un sujet dont on ne tire pas de numéro : échec nommé, dépôt intact
    outcome = joue("worktree", repo, workspace, "pipeline-x:oom")
    assert outcome["outcome"] == "failed", outcome
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
    print(f"4. {outcome['summary']} ✓")

    # 5. les nœuds de déploiement sans rien à quoi se raccrocher : ils
    #    rendent leur issue quand même — un nœud shell ne coince jamais
    absent = tmp / "pas-un-clone"
    assert joue("deploy", absent, workspace)["outcome"] == "failed"
    assert joue("verify_deploy", absent, workspace)["outcome"] == "fail"
    for rapport in ("deploy.md", "verify_deploy.md"):
        assert (workspace / rapport).read_text().strip(), rapport
    print("5. deploy et verify_deploy sans environnement : outcome écrit ✓")

    # 6. le clone de référence est parfois un worktree — son `.git` est alors
    #    un fichier, et le garde-fou du deploy doit quand même le reconnaître.
    #    Sans `gh`, les deux pas git se jouent puis le nœud s'arrête au jeton :
    #    le test ne construit jamais d'image ni ne lance de service.
    reference = reference_worktree(tmp)
    assert (reference / ".git").is_file(), "le clone de référence n'est pas un worktree"
    outcome = joue("deploy", reference, workspace, plus={"PATH": sans_gh(tmp)})
    assert outcome["outcome"] == "failed", outcome
    assert "pas 2" in outcome["summary"], outcome  # le jeton, pas le garde-fou
    assert "$ git switch --detach origin/main" in (workspace / "deploy.md").read_text()
    assert sha_deploye(workspace) == git(reference, "rev-parse", "--short", "origin/main")
    print(f"6. {outcome['summary']} ✓")

    # 7. le même worktree, mais `main` est tenue par le checkout principal :
    #    `switch -f main` sortait là en code 128, la tête détachée passe
    prise = reference_worktree_main_prise(tmp)
    assert git(prise, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", "pas détaché"
    outcome = joue("deploy", prise, workspace, plus={"PATH": sans_gh(tmp)})
    assert outcome["outcome"] == "failed", outcome
    assert "pas 2" in outcome["summary"], outcome  # les pas git ont tous passé
    assert sha_deploye(workspace) == git(prise, "rev-parse", "--short", "origin/main")
    print(f"7. {outcome['summary']} ✓")

    # 8. le script de release sort sur le code du pas qui a lâché
    assert release(repo, workspace, "pipeline-x:oom") == 2  # sujet illisible
    assert release(repo, workspace, "gh:Acid3croco/graphatom#999") == 3  # autre branche
    assert "code 3" in (workspace / "release.md").read_text()
    print("8. release.sh : code 2 sur le sujet, code 3 sur le worktree ✓")

    # 9. le pas de rapprochement, ses quatre cas. Sans `gh`, la release va
    #    toujours jusqu'au pas de la PR (code 6) : le rapprochement a donc
    #    laissé passer, et aucun appel n'atteint GitHub.
    aveugle = {"PATH": sans_gh(tmp)}

    repo, workspace, worktree = cas_release(tmp, "a-jour")
    tete = git(worktree, "rev-parse", "HEAD")
    assert release(repo, workspace, SUJET, aveugle) == 6
    rapport = (workspace / "release.md").read_text()
    assert "déjà à jour" in rapport, rapport
    assert git(worktree, "rev-parse", "HEAD") == tete, "le worktree a bougé"

    repo, workspace, worktree = cas_release(tmp, "disjoint")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    amont = avance_main(repo, "voisin.txt", "un autre item est passé par là\n")
    assert release(repo, workspace, SUJET, aveugle) == 6
    rapport = (workspace / "release.md").read_text()
    assert amont in rapport, rapport  # le SHA d'origin/main absorbé
    assert len(git(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3, \
        "HEAD n'est pas un commit de merge"
    git(worktree, "merge-base", "--is-ancestor", "origin/main", "HEAD")

    repo, workspace, worktree = cas_release(tmp, "conflit")
    travaille(worktree, "socle.txt", "la version de l'item\n")
    avance_main(repo, "socle.txt", "la version de main\n")
    assert release(repo, workspace, SUJET, aveugle) == 9
    rapport = (workspace / "release.md").read_text()
    assert "socle.txt" in rapport, rapport
    assert not merge_en_cours(worktree), "le merge est resté ouvert"
    assert git(worktree, "status", "--porcelain") == "", "worktree laissé sale"
    assert "<<<<<<<" not in (worktree / "socle.txt").read_text()

    repo, workspace, worktree = cas_release(tmp, "sans-reseau")
    git(repo, "remote", "set-url", "origin", str(tmp / "origin-qui-n-existe-pas.git"))
    assert release(repo, workspace, SUJET, aveugle) == 10
    rapport = (workspace / "release.md").read_text()
    assert "fetch" in rapport, rapport
    assert "gh pr" not in rapport, "une PR tentée sur une vision périmée de main"
    print("9. release.sh : déjà à jour, merge automatique, conflit en code 9 "
          "worktree propre, fetch en échec en code 10 ✓")

    # 9 bis. le worktree sale, la forme que le rail a vraiment : le
    #    rapprochement passe avant le commit, donc l'implémentation est encore
    #    à nu. Main ailleurs, le merge passe quand même ; main sur un fichier
    #    que l'item a ouvert, git refuse d'entrée — code 9, worktree intact
    repo, workspace, worktree = cas_release(tmp, "sale-ailleurs")
    (worktree / "socle.txt").write_text("le travail de l'item, pas encore commité\n")
    avance_main(repo, "voisin.txt", "main a bougé ailleurs\n")
    assert release(repo, workspace, SUJET, aveugle) == 4  # le pas du commit, sans `gh`
    rapport = (workspace / "release.md").read_text()
    assert "merge automatique" in rapport, rapport
    assert git(worktree, "status", "--porcelain") == "M socle.txt", "le travail a bougé"

    repo, workspace, worktree = cas_release(tmp, "sale-meme-fichier")
    (worktree / "socle.txt").write_text("le travail de l'item, pas encore commité\n")
    avance_main(repo, "socle.txt", "main a bougé au même endroit\n")
    assert release(repo, workspace, SUJET, aveugle) == 9
    rapport = (workspace / "release.md").read_text()
    assert "refusé d'entrée" in rapport, rapport
    assert not merge_en_cours(worktree), "le merge est resté ouvert"
    assert git(worktree, "status", "--porcelain") == "M socle.txt", "le travail a bougé"
    print("9 bis. worktree sale : merge quand même si main est ailleurs, "
          "code 9 sans rien perdre sinon ✓")

    # 10. la frontière du bundle, relue à chaque tour : les nœuds mécaniques
    #    n'appellent aucun modèle, et ceux qui en appellent un rendent le
    #    coût de la tentative — un merge d'amont ne doit rien reprendre
    #    d'un côté ni de l'autre
    for nom in ("worktree", "deploy", "verify_deploy",
                "cleanup", "cleanup_unresolved", "cleanup_split"):
        cmd = BUNDLE["nodes"][nom]["config"]["agent"]["cmd"]
        assert "claude " not in cmd, f"{nom} lance encore un modèle"
    for nom in ("scope", "implement", "test_backend", "test_frontend", "release"):
        cmd = BUNDLE["nodes"][nom]["config"]["agent"]["cmd"]
        assert "claude " in cmd, f"{nom} n'est plus un agent"
        assert "usage.json" in cmd, f"{nom} ne rend pas son usage.json"
    # les trois retraits sont le même shell : seul leur prompt les distingue,
    # et une correction sur l'un doit se voir sur les trois
    retraits = {BUNDLE["nodes"][nom]["config"]["agent"]["cmd"]
                for nom in ("cleanup", "cleanup_unresolved", "cleanup_split")}
    assert len(retraits) == 1, "les nœuds de retrait ont divergé"
    # `.git` n'est un répertoire que dans un clone ordinaire : aucun shell du
    # rail ne doit s'en servir pour reconnaître un dépôt
    shells = {nom: node.get("config", {}).get("agent", {}).get("cmd", "")
              for nom, node in BUNDLE["nodes"].items()}
    shells["scripts/release.sh"] = (ROOT / "scripts" / "release.sh").read_text()
    for nom, shell in shells.items():
        assert "/.git" not in shell, f"{nom} teste .git comme un chemin"
    print("10. six nœuds sans modèle dont trois retraits identiques, "
          "cinq agents qui rendent leur usage, aucun test sur .git ✓")

    # 11. les portes de verify_deploy attendent. `docker compose up` rend la
    #    main avant que les services écoutent : un front reconstruit ouvre
    #    son port quelques secondes plus tard, et la porte doit le voir
    #    arriver au lieu de conclure sur le refus de connexion
    portes = tmp / "portes"
    portes.mkdir()
    docker = faux_docker(tmp, "docker-en-marche", "github-sync\nweb\nfront\n")
    lent, secours = Serveur(delai=4.0), Serveur(corps="{}")
    lent.start()
    secours.start()
    outcome = joue("verify_deploy", tmp, portes, plus={
        "PATH": docker, "GRAPHATOM_FRONT_URL": lent.url,
        "GRAPHATOM_WEB_URL": secours.url, "GRAPHATOM_PORTES_DELAI_S": "30"})
    assert outcome["outcome"] == "pass", outcome
    rapport = (portes / "verify_deploy.md").read_text()
    assert attente(rapport, 2) >= 3, rapport  # elle a bien attendu le front
    lent.stop()
    print(f"11. {outcome['summary']} ✓")

    # 12. le service qui ne répond jamais : la porte échoue, mais après avoir
    #    épuisé le budget, et elle dit combien de temps elle a tenu
    sourd = Serveur()  # jamais démarré : le port est réservé, rien n'écoute
    outcome = joue("verify_deploy", tmp, portes, plus={
        "PATH": docker, "GRAPHATOM_FRONT_URL": sourd.url,
        "GRAPHATOM_WEB_URL": secours.url, "GRAPHATOM_PORTES_DELAI_S": "6"})
    assert outcome["outcome"] == "fail", outcome
    assert "porte 2" in outcome["summary"], outcome
    tenu = re.search(r"après (\d+) s d'attente", outcome["summary"])
    assert tenu and int(tenu.group(1)) >= 6, outcome
    assert attente((portes / "verify_deploy.md").read_text(), 2) >= 6
    sourd.stop()
    print(f"12. {outcome['summary']} ✓")

    # 13. la réponse fausse ne s'améliore pas en attendant : un 500, ou un
    #    corps sans `_next`, fait échouer la porte tout de suite. Le budget
    #    est mis à 60 s exprès — si la porte le consommait, on le verrait
    for nom, serveur, marque in (
            ("un 500", Serveur(code=500, corps="erreur"), "500"),
            ("le stdlib", Serveur(corps="<html>le web stdlib</html>"), "_next")):
        serveur.start()
        depart = time.monotonic()
        outcome = joue("verify_deploy", tmp, portes, plus={
            "PATH": docker, "GRAPHATOM_FRONT_URL": serveur.url,
            "GRAPHATOM_WEB_URL": secours.url, "GRAPHATOM_PORTES_DELAI_S": "60"})
        ecoule = time.monotonic() - depart
        assert outcome["outcome"] == "fail", outcome
        assert "porte 2" in outcome["summary"] and marque in outcome["summary"], outcome
        assert ecoule < 15, f"{nom} a consommé {ecoule:.0f} s du budget de 60 s"
        assert attente((portes / "verify_deploy.md").read_text(), 2) < 15
        serveur.stop()
    secours.stop()
    print("13. un 500 et un corps sans _next : échec immédiat, budget intact ✓")

    # 14. le budget d'attente tient dans le couperet du nœud, donc dans son
    #    bail — et le README dit pourquoi cette valeur-là
    config = BUNDLE["nodes"]["verify_deploy"]["config"]
    cmd = config["agent"]["cmd"]
    budget = int(re.search(r"GRAPHATOM_PORTES_DELAI_S:-(\d+)", cmd).group(1))
    assert budget < config["agent"]["timeout_s"] < config["lease_s"], config
    readme = (ROOT / "README.md").read_text()
    assert "GRAPHATOM_PORTES_DELAI_S" in readme, "le README ne justifie pas le budget"
    assert f"{budget} s d'attente" in readme, "le README ne dit pas le budget retenu"
    print(f"14. budget d'attente {budget} s < timeout_s "
          f"{config['agent']['timeout_s']} s < bail {config['lease_s']} s ✓")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nnœuds shell : OK — déterministes, et jamais sans outcome")


if __name__ == "__main__":
    main()
