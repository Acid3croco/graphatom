"""Le canal GitHub — module hors noyau, par polling.

GitHub est l'interface humaine et la cible des effets ; Postgres reste
l'unique autorité d'exécution. Un tour du canal, dans l'ordre :

  1. admission  — `github_admission` : la plus ancienne issue ouverte
                  portant le label devient un sujet
  2. accusé     — l'occurrence ouverte reçoit son commentaire de prise en
                  charge, réécrit ensuite à chaque transition pour porter
                  la trajectoire en direct (une édition ne notifie personne)
  3. critères, questions, réponses — `github_questions`
  4. état       — un label `rail:<état>` projette l'état de l'item actif ;
                  `rail:stalled` s'y ajoute quand le worker ne bat plus
  5. rapports   — un item terminal reçoit son commentaire de clôture, et
                  perd ses labels

Aucune lecture de GitHub comme état d'item. Chaque prise de parole du rail
est un effet : clé logique, intention commise avant, réconciliation en
relisant les commentaires — voir `github_speech`.
"""

import json
import os
import time
import urllib.error

from psycopg import Connection

from . import db, graph, heartbeat
from .github_admission import admit_labeled
from .github_client import BLOCKED, QUEUED, RAIL, STALLED, GitHub, from_env
from .github_questions import collect_answers, publish_criteria, publish_questions
from .github_speech import (ADMITTED, applied, gh_items, issue_number, marker,
                            pending, speak, web_url)


def ack_key(item: dict) -> str:
    # la clé porte le graph et la génération : chaque occurrence a droit à
    # son accusé, une ré-admission n'est pas un doublon
    return f"{item['graph']}-g{item['generation']}{ADMITTED}"


def ack_body(item: dict, retry: tuple[int, int] | None = None) -> str:
    lineage = f", reprise de #{retry[0]} (item {retry[1]})" if retry else ""
    return (f"**Prise en charge par le rail** — item {item['id']}, "
            f"graph `{item['graph']}` (génération {item['generation']})"
            f"{lineage}.\n\n"
            f"Le label `{RAIL}<état>` suit la trajectoire ; les questions "
            f"arrivent ici en commentaire.\n"
            f"Trajectoire et artefacts : {web_url()}/item/{item['id']}")


def _ack_retry(item: dict, body: str) -> tuple[int, int] | None:
    """Relit la filiation dans l'accusé que le rail va mettre à jour.

    Le corps de l'issue n'est jamais relu. La filiation reste dans sa seule
    projection durable, le commentaire d'accusé, avec une forme fermée qui
    empêche de prendre une prose humaine pour un état du rail.
    """
    header = ack_body(item).splitlines()[0]
    prefix = f"{header.removesuffix('.')}, reprise de #"
    for line in body.splitlines():
        if not (line.startswith(prefix) and line.endswith(").")):
            continue
        source, separator, previous = line[len(prefix):-2].partition(" (item ")
        if separator and source.isdigit() and previous.isdigit():
            return int(source), int(previous)
    return None


def acknowledge(conn: Connection, gh: GitHub,
                retries: dict[int, tuple[int, int]] | None = None) -> None:
    """Accusé de prise en charge : entre l'admission et la première question,
    l'issue dit déjà qu'on travaille dessus — jamais deux accusés par occurrence."""
    retries = retries or {}
    for item in gh_items(conn, gh):
        speak(conn, gh, issue_number(item["subject_key"]), item["id"],
              ack_key(item), ack_body(item, retries.get(item["id"])))


# --------------------------------------------------------------------- labels


def paint_states(conn: Connection, gh: GitHub, blocked: set[int],
                 queued: set[int], stalled: bool) -> None:
    """Les labels du rail : une projection possédée par le rail.

    Jamais lue comme état d'item, seulement réécrite depuis la base : les
    anciens labels rail partent, ceux qu'on veut arrivent. Un item terminal
    n'en porte aucun, et un label bricolé à la main est repeint au tick
    suivant. `rail:blocked`, `rail:queued` et `rail:stalled` sortent du même
    pinceau sans venir de la base.
    """
    active = {
        issue_number(r["subject_key"]): {f"{RAIL}{r['state']}"}
        | ({STALLED} if stalled else set())
        for r in gh_items(conn, gh, "AND w.terminal_at IS NULL")
    }
    wanted = {number: {BLOCKED} for number in blocked}
    wanted.update({number: {QUEUED} for number in queued})
    # L'item actif gagne sur une vue différée issue d'une course entre deux
    # processus de sync : le perdant de l'admission ne doit pas repeindre
    # l'issue active en `rail:queued` pendant un tick.
    wanted.update(active)
    for issue in gh.labeled_issues():
        number = issue["number"]
        want = wanted.get(number, set())
        painted = {lab["name"] for lab in issue["labels"]
                   if lab["name"].startswith(RAIL)}
        for name in sorted(want - painted):
            gh.create_label(name)
            gh.add_label(number, name)
            print(f"#{number} ← {name}", flush=True)
        for stale in sorted(painted - want):
            gh.remove_label(number, stale)
            print(f"#{number} ⌫ {stale}", flush=True)


def clear_terminal_labels(conn: Connection, gh: GitHub) -> None:
    """Le retrait du label d'état au terminal — inconditionnel.

    Le repeint par tick ne voit que les issues ouvertes : une issue fermée
    par le merge de sa propre PR garderait son `rail:<état>` pour toujours.
    Ici on vise l'issue par son numéro, ouverte ou fermée, et l'effet retient
    que c'est fait.
    """
    active = {issue_number(r["subject_key"])
              for r in gh_items(conn, gh, "AND w.terminal_at IS NULL")}
    for item in gh_items(conn, gh, "AND w.terminal_at IS NOT NULL"):
        number = issue_number(item["subject_key"])
        if number in active:
            continue  # une autre génération tourne : le label est à elle
        key = f"{item['graph']}-g{item['generation']}-unlabeled"
        target = f"gh:{gh.repo}#{number}"
        if not pending(conn, item["id"], target, key, {"unlabel": RAIL}):
            continue
        for name in gh.issue_labels(number):
            if name.startswith(RAIL):
                gh.remove_label(number, name)
                print(f"#{number} ⌫ {name}", flush=True)
        applied(conn, target, key)


# ---------------------------------------------------------------- trajectoire


def _events(conn: Connection, item_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM event WHERE item_id = %s ORDER BY item_version", (item_id,),
    ).fetchall()


def _transition(e: dict) -> str:
    return f"{(e['from_state'] + ' → ') if e['from_state'] else ''}{e['to_state']}"


def _journal(events: list[dict]) -> str:
    """La trajectoire au format du journal : une ligne par transition."""
    return "\n".join(
        f"v{e['item_version']:>2} {e['at']:%H:%M:%S} {e['kind']:<9} {_transition(e)}"
        f"{(' [' + e['outcome'] + ']') if e['outcome'] else ''}" for e in events)


def _redraw(conn: Connection, gh: GitHub, item: dict) -> None:
    """Réécrit l'accusé avec le journal à jour — s'il est déjà posté."""
    number = issue_number(item["subject_key"])
    mark = marker(ack_key(item))
    comment = next((c for c in gh.comments(number) if mark in c["body"]), None)
    if comment is None:
        return  # l'accusé n'est pas encore posté — au prochain tick
    events = _events(conn, item["id"])
    retry = _ack_retry(item, comment["body"])
    body = (f"{mark}\n{ack_body(item, retry)}\n\n"
            f"**Trajectoire** (v{item['version']}, {len(events)} transitions)\n\n"
            f"```\n{_journal(events)}\n```")
    if comment["body"] != body:
        gh.edit_comment(comment["id"], body)
        print(f"#{number} ✎ trajectoire v{item['version']}", flush=True)


def paint_trajectories(conn: Connection, gh: GitHub, drawn: dict[int, int]) -> None:
    """La trajectoire vivante : l'accusé tient le journal à jour.

    Une projection, comme les labels : le corps est reconstruit depuis la
    base. `drawn` retient la version déjà peinte pour ne rien faire quand
    rien n'a bougé ; un PATCH raté n'y entre pas et se retente au tick
    suivant, sans bloquer le rail.
    """
    for item in gh_items(conn, gh):
        if drawn.get(item["id"]) == item["version"]:
            continue
        try:
            _redraw(conn, gh, item)
        except (urllib.error.URLError, OSError) as exc:
            print(f"#{issue_number(item['subject_key'])} trajectoire non éditée : "
                  f"{exc} — on réessaie", flush=True)
            continue
        drawn[item["id"]] = item["version"]


def report_terminals(conn: Connection, gh: GitHub) -> None:
    for item in gh_items(conn, gh, "AND w.terminal_at IS NOT NULL"):
        events = _events(conn, item["id"])
        lines = "\n".join(
            f"| v{e['item_version']} | {e['kind']} | {_transition(e)} | "
            f"{e['outcome'] or ''} |" for e in events)
        body = (f"**Item terminé : `{item['state']}`** "
                f"(génération {item['generation']}, {len(events)} transitions)\n\n"
                f"| v | événement | transition | issue |\n|---|---|---|---|\n{lines}")
        # la clé porte le graph : deux graphs peuvent traiter la même issue,
        # chacun a droit à son rapport de génération
        speak(conn, gh, issue_number(item["subject_key"]), item["id"],
              f"{item['graph']}-g{item['generation']}-terminal", body)


# ----------------------------------------------------------------------- tick


def tick(conn: Connection, gh: GitHub, revision: str, allowed: set[str],
         drawn: dict[int, int], said: set[tuple[int, str]]) -> None:
    """Un tour du canal : les gestes dans l'ordre, et l'incident réseau au bout.

    Injoignable, lent ou muet : GitHub casse toujours de la même façon, une
    `OSError` — et un timeout n'est rien d'autre. Le tour s'arrête là, la
    ligne le dit, le tick suivant repart. Rien n'est perdu : chaque geste est
    un effet, commis en base avant d'être fait, donc réconcilié à la reprise.
    """
    try:
        # le battement du worker ne sert qu'à cette projection : le sync ne
        # décide d'aucun état avec — la base reste l'autorité
        stalled = heartbeat.stalled(heartbeat.last(conn, heartbeat.RAIL))
        retries: dict[int, tuple[int, int]] = {}
        blocked, queued = admit_labeled(conn, gh, revision, said, retries)
        acknowledge(conn, gh, retries)
        publish_criteria(conn, gh)
        publish_questions(conn, gh)
        collect_answers(conn, gh, allowed)
        paint_states(conn, gh, blocked, queued, stalled)
        paint_trajectories(conn, gh, drawn)
        report_terminals(conn, gh)
        clear_terminal_labels(conn, gh)
    except (urllib.error.URLError, OSError) as exc:
        print(f"github injoignable : {exc} — on réessaie", flush=True)


def answerers(repo: str) -> set[str]:
    """Les répondants autorisés : la variable d'environnement, sinon le propriétaire."""
    raw = os.environ.get("GRAPHATOM_ANSWERERS", repo.split("/")[0])
    return set(filter(None, raw.split(",")))


def sync_forever(repo: str, bundle_path: str, poll_s: float = 15.0) -> None:
    allowed = answerers(repo)
    gh = from_env(repo)
    drawn: dict[int, int] = {}   # item → version de trajectoire déjà peinte
    said: set[tuple[int, str]] = set()  # actes de parole d'avant l'admission, déjà dits

    with db.connect() as conn:
        revision = graph.publish(conn, json.loads(open(bundle_path).read()))
        print(f"canal github sur {repo} — révision {revision[:12]}…, "
              f"répondants : {', '.join(sorted(allowed))}", flush=True)
        while True:
            # hors du tick, donc hors de sa rattrape d'incident réseau : ce
            # battement dit que la boucle tourne, pas que GitHub répond
            heartbeat.beat(conn, heartbeat.GITHUB_SYNC)
            tick(conn, gh, revision, allowed, drawn, said)
            time.sleep(poll_s)
