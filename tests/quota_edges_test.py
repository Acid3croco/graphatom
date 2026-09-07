"""Les bords du quota de constructions : refus, attente, révocation, signaux.

Sur un Postgres jetable, `quota.run` joué dans ce processus — le preneur pose
des gestionnaires de signaux, donc il vit dans le fil principal ; le test
tient ou coupe les verrous depuis un fil d'à côté :

  1. sans DSN, sans identité d'item, sans commande : refus nommé, jamais
     une place prise ; un workspace nomme l'item ou lui vaut une identité stable
  2. sans workspace, aucun marqueur d'attente n'est écrit ni retiré, et le
     code de la commande est rendu tel quel
  3. la place globale occupée : le preneur attend, redit son attente après
     `REPORT_S`, touche son marqueur, puis prend la place libérée
  4. `_stop` : une commande déjà finie n'est pas touchée ; une famille qui
     ignore SIGTERM tombe au SIGKILL ; un groupe qui disparaît sous le coup
     est absorbé, le chef récolté
  5. la session PostgreSQL du preneur meurt en pleine commande : la commande
     est arrêtée, le code 75 rendu, la perte imprimée
  6. une base injoignable : 75 sans rien lancer
  7. SIGTERM pendant la commande : elle est arrêtée, la sortie vaut 128+15,
     le gestionnaire d'avant est remis
  8. SIGTERM pendant l'attente d'une place : sortie 128+15, marqueur retiré

Usage : uv run python tests/quota_edges_test.py
"""

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import provision_postgres  # noqa: E402

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-quota-edges")

from graphatom import db, quota  # noqa: E402

from bords_outils import attend, patche, sortie_capturee  # noqa: E402

INJOIGNABLE = "postgresql://graphatom:x@127.0.0.1:1/graphatom"
DSN_VARS = ("GRAPHATOM_QUOTA_DSN", "GRAPHATOM_AGENT_DSN", "GRAPHATOM_DSN")
TETU = "trap '' TERM; echo pret > {temoin}; sleep 30"


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


def refus() -> None:
    """1. sans DSN, sans item, sans commande : refusé avant toute place."""
    with environnement(**{nom: None for nom in DSN_VARS}):
        refuse(quota._dsn, RuntimeError, "GRAPHATOM_QUOTA_DSN absente")
    with environnement(GRAPHATOM_ITEM_ID=None, GRAPHATOM_WORKSPACE=None):
        refuse(quota._item_id, RuntimeError, "GRAPHATOM_WORKSPACE absents")
    with environnement(GRAPHATOM_ITEM_ID=None, GRAPHATOM_WORKSPACE="/data/item-12/c1"):
        assert quota._item_id() == 12
    with environnement(GRAPHATOM_ITEM_ID=None, GRAPHATOM_WORKSPACE="/tmp/ailleurs"):
        assert quota._item_id() == quota._item_id() != 12, "identité instable"
    refuse(lambda: quota.run([]), ValueError, "demande une commande")
    print("1. DSN, identité d'item ou commande absente : refus nommé ; un "
          "workspace item-N vaut N, un autre une identité stable ✓")


def sans_workspace() -> None:
    """2. sans workspace : aucun marqueur, même en attendant, et le code tel quel."""
    fil = tenant(0.4)
    time.sleep(0.1)
    with environnement(GRAPHATOM_WORKSPACE=None), patche(quota, "LEASE_REFRESH_S", 0.1), \
            sortie_capturee() as sortie:
        assert quota.run(["sh", "-c", "exit 4"]) == 4
    fil.join()
    assert "place 1/1 prise après" in sortie.getvalue(), sortie.getvalue()
    print("2. sans workspace : attente sans marqueur, place prise, code 4 tel quel ✓")


def tenant(duree_s: float, pret: threading.Event | None = None) -> threading.Thread:
    """Un fil qui tient la seule place globale pendant `duree_s`."""
    def tient():
        with psycopg.connect(db.DSN, autocommit=True) as conn:
            assert conn.execute("SELECT pg_try_advisory_lock(%s, 0)",
                                (quota.BUILD_LOCK_NAMESPACE,)).fetchone()[0]
            if pret is not None:
                pret.set()
            time.sleep(duree_s)
    fil = threading.Thread(target=tient, daemon=True)
    fil.start()
    return fil


def place_occupee(workspace: Path) -> None:
    """3. la place occupée : attente dite, marqueur touché, puis place prise."""
    marqueur = workspace / quota.WAIT_FILE
    fil = tenant(0.8)
    time.sleep(0.2)
    with patche(quota, "REPORT_S", 0.1), patche(quota, "LEASE_REFRESH_S", 0.1), \
            sortie_capturee() as sortie:
        depart = time.monotonic()
        code = quota.run(["sh", "-c", f"test -e {marqueur} && exit 9; exit 0"])
        duree = time.monotonic() - depart
    fil.join()
    assert code == 0, "le marqueur d'attente a survécu à la prise"
    assert duree >= 0.5, duree
    assert "quota de constructions occupé — attente" in sortie.getvalue(), sortie.getvalue()
    assert "place 1/1 prise après" in sortie.getvalue()
    assert not marqueur.exists()
    print(f"3. place occupée {duree:.1f} s : attente dite, marqueur touché puis "
          "retiré, place prise à la libération ✓")


def arret(tmp: Path) -> None:
    """4. `_stop` : rien sur une commande finie ; SIGKILL sur une famille têtue."""
    finie = subprocess.Popen(["true"], start_new_session=True)
    finie.wait()
    quota._stop(finie)
    assert finie.returncode == 0
    temoin = tmp / "tetu.txt"
    tetu = subprocess.Popen(["sh", "-c", TETU.format(temoin=temoin)],
                            start_new_session=True)
    assert attend(temoin.exists), "le piège TERM n'est jamais armé"
    depart = time.monotonic()
    quota._stop(tetu)
    duree = time.monotonic() - depart
    assert tetu.returncode == -signal.SIGKILL, tetu.returncode
    assert duree >= 5, duree
    print(f"4. commande finie : intouchée ; famille têtue : SIGKILL après "
          f"{duree:.1f} s ✓")


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
            assert attend(pid_txt.exists)
            pid = conn.execute(requete, (quota.BUILD_LOCK_NAMESPACE,)).fetchone()[0]
            conn.execute("SELECT pg_terminate_backend(%s)", (pid,))

    fil = threading.Thread(target=coupe, daemon=True)
    fil.start()
    with sortie_capturee() as sortie:
        code = quota.run(["sh", "-c", f"echo $$ > {pid_txt}; exec sleep 30"])
    fil.join()
    assert code == 75, code
    assert "quota de constructions perdu" in sortie.getvalue(), sortie.getvalue()
    enfant = int(pid_txt.read_text())
    assert attend(lambda: not Path(f"/proc/{enfant}").exists()), "la commande survit"
    print("5. session PostgreSQL coupée : commande arrêtée, code 75, perte dite ✓")


def base_injoignable(tmp: Path) -> None:
    """6. une base injoignable : 75, sans lancer la commande."""
    with environnement(GRAPHATOM_QUOTA_DSN=INJOIGNABLE), sortie_capturee():
        assert quota.run(["touch", str(tmp / "jamais.txt")]) == 75
    assert not (tmp / "jamais.txt").exists(), "commande lancée sans place"
    print("6. base injoignable : 75, commande jamais lancée ✓")


def signal_differe(quand, sig: int = signal.SIGTERM) -> threading.Thread:
    """Un fil qui envoie `sig` au processus dès que `quand` devient vrai."""
    def envoie():
        assert attend(quand), "le moment du signal n'est jamais venu"
        os.kill(os.getpid(), sig)
    fil = threading.Thread(target=envoie, daemon=True)
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
    fil = signal_differe(pid_txt.exists)
    with sortie_capturee():
        code = interrompu(lambda: quota.run(
            ["sh", "-c", f"echo $$ > {pid_txt}; exec sleep 30"]))
    fil.join()
    assert code == 128 + signal.SIGTERM, code
    enfant = int(pid_txt.read_text())
    assert attend(lambda: not Path(f"/proc/{enfant}").exists()), "la commande survit"
    assert signal.getsignal(signal.SIGTERM) is avant, "gestionnaire non remis"
    print("7. SIGTERM en pleine commande : commande arrêtée, sortie 143, "
          "gestionnaire d'avant remis ✓")


def sigterm_en_attente(workspace: Path) -> None:
    """8. SIGTERM pendant l'attente d'une place : sortie 143, marqueur retiré."""
    marqueur = workspace / quota.WAIT_FILE
    pret = threading.Event()
    fil = tenant(3.0, pret)
    assert pret.wait(5), "la place n'est jamais tenue"
    signal_differe(marqueur.exists)
    with sortie_capturee():
        code = interrompu(lambda: quota.run(["touch", str(workspace / "jamais.txt")]))
    fil.join()
    assert code == 128 + signal.SIGTERM, code
    assert not marqueur.exists(), "marqueur d'attente laissé"
    assert not (workspace / "jamais.txt").exists(), "commande lancée sans place"
    print("8. SIGTERM pendant l'attente : sortie 143, marqueur retiré, rien lancé ✓")


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
        sans_workspace()
        place_occupee(workspace)
        arret(tmp)
        groupe_evanoui()
        session_coupee(tmp)
        base_injoignable(tmp)
        sigterm_en_commande(tmp)
        sigterm_en_attente(workspace)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nbords du quota : OK")


if __name__ == "__main__":
    main()
