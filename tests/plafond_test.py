"""Le test des plafonds de l'ordonnanceur : la charge est une file.

Le fan-out multiplie les runs réservables, et le nombre d'items ne les borne
plus. Ce que les deux plafonds doivent tenir, sur une base jetable à soi —
le dispatch réel, avec un exécuteur factice qui garde les runs en vol jusqu'à
ce que le test les rende :

  1. plafond global à N : plus d'items et de candidats réservables que N, et
     jamais plus de N runs `running` en base, tick après tick
  2. ce que le plafond retient attend — aucune ligne `node_run`, donc aucun
     bail posé et aucune tentative consommée — puis part aux ticks suivants,
     sans qu'aucun run ne passe par un état d'échec
  3. plafond par item : un item en fan-out plus large que son plafond
     n'empêche pas un item sur un nœud bon marché de démarrer, au même tick
  4. le défaut se dérive du nombre de cœurs, la configuration le surcharge,
     et `GET /api/load` rend les runs en vol et le plafond effectif

La base est celle de l'item de test, dérivée comme celle d'un agent : le
compte des runs en vol est global, donc il ne se mesure que seul.

Usage : uv run python tests/plafond_test.py
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import blocks, db, graph, kernel, scheduler, web  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# l'instance jetable : celle de l'agent qui nous lance, sinon la nôtre
INSTANCE = os.environ.get("GRAPHATOM_AGENT_DSN") or db.DSN
os.environ["GRAPHATOM_AGENT_DSN"] = INSTANCE
# un numéro d'item que le rail n'atteindra pas, et le pid pour que deux
# exécutions concurrentes ne se disputent pas la même base
ITEM_ID = 930000 + os.getpid() % 10000

LEASE_S = 600  # aucun bail n'expire pendant le test : le faucheur n'a rien à faire

# Les runs que l'exécuteur factice tient en main. Ils restent `running` en
# base tant que le test ne les rend pas : c'est ce qui fait la charge.
EN_VOL: list[int] = []


def bundle(k: int | None) -> dict:
    """Un ACT stub, en fan-out de k candidats — ou sans fan-out du tout."""
    config: dict = {"duration_s": 0, "lease_s": LEASE_S}
    if k is not None:
        config["fanout"] = {"variants": [{"label": f"v{i}"} for i in range(k)],
                            "reduce": "first_pass"}
    return {
        "name": f"plafond-{k}",
        "entry": "travail",
        "budgets": {"escalations": 2, "wall_deadline_hours": 1},
        "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
        "nodes": {
            "travail": {"block": "ACT", "config": config, "edges": {"ok": "fini"}},
            "escalate": {
                "block": "WAIT", "escalade": True,
                "config": {"question": "On retente ?", "options": ["retry"],
                           "owner": "test", "deadline_minutes": 60},
                "edges": {"retry": "travail", "expired": "abandon"},
            },
            "fini": {"terminal": True},
            "abandon": {"terminal": True},
        },
    }


# ------------------------------------------------------------------ outillage


def fake_execute(run_id: int, item_id: int) -> None:
    """L'exécuteur du tick, en factice : le run vole, et ne rend rien."""
    EN_VOL.append(run_id)


def seme(conn, k: int | None) -> int:
    """Un item de plus, sur son nœud d'entrée — en fan-out de k, ou seul."""
    revision = graph.publish(conn, bundle(k))
    return kernel.admit(conn, revision, f"plafond:{uuid.uuid4().hex[:8]}")


def en_vol(conn) -> int:
    return scheduler.en_vol(conn)


def par_item(conn, item_id: int) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM node_run WHERE item_id = %s AND status = 'running'",
        (item_id,),
    ).fetchone()["n"]


def runs_de(conn, item_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM node_run WHERE item_id = %s ORDER BY id", (item_id,)
    ).fetchall()


def actifs(conn, items: list[int]) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM work_item WHERE id = ANY(%s) AND terminal_at IS NULL",
        (items,),
    ).fetchall()
    return [r["id"] for r in rows]


def rendre(conn) -> int:
    """Rend tout ce que l'exécuteur factice tient — les runs quittent le vol."""
    tenus, EN_VOL[:] = list(EN_VOL), []
    for run_id in tenus:
        kernel.apply(conn, run_id, {"outcome": "ok"})
    return len(tenus)


# -------------------------------------------------------------------- épreuves


def plafond_global(conn) -> None:
    """1 et 2 : jamais plus de N en vol, et ce qui attend ne coûte rien."""
    scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM = 2, 2
    simples = [seme(conn, None) for _ in range(4)]
    large = seme(conn, 3)
    items = simples + [large]
    assert en_vol(conn) == 0, "la base de test n'est qu'à nous"

    # 1. le premier tick est déjà plein : deux runs en vol, et pas trois
    scheduler.tick(conn)
    assert en_vol(conn) == 2, en_vol(conn)
    print(f"1. {len(items)} items, 7 runs réservables, plafond 2 → "
          f"{en_vol(conn)} runs en vol au premier tick ✓")

    # 2. ce que le plafond retient n'a aucune ligne : ni bail, ni tentative
    attendus = [i for i in items if not runs_de(conn, i)]
    assert len(attendus) >= 3, attendus
    for item_id in attendus:
        assert runs_de(conn, item_id) == [], \
            f"item {item_id} : un run retenu par le plafond n'est pas réservé"
    print(f"2. items {attendus} en attente : aucune ligne node_run, donc "
          "aucun bail posé et aucune tentative consommée ✓")

    # les ticks suivants les prennent, deux par deux, jusqu'au dernier
    charges = [en_vol(conn)]
    for _ in range(40):
        if not actifs(conn, items):
            break
        rendre(conn)
        scheduler.tick(conn)
        charges.append(en_vol(conn))
        assert charges[-1] <= scheduler.MAX_RUNS, \
            f"{charges[-1]} runs en vol pour un plafond de {scheduler.MAX_RUNS}"
    rendre(conn)
    assert not actifs(conn, items), f"des items n'ont jamais démarré : {actifs(conn, items)}"
    assert max(charges) <= scheduler.MAX_RUNS, charges
    print(f"   charge tick par tick : {charges} — jamais au-dessus de "
          f"{scheduler.MAX_RUNS} ✓")

    # 3. aucun run n'a échoué d'avoir attendu, et aucun n'a brûlé de tentative
    for item_id in items:
        for run in runs_de(conn, item_id):
            assert run["status"] in ("applied", "superseded"), run
            assert run["attempt"] == 1, \
                f"run {run['id']} : l'attente a coûté une tentative — {run['attempt']}"
            assert run["outcome"] in ("ok", None), run
    print("3. tous les items sont partis, aucun état d'échec, toutes les "
          "tentatives à 1 : l'attente ne coûte rien ✓")


def plafond_par_item(conn) -> None:
    """4 : un fan-out large ne prend pas toute la place — l'autre item démarre."""
    scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM = 3, 2
    large = seme(conn, 4)   # K = 4, au-dessus de son plafond de 2
    petit = seme(conn, None)  # le nœud bon marché, admis après lui

    scheduler.tick(conn)
    assert par_item(conn, large) == 2, par_item(conn, large)
    assert par_item(conn, petit) == 1, \
        "l'item bon marché n'a pas démarré : le fan-out a mangé la capacité"
    assert en_vol(conn) == 3, en_vol(conn)
    # les candidats que le plafond de l'item retient n'ont, eux non plus, rien
    assert len(runs_de(conn, large)) == 2, runs_de(conn, large)
    print(f"4. item {large} en fan-out de 4 → 2 runs (son plafond), item "
          f"{petit} démarre au même tick → {en_vol(conn)} en vol pour 3 ✓")

    rendre(conn)
    for _ in range(20):
        if not actifs(conn, [large, petit]):
            break
        scheduler.tick(conn)
        assert par_item(conn, large) <= scheduler.MAX_RUNS_PER_ITEM, par_item(conn, large)
        assert en_vol(conn) <= scheduler.MAX_RUNS, en_vol(conn)
        rendre(conn)
    assert not actifs(conn, [large, petit]), actifs(conn, [large, petit])
    print("   les deux items sont allés au bout, aucun item au-dessus de son "
          f"plafond de {scheduler.MAX_RUNS_PER_ITEM} ✓")


def defaut_des_coeurs() -> None:
    """6 : le défaut sort du nombre de cœurs, et la configuration le surcharge."""
    import importlib

    garde = {k: os.environ.get(k)
             for k in ("GRAPHATOM_MAX_RUNS", "GRAPHATOM_MAX_RUNS_PER_ITEM")}
    try:
        for cle in garde:
            os.environ.pop(cle, None)
        importlib.reload(scheduler)
        coeurs = os.cpu_count() or 4
        assert scheduler.MAX_RUNS == max(2, coeurs // 2), \
            f"{scheduler.MAX_RUNS} n'est pas dérivé des {coeurs} cœurs"
        assert scheduler.MAX_RUNS_PER_ITEM == max(1, scheduler.MAX_RUNS // 2), \
            scheduler.MAX_RUNS_PER_ITEM
        assert scheduler.MAX_RUNS_PER_ITEM < scheduler.MAX_RUNS, \
            "un item pourrait prendre toute la capacité"
        print(f"6. {coeurs} cœurs → plafond {scheduler.MAX_RUNS}, "
              f"{scheduler.MAX_RUNS_PER_ITEM} par item ✓")

        os.environ["GRAPHATOM_MAX_RUNS"] = "5"
        os.environ["GRAPHATOM_MAX_RUNS_PER_ITEM"] = "2"
        importlib.reload(scheduler)
        assert (scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM) == (5, 2), \
            (scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM)
        print("   GRAPHATOM_MAX_RUNS=5 et GRAPHATOM_MAX_RUNS_PER_ITEM=2 "
              "surchargent le défaut ✓")
    finally:
        for cle, valeur in garde.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        importlib.reload(scheduler)
        scheduler._execute = fake_execute


def api_load(conn) -> None:
    """5 : la charge se lit hors de la base — une requête sur `/api/load`."""
    scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM = 3, 2
    item_id = seme(conn, 2)
    scheduler.tick(conn)
    assert en_vol(conn) == 2, en_vol(conn)

    with socket.socket() as libre:  # un port éphémère : le rail peut tourner à côté
        libre.bind(("127.0.0.1", 0))
        port = libre.getsockname()[1]
    threading.Thread(target=web.serve, args=(port, "plafond-test"),
                     daemon=True).start()
    url = f"http://127.0.0.1:{port}/api/load"
    charge = None
    for _ in range(40):
        try:
            with urllib.request.urlopen(url, timeout=5) as reponse:
                charge = json.loads(reponse.read())
            break
        except OSError:
            time.sleep(0.1)
    assert charge is not None, f"{url} n'a jamais répondu"
    assert charge == {"running": 2, "max_runs": 3, "max_runs_per_item": 2}, charge
    print(f"5. GET /api/load → {charge} : les runs en vol et le plafond "
          "effectif, hors de la base ✓")

    rendre(conn)
    for _ in range(10):
        if not actifs(conn, [item_id]):
            break
        scheduler.tick(conn)
        rendre(conn)


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="graphatom-plafond-"))
    blocks.DATA_DIR = workdir
    # aucun dépôt à portée : la promotion et le rangement des ateliers d'un
    # fan-out n'ont alors rien à toucher, et rien de la production ne fuit
    os.environ["GRAPHATOM_REPO_DIR"] = str(workdir / "sans-depot")
    garde = (db.DSN, scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM,
             scheduler._execute)
    dsn = db.agent_dsn(ITEM_ID)
    assert dsn, "aucune instance jetable : impossible de compter seul"
    db.DSN = dsn
    scheduler._execute = fake_execute
    try:
        db.init_db()
        with db.connect() as conn:
            plafond_global(conn)
            plafond_par_item(conn)
            api_load(conn)
            defaut_des_coeurs()
    finally:
        db.drop_agent_db()
        (db.DSN, scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM,
         scheduler._execute) = garde
        shutil.rmtree(workdir, ignore_errors=True)

    print("\nplafonds : OK — la charge est bornée, et ce qu'elle retient attend")


if __name__ == "__main__":
    main()
