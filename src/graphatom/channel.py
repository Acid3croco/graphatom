"""Le canal humain — module hors noyau.

Un canal ne fait que deux choses : montrer les questions ouvertes, et
enregistrer une réponse parmi les options autorisées. Il n'écrit jamais
l'état d'un item — c'est l'ordonnanceur qui route la réponse via
apply_item, au tick suivant. La séparation canal / noyau est la règle.
"""

from psycopg import Connection


def open_questions(conn: Connection) -> list[dict]:
    """Les questions ouvertes, avec le contexte de leur item."""
    return list(conn.execute(
        "SELECT q.*, s.subject_key, w.state AS item_state, w.escalations "
        "FROM question q "
        "JOIN work_item w ON w.id = q.item_id "
        "JOIN subject s ON s.id = w.subject_id "
        "WHERE q.state = 'open' ORDER BY q.deadline"
    ))


def record_answer(conn: Connection, question_id: int, option: str, by: str) -> str | None:
    """Enregistre une réponse. Retourne None si ok, sinon le motif du refus."""
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM question WHERE id = %s FOR UPDATE", (question_id,)
        ).fetchone()
        if row is None:
            return "question inconnue"
        if row["state"] != "open":
            return f"question déjà réglée ({row['state']})"
        if option not in row["options"]:
            return f"option invalide — attendu : {', '.join(row['options'])}"
        conn.execute(
            "UPDATE question SET state = 'answered', answer = %s, "
            "answered_by = %s, answered_at = now() WHERE id = %s",
            (option, by, question_id),
        )
    return None
