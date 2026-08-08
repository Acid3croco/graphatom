"""Le test des nœuds shell de `code-task` : du déterminisme, pas du prompt.

Les `cmd` du graph sont joués tels quels sur un dépôt jetable — aucun
modèle, aucun réseau, aucun docker. Ce qu'on vérifie, c'est la promesse
qui autorise un nœud sans agent : il écrit toujours son `outcome.json`.
Une base est nécessaire depuis que `deploy` y pose le verrou de sa file ;
le test prend celle que `GRAPHATOM_DSN` désigne, sans y écrire une ligne.

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
     ; puis sa porte de construction traverse les cinq siens : absorption
     vide sans aucune porte, contenu sain avec et sans `front/`, et les
     deux ruptures — python, puis build front — en code 11 sans PR
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = json.loads((ROOT / "examples" / "code-task.json").read_text())
SUJET = "gh:Acid3croco/graphatom#77"
# la base où `deploy` pose son verrou. Celle de l'item qui nous lance fait
# l'affaire : les verrous consultatifs sont locaux à une base, et la clé,
# elle, vient du chemin de la cible — deux exécutions de ce test dans deux
# répertoires temporaires ne se disputent donc jamais le même verrou.
VERROU_DSN = db.DSN
TIENT_LE_VERROU = """
import sys
import time

import psycopg

conn = psycopg.connect(sys.argv[1], autocommit=True)
conn.execute("SELECT pg_advisory_lock(%s)", (int(sys.argv[2]),))
print("pris", flush=True)
time.sleep(3600)
"""


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
    # le paquet python du dépôt jetable : la porte de construction de la
    # release compile `src/`, et un dépôt sans `src/` n'aurait rien à lui dire
    (source / "src").mkdir()
    (source / "src" / "socle.py").write_text("def socle():\n    return 1\n")
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


def faux_npm(tmp: Path, nom: str, code: int, sortie: str) -> str:
    """Un PATH sans `gh`, où `npm` joue le build du front et rend `code`.

    Le vrai `npm run build` demande `node_modules` et coûte des minutes :
    ce qui se vérifie ici, c'est que la porte le lance et lit son code, pas
    que Next sait construire.
    """
    binaires = tmp / nom
    binaires.mkdir()
    faux = binaires / "npm"
    faux.write_text(f"#!/bin/sh\necho '{sortie}'\nexit {code}\n")
    faux.chmod(0o755)
    refus = binaires / "gh"
    refus.write_text("#!/bin/sh\nexit 1\n")
    refus.chmod(0o755)
    return f"{binaires}:{os.environ['PATH']}"


def portes_muettes(tmp: Path, nom: str) -> str:
    """Un PATH sans `gh`, où `python3` et `npm` échouent d'entrée.

    Rien d'autre que la porte de construction ne les lance : une release
    qui traverse ce PATH n'a donc exécuté aucune porte, et le prouve mieux
    qu'une absence de ligne dans le compte rendu.
    """
    binaires = tmp / nom
    binaires.mkdir()
    for outil in ("python3", "npm", "gh"):
        refus = binaires / outil
        refus.write_text(f"#!/bin/sh\necho '{outil} ne devait pas être lancé' >&2\nexit 1\n")
        refus.chmod(0o755)
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


def faux_deploiement(dossier: Path, duree: str = "0", etiquette: bool = True,
                     conflit: str = "") -> str:
    """Un PATH où `docker` et `gh` jouent le déploiement, et notent tout.

    Le journal garde l'intervalle de chaque `docker compose up`, en
    nanosecondes : c'est ce qui prouve que deux déploiements ne se sont pas
    recouverts. Le shim rend aussi l'étiquette `com.graphatom.sha` que le
    vrai compose pose sur les conteneurs, et sait rendre **une fois** le
    conflit de nom qu'un `up` interrompu laisse derrière lui.
    """
    dossier.mkdir(parents=True)
    (dossier / "duree.txt").write_text(duree)
    (dossier / "conflit.txt").write_text(conflit)
    (dossier / "services.txt").write_text("github-sync\nweb\nfront\n")
    (dossier / "journal.txt").write_text("")
    if etiquette:
        (dossier / "etiquette").write_text("")
    faux = dossier / "docker"
    faux.write_text(
        "#!/bin/sh\n"
        f'ICI="{dossier}"\n'
        'for A in "$@"; do DERNIER="$A"; done\n'
        'printf "appel %s\\n" "$*" >> "$ICI/journal.txt"\n'
        'case "$1" in\n'
        '  inspect) cat "$ICI/sha-${DERNIER#id-}.txt" 2>/dev/null; exit 0 ;;\n'
        '  rm) printf "retiré %s\\n" "$DERNIER" >> "$ICI/journal.txt"; exit 0 ;;\n'
        "esac\n"
        'case "$2" in\n'
        "  up)\n"
        '    DEBUT=$(date +%s%N)\n'
        '    sleep "$(cat "$ICI/duree.txt")"\n'
        '    printf "up %s %s\\n" "$DEBUT" "$(date +%s%N)" >> "$ICI/journal.txt"\n'
        '    if [ -s "$ICI/conflit.txt" ]; then\n'
        '      cat "$ICI/conflit.txt"; : > "$ICI/conflit.txt"; exit 1\n'
        "    fi\n"
        '    if [ -f "$ICI/etiquette" ]; then\n'
        '      for S in github-sync web front; do\n'
        '        printf "%s" "${GRAPHATOM_SHA:-}" > "$ICI/sha-$S.txt"\n'
        "      done\n"
        "    fi\n"
        '    echo "reconstruit"; exit 0 ;;\n'
        "  ps)\n"
        '    case " $* " in *" -q "*) printf "id-%s\\n" "$DERNIER"; exit 0 ;; esac\n'
        '    cat "$ICI/services.txt"; exit 0 ;;\n'
        "esac\n"
        'echo "commande inconnue : $*" >&2\n'
        "exit 1\n"
    )
    faux.chmod(0o755)
    jeton = dossier / "gh"
    jeton.write_text('#!/bin/sh\necho "jeton-de-test"\n')
    jeton.chmod(0o755)
    return f"{dossier}:{os.environ['PATH']}"


def montees(dossier: Path) -> list[tuple[int, int]]:
    """Les intervalles des `docker compose up` que le faux docker a vus."""
    vus = []
    for ligne in (dossier / "journal.txt").read_text().splitlines():
        if ligne.startswith("up "):
            debut, fin = ligne.split()[1:3]
            vus.append((int(debut), int(fin)))
    return sorted(vus)


def cle_verrou(repo: Path) -> int:
    """La clé du verrou de `deploy` : la somme de contrôle du chemin visé."""
    out = subprocess.run(f"printf '%s' '{repo}' | cksum", shell=True,
                         capture_output=True, text=True, check=True)
    return int(out.stdout.split()[0])


def tenant(dsn: str, cle: int) -> subprocess.Popen:
    """Une session postgres qui prend le verrou et ne le rend jamais."""
    proc = subprocess.Popen([sys.executable, "-c", TIENT_LE_VERROU, dsn, str(cle)],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "pris", "le tenant n'a pas pris le verrou"
    return proc


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
    aucun test ne doit tomber sur le déploiement réel de la machine. Les
    deux budgets d'attente, celui des portes et celui du verrou, sont
    ramenés à quelques secondes — les valeurs de production se vérifient en
    lisant le `cmd`, pas en les subissant.
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
                          "GRAPHATOM_PORTES_DELAI_S": "4",
                          "GRAPHATOM_VERROU_DSN": VERROU_DSN,
                          "GRAPHATOM_VERROU_DELAI_S": "10"} | (plus or {}),
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
    (source / fichier).parent.mkdir(parents=True, exist_ok=True)
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

    # 9 ter. la porte de construction, après une absorption non vide. Deux
    #    branches se compilaient chacune de son côté, leur merge n'a pas un
    #    marqueur de conflit, et `main` sort cassée : git a raison sur le
    #    texte et tort sur le sens. La porte tranche ce cas au compilateur,
    #    en secondes, plutôt qu'à un cycle d'agents de test.

    # 1. absorption vide : aucune porte n'est exécutée. Le PATH le prouve —
    #    `python3` et `npm` y échouent, et la release passe quand même
    repo, workspace, worktree = cas_release(tmp, "portes-a-jour")
    assert release(repo, workspace, SUJET, {"PATH": portes_muettes(tmp, "muettes")}) == 6
    rapport = (workspace / "release.md").read_text()
    assert "rien d'absorbé" in rapport, rapport
    assert "porte" not in rapport, rapport

    # 2. absorption non vide et saine, hors `front/` : la porte python passe
    #    et se nomme — et la porte front, elle, n'est pas exécutée
    repo, workspace, worktree = cas_release(tmp, "portes-saines")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    avance_main(repo, "src/voisin.py", "def voisin():\n    return 1\n")
    assert release(repo, workspace, SUJET, aveugle) == 6
    rapport = (workspace / "release.md").read_text()
    assert "porte compilation python : passée" in rapport, rapport
    assert "front" not in rapport, rapport  # rien d'absorbé sous front/

    # 3. absorption non vide touchant `front/` : les deux portes s'exécutent
    repo, workspace, worktree = cas_release(tmp, "portes-front")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    avance_main(repo, "front/graph-view.tsx", "export const vue = () => null\n")
    assert release(repo, workspace, SUJET, {"PATH": faux_npm(tmp, "npm-passe", 0, "construit")}) == 6
    rapport = (workspace / "release.md").read_text()
    assert "porte compilation python : passée" in rapport, rapport
    assert "porte construction front : passée" in rapport, rapport

    # 4. l'absorption casse la compilation python : code 11, la sortie du
    #    compilateur dans `release.md`, aucune PR tentée, et le worktree
    #    laissé sur sa fusion — c'est un état à réparer, pas à effacer
    repo, workspace, worktree = cas_release(tmp, "portes-python-cassee")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    avance_main(repo, "src/casse.py", "def casse(:\n    return 1\n")
    assert release(repo, workspace, SUJET, aveugle) == 11
    rapport = (workspace / "release.md").read_text()
    assert "casse.py" in rapport and "SyntaxError" in rapport, rapport
    assert "gh pr" not in rapport, "une PR tentée sur un contenu qui ne compile pas"
    assert len(git(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3, \
        "la fusion à réparer a été effacée"

    # 5. l'absorption casse le build du front : même code, même compte rendu
    repo, workspace, worktree = cas_release(tmp, "portes-front-cassee")
    travaille(worktree, "travail.txt", "l'implémentation de l'item\n")
    avance_main(repo, "front/graph-view.tsx", "export const vue = () => null\n")
    casse = faux_npm(tmp, "npm-casse", 1, "graph-view.tsx(25,8): error TS2741")
    assert release(repo, workspace, SUJET, {"PATH": casse}) == 11
    rapport = (workspace / "release.md").read_text()
    assert "TS2741" in rapport, rapport
    assert "gh pr" not in rapport, "une PR tentée sur un front qui ne construit pas"

    # 6. le README dit la release telle qu'elle est : un merge, jamais un
    #    rebase, et la porte que ce merge traverse
    readme = (ROOT / "README.md").read_text()
    assert "rebase quand le rebase passe tout seul" not in readme, \
        "le README décrit encore la release comme rebasant"
    assert "porte de construction" in readme, "le README ne documente pas la porte"
    print("9 ter. porte de construction : rien sur une absorption vide, python "
          "et front sur une absorption saine, code 11 sur chaque rupture ✓")

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

    # 15. la cible du deploy est unique : deux items qui l'atteignent en même
    #    temps doivent déployer l'un après l'autre. Le faux docker met 2 s à
    #    monter et note l'intervalle de chaque `up` — si les deux se
    #    recouvraient, un vrai docker refuserait le second sur un nom pris
    file = tmp / "file"
    file.mkdir()
    concurrent = depot(file)
    docker15 = faux_deploiement(file / "bin", duree="2", etiquette=False)
    rendus: dict[str, dict] = {}
    fils = []
    for nom in ("premier", "second"):
        atelier = file / nom
        atelier.mkdir()
        fils.append(threading.Thread(
            target=lambda n=nom, a=atelier: rendus.__setitem__(
                n, joue("deploy", concurrent, a, plus={"PATH": docker15,
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

    # 16. rejoué sur le même `main`, le deploy ne reconstruit rien : les
    #    conteneurs portent déjà le SHA visé, et le rapport le dit
    memes = tmp / "deja"
    memes.mkdir()
    stable = depot(memes)
    docker16 = faux_deploiement(memes / "bin")
    for nom in ("un", "deux"):
        atelier = memes / nom
        atelier.mkdir()
        outcome = joue("deploy", stable, atelier, plus={"PATH": docker16})
        assert outcome["outcome"] == "done", outcome
    assert len(montees(memes / "bin")) == 1, montees(memes / "bin")
    assert "déjà déployé" in outcome["summary"], outcome
    sha = git(stable, "rev-parse", "--short", "origin/main")
    rapport = (memes / "deux" / "deploy.md").read_text()
    assert f"front porte {sha} - voulu {sha}" in rapport, rapport
    print(f"16. {outcome['summary']} ✓")

    # 17. le verrou meurt avec sa session : celui d'un shell tué en plein vol
    #    ne bloque pas le suivant. Rien sur disque ne lui survit — un verrou
    #    qui survit à un crash serait pire que pas de verrou du tout
    mort = tmp / "session-morte"
    mort.mkdir()
    orphelin = depot(mort)
    docker17 = faux_deploiement(mort / "bin")
    atelier = mort / "item"
    atelier.mkdir()
    tueur = tenant(VERROU_DSN, cle_verrou(orphelin))
    tueur.kill()
    tueur.wait()
    outcome = joue("deploy", orphelin, atelier, plus={"PATH": docker17,
                                                     "GRAPHATOM_VERROU_DELAI_S": "20"})
    assert outcome["outcome"] == "done", outcome
    assert len(montees(mort / "bin")) == 1, montees(mort / "bin")
    deploiement = BUNDLE["nodes"]["deploy"]["config"]["agent"]["cmd"]
    for interdit in ("flock", ".lock", "mkdir"):
        assert interdit not in deploiement, f"le verrou passe par {interdit}"
    print("17. le verrou d'une session tuée est rendu par postgres, "
          "et rien sur disque ne le porte ✓")

    # 18. l'attente est bornée : le verrou tenu par une session vivante fait
    #    rendre `waiting` — que le graph renvoie sur `deploy`, pas sur
    #    `escalate`. Jamais d'escalade pour cause de concurrence
    tenu = tmp / "attente"
    tenu.mkdir()
    occupe = depot(tenu)
    docker18 = faux_deploiement(tenu / "bin")
    atelier = tenu / "item"
    atelier.mkdir()
    tient = tenant(VERROU_DSN, cle_verrou(occupe))
    depart = time.monotonic()
    outcome = joue("deploy", occupe, atelier, plus={"PATH": docker18,
                                                   "GRAPHATOM_VERROU_DELAI_S": "3"})
    ecoule = time.monotonic() - depart
    tient.kill()
    tient.wait()
    assert outcome["outcome"] == "waiting", outcome
    assert 3 <= ecoule < 30, f"l'attente a duré {ecoule:.0f} s"
    assert montees(tenu / "bin") == [], "un `up` est passé sans le verrou"
    assert BUNDLE["nodes"]["deploy"]["edges"]["waiting"] == "deploy", "waiting n'attend pas"
    print(f"18. {outcome['summary']} ✓")

    # 19. le conteneur bâtard d'un `up` interrompu est rattrapé au
    #    déploiement suivant : le message nomme le coupable, le shell le
    #    retire et rejoue — pas d'humain dans la boucle
    batard = tmp / "conflit-docker"
    batard.mkdir()
    coince = depot(batard)
    reste = ("741ae7f1a135_imperative-crunching-conway-init-1")
    docker19 = faux_deploiement(batard / "bin", conflit=(
        "Error response from daemon: Conflict. The container name "
        f'"/{reste}" is already in use by container "86fea9f768f3".\n'))
    atelier = batard / "item"
    atelier.mkdir()
    outcome = joue("deploy", coince, atelier, plus={"PATH": docker19})
    assert outcome["outcome"] == "done", outcome
    assert len(montees(batard / "bin")) == 2, montees(batard / "bin")
    assert f"retiré {reste}" in (batard / "bin" / "journal.txt").read_text()
    assert reste in (atelier / "deploy.md").read_text()
    print(f"19. conteneur bâtard {reste[:20]}… retiré, `up` rejoué ✓")

    # 20. les garde-fous du verrou, relus dans le bundle à chaque tour : lui
    #    seul se sérialise, son attente tient dans le couperet du nœud, et
    #    l'arbitrage entre exclusion mutuelle et attente bornée est écrit
    for nom, node in BUNDLE["nodes"].items():
        shell = node.get("config", {}).get("agent", {}).get("cmd", "")
        if nom != "deploy":
            assert "advisory_lock" not in shell, f"{nom} se sérialise lui aussi"
    config = BUNDLE["nodes"]["deploy"]["config"]
    verrou = int(re.search(r"GRAPHATOM_VERROU_DELAI_S:-(\d+)", deploiement).group(1))
    assert verrou < config["agent"]["timeout_s"] < config["lease_s"], config
    prompt = config["agent"]["prompt"]
    for marque in ("exclusion mutuelle", "attente bornée", "pg_advisory_lock"):
        assert marque in prompt, f"le prompt de deploy ne dit pas « {marque} »"
    readme = (ROOT / "README.md").read_text()
    assert "GRAPHATOM_VERROU_DELAI_S" in readme, "le README ne justifie pas l'attente"
    assert f"{verrou} s d'attente" in readme, "le README ne dit pas le budget retenu"
    print(f"20. seul deploy prend le verrou, attente {verrou} s < timeout_s "
          f"{config['agent']['timeout_s']} s < bail {config['lease_s']} s ✓")

    # 21. le worker du rail n'a pas son venv dans son PATH : le `python3`
    #    ambiant y est celui du système, sans psycopg. Le nœud doit trouver
    #    l'interprète du clone de référence — sinon le verrou se tairait, et
    #    la file ne serait qu'un commentaire dans le bundle
    nu = tmp / "python-nu"
    nu.mkdir()
    cible = depot(nu)
    venv = cible / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python3").symlink_to(sys.executable)
    docker21 = faux_deploiement(nu / "bin")
    sourd = nu / "bin" / "python3"  # celui du PATH ne sait pas importer psycopg
    sourd.write_text("#!/bin/sh\nexit 1\n")
    sourd.chmod(0o755)
    atelier = nu / "item"
    atelier.mkdir()
    tient = tenant(VERROU_DSN, cle_verrou(cible))
    outcome = joue("deploy", cible, atelier, plus={"PATH": docker21,
                                                  "GRAPHATOM_VERROU_DELAI_S": "3"})
    tient.kill()
    tient.wait()
    assert outcome["outcome"] == "waiting", \
        f"le verrou s'est tu faute d'interprète : {outcome}"
    assert montees(nu / "bin") == [], "un `up` est passé sans le verrou"
    print("21. le python du PATH n'a pas psycopg : celui du clone de "
          "référence prend le verrou quand même ✓")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nnœuds shell : OK — déterministes, et jamais sans outcome")


if __name__ == "__main__":
    main()
