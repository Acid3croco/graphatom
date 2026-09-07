"""Le nœud arbitre et les blocs qui délèguent : sans base, sans modèle.

Les finalistes viennent d'une base réduite à ses réponses, le juge est une
commande shell qui écrit son issue, et aucun dépôt n'est là — la promotion
n'a rien à faire, sauf quand le test lui oppose un empêchement :

  1. aucun finaliste : `none`, et le verdict dit que rien n'est à départager
  2. un seul finaliste : `sole`, traversé sans agent — aucun prompt rendu —
     et le verdict nomme le court-circuit et le workspace de l'élu
  3. le même finaliste seul, promotion empêchée : `crashed`, le verdict porte
     le refus, les ateliers restent en place
  4. plusieurs finalistes : le dossier du juge nomme A et B sans candidat ni
     modèle, l'agent élit B, le verdict cite sa raison et le résultat porte `elu`
  5. une issue `chosen` sans lettre lisible, ou avec une lettre qui ne nomme
     personne : `invalid_result`, avec l'erreur
  6. l'élu promu impossible : `crashed`, la raison dans le résumé et le verdict
  7. un juge qui ne rend pas `chosen` — crash — est rendu tel quel
  8. les blocs délèguent : JUDGE et CHECK avec exécution jouent l'agent, le
     CHECK muet rend son `stub_outcome` ou `pass`
  9. les lettres aux bornes : A élit le premier finaliste, la lettre qui
     suit le dernier ne nomme personne, et une minuscule vaut sa majuscule

Usage : uv run python tests/arbitrage_test.py
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.pop("GRAPHATOM_REPO_DIR", None)

from graphatom import arbitrage, blocks  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

from bords_outils import FauxConn, commande, contexte, issue, patche  # noqa: E402
from outils import data_dir_jetable  # noqa: E402

AMONT = "implement"
JUGE = "juge"


def finaliste(candidate: int, status: str = "applied", outcome: str = "done") -> dict:
    return {"candidate": candidate, "status": status, "outcome": outcome}


def juge(cmd: str, finalistes: list[dict], attempt: int = 1):
    """Le contexte d'un JUDGE arbitre dont l'agent est `cmd`."""
    spec = {"block": "JUDGE", "edges": {"sole": "fini", "chosen": "fini", "none": "amont"},
            "config": {"finalists_from": AMONT,
                       "execution": {"kind": "agent", "cmd": cmd,
                                     "timeout_s": 5, "silence_s": 5},
                       "agent": {"prompt": "juge les finalistes", "passation": False}}}
    conn = FauxConn(listes={"FROM node_run": finalistes})
    return contexte(spec, conn, node=JUGE, attempt=attempt)


def verdict() -> str:
    return (bureau.item_workspace(0) / bureau.VERDICT_NAME).read_text()


def purge() -> None:
    (bureau.item_workspace(0) / bureau.VERDICT_NAME).unlink(missing_ok=True)


def personne() -> None:
    """1. aucun finaliste : `none`, et rien n'est jugé."""
    purge()
    rendu = blocks.judge(juge("false", [finaliste(0, "superseded"),
                                       finaliste(1, outcome="crashed"),
                                       {"candidate": None, "status": "applied",
                                        "outcome": "done"}]))
    assert rendu["outcome"] == "none", rendu
    assert "Aucun finaliste" in verdict(), verdict()
    print("1. supersédé, crashé ou sans candidat : aucun finaliste → none ✓")


def seul() -> None:
    """2. un finaliste unique traverse sans agent, et le verdict le dit."""
    purge()
    ctx = juge("false", [finaliste(3)])
    rendu = arbitrage.arbitrate(ctx)
    assert rendu["outcome"] == "sole", rendu
    assert not list(ctx.workspace.glob("prompt*")), "un prompt a été rendu"
    texte = verdict()
    assert "Élu : finaliste A, et il était seul" in texte, texte
    assert "son workspace : c3" in texte and "retirés : aucun" in texte, texte
    print("2. finaliste unique : sole sans agent, verdict nommant c3 ✓")


def seul_sans_promotion() -> None:
    """3. l'unique finaliste qu'on ne peut pas promouvoir : un échec franc."""
    purge()
    with patche(arbitrage.worktree, "promote", lambda *_a: "merge --ff-only refusé"):
        rendu = arbitrage.arbitrate(juge("false", [finaliste(3)]))
    assert rendu == {"outcome": "crashed",
                     "summary": "promotion de l'élu impossible — merge --ff-only refusé"}
    assert "Promotion refusée — merge --ff-only refusé" in verdict(), verdict()
    assert "restent en place" in verdict(), verdict()
    print("3. finaliste unique non promouvable : crashed, refus au verdict ✓")


def elu(lettre: str = "B", attempt: int = 1) -> tuple:
    """Un juge à deux finalistes dont l'agent élit `lettre`."""
    ctx = juge(issue("chosen", elu=lettre), [finaliste(2), finaliste(5)], attempt)
    return ctx, arbitrage.arbitrate(ctx)


def choisi() -> None:
    """4. le dossier anonyme, l'élection, la raison au verdict, `elu` au résultat."""
    purge()
    ctx, rendu = elu()
    assert rendu["outcome"] == "chosen" and rendu["elu"] == "B", rendu
    dossier = (ctx.workspace / f"prompt-{JUGE}-1-1.md").read_text()
    assert "### Finaliste A" in dossier and "### Finaliste B" in dossier, dossier
    assert "(travail illisible" in dossier, dossier  # sans dépôt : aucun diff à lire
    for secret in ("c2", "c5", "candidat", "model", "cli"):
        assert secret not in dossier.split("--- Les finalistes")[1], secret
    texte = verdict()
    assert "## Verdict\n\nÉlu : finaliste B — fait" in texte, texte
    assert "son workspace : c5" in texte, texte
    print("4. deux finalistes : dossier anonyme, B élu, raison au verdict, "
          "résultat porte elu=B ✓")


def lettre_illisible() -> None:
    """5. `chosen` sans lettre, ou une lettre qui ne nomme personne : invalide."""
    purge()
    ctx = juge(issue("chosen"), [finaliste(2), finaliste(5)], attempt=2)
    sans = arbitrage.arbitrate(ctx)
    assert sans["outcome"] == "invalid_result" and "lettre" in sans["error"], sans
    _, hors = elu("Z", attempt=3)
    assert hors["outcome"] == "invalid_result", hors
    assert not (bureau.item_workspace(0) / bureau.VERDICT_NAME).exists(), "verdict écrit"
    print("5. chosen sans lettre ou lettre Z : invalid_result, aucun verdict ✓")


def choisi_sans_promotion() -> None:
    """6. l'élu qu'on ne peut pas promouvoir : crashed, la raison partout."""
    purge()
    with patche(arbitrage.worktree, "promote", lambda *_a: "atelier de c5 disparu"):
        _, rendu = elu("B", attempt=4)
    assert rendu["outcome"] == "crashed", rendu
    assert rendu["summary"] == "promotion de l'élu impossible — atelier de c5 disparu"
    assert "Promotion refusée — atelier de c5 disparu" in verdict(), verdict()
    print("6. élu non promouvable : crashed, refus dans le résumé et le verdict ✓")


def juge_en_panne() -> None:
    """7. un juge qui ne rend pas `chosen` est rendu tel quel, sans élection."""
    purge()
    rendu = arbitrage.arbitrate(juge("exit 3", [finaliste(2), finaliste(5)], 5))
    assert rendu["outcome"] == "crashed" and rendu["exit_code"] == 3, rendu
    assert not (bureau.item_workspace(0) / bureau.VERDICT_NAME).exists(), "verdict écrit"
    print("7. juge crashé : son autopsie, aucun verdict ✓")


def lettres_aux_bornes() -> None:
    """9. A : le premier finaliste ; C, au-delà du dernier : personne ; b : le second."""
    purge()
    _, premier = elu("A", attempt=6)
    assert premier["outcome"] == "chosen" and premier["elu"] == "A", premier
    assert "son workspace : c2" in verdict(), verdict()
    purge()
    hors_liste = arbitrage.LETTRES[2]  # deux finalistes : la troisième lettre ne nomme personne
    _, hors = elu(hors_liste, attempt=7)
    assert hors["outcome"] == "invalid_result", hors
    _, second = elu("b", attempt=8)
    assert second["elu"] == "B" and "son workspace : c5" in verdict(), verdict()
    purge()
    _, en_mots = elu("B — le plus sobre", attempt=9)  # la première lettre suffit
    assert en_mots["elu"] == "B" and "son workspace : c5" in verdict(), verdict()
    print(f"9. A élit le premier (c2) ; {hors_liste}, au-delà du dernier : "
          "invalid_result ; b minuscule, ou « B — … » en mots, élit le second (c5) ✓")


def delegations() -> None:
    """8. JUDGE et CHECK avec exécution jouent l'agent ; le CHECK muet, son stub."""
    execute = commande(issue("pass"))
    assert blocks.judge(contexte(execute | {"block": "JUDGE"}, node="j"))["outcome"] == "pass"
    assert blocks.check(contexte(execute | {"block": "CHECK"}, node="c"))["outcome"] == "pass"
    muet = {"block": "CHECK", "edges": {"pass": "fini", "fail": "amont"}, "config": {}}
    assert blocks.check(contexte(muet, node="stub")) == {"outcome": "pass"}
    muet["config"]["stub_outcome"] = "fail"
    assert blocks.check(contexte(muet, node="stub")) == {"outcome": "fail"}
    print("8. JUDGE et CHECK exécutés : l'issue de l'agent ; CHECK muet : "
          "stub_outcome ou pass ✓")


def main() -> None:
    workdir = data_dir_jetable("graphatom-arbitrage-")
    bureau.item_workspace(0).mkdir(parents=True)
    try:
        personne()
        seul()
        seul_sans_promotion()
        choisi()
        lettre_illisible()
        choisi_sans_promotion()
        juge_en_panne()
        lettres_aux_bornes()
        delegations()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("\narbitrage : OK")


if __name__ == "__main__":
    main()
