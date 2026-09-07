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
  7. le plafond global se lit sur les runs en vol, jamais en plus d'eux :
     trois items d'un run, plafond 2 → un tick en réserve deux, et rend 2
  8. la place d'un item est son plafond moins ses runs en vol : une course
     plus large que la place attend entière, part au dernier recours à sa
     largeur — le tick la rend —, et un candidat déjà en vol réduit la place

La base est celle de l'item de test, dérivée comme celle d'un agent : le
compte des runs en vol est global, donc il ne se mesure que seul.

Usage : uv run python tests/plafond_test.py
"""

import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, graph, kernel, quota, scheduler, web  # noqa: E402

from outils import admettre, bundle_epreuve, data_dir_jetable  # noqa: E402
from outils import fanout_stub, runs_de  # noqa: E402

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
        config["fanout"] = fanout_stub(k)
    return bundle_epreuve(f"plafond-{k}", {
        "travail": {"block": "ACT", "config": config, "edges": {"ok": "fini"}},
    })


# ------------------------------------------------------------------ outillage


def fake_execute(run_id: int, item_id: int) -> None:
    """L'exécuteur du tick, en factice : le run vole, et ne rend rien."""
    EN_VOL.append(run_id)


def seme(conn, k: int | None) -> int:
    """Un item factice de plus pour éprouver les plafonds multi-items."""
    return admettre(conn, bundle(k), "plafond")


def en_vol(conn) -> int:
    return scheduler.en_vol(conn)


def par_item(conn, item_id: int) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM node_run WHERE item_id = %s AND status = 'running'",
        (item_id,),
    ).fetchone()["n"]


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


def epuiser(conn, items: list[int]) -> None:
    """Rend et tick jusqu'à ce que ces items soient tous au terminal."""
    rendre(conn)
    for _ in range(20):
        if not actifs(conn, items):
            break
        scheduler.tick(conn)
        rendre(conn)
    assert not actifs(conn, items), actifs(conn, items)


# -------------------------------------------------------------------- épreuves


LARGE = 3  # la course la plus large semée ici — voir la borne effective plus bas


def plafond_global(conn) -> None:
    """1 et 2 : jamais plus de N en vol, et ce qui attend ne coûte rien."""
    scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM = 2, 2
    simples = [seme(conn, None) for _ in range(4)]
    large = seme(conn, LARGE)
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

    # les ticks suivants les prennent, jusqu'au dernier
    #
    # Le plafond borne le **nombre de courses simultanées**, jamais la largeur
    # d'une seule : une course se réserve entière, sans quoi `keep_n`
    # trancherait sur un lot amputé. Une course plus large que le plafond ne
    # peut donc pas être différée — elle serait interdite pour toujours — et
    # passe seule, rail vide. Ici le plafond vaut 2 et la course en compte 3 :
    # la borne effective est celle de la plus large course semée. En
    # production le cas n'existe pas, les plafonds ayant `FANOUT_MAX_CANDIDATES`
    # pour plancher.
    borne = max(scheduler.MAX_RUNS, LARGE)
    charges = [en_vol(conn)]
    for _ in range(40):
        if not actifs(conn, items):
            break
        rendre(conn)
        scheduler.tick(conn)
        charges.append(en_vol(conn))
        assert charges[-1] <= borne, \
            f"{charges[-1]} runs en vol pour une borne de {borne}"
    rendre(conn)
    assert not actifs(conn, items), f"des items n'ont jamais démarré : {actifs(conn, items)}"
    assert max(charges) <= borne, charges
    print(f"   charge tick par tick : {charges} — jamais au-dessus de "
          f"{borne} (plafond {scheduler.MAX_RUNS}, course la plus large {LARGE}) ✓")

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
    """4 : un fan-out large ne prend pas toute la place — l'autre item démarre.

    Et il ne part **jamais en morceaux** : une course se réserve entière ou
    attend. Une course coupée en deux serait pire qu'une course différée :
    une réduction ne voit que le lot qui existe en base, non celui qui devrait
    exister. Deux candidats sur quatre pourraient suffire à `keep_n`, faire
    avancer l'item, et empêcher les deux autres de naître.
    """
    scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM = 3, 2
    large = seme(conn, 4)   # K = 4, au-dessus de son plafond de 2
    petit = seme(conn, None)  # le nœud bon marché, admis après lui

    scheduler.tick(conn)
    assert par_item(conn, large) == 0, \
        f"la course est partie en morceaux : {par_item(conn, large)} candidats sur 4"
    assert runs_de(conn, large) == [], \
        "une course retenue ne doit poser ni bail ni tentative"
    assert par_item(conn, petit) == 1, \
        "l'item bon marché n'a pas démarré : le fan-out a mangé la capacité"
    print(f"4. item {large} en fan-out de 4 : rien réservé — la course attend "
          f"entière ; item {petit} démarre au même tick ✓")

    # rail vidé, la course passe d'un bloc : le plafond borne le nombre de
    # courses simultanées, jamais la largeur d'une seule
    rendre(conn)
    scheduler.tick(conn)
    assert par_item(conn, large) == 4, par_item(conn, large)
    print(f"   rail vide au tick suivant → les 4 candidats partent ensemble, "
          "jamais en deux fois ✓")

    epuiser(conn, [large, petit])
    print("   les deux items sont allés au bout ✓")


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
        # le plancher : une course se réserve entière, donc un plafond sous
        # la plus large course publiable l'interdirait pour toujours
        assert scheduler.MAX_RUNS == max(graph.FANOUT_MAX_CANDIDATES,
                                        coeurs // 2), \
            f"{scheduler.MAX_RUNS} n'est ni les {coeurs} cœurs ni le plancher"
        assert scheduler.MAX_RUNS_PER_ITEM == max(graph.FANOUT_MAX_CANDIDATES,
                                                 scheduler.MAX_RUNS // 2), \
            scheduler.MAX_RUNS_PER_ITEM
        # Le plafond par item reste sous le global **dès que la machine a de
        # quoi** — c'est ce qui empêche un item d'affamer les autres. Sur une
        # petite machine les deux tombent sur le même plancher, et c'est
        # assumé : une course se réserve entière, donc interdire à un item
        # d'atteindre la largeur maximale reviendrait à interdire la course.
        assert scheduler.MAX_RUNS_PER_ITEM <= scheduler.MAX_RUNS, \
            "un item pourrait dépasser la capacité globale"
        if scheduler.MAX_RUNS > graph.FANOUT_MAX_CANDIDATES:
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
    assert charge == {"running": 2, "max_runs": 3, "max_runs_per_item": 2,
                      "max_active_items": scheduler.MAX_ACTIVE_ITEMS,
                      "solo": {"running": 0, "waiting": 0},
                      "builds": 0, "max_builds": quota.MAX_BUILDS}, charge
    print(f"5. GET /api/load → {charge} : runs et constructions en vol, "
          "avec leurs plafonds effectifs ✓")

    epuiser(conn, [item_id])


def plafond_global_borne(conn) -> None:
    """7 : un rail plein n'admet rien — le plafond se lit sur les runs en vol.

    La course large est admise la première : c'est elle que le dispatch
    regarde en premier, sur un rail que les deux runs d'après remplissent.
    Un plafond lu en plus des runs en vol, et non en moins, la ferait naître
    par-dessus la charge.
    """
    scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM = 2, 8
    scheduler.MAX_ACTIVE_ITEMS = 8
    assert en_vol(conn) == 0, "le rail n'est pas vide au départ"
    large = seme(conn, LARGE)
    etroits = [seme(conn, None) for _ in range(2)]
    assert scheduler.tick(conn) == 2, "le tick ne rend pas le compte des runs réservés"
    assert [par_item(conn, i) for i in etroits] == [1, 1], "un item étroit attend"
    assert runs_de(conn, large) == [], "la course est partie sur la place des étroits"
    print(f"7. plafond 2 : la course de {LARGE} attend, les deux items étroits "
          "volent, le tick rend 2 ✓")

    assert scheduler.tick(conn) == 0, "un rail plein a réservé"
    assert en_vol(conn) == 2 and runs_de(conn, large) == [], \
        f"{en_vol(conn)} runs en vol : la course est née par-dessus le plafond"
    print("   rail plein, rien rendu : le tick suivant ne réserve rien et rend 0 ✓")
    epuiser(conn, [large, *etroits])


def place_par_item(conn) -> None:
    """8 : la place d'un item, c'est son plafond moins ses runs en vol.

    Le dispatch ne coupe jamais une course : le seul état où un item a des
    runs en vol *et* des candidats à naître, le test le joue à la main par
    `_reserve`. C'est là que la soustraction se voit.
    """
    scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM = 8, 2
    scheduler.MAX_ACTIVE_ITEMS = 8
    large, petit = seme(conn, LARGE), seme(conn, None)
    assert scheduler.tick(conn) == 1, "le tick a réservé au-delà de l'item étroit"
    assert (par_item(conn, large), par_item(conn, petit)) == (0, 1), \
        (par_item(conn, large), par_item(conn, petit))
    rendre(conn)
    assert scheduler.tick(conn) == LARGE, "le dernier recours ne rend pas sa largeur"
    assert par_item(conn, large) == LARGE, par_item(conn, large)
    print(f"8. place 2 < largeur {LARGE} : l'item étroit part seul, tick 1 ; rail "
          f"vide, la course passe entière et le tick rend {LARGE} ✓")
    epuiser(conn, [large, petit])

    entamee = seme(conn, LARGE)
    assert scheduler._reserve(conn, entamee, 1) == 1
    assert scheduler._dispatch(conn) == 0, \
        "le dispatch a complété une course sur une place déjà prise"
    assert par_item(conn, entamee) == 1, par_item(conn, entamee)
    print("   un candidat en vol compte dans la place de son item : 2 - 1 < "
          f"{LARGE}, rien de plus ne naît ✓")
    epuiser(conn, [entamee])


def main() -> None:
    workdir = data_dir_jetable("graphatom-plafond-")
    # aucun dépôt à portée : la promotion et le rangement des ateliers d'un
    # fan-out n'ont alors rien à toucher, et rien de la production ne fuit
    os.environ["GRAPHATOM_REPO_DIR"] = str(workdir / "sans-depot")
    garde = (db.DSN, scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM,
             scheduler.MAX_ACTIVE_ITEMS, scheduler._execute)
    dsn = db.agent_dsn(ITEM_ID)
    assert dsn, "aucune instance jetable : impossible de compter seul"
    db.DSN = dsn
    scheduler.MAX_ACTIVE_ITEMS = 100
    scheduler._execute = fake_execute
    try:
        db.init_db()
        with db.connect() as conn:
            plafond_global(conn)
            plafond_par_item(conn)
            api_load(conn)
            defaut_des_coeurs()
            plafond_global_borne(conn)
            place_par_item(conn)
    finally:
        db.drop_agent_db()
        (db.DSN, scheduler.MAX_RUNS, scheduler.MAX_RUNS_PER_ITEM,
         scheduler.MAX_ACTIVE_ITEMS, scheduler._execute) = garde
        shutil.rmtree(workdir, ignore_errors=True)

    print("\nplafonds : OK — la charge est bornée, et ce qu'elle retient attend")


if __name__ == "__main__":
    main()
