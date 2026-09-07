"""Les questions du rail sur GitHub, et les réponses humaines qui en reviennent.

Chaque WAIT ouvert est publié en commentaire d'issue, avec le `criteria.md`
et le `validate.md` du workspace quand ils sont là : l'humain voit ce qui
est demandé et ce qui est coché. Une escalade née d'un couperet dit en plus
quel budget a sauté et sur quoi l'agent en était.

Une réponse est un commentaire dont la *première ligne* est
`/answer <id> <option>`, d'un auteur autorisé et postérieur à l'armement de
la question. La prose qui suit est libre. Une commande qui vise cette
question mais tombe à côté de la grammaire reçoit le mode d'emploi ; seule
une commande qui vise une autre question est ignorée sans bruit.
"""

import datetime as dt
import hashlib

from psycopg import Connection

from . import channel, graph
from .github_client import GitHub
from .github_speech import cite, gh_items, issue_number, speak, web_url
from .policy import agent_timeout_s

CHECKLIST = "validate.md"   # la checklist du nœud validate, dans le workspace de l'item
CHECKLIST_LINES = 40        # ce qu'on en cite : de quoi tenir sans noyer la question
CHECKLIST_TITLE = "**Critères de succès, cochés par `validate`**"
CRITERIA = "criteria.md"    # les critères figés par le nœud scope, même workspace
CRITERIA_LINES = 80         # une spécification proposée est plus longue qu'une checklist
CRITERIA_TITLE = "**Ce que le rail a compris** — critères figés par `scope`"
TIMEOUT_TITLE = "**Escalade après timeout** — le budget du nœud a été dépassé"
STARVED_TITLE = "**Escalade sans fournisseur** — le fournisseur est indisponible"
# la pendaison d'un agent, à ne pas confondre avec le label `rail:stalled`,
# qui parle du worker : l'un est un run muet, l'autre un rail sans battement
HUNG_TITLE = "**Escalade après pendaison** — l'agent n'a jamais rien produit"
DEATHS = ("timed_out", "starved", "stalled")


def gh_questions(conn: Connection, gh: GitHub) -> list[dict]:
    return list(conn.execute(
        "SELECT q.*, s.subject_key, w.generation, "
        "  (SELECT max(at) FROM event e WHERE e.item_id = q.item_id "
        "   AND e.to_state = q.node) AS armed_at "
        "FROM question q JOIN work_item w ON w.id = q.item_id "
        "JOIN subject s ON s.id = w.subject_id "
        "WHERE q.state = 'open' AND s.subject_key LIKE %s ORDER BY q.id",
        (f"gh:{gh.repo}#%",),
    ))


# ------------------------------------------------------------------ critères


def publish_criteria(conn: Connection, gh: GitHub) -> None:
    """Les critères de `scope`, dits sur l'issue avant la première ligne de code.

    La clé logique porte l'empreinte du fichier : deux ticks sur des critères
    inchangés ne laissent qu'un commentaire, et des critères réécrits se
    redisent. Les items terminaux sont hors de portée.
    """
    web = web_url()
    for item in gh_items(conn, gh, "AND w.terminal_at IS NULL"):
        body = cite(item["id"], CRITERIA, CRITERIA_LINES, CRITERIA_TITLE, web)
        if not body:  # `scope` n'a pas encore écrit, ou n'écrira jamais : rien à dire
            continue
        empreinte = hashlib.sha256(body.encode()).hexdigest()[:12]
        speak(conn, gh, issue_number(item["subject_key"]), item["id"],
              f"{item['graph']}-g{item['generation']}-criteria-{empreinte}",
              body + "Un commentaire sur l'issue est lu au passage suivant ; "
                     "le corps de l'issue, lui, reste à l'humain.")


# ----------------------------------------------------------------- questions


def _budget_s(conn: Connection, run: dict) -> float:
    """Le couperet d'une tentative sur ce nœud, lu sur la révision du run."""
    node = graph.load_bundle(conn, run["revision"])["nodes"][run["node"]]
    return agent_timeout_s(node.get("config") or {})


def _death_header(conn: Connection, outcome: str, run: dict) -> str:
    if outcome == "timed_out":
        return (f"{TIMEOUT_TITLE} : `{run['node']}` a dépassé son `timeout_s` de "
                f"{_budget_s(conn, run):.0f} s "
                f"(passage {run['cycle']}, tentative {run['attempt']}).")
    if outcome == "starved":
        result = run["result"] or {}
        provider = result.get("provider", "inconnu")
        reason = result.get("reason", "raison absente")
        return (f"{STARVED_TITLE} : `{run['node']}` a perdu le fournisseur "
                f"`{provider}` (passage {run['cycle']}, tentative "
                f"{run['attempt']}).\n\nRaison du fournisseur : {reason}")
    return (f"{HUNG_TITLE} : `{run['node']}` a été coupé sans avoir écrit un "
            "octet ni toucher un fichier — l'agent n'a jamais démarré. "
            f"{run['attempt']} tentatives relancées sur place n'y ont rien "
            f"changé (passage {run['cycle']}).")


def death_report(conn: Connection, item_id: int, node: str) -> str:
    """Le post-mortem du couperet qui vient d'escalader — sinon rien du tout.

    Trois morts, et l'humain ne tranche pas pareil entre elles : `timed_out`,
    la question utile est celle du budget ; `stalled`, celle de l'infra ;
    `starved`, une relance identique ne rendra ni crédits ni accès. On ne lit
    que le dernier pas — l'événement qui a armé cette question.
    """
    event = conn.execute(
        "SELECT outcome, run_id FROM event WHERE item_id = %s AND to_state = %s "
        "ORDER BY item_version DESC LIMIT 1", (item_id, node),
    ).fetchone()
    if not event or event["run_id"] is None or event["outcome"] not in DEATHS:
        return ""
    run = conn.execute(
        "SELECT n.*, w.revision FROM node_run n JOIN work_item w ON w.id = n.item_id "
        "WHERE n.id = %s", (event["run_id"],),
    ).fetchone()
    if run is None:
        return ""
    entete = _death_header(conn, event["outcome"], run)
    tail = ((run["result"] or {}).get("log_tail") or "").strip()
    quoted = "\n".join(f"> {line}".rstrip() for line in tail.splitlines())
    return entete + "\n\n" + (f"Queue du journal :\n\n{quoted}\n\n" if quoted else "")


def question_body(conn: Connection, q: dict, web: str) -> str:
    """Le corps publié pour une question ouverte — construit ici, dit ailleurs."""
    options = " / ".join(f"`{o}`" for o in q["options"])
    return (f"**Question du rail** — pour @{q['owner']}, "
            f"avant le {q['deadline']:%d/%m %H:%M} UTC\n\n{q['text']}\n\n"
            f"{death_report(conn, q['item_id'], q['node'])}"
            f"{cite(q['item_id'], CRITERIA, CRITERIA_LINES, CRITERIA_TITLE, web)}"
            f"{cite(q['item_id'], CHECKLIST, CHECKLIST_LINES, CHECKLIST_TITLE, web)}"
            f"Options : {options}\n"
            f"Répondre par un commentaire : `/answer {q['id']} <option>`\n"
            f"Trajectoire et artefacts (previews) : {web}/item/{q['item_id']}")


def publish_questions(conn: Connection, gh: GitHub) -> None:
    web = web_url()
    for q in gh_questions(conn, gh):
        speak(conn, gh, issue_number(q["subject_key"]), q["item_id"],
              f"q{q['id']}", question_body(conn, q, web))


# ------------------------------------------------------------------- réponses


def mode_emploi(q: dict, author: str) -> str:
    """Le reproche du canal : la commande n'est pas prise, et voici la forme."""
    options = " / ".join(f"`{o}`" for o in q["options"])
    return (f"@{author} : commande non prise. La première ligne doit être "
            f"exactement `/answer {q['id']} <option>` — trois mots, rien de plus ; "
            f"une explication peut suivre après un saut de ligne.\n"
            f"Options : {options}")


def _posted_before_armed(comment: dict, q: dict) -> bool:
    posted = dt.datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00"))
    return bool(q["armed_at"]) and posted < q["armed_at"]


def _answer(conn: Connection, gh: GitHub, q: dict, comment: dict,
            allowed: set[str]) -> bool:
    """Un commentaire `/answer` face à cette question. Vrai s'il l'a réglée.

    Une commande qui vise une *autre* question est ignorée sans bruit ; une
    commande qui vise *cette* question, ou aucun id lisible, reçoit le mode
    d'emploi. La clé logique `reply-<commentaire>` fait que le reproche n'est
    dit qu'une fois. La grammaire ne bouge pas : trois mots, pas de synonyme.
    """
    number = issue_number(q["subject_key"])
    author = comment["user"]["login"]
    parts = comment["body"].strip().partition("\n")[0].split()
    reply = f"reply-{comment['id']}"
    if author not in allowed:
        speak(conn, gh, number, q["item_id"], reply,
              f"@{author} : seuls {', '.join(sorted(allowed))} "
              "peuvent répondre aux questions du rail.")
        return False
    vise = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    if vise is not None and vise != q["id"]:
        return False  # commande pour une autre question : silence légitime
    if vise is None or len(parts) != 3:
        speak(conn, gh, number, q["item_id"], reply, mode_emploi(q, author))
        return False
    err = channel.record_answer(conn, q["id"], parts[2], by=author)
    if err:
        speak(conn, gh, number, q["item_id"], reply, f"@{author} : {err}")
        return False
    speak(conn, gh, number, q["item_id"], f"q{q['id']}-receipt",
          f"Réponse `{parts[2]}` enregistrée (par @{author}) — le rail reprend.")
    return True


def collect_answers(conn: Connection, gh: GitHub, allowed: set[str]) -> None:
    """Les réponses humaines, lues sur la première ligne — et les ratés, dits.

    Une commande d'avant l'armement de la question est d'une vie antérieure,
    jamais rejouée. La première réponse valide gagne.
    """
    for q in gh_questions(conn, gh):
        for comment in gh.comments(issue_number(q["subject_key"])):
            if not comment["body"].strip().startswith("/answer"):
                continue
            if _posted_before_armed(comment, q):
                continue
            if _answer(conn, gh, q, comment, allowed):
                break
