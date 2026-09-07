"""Le nœud arbitre : un JUDGE qui départage les finalistes d'un `keep_n`.

Un seul finaliste, et il est traversé sans dépenser un jeton ; plusieurs,
et un modèle lit leurs diffs — anonymes — puis élit ; aucun, et le travail
repart en amont. Dans les deux premiers cas, le travail de l'élu est promu
sur la branche de l'item et les ateliers des finalistes sont détruits.
"""

import json
from pathlib import Path

from . import worktree
from .agent import run_agent
from .bundle import KERNEL_OUTCOMES, judge_source
from .context import Context
from .workspace import OUTCOME_NAME, VERDICT_NAME

JUDGE_WORK_CHARS = 30000  # le travail d'un finaliste cité dans le prompt du juge
LETTRES = "ABCDEFGH"  # le nom anonyme d'un finaliste dans le dossier du juge


def _finalists(ctx: Context) -> list[int]:
    """Les candidats que la réduction `keep_n` a laissés passer, dans l'ordre.

    Ce sont les runs encore `applied` de la dernière tentative du nœud de
    fan-out d'amont : `keep_n` a recalé les autres réussites en `superseded`,
    et les échecs portent une issue du noyau. L'ordre est celui des dates de
    fin, celui-là même dont `keep_n` s'est servie.
    """
    source = judge_source(ctx.node)
    rows = ctx.conn.execute(
        "SELECT candidate, status, outcome FROM node_run WHERE item_id = %s "
        "AND node = %s AND (cycle, attempt) = ("
        "  SELECT cycle, attempt FROM node_run WHERE item_id = %s AND node = %s "
        "  ORDER BY cycle DESC, attempt DESC LIMIT 1) "
        "ORDER BY finished_at, id",
        (ctx.item["id"], source, ctx.item["id"], source),
    ).fetchall()
    return [r["candidate"] for r in rows
            if r["candidate"] is not None and r["status"] == "applied"
            and r["outcome"] and r["outcome"] not in KERNEL_OUTCOMES]


def _verdict(ctx: Context, texte: str) -> None:
    """Écrit — ou complète — le verdict du juge dans le workspace de l'item."""
    with (ctx.workspace / VERDICT_NAME).open("a") as f:
        f.write(texte)


def _dossier(ctx: Context, finalists: list[int]) -> str:
    """Le dossier du juge : un finaliste par lettre, son travail, et rien d'autre.

    **Ce qui n'y est pas est le cœur du nœud.** Ni le modèle, ni la CLI, ni le
    `label` ou la `strategy` de la variante, ni même le numéro de candidat :
    un juge qui saurait qu'un finaliste vient d'un « gros » modèle le
    préfèrerait. Il ne reste que les diffs et les critères — de quoi juger le
    travail, pas son auteur.
    """
    parts = [
        f"\n\n--- Les finalistes ({len(finalists)}) ---\n"
        "Chaque finaliste est une implémentation complète, partie du même commit. "
        "Tu ne sais rien de qui l'a produite, et c'est voulu : juge le travail, "
        "pas son auteur."
    ]
    for lettre, candidate in zip(LETTRES, finalists):
        work = worktree.candidate_work(ctx.item["id"], candidate, JUDGE_WORK_CHARS)
        parts.append(f"\n### Finaliste {lettre}\n\n"
                     + (work or "(travail illisible : aucun diff à lire)"))
    return "\n".join(parts)


def _elu(workspace: Path, finalists: list[int]) -> int | None:
    """La lettre que le juge a rendue, retraduite en numéro de candidat.

    Une lettre absente ou qui ne nomme aucun finaliste n'est pas un choix :
    le bloc rend None, et le noyau reverra la tentative.
    """
    try:
        elu = str(json.loads((workspace / OUTCOME_NAME).read_text())["elu"]).strip()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    index = LETTRES.find(elu[:1].upper()) if elu else -1
    return finalists[index] if 0 <= index < len(finalists) else None


def _promote(ctx: Context, candidate: int) -> tuple[str | None, str]:
    """Le travail de l'élu devient celui de l'item, et les ateliers partent.

    L'ordre compte : promouvoir d'abord, ranger ensuite — l'inverse
    détruirait la branche qu'on allait fusionner. Rend l'empêchement s'il y
    en a un, et le texte à mettre au verdict. Une promotion impossible ne
    range rien : le travail est encore dans les ateliers.
    """
    empechement = worktree.promote(ctx.item["id"], candidate)
    if empechement is not None:
        return empechement, (
            f"Promotion refusée — {empechement}. Le travail de l'élu n'a pas "
            "rejoint la branche de l'item ; les ateliers des finalistes restent "
            "en place.\n")
    retirees = worktree.discard(ctx.item["id"])
    # le workspace de l'élu est nommé ici, et seulement ici : le choix est
    # fait, l'anonymat du dossier n'a plus rien à protéger
    return None, (f"Travail de l'élu promu sur la branche de l'item — son workspace : "
                  f"c{candidate}. Ateliers des finalistes retirés : "
                  f"{', '.join(retirees) or 'aucun'}.\n")


def _promotion_ratee(empechement: str) -> dict:
    """L'issue d'un juge qui n'a pas pu promouvoir son élu : un échec franc."""
    return {"outcome": "crashed",
            "summary": f"promotion de l'élu impossible — {empechement}"}


def _sole(ctx: Context, candidate: int) -> dict:
    """Un seul finaliste : traversé sans dépenser un jeton.

    Un juge à qui l'on présente une option unique dit toujours oui : ce
    serait un tampon payant, qui fabriquerait de la fausse confiance.
    """
    empechement, rangement = _promote(ctx, candidate)
    _verdict(ctx, f"# Verdict — {ctx.run['node']}\n\n"
                  "Élu : finaliste A, et il était seul. Court-circuit mécanique : "
                  "aucun modèle n'a été appelé, aucun jeton dépensé — un juge à "
                  "qui l'on présente une option unique dit toujours oui.\n\n"
                  + rangement)
    if empechement is not None:
        return _promotion_ratee(empechement)
    return {"outcome": "sole",
            "summary": "finaliste unique — traversé sans appel de modèle"}


def _chosen(ctx: Context, finalists: list[int]) -> dict:
    """Plusieurs finalistes : un modèle lit les diffs, élit, et dit pourquoi."""
    workspace = ctx.workspace.resolve()
    ctx.extra = _dossier(ctx, finalists)
    result = run_agent(ctx)
    if result["outcome"] != "chosen":  # panne, dépassement, issue illisible
        return result
    candidate = _elu(workspace, finalists)
    if candidate is None:
        return result | {"outcome": "invalid_result",
                         "error": "issue « chosen » sans lettre de finaliste lisible"}
    lettre = LETTRES[finalists.index(candidate)]
    empechement, rangement = _promote(ctx, candidate)
    _verdict(ctx, f"\n\n## Verdict\n\nÉlu : finaliste {lettre} — "
                  f"{result.get('summary') or 'sans raison rendue'}\n\n" + rangement)
    if empechement is not None:
        return result | _promotion_ratee(empechement)
    return result | {"elu": lettre}


def arbitrate(ctx: Context) -> dict:
    """Le nœud arbitre : il choisit entre des finalistes, il ne corrige rien.

    Trois issues fermées : `sole`, un seul finaliste, traversé sans modèle ;
    `chosen`, plusieurs, un modèle élit ; `none`, aucun, retour vers l'amont
    borné par le budget d'escalade existant.
    """
    finalists = _finalists(ctx)
    if not finalists:
        _verdict(ctx, f"# Verdict — {ctx.run['node']}\n\n"
                      "Aucun finaliste : rien à départager, le travail repart en "
                      "amont.\n")
        return {"outcome": "none",
                "summary": "aucun finaliste — retour en amont sans jugement"}
    if len(finalists) == 1:
        return _sole(ctx, finalists[0])
    return _chosen(ctx, finalists)
