"""L'admission par GitHub : la grammaire fermée du corps d'une issue.

La plus ancienne issue ouverte portant le label devient un sujet — une
seule admission automatique par issue, et un seul item non terminal dans le
rail ; les suivantes restent sur GitHub avec `rail:queued`. Une ligne
`Depends-on: #N` diffère l'admission tant que l'issue visée est ouverte ;
une ligne `Retry-of: #N` copie les pièces du dernier item terminal.

La découpe, hors tick : les issues qui attendaient la mère sont reposées
sur la dernière fille, puis seulement la mère est fermée.

Aucun parsing de langage naturel : chaque ligne est prise au mot.
"""

import shutil

from psycopg import Connection

from . import graph, kernel
from .github_client import GitHub
from .github_speech import ADMITTED, say_once
from .workspace import item_workspace

DEPENDS = "Depends-on:"     # la grammaire fermée des dépendances, dans le corps
RETRY = "Retry-of:"          # une reprise explicite, lue une fois à l'admission
RETRY_FILES = ("echec.md", "criteria.md", "validate.md")


def _lines(body: str, prefix: str) -> list[str]:
    """Le membre de droite de chaque ligne `<prefix> …` du corps.

    Grammaire fermée, comme `/answer` : la ligne est prise au mot, et rien
    d'autre n'est lu. Une task list `- [ ] #29` est de la lisibilité GitHub,
    jamais une dépendance — la vérité du rail est ici.
    """
    return [line.strip()[len(prefix):].strip()
            for line in (body or "").splitlines()
            if line.strip().startswith(prefix)]


def depends_on(body: str) -> list[str]:
    return _lines(body, DEPENDS)


def retry_of(body: str) -> list[str]:
    return _lines(body, RETRY)


def _issue_state(gh: GitHub, number: int, states: dict[int, str | None]) -> str | None:
    """`open`, `closed`, ou None si l'issue n'existe pas — un cache par tick."""
    if number not in states:
        issue = gh.issue(number)
        states[number] = issue["state"] if issue else None
    return states[number]


def _reference(raw: str, keyword: str, number: int) -> tuple[int | None, str | None]:
    """Un `#<numéro>` vers une autre issue, ou la plainte qui dit ce qui cloche."""
    if not (raw.startswith("#") and raw[1:].isdigit()):
        return None, f"`{keyword} {raw}` — attendu `{keyword} #<numéro>`"
    target = int(raw[1:])
    if target == number:
        verb = "dépend" if keyword == DEPENDS else "reprend"
        objet = "d'elle-même" if keyword == DEPENDS else "elle-même"
        return None, f"`{keyword} #{target}` — une issue ne {verb} pas {objet}"
    return target, None


def pending_deps(gh: GitHub, issue: dict, name: str, states: dict[int, str | None],
                 said: set[tuple[int, str]]) -> list[int]:
    """Les dépendances encore ouvertes de l'issue — vide si elle peut être admise.

    Une déclaration invalide — numéro illisible, renvoi à soi-même, issue
    inexistante — est ignorée, mais dite une fois : rien ne passe en silence.
    """
    number, waiting, invalid = issue["number"], [], []
    for raw in depends_on(issue["body"]):
        dep, complaint = _reference(raw, DEPENDS, number)
        if dep is None:
            invalid.append(complaint)
            continue
        state = _issue_state(gh, dep, states)
        if state is None:
            invalid.append(f"`{DEPENDS} #{dep}` — cette issue n'existe pas")
        elif state == "open":
            waiting.append(dep)
    if invalid:
        say_once(gh, number, f"{name}-depends-invalid",
                 "**Dépendances ignorées** — le rail ne lit que "
                 f"`{DEPENDS} #<numéro>`, vers une autre issue existante :\n\n"
                 + "\n".join(f"- {line}" for line in invalid), said)
    return waiting


def _previous_terminal(conn: Connection, name: str, subject_key: str) -> int | None:
    row = conn.execute(
        "SELECT w.id FROM work_item w JOIN subject s ON s.id = w.subject_id "
        "WHERE s.graph = %s AND s.subject_key = %s AND w.terminal_at IS NOT NULL "
        "ORDER BY w.generation DESC, w.id DESC LIMIT 1",
        (name, subject_key),
    ).fetchone()
    return row["id"] if row else None


def _retry_target(conn: Connection, gh: GitHub, issue: dict, name: str,
                  states: dict[int, str | None]) -> tuple[tuple[int, int] | None, str | None]:
    """La cible de reprise et son item terminal, ou la plainte."""
    declarations = retry_of(issue["body"])
    if len(declarations) != 1:
        return None, f"`{RETRY}` doit apparaître sur une seule ligne du corps"
    target, complaint = _reference(declarations[0], RETRY, issue["number"])
    if target is None:
        return None, complaint
    if _issue_state(gh, target, states) is None:
        return None, f"`{RETRY} #{target}` — cette issue n'existe pas"
    previous = _previous_terminal(conn, name, f"gh:{gh.repo}#{target}")
    if previous is None:
        return None, f"`{RETRY} #{target}` — cette issue n'a aucun item terminal"
    return (target, previous), None


def retry_source(conn: Connection, gh: GitHub, issue: dict, name: str,
                 states: dict[int, str | None],
                 said: set[tuple[int, str]]) -> tuple[int, int] | None:
    """La cible et son dernier item terminal, ou None si la ligne est absente/invalide.

    Une reprise invalide ne bloque jamais l'admission. Elle est toutefois
    dite par le même mécanisme à clé logique que les dépendances invalides.
    """
    if not retry_of(issue["body"]):
        return None
    found, complaint = _retry_target(conn, gh, issue, name, states)
    if found is not None:
        return found
    say_once(gh, issue["number"], f"{name}-retry-invalid",
             "**Reprise ignorée** — le rail ne lit qu'une ligne "
             f"`{RETRY} #<numéro>`, vers une autre issue qui porte un "
             f"item terminal :\n\n- {complaint}", said)
    return None


def copy_retry_files(previous_item: int, item_id: int) -> None:
    """Copie les seules pièces de reprise qui existent vers le nouvel item."""
    source = item_workspace(previous_item)
    target = item_workspace(item_id)
    files = [name for name in RETRY_FILES if (source / name).is_file()]
    if not files:
        return
    target.mkdir(parents=True, exist_ok=True)
    for name in files:
        shutil.copy2(source / name, target / name)


# ------------------------------------------------------------------ admission


def _remember_title(conn: Connection, name: str, subject_key: str,
                    title: str) -> None:
    """Range le titre de l'issue sur le sujet, quand il a bougé.

    Rattrape un titre édité sur GitHub, et les sujets admis avant que la
    colonne existe. Un sujet inconnu n'a pas de ligne : rien n'est touché.
    """
    conn.execute(
        "UPDATE subject SET title = %s WHERE graph = %s AND subject_key = %s "
        "AND title IS DISTINCT FROM %s",
        (title, name, subject_key, title),
    )


def _known(conn: Connection, name: str, subject_key: str) -> bool:
    """Une seule admission automatique — ré-admettre est un geste explicite."""
    return conn.execute(
        "SELECT 1 FROM subject s JOIN work_item w ON w.subject_id = s.id "
        "WHERE s.graph = %s AND s.subject_key = %s LIMIT 1", (name, subject_key),
    ).fetchone() is not None


def _admit(conn: Connection, revision: str, subject_key: str, issue: dict,
           retry: tuple[int, int] | None) -> int:
    if retry is None:
        return kernel.admit(conn, revision, subject_key, issue["title"])
    return kernel.admit(
        conn, revision, subject_key, issue["title"],
        prepare=lambda new_id: copy_retry_files(retry[1], new_id),
    )


def admit_labeled(conn: Connection, gh: GitHub, revision: str,
                  said: set[tuple[int, str]],
                  retries: dict[int, tuple[int, int]] | None = None
                  ) -> tuple[set[int], set[int]]:
    """Admet au plus une issue ; les dépendances et la voie diffèrent le reste.

    Renvoie séparément les dépendances (`rail:blocked`) et la file de la voie
    unique (`rail:queued`). Un item en attente n'existe qu'en issue GitHub :
    il n'a donc ni deadline, ni bail, ni tentative avant son vrai départ.
    """
    name = graph.load_bundle(conn, revision)["name"]
    blocked: set[int] = set()
    queued: set[int] = set()
    free = conn.execute(
        "SELECT id FROM work_item WHERE terminal_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone() is None
    states: dict[int, str | None] = {}
    for issue in sorted(gh.labeled_issues(), key=lambda row: row["number"]):
        subject_key = f"gh:{gh.repo}#{issue['number']}"
        _remember_title(conn, name, subject_key, issue["title"])
        if _known(conn, name, subject_key):
            continue
        waiting = pending_deps(gh, issue, name, states, said)
        if waiting:
            blocked.add(issue["number"])
            refs = ", ".join(f"#{dep}" for dep in waiting)
            say_once(gh, issue["number"], f"{name}-blocked",
                     f"**Admission différée** — en attente de {refs}.\n\n"
                     f"Le corps déclare `{DEPENDS} #<numéro>` : le rail admet "
                     "cette issue au tick qui suit la fermeture de la dernière "
                     "dépendance.", said)
            continue
        if not free:
            queued.add(issue["number"])
            continue
        retry = retry_source(conn, gh, issue, name, states, said)
        try:
            item_id = _admit(conn, revision, subject_key, issue, retry)
        except kernel.LaneOccupied:
            queued.add(issue["number"])
            free = False
            continue
        except RuntimeError as exc:
            print(f"#{issue['number']} refusé : {exc}", flush=True)
            continue
        if retry and retries is not None:
            retries[item_id] = retry
        print(f"#{issue['number']} admis → item {item_id}", flush=True)
        free = False
    return blocked, queued


# -------------------------------------------------------------------- découpe


def reparent(body: str, mother: int, child: int) -> str:
    """Le corps, avec les seules lignes `Depends-on: #mère` visant la fille.

    Rien d'autre n'est touché — pas même une ligne `Depends-on:` vers une
    autre issue, ni le `#mère` qui traînerait dans la prose : le corps
    appartient à l'humain, cette ligne seule appartient au rail.
    """
    lines = [f"{DEPENDS} #{child}"
             if line.strip().startswith(DEPENDS)
             and line.strip()[len(DEPENDS):].strip() == f"#{mother}"
             else line
             for line in (body or "").splitlines()]
    return "\n".join(lines) + ("\n" if (body or "").endswith("\n") else "")


def _admitted(gh: GitHub, number: int) -> bool:
    """Le rail a-t-il déjà pris cette issue en charge ?

    L'accusé posté sur l'issue est la trace durable de l'admission — le
    label `rail:<état>`, lui, s'en va au terminal.
    """
    return any(f"{ADMITTED} -->" in c["body"] for c in gh.comments(number))


def _dependents(gh: GitHub, mother: int) -> list[dict]:
    """Les issues encore en attente qui déclarent dépendre de la mère."""
    return [issue for issue in gh.labeled_issues()
            if f"#{mother}" in depends_on(issue["body"])
            and not _admitted(gh, issue["number"])]


def _refs(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def split_close(gh: GitHub, mother: int, children: list[int]) -> None:
    """La fin d'une découpe : les dépendances reportées, puis la mère fermée.

    Les dépendants sont reposés sur la **dernière fille** de la chaîne : sa
    fermeture à elle dit que le travail de la mère est intégralement livré.
    L'ordre fait la sûreté : tant qu'un dépendant n'est pas reporté, la mère
    reste ouverte — d'où le `SystemExit` qui nomme l'issue non réécrite.
    Rejouable : fermer une issue fermée est sans effet.
    """
    last, said = children[-1], set()
    for issue in _dependents(gh, mother):
        number = issue["number"]
        try:
            gh.edit_issue(number, reparent(issue["body"], mother, last))
            say_once(gh, number, f"reparent-{mother}",
                     f"**Reprise de dépendance** — `{DEPENDS} #{mother}` devient "
                     f"`{DEPENDS} #{last}`.\n\n"
                     f"#{mother} a été découpée en {_refs(children)} ; sa "
                     f"fermeture ne livre plus le travail attendu. La dernière "
                     f"fille, #{last}, le livre : cette issue attend maintenant "
                     f"celle-là.", said)
        except OSError as exc:
            raise SystemExit(
                f"#{number} non réécrite ({exc}) — découpe interrompue, "
                f"#{mother} reste ouverte")
        print(f"#{number} ← {DEPENDS} #{last} (était #{mother})", flush=True)
    gh.post_comment(mother, f"Découpée en {_refs(children)} — suivi sur les filles.")
    gh.close_issue(mother)
    print(f"#{mother} fermée — découpée en {_refs(children)}", flush=True)
