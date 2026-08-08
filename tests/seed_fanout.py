"""La fixture du fan-out : de quoi voir une course sur la page d'un item.

Aucun graph de production ne déclare encore de fan-out, et attendre qu'un
vrai cycle en produise un pour regarder la page serait circulaire. Ici, les
lignes sont écrites à la main : un graph publié qui déclare son `fanout`,
et des `node_run` d'un même `(nœud, passage, tentative)` à `candidate`
distincts, avec leurs issues, leurs fins et leurs jetons.

Ce que la fixture pose, et pourquoi :

  * un item **terminé** dont le nœud `travail` a couru deux fois —
    - tentative 1 : les quatre candidats échouent, l'issue majoritaire
      (`crashed`) renvoie sur place ; aucun gagnant, et ça doit se lire
    - tentative 2 : un candidat gagne, un a franchi une porte interne, deux
      sont révoqués par la décision — les trois façons de perdre
  * un item **en vol**, ses quatre candidats en course sur `travail` : le
    graph doit continuer de peindre un seul nœud courant

Les horodatages sont choisis, pas tirés : la durée d'horloge de chaque
étape est bien inférieure à la somme des durées des candidats — c'est tout
l'intérêt du parallélisme, et c'est ce que la page doit montrer.

Rien n'est détruit : la base est créée si absente, jamais droppée, et les
sujets portent le pid — deux exécutions ne se marchent pas dessus.

Usage : uv run python tests/seed_fanout.py
"""

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, graph, kernel  # noqa: E402

# hermétisme : la fixture n'écrit que dans la base qu'on lui donne
os.environ.pop("GRAPHATOM_AGENT_DSN", None)

UTC = dt.timezone.utc

# la variante lente hérite de la commande du nœud ; la rapide la surcharge —
# c'est exactement le pari que le fan-out sert à trancher, et la page doit
# montrer les deux modèles côte à côte
BUNDLE = {
    "name": "fanout-vitrine",
    "entry": "prepare",
    "budgets": {"escalations": 2, "wall_deadline_hours": 6},
    "on_kernel": {"escalate_to": "escalate", "exhausted_to": "abandon"},
    "nodes": {
        "prepare": {
            "block": "FETCH",
            "config": {"agent": {"cmd": 'claude --model sonnet -p "$PROMPT"',
                                 "prompt": "Prépare le terrain.", "timeout_s": 600}},
            "edges": {"ok": "travail"},
        },
        "travail": {
            "block": "ACT",
            "config": {
                "lease_s": 900,
                "agent": {"cmd": 'claude --model opus -p "$PROMPT"',
                          "prompt": "Fais le travail. Stratégie : {strategy}.",
                          "timeout_s": 1800},
                "fanout": {
                    "variants": [
                        {"label": "opus", "strategy": "raisonne longtemps"},
                        {"label": "haiku", "strategy": "va droit au but",
                         "agent": {"cmd": 'claude --model haiku -p "$PROMPT"'}},
                    ],
                    "repeat": 2,
                    "reduce": "first_pass",
                },
            },
            "edges": {"ok": "fini"},
        },
        "escalate": {
            "block": "WAIT", "escalade": True,
            "config": {"question": "On retente ?", "options": ["retry"],
                       "owner": "vitrine", "deadline_minutes": 120},
            "edges": {"retry": "travail", "expired": "abandon"},
        },
        "fini": {"terminal": True},
        "abandon": {"terminal": True},
    },
}

# un candidat par entrée, dans l'ordre de son numéro : son issue, son statut,
# la seconde où il a fini — comptée depuis le début de l'étape — et ce qu'il
# a consommé. La somme des durées dépasse de loin la durée d'horloge.
ATTEMPT_1 = [
    (0, "applied", "crashed", 150, {"input_tokens": 41200, "output_tokens": 3100,
                                    "total_cost_usd": 0.6180}),
    (1, "faulted", "timed_out", 180, {"input_tokens": 52800, "output_tokens": 2400,
                                      "total_cost_usd": 0.7920}),
    (2, "applied", "crashed", 185, {"input_tokens": 18600, "output_tokens": 1900,
                                    "total_cost_usd": 0.0465}),
    (3, "applied", "invalid_result", 95, {"input_tokens": 12400, "output_tokens": 900,
                                          "total_cost_usd": 0.0310}),
]
ATTEMPT_2 = [
    (0, "superseded", None, 142, {"input_tokens": 61500, "output_tokens": 4800,
                                  "total_cost_usd": 0.9225}),
    (1, "superseded", None, 142, {"input_tokens": 58900, "output_tokens": 4100,
                                  "total_cost_usd": 0.8835}),
    (2, "applied", "ok", 140, {"input_tokens": 21300, "output_tokens": 5600,
                               "total_cost_usd": 0.0532}),
    (3, "applied", "invalid_result", 60, {"input_tokens": 9800, "output_tokens": 700,
                                          "total_cost_usd": 0.0245}),
]

PREPARE_USAGE = {"input_tokens": 8300, "output_tokens": 1200,
                 "total_cost_usd": 0.0415}


def run_row(conn, item_id: int, node: str, attempt: int, candidate: int | None,
            status: str, outcome: str | None, finished: dt.datetime | None,
            usage: dict) -> int:
    """Une ligne `node_run` posée telle quelle — le résultat porte son usage."""
    result = {"outcome": outcome, "usage": usage} if usage else None
    return conn.execute(
        "INSERT INTO node_run (item_id, node, cycle, attempt, candidate, status, "
        "fence, expected_version, lease_expires_at, finished_at, outcome, result) "
        "VALUES (%s, %s, 1, %s, %s, %s, 1, 1, %s, %s, %s, %s) RETURNING id",
        (item_id, node, attempt, candidate, status,
         (finished or dt.datetime.now(UTC)) + dt.timedelta(minutes=15),
         finished, outcome, json.dumps(result) if result else None),
    ).fetchone()["id"]


def event(conn, item_id: int, version: int, at: dt.datetime, kind: str,
          from_state: str | None, to_state: str, outcome: str | None,
          run_id: int | None) -> None:
    conn.execute(
        "INSERT INTO event (item_id, item_version, at, kind, from_state, to_state, "
        "outcome, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (item_id, version, at, kind, from_state, to_state, outcome, run_id),
    )


def move(conn, item_id: int, state: str, version: int,
         terminal: dt.datetime | None) -> None:
    conn.execute(
        "UPDATE work_item SET state = %s, version = %s, terminal_at = %s WHERE id = %s",
        (state, version, terminal, item_id),
    )


def batch(conn, item_id: int, attempt: int, start: dt.datetime,
          plan: list) -> dict[int, int]:
    """Les K candidats d'une tentative, chacun fini à sa seconde. Rend leurs ids."""
    return {k: run_row(conn, item_id, "travail", attempt, k, status, outcome,
                       start + dt.timedelta(seconds=seconds), usage)
            for k, status, outcome, seconds, usage in plan}


def item_termine(conn, rev: str, tag: int) -> int:
    """L'item dont la course est finie : deux tentatives, un seul gagnant."""
    item_id = kernel.admit(conn, rev, f"vitrine-fanout:{tag}", "fan-out, la course")
    t0 = dt.datetime.now(UTC) - dt.timedelta(minutes=20)
    conn.execute("UPDATE event SET at = %s WHERE item_id = %s AND item_version = 1",
                 (t0, item_id))

    # v2 : une étape ordinaire, sans le moindre candidat — elle doit
    # continuer de s'afficher exactement comme avant
    prep = run_row(conn, item_id, "prepare", 1, None, "applied", "ok",
                   t0 + dt.timedelta(seconds=30), PREPARE_USAGE)
    v2_at = t0 + dt.timedelta(seconds=30)
    event(conn, item_id, 2, v2_at, "result", "prepare", "travail", "ok", prep)

    # v3 : tentative 1, tous en échec — l'issue majoritaire renvoie sur place,
    # et le premier terminé à la porter tranche l'égalité (c0, `crashed`)
    ids = batch(conn, item_id, 1, v2_at, ATTEMPT_1)
    v3_at = v2_at + dt.timedelta(seconds=185)
    event(conn, item_id, 3, v3_at, "result", "travail", "travail", "crashed", ids[0])

    # v4 : tentative 2, c2 gagne — les deux `opus` encore en vol sont révoqués
    ids = batch(conn, item_id, 2, v3_at, ATTEMPT_2)
    v4_at = v3_at + dt.timedelta(seconds=142)
    event(conn, item_id, 4, v4_at, "result", "travail", "fini", "ok", ids[2])
    move(conn, item_id, "fini", 4, v4_at)
    return item_id


def item_en_vol(conn, rev: str, tag: int) -> int:
    """L'item dont les quatre candidats courent encore : un seul nœud courant."""
    item_id = kernel.admit(conn, rev, f"vitrine-fanout-vol:{tag}", "fan-out en vol")
    t0 = dt.datetime.now(UTC) - dt.timedelta(minutes=4)
    conn.execute("UPDATE event SET at = %s WHERE item_id = %s AND item_version = 1",
                 (t0, item_id))
    prep = run_row(conn, item_id, "prepare", 1, None, "applied", "ok",
                   t0 + dt.timedelta(seconds=25), PREPARE_USAGE)
    v2_at = t0 + dt.timedelta(seconds=25)
    event(conn, item_id, 2, v2_at, "result", "prepare", "travail", "ok", prep)
    move(conn, item_id, "travail", 2, None)
    for k in range(4):
        run_row(conn, item_id, "travail", 1, k, "running", None, None, {})
    return item_id


def main() -> None:
    db.init_db()
    with db.connect() as conn:
        rev = graph.publish(conn, BUNDLE)
        tag = os.getpid()
        fini = item_termine(conn, rev, tag)
        vol = item_en_vol(conn, rev, tag)

    print(f"révision publiée : {rev}")
    print(f"item {fini} : fan-out terminé — 2 tentatives, 8 candidats, un gagnant (c2)")
    print(f"item {vol} : fan-out en vol — 4 candidats en course sur `travail`")
    print("\nseed_fanout : OK — la base porte une course à regarder")


if __name__ == "__main__":
    main()
