"""Les outils partagés des preuves shell de `code-task` (`shell_*_test.py`).

Chaque script se lance seul, `uv run python tests/shell_<thème>_test.py`, et
`tests/` en tête de `sys.path` rend `import shell_outils` possible. Ce
module rassemble ce que plusieurs scripts jouent à l'identique : le dépôt
jetable, le `cmd` d'un nœud joué tel quel, le faux docker qui note ses
`up`, la session qui tient le verrou de `deploy`, le serveur HTTP jetable.

À importer APRÈS avoir posé `GRAPHATOM_DSN` : `db.DSN` se fige à l'import,
et c'est lui que `joue` donne aux nœuds comme base du verrou et du quota.
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, executors  # noqa: E402

from outils import git  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = json.loads((ROOT / "examples" / "code-task.json").read_text())
SUJET = "gh:Acid3croco/graphatom#77"
# la base où `deploy` pose son verrou. Celle du script qui nous lance fait
# l'affaire : les verrous consultatifs sont locaux à une base, et la clé,
# elle, vient du chemin de la cible — deux exécutions d'un même test dans
# deux répertoires temporaires ne se disputent donc jamais le même verrou.
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


def dans_le_path(dossier: Path) -> str:
    """Le PATH courant, `dossier` en tête : ses shims passent avant le vrai."""
    return f"{dossier}:{os.environ['PATH']}"


def sans_gh(tmp: Path) -> str:
    """Un PATH où `gh` échoue : le deploy s'arrête au jeton, avant docker."""
    binaires = tmp / "bin"
    binaires.mkdir(exist_ok=True)
    faux = binaires / "gh"
    faux.write_text("#!/bin/sh\nexit 1\n")
    faux.chmod(0o755)
    return dans_le_path(binaires)


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
        'for MOT in "$@"; do DERNIER="$MOT"; done\n'
        'if [ "$1" = inspect ]; then\n'
        '  if [ "${GRAPHATOM_TEST_BAD_SERVICE:-}" = "${DERNIER#id-}" ]; then\n'
        '    echo mauvais-sha\n'
        '  else printf "%s\\n" "$GRAPHATOM_WORKER_SHA"; fi\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$2" = ps ]; then\n'
        '  case " $* " in *" -q "*) printf "id-%s\\n" "$DERNIER" ;;\n'
        '    *) cat "$ICI/services.txt" ;; esac\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$2" = logs ]; then cat "$ICI/log.txt"; exit 0; fi\n'
        'echo "commande inconnue : $*" >&2\n'
        "exit 1\n"
    )
    faux.chmod(0o755)
    return dans_le_path(binaires)


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
    return dans_le_path(dossier)


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

            def log_message(self, *_):
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
    worker = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        executors.command(executors.resolve(BUNDLE, BUNDLE["nodes"][node])),
        shell=True, cwd=workspace, check=False, capture_output=True,
        env=os.environ | {"GRAPHATOM_REPO_DIR": str(repo),
                          "GRAPHATOM_WORKSPACE": str(workspace),
                          "GRAPHATOM_SUBJECT_KEY": subject,
                          "GRAPHATOM_WORKER_SHA": worker,
                          "GRAPHATOM_WEB_URL": "http://127.0.0.1:9",
                          "GRAPHATOM_FRONT_URL": "http://127.0.0.1:9",
                          "GRAPHATOM_PORTES_DELAI_S": "4",
                          "GRAPHATOM_QUOTA_DSN": VERROU_DSN,
                          "GRAPHATOM_VERROU_DSN": VERROU_DSN,
                          "GRAPHATOM_VERROU_DELAI_S": "10"} | (plus or {}),
    )
    return json.loads((workspace / "outcome.json").read_text())


def cible_deployable(tmp: Path, nom: str, **shim) -> tuple[Path, Path, Path]:
    """Un quartier neuf : son clone de référence, son faux docker, son atelier.

    Chaque preuve du verrou veut une cible à elle — la clé du verrou vient
    du chemin du clone, et le journal du faux docker ne doit compter que
    ses `up`. Rend le clone, le dossier du shim (à mettre dans le PATH, à
    relire avec `montees`) et le workspace de l'item.
    """
    quartier = tmp / nom
    quartier.mkdir()
    cible = depot(quartier)
    faux_deploiement(quartier / "bin", **shim)
    atelier = quartier / "item"
    atelier.mkdir()
    return cible, quartier / "bin", atelier
