"""Les bords du quota de constructions : refus, attente, révocation, signaux.

Sur un Postgres jetable, `quota.run` joué dans ce processus — le preneur pose
des gestionnaires de signaux, donc il vit dans le fil principal ; le test
tient ou coupe les verrous depuis un fil d'à côté, dont toute faute remonte
à la preuve au `join`. Aucun lancement ne se fie à une pause : chaque fil et
chaque commande disent quand ils sont prêts.

  1. sans DSN, sans identité d'item, sans commande : refus nommé, jamais
     une place prise ; un workspace nomme l'item — le `item-N` le plus
     profond gagne, un suffixe qui n'est pas un nombre ne compte pas — ou
     lui vaut une identité stable — la même d'un processus python à
     l'autre, distincte d'un chemin à l'autre
  2. sans workspace, aucun marqueur d'attente n'est écrit ni retiré, et le
     code de la commande est rendu tel quel
  3. la place globale occupée : le preneur attend en silence, redit son
     attente une seule fois après `REPORT_S` — le temps écoulé depuis le
     début de l'attente —, touche son marqueur, puis prend la place libérée
  4. `_stop` : une commande déjà finie n'est pas touchée ; une famille qui
     ignore SIGTERM tombe au SIGKILL, le chef comme son enfant ; un groupe
     qui disparaît sous le coup est absorbé, le chef récolté
  5. la session PostgreSQL du preneur meurt en pleine commande : la commande
     et son enfant sont arrêtés, le code 75 rendu, la perte imprimée ; morte
     pendant l'attente d'une place : 75, la perte dite, rien lancé
  6. une base injoignable : 75 sans rien lancer ; une commande introuvable :
     son erreur remonte telle quelle, la place globale et la place d'item
     rendues
  7. SIGTERM pendant la commande : elle et son enfant sont arrêtés, la sortie
     vaut 128+15, le gestionnaire d'avant est remis
  8. SIGTERM pendant l'attente d'une place : sortie 128+15, marqueur retiré
  9. quota 2 : deux commandes d'un même item se suivent — la seconde part
     après la fin de la première —, deux items se recouvrent

Usage : uv run python tests/quota_edges_test.py
"""

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-quota-edges")

from graphatom import db, quota  # noqa: E402

from bords_outils import attend, patche, sortie_capturee  # noqa: E402

SRC = str(Path(__file__).resolve().parents[1] / "src")
INJOIGNABLE = "postgresql://graphatom:x@127.0.0.1:1/graphatom"
EN_ATTENTE = "graphatom-quota-en-attente"  # le nom de session du preneur qui attend
DSN_VARS = ("GRAPHATOM_QUOTA_DSN", "GRAPHATOM_AGENT_DSN", "GRAPHATOM_DSN")
# le chef écrit son PID et celui de son enfant, puis l'attend : c'est
# l'enfant — le fils du fils — qui doit mourir avec lui
FAMILLE = "sleep 30 & echo $$ $! > {temoin}; wait"
TETU = "trap '' TERM; " + FAMILLE
IDENTITE = "from graphatom import quota; print(quota._item_id())"
PRENEUR = "import sys; from graphatom import quota; sys.exit(quota.run(sys.argv[1:]))"
# la commande d'un preneur externe date son début, attend le feu vert, date sa fin
CHRONO = ("date +%s.%N > {ou}/debut; while ! test -e {go}; do sleep 0.05; done; "
          "date +%s.%N > {ou}/fin")


class Fil(threading.Thread):
    """Un fil démon dont la faute remonte à la preuve principale, au `join`."""

    def __init__(self, cible):
        super().__init__(target=cible, daemon=True)
        self.faute: BaseException | None = None

    def run(self) -> None:
        try:
            super().run()
        except BaseException as exc:  # noqa: BLE001 — relevée au join
            self.faute = exc

    def join(self, timeout: float | None = None) -> None:
        super().join(timeout)
        if self.faute is not None:
            raise self.faute


@contextlib.contextmanager
def environnement(**valeurs: str | None):
    """Des variables posées ou retirées le temps d'une preuve, puis remises."""
    anciennes = {nom: os.environ.get(nom) for nom in valeurs}
    for nom, valeur in valeurs.items():
        if valeur is None:
            os.environ.pop(nom, None)
        else:
            os.environ[nom] = valeur
    try:
        yield
    finally:
        for nom, valeur in anciennes.items():
            if valeur is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = valeur


def refuse(appel, erreur: type, motif: str) -> None:
    try:
        appel()
    except erreur as exc:
        assert motif in str(exc), exc
    else:
        raise AssertionError(f"aucun refus : {motif}")


def famille(temoin: Path) -> list[int]:
    """Les PID du chef et de son enfant, tels que la commande les a écrits."""
    return [int(pid) for pid in temoin.read_text().split()] if temoin.exists() else []


def famille_prete(temoin: Path) -> bool:
    return len(famille(temoin)) == 2


def famille_morte(temoin: Path) -> bool:
    """Ni le chef ni son enfant ne figurent plus dans /proc."""
    pids = famille(temoin)
    return len(pids) == 2 and not any(Path(f"/proc/{pid}").exists() for pid in pids)


def identite(workspace: str) -> int:
    """`_item_id()` sans GRAPHATOM_ITEM_ID, lu du seul workspace."""
    with environnement(GRAPHATOM_ITEM_ID=None, GRAPHATOM_WORKSPACE=workspace):
        return quota._item_id()


def refus() -> None:
    """1. sans DSN, sans item, sans commande : refusé avant toute place."""
    with environnement(**{nom: None for nom in DSN_VARS}):
        refuse(quota._dsn, RuntimeError, "GRAPHATOM_QUOTA_DSN absente")
    with environnement(GRAPHATOM_ITEM_ID=None, GRAPHATOM_WORKSPACE=None):
        refuse(quota._item_id, RuntimeError, "GRAPHATOM_WORKSPACE absents")
    assert identite("/data/item-12/c1") == 12
    assert identite("/tmp/x/item-1/item-7") == 7, "le item-N le plus profond ne gagne pas"
    assert identite("/tmp/x/item-abc/") == zlib.crc32(b"/tmp/x/item-abc"), \
        "un suffixe qui n'est pas un nombre devrait valoir l'identité du chemin"
    refuse(lambda: quota.run([]), ValueError, "demande une commande")
    print("1. DSN, identité d'item ou commande absente : refus nommé ; un "
          "workspace item-N vaut N, le plus profond, item-abc l'identité du chemin ✓")


def identite_ailleurs(workspace: Path) -> int:
    """`_item_id()` lu par un autre processus python, pour ce workspace."""
    env = {**os.environ, "GRAPHATOM_WORKSPACE": str(workspace), "PYTHONPATH": SRC}
    env.pop("GRAPHATOM_ITEM_ID", None)
    sortie = subprocess.run([sys.executable, "-c", IDENTITE], env=env,
                            capture_output=True, text=True, check=True)
    return int(sortie.stdout)


def identite_stable(tmp: Path) -> None:
    """1 bis. un workspace sans item-N : une identité au chemin, la même partout."""
    ici, la = tmp / "ailleurs", tmp / "encore-ailleurs"
    stable = identite(str(ici))
    assert stable != 7 and stable == identite_ailleurs(ici) == identite_ailleurs(ici), \
        "l'identité change d'un processus à l'autre"
    assert identite_ailleurs(la) != stable, "deux chemins, une seule identité"
    print("1 bis. identité d'un workspace : la même d'un processus python à l'autre, "
          "distincte d'un chemin à l'autre ✓")


def tenant(duree_s: float) -> Fil:
    """Un fil qui tient la seule place globale pendant `duree_s` — rendu une
    fois la place prise, jamais avant."""
    pret = threading.Event()

    def tient():
        with psycopg.connect(db.DSN, autocommit=True) as conn:
            assert conn.execute("SELECT pg_try_advisory_lock(%s, 0)",
                                (quota.BUILD_LOCK_NAMESPACE,)).fetchone()[0]
            pret.set()
            time.sleep(duree_s)

    fil = Fil(tient)
    fil.start()
    if not pret.wait(5):
        fil.join()  # sa faute, s'il en a une, avant la nôtre
        raise AssertionError("la place n'est jamais tenue")
    return fil


def sans_workspace() -> None:
    """2. sans workspace : aucun marqueur, même en attendant, et le code tel quel."""
    fil = tenant(0.4)
    with environnement(GRAPHATOM_WORKSPACE=None), patche(quota, "LEASE_REFRESH_S", 0.1), \
            sortie_capturee() as sortie:
        assert quota.run(["sh", "-c", "exit 4"]) == 4
    fil.join()
    assert "place 1/1 prise après" in sortie.getvalue(), sortie.getvalue()
    print("2. sans workspace : attente sans marqueur, place prise, code 4 tel quel ✓")


def place_occupee(workspace: Path) -> None:
    """3. la place occupée : attente dite une fois, en secondes écoulées,
    marqueur touché, puis place prise."""
    marqueur = workspace / quota.WAIT_FILE
    fil = tenant(3.0)
    with patche(quota, "REPORT_S", 2.0), patche(quota, "LEASE_REFRESH_S", 0.1), \
            sortie_capturee() as sortie:
        depart = time.monotonic()
        code = quota.run(["sh", "-c", f"test -e {marqueur} && exit 9; exit 0"])
        duree = time.monotonic() - depart
    fil.join()
    assert code == 0, "le marqueur d'attente a survécu à la prise"
    assert duree >= 2.5, duree
    rapports = re.findall(r"quota de constructions occupé — attente (\d+) s",
                          sortie.getvalue())
    assert len(rapports) == 1, f"l'attente devrait être dite une fois : {sortie.getvalue()}"
    assert 2 <= int(rapports[0]) <= 4, f"le temps dit n'est pas le temps écoulé : {rapports}"
    prise = re.search(r"place 1/1 prise après (\d+\.\d) s", sortie.getvalue())
    assert prise, sortie.getvalue()
    assert 2.5 <= float(prise.group(1)) <= duree + 0.1, (prise.group(1), duree)
    assert not marqueur.exists()
    print(f"3. place occupée {duree:.1f} s : attente dite une fois, après {rapports[0]} s, "
          f"marqueur touché puis retiré, place prise après {prise.group(1)} s ✓")


def arret(tmp: Path) -> None:
    """4. `_stop` : rien sur une commande finie ; SIGKILL sur une famille têtue."""
    finie = subprocess.Popen(["true"], start_new_session=True)
    finie.wait()
    quota._stop(finie)
    assert finie.returncode == 0
    temoin = tmp / "tetu.txt"
    tetu = subprocess.Popen(["sh", "-c", TETU.format(temoin=temoin)],
                            start_new_session=True)
    assert attend(lambda: famille_prete(temoin)), "le piège TERM n'est jamais armé"
    depart = time.monotonic()
    quota._stop(tetu)
    duree = time.monotonic() - depart
    assert tetu.returncode == -signal.SIGKILL, tetu.returncode
    assert duree >= 5, duree
    assert attend(lambda: famille_morte(temoin)), "l'enfant de la famille têtue survit"
    print(f"4. commande finie : intouchée ; famille têtue : SIGKILL après "
          f"{duree:.1f} s, chef et enfant partis ✓")


def groupe_evanoui() -> None:
    """4 bis. le groupe qui disparaît sous `_stop` n'est pas une erreur : le chef
    est quand même récolté."""
    reel = os.killpg
    vivant = subprocess.Popen(["sleep", "30"], start_new_session=True)

    def disparu(pgid: int, sig: int) -> None:
        if sig == signal.SIGKILL:  # le groupe part juste avant qu'on le frappe
            reel(pgid, sig)
        raise ProcessLookupError(pgid)

    with patche(os, "killpg", disparu):
        quota._stop(vivant)
    assert vivant.returncode == -signal.SIGKILL, vivant.returncode
    print("4 bis. groupe disparu entre SIGTERM et SIGKILL : absorbé, chef récolté ✓")


def session_coupee(tmp: Path) -> None:
    """5. la session du preneur meurt : commande arrêtée, 75, perte imprimée."""
    pid_txt = tmp / "pid.txt"

    def coupe():
        with psycopg.connect(db.DSN, autocommit=True) as conn:
            requete = ("SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
                       "AND granted AND classid = %s AND objsubid = 2")
            assert attend(lambda: conn.execute(
                requete, (quota.BUILD_LOCK_NAMESPACE,)).fetchone() is not None)
            assert attend(lambda: famille_prete(pid_txt)), "la commande n'a pas démarré"
            pid = conn.execute(requete, (quota.BUILD_LOCK_NAMESPACE,)).fetchone()[0]
            conn.execute("SELECT pg_terminate_backend(%s)", (pid,))

    fil = Fil(coupe)
    fil.start()
    with sortie_capturee() as sortie:
        code = quota.run(["sh", "-c", FAMILLE.format(temoin=pid_txt)])
    fil.join()
    assert code == 75, code
    assert "quota de constructions perdu" in sortie.getvalue(), sortie.getvalue()
    assert attend(lambda: famille_morte(pid_txt)), "la commande ou son enfant survit"
    print("5. session PostgreSQL coupée : commande et enfant arrêtés, code 75, "
          "perte dite ✓")


def session_coupee_en_attente(workspace: Path) -> None:
    """5 bis. la session meurt pendant l'attente d'une place : 75, perte dite,
    rien lancé."""
    marqueur, jamais = workspace / quota.WAIT_FILE, workspace / "jamais-attendu.txt"
    fil = tenant(3.0)

    def coupe():
        with psycopg.connect(db.DSN, autocommit=True) as conn:
            assert attend(marqueur.exists), "le preneur n'attend jamais"
            coupees = conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name = %s", (EN_ATTENTE,)).fetchall()
            assert coupees == [(True,)], f"session du preneur introuvable : {coupees}"

    tueur = Fil(coupe)
    tueur.start()
    with environnement(GRAPHATOM_QUOTA_DSN=make_conninfo(db.DSN, application_name=EN_ATTENTE)), \
            sortie_capturee() as sortie:
        code = quota.run(["touch", str(jamais)])
    tueur.join()
    fil.join()
    assert code == 75, code
    assert "quota de constructions perdu" in sortie.getvalue(), sortie.getvalue()
    assert not jamais.exists(), "commande lancée sans place"
    print("5 bis. session PostgreSQL coupée pendant l'attente : code 75, perte dite, "
          "commande jamais lancée ✓")


def base_injoignable(tmp: Path) -> None:
    """6. une base injoignable : 75, sans lancer la commande."""
    with environnement(GRAPHATOM_QUOTA_DSN=INJOIGNABLE), sortie_capturee():
        assert quota.run(["touch", str(tmp / "jamais.txt")]) == 75
    assert not (tmp / "jamais.txt").exists(), "commande lancée sans place"
    print("6. base injoignable : 75, commande jamais lancée ✓")


def commande_introuvable(tmp: Path) -> None:
    """6 bis. une commande introuvable sous quota : son erreur remonte telle
    quelle, et les deux places sont rendues."""
    with sortie_capturee():
        refuse(lambda: quota.run([str(tmp / "nulle-part")]), FileNotFoundError, "nulle-part")
    with psycopg.connect(db.DSN, autocommit=True, row_factory=dict_row) as conn:
        assert quota.en_vol(conn) == 0, "place globale gardée"
        assert quota._try_item_place(conn, 7) is not None, "place d'item gardée"
    print("6 bis. commande introuvable : FileNotFoundError telle quelle, place globale "
          "et place d'item rendues ✓")


def signal_differe(quand, sig: int = signal.SIGTERM) -> Fil:
    """Un fil qui envoie `sig` au processus dès que `quand` devient vrai."""
    def envoie():
        assert attend(quand), "le moment du signal n'est jamais venu"
        os.kill(os.getpid(), sig)
    fil = Fil(envoie)
    fil.start()
    return fil


def interrompu(appel) -> int:
    """Le code de sortie qu'un SIGTERM impose au preneur."""
    try:
        appel()
    except SystemExit as exc:
        return exc.code
    raise AssertionError("le preneur a survécu au signal")


def sigterm_en_commande(tmp: Path) -> None:
    """7. SIGTERM pendant la commande : arrêtée, sortie 143, gestionnaire remis."""
    pid_txt = tmp / "pid-terme.txt"
    avant = signal.getsignal(signal.SIGTERM)
    fil = signal_differe(lambda: famille_prete(pid_txt))
    with sortie_capturee():
        code = interrompu(lambda: quota.run(["sh", "-c", FAMILLE.format(temoin=pid_txt)]))
    fil.join()
    assert code == 128 + signal.SIGTERM, code
    assert attend(lambda: famille_morte(pid_txt)), "la commande ou son enfant survit"
    assert signal.getsignal(signal.SIGTERM) is avant, "gestionnaire non remis"
    print("7. SIGTERM en pleine commande : commande et enfant arrêtés, sortie 143, "
          "gestionnaire d'avant remis ✓")


def sigterm_en_attente(workspace: Path) -> None:
    """8. SIGTERM pendant l'attente d'une place : sortie 143, marqueur retiré."""
    marqueur = workspace / quota.WAIT_FILE
    fil = tenant(3.0)
    tireur = signal_differe(marqueur.exists)
    with sortie_capturee():
        code = interrompu(lambda: quota.run(["touch", str(workspace / "jamais.txt")]))
    tireur.join()
    fil.join()
    assert code == 128 + signal.SIGTERM, code
    assert not marqueur.exists(), "marqueur d'attente laissé"
    assert not (workspace / "jamais.txt").exists(), "commande lancée sans place"
    print("8. SIGTERM pendant l'attente : sortie 143, marqueur retiré, rien lancé ✓")


def preneur_externe(item: int, ou: Path, go: Path) -> subprocess.Popen:
    """Un preneur dans son propre python, quota 2, pour l'item `item` : sa
    commande date son début dans `ou`, attend `go`, puis date sa fin."""
    ou.mkdir()
    env = {**os.environ, "GRAPHATOM_ITEM_ID": str(item), "GRAPHATOM_MAX_BUILDS": "2",
           "GRAPHATOM_WORKSPACE": str(ou), "PYTHONPATH": SRC}
    return subprocess.Popen(
        [sys.executable, "-c", PRENEUR, "sh", "-c", CHRONO.format(ou=ou, go=go)],
        env=env, stdout=subprocess.DEVNULL)


def chronos(tmp: Path, items: tuple[int, int], demarrages: int) -> tuple[float, float]:
    """Deux preneurs externes lancés ensemble, libérés dès que `demarrages`
    commandes ont daté leur début : rend le dernier début et la première fin."""
    nom = f"preneurs-{items[0]}-{items[1]}"
    go = tmp / f"{nom}.go"
    dossiers = [tmp / f"{nom}-{rang}" for rang in range(2)]
    procs = [preneur_externe(item, ou, go) for item, ou in zip(items, dossiers)]

    def demarres() -> int:
        return sum((ou / "debut").exists() for ou in dossiers)

    def moments(nom: str) -> list[float]:
        return [float((ou / nom).read_text()) for ou in dossiers]

    assert attend(lambda: demarres() >= demarrages, 60), "les commandes ne démarrent pas"
    go.touch()
    assert [proc.wait(timeout=60) for proc in procs] == [0, 0]
    return max(moments("debut")), min(moments("fin"))


def places_par_item(tmp: Path) -> None:
    """9. quota 2 : un même item se suit, deux items se recouvrent."""
    dernier_debut, premiere_fin = chronos(tmp, (7, 7), demarrages=1)
    assert dernier_debut >= premiere_fin, "le même item a pris les deux places"
    dernier_debut, premiere_fin = chronos(tmp, (7, 8), demarrages=2)
    assert dernier_debut < premiere_fin, "deux items ne se recouvrent pas"
    print("9. quota 2 : la seconde commande d'un item part après la fin de la première, "
          "deux items travaillent en même temps ✓")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-quota-edges-"))
    workspace = tmp / "item-7"
    workspace.mkdir()
    db.init_db()
    quota.MAX_BUILDS = quota.MAX_BUILDS_PER_ITEM = 1
    os.environ["GRAPHATOM_QUOTA_DSN"] = db.DSN
    os.environ["GRAPHATOM_ITEM_ID"] = "7"
    os.environ["GRAPHATOM_WORKSPACE"] = str(workspace)
    for nom in ("GRAPHATOM_RUN_ID", "GRAPHATOM_LEASE_S"):
        os.environ.pop(nom, None)
    try:
        refus()
        identite_stable(tmp)
        sans_workspace()
        place_occupee(workspace)
        arret(tmp)
        groupe_evanoui()
        session_coupee(tmp)
        session_coupee_en_attente(workspace)
        base_injoignable(tmp)
        commande_introuvable(tmp)
        sigterm_en_commande(tmp)
        sigterm_en_attente(workspace)
        places_par_item(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nbords du quota : OK")


if __name__ == "__main__":
    main()
