"""Les portes déterministes d'un nœud : ce qu'elles rétrogradent, et quand elles se taisent.

Sans base : la porte de déploiement ne lit que deux lignes — la demande
durable et le heartbeat —, qu'une base réduite à ses réponses lui donne. Le
checkout est un dépôt jetable, docker et gh des scripts qui notent leurs
appels.

  1. un résultat qui n'est pas `pass` est laissé tel quel, sans une requête
  2. un `pass` sur un code qui n'est pas la release est rétrogradé en `fail`,
     et le résumé nomme chaque discordance : SHA durable, checkout, worker,
     services ; la ligne rejoint `<nœud>.md`
  3. quand tout concorde, la porte se tait et n'écrit rien
  4. un rapport qu'on ne peut pas ouvrir n'empêche pas la rétrogradation
  5. `elected_verdict` : un non-`pass` passe ; des critères ratés par le juge
     élu font un `fail` qui les numérote ; un verdict sans statut numéroté
     est un défaut de format ; un verdict propre laisse le `pass`
  6. `elected_failures` : fichier absent, pas d'élu, section absente, sans
     entrée numérotée, ligne « raté » sans numéro
  7. `_command_output` : exécutable absent, délai dépassé, code non nul
     rendent tous une sortie vide
  8. `deployed_service_shas` sans conteneur : chaque service vide, et aucun
     `inspect` ; un `gh` absent ne casse pas la sonde

Usage : uv run python tests/gates_test.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import gates  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

from bords_outils import (ConnMuette, FauxConn, commande, contexte,  # noqa: E402
                          lignes)
from outils import data_dir_jetable, depot_git  # noqa: E402

VERDICT_RATE = ("## Finaliste B\n\n1. Tenu. oui\n2. Raté. preuve absente\n"
                "4. Raté. non prouvé\n\n## Verdict\n\nÉlu : finaliste B\n")


def faux_docker(dossier: Path) -> Path:
    """Un docker qui note ses appels et rend l'étiquette `GATES_SHA`, s'il y en a une."""
    journal = dossier / "journal.txt"
    journal.write_text("")
    docker = dossier / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{journal}"\n'
        'case "$1 $2" in\n'
        '  "compose ps") [ -n "$GATES_SHA" ] && echo "conteneur-$4"; exit 0 ;;\n'
        '  "inspect "*) echo "$GATES_SHA"; exit 0 ;;\n'
        "esac\n"
        "exit 1\n"
    )
    docker.chmod(0o755)
    return journal


def conn_de(expected: str | None, worker: str | None) -> FauxConn:
    """La base telle que la porte la lit : la demande durable et le heartbeat."""
    return FauxConn({"FROM node_run": None if expected is None else {"sha": expected},
                     "FROM heartbeat": None if worker is None else {"worker_sha": worker}})


def porte(conn, resultat: dict, nom: str = "deploy_verification") -> dict | None:
    ctx = contexte(commande("true"), conn, node="verify")
    return gates.apply(nom, ctx, ctx.workspace, resultat)


def rapport() -> list[str]:
    return lignes(bureau.item_workspace(0) / "verify.md")


def laisse_tranquille() -> None:
    """1. un résultat qui n'est pas `pass` traverse la porte sans une requête."""
    for outcome in ("fail", "done", "crashed"):
        assert porte(ConnMuette(), {"outcome": outcome}) is None, outcome
    assert rapport() == [], rapport()
    print("1. fail, done, crashed : laissés tels quels, la base jamais lue ✓")


def toutes_les_discordances() -> None:
    """2. un `pass` sur un code qui n'est pas la release nomme chaque écart."""
    os.environ["GATES_SHA"] = ""
    rendu = porte(conn_de("pas-un-sha", "c" * 40), {"outcome": "pass", "summary": "ok"})
    assert rendu["outcome"] == "fail", rendu
    for attendu in ("SHA de déploiement durable absent ou invalide",
                    "checkout", "différent de pas-un-sha", f"worker {'c' * 40}",
                    "services discordants : github-sync, web, front"):
        assert attendu in rendu["summary"], (attendu, rendu)
    assert rapport() == [f"porte du noyau - {rendu['summary']}"], rapport()
    absent = porte(conn_de(None, None), {"outcome": "pass"})
    assert "absent ou invalide" in absent["summary"], absent
    assert "worker inconnu" not in absent["summary"], absent  # rien ne diffère de rien
    print("2. pass rétrogradé : SHA invalide, checkout, worker et services "
          "nommés dans le résumé et dans verify.md ✓")


def concordance(checkout: str) -> None:
    """3. quand tout concorde, la porte se tait et n'écrit rien."""
    os.environ["GATES_SHA"] = checkout
    avant = rapport()
    assert porte(conn_de(checkout, checkout), {"outcome": "pass"}) is None
    assert rapport() == avant, "la porte a écrit sans rien refuser"
    print("3. release durable = checkout = worker = services : la porte se tait ✓")


def rapport_inaccessible() -> None:
    """4. un rapport impossible à ouvrir n'empêche pas la rétrogradation."""
    chemin = bureau.item_workspace(0) / "verify.md"
    sauve = chemin.read_text()
    chemin.unlink()
    chemin.mkdir()
    try:
        rendu = porte(conn_de("b" * 40, "b" * 40), {"outcome": "pass"})
    finally:
        chemin.rmdir()
        chemin.write_text(sauve)
    assert rendu["outcome"] == "fail" and "checkout" in rendu["summary"], rendu
    print("4. verify.md inaccessible : la porte rétrograde quand même ✓")


def verdict_elu() -> None:
    """5. la porte du juge élu : ratés numérotés, format, ou silence."""
    verdict = bureau.item_workspace(0) / bureau.VERDICT_NAME
    verdict.write_text(VERDICT_RATE)
    assert porte(ConnMuette(), {"outcome": "fail"}, "elected_verdict") is None
    rate = porte(ConnMuette(), {"outcome": "pass"}, "elected_verdict")
    assert rate["outcome"] == "fail" and ": 2, 4;" in rate["summary"], rate
    assert "- [ ] 2, 4 : preuve du juge élu insuffisante." in rapport(), rapport()
    verdict.write_text("Comparaison libre.\n\n## Verdict\n\nÉlu : finaliste B\n")
    forme = porte(ConnMuette(), {"outcome": "pass"}, "elected_verdict")
    assert "aucun statut numéroté" in forme["summary"], forme
    assert "- [ ] format du verdict : preuve du juge élu insuffisante." in rapport()
    verdict.write_text(VERDICT_RATE.replace("Raté", "Tenu"))
    assert porte(ConnMuette(), {"outcome": "pass"}, "elected_verdict") is None
    print("5. elected_verdict : non-pass laissé, ratés 2 et 4 numérotés, format "
          "sans statut refusé, verdict propre silencieux ✓")


def lecture_du_verdict(tmp: Path) -> None:
    """6. chaque forme dégradée du verdict rend ce que la porte doit en faire."""
    verdict = tmp / "verdict.md"
    cas = [
        (None, []),
        ("## Finaliste A\n\n1. Tenu.\n\n## Verdict\n\nPersonne.\n", []),
        ("## Finaliste A\n\n1. Tenu.\n\n## Verdict\n\nÉlu : finaliste B\n", [0]),
        ("## Finaliste A\n\nProse sans numéro.\n\n## Verdict\n\nÉlu : finaliste A\n",
         [0]),
        ("## Finaliste A\n\n1. Tenu.\nUn critère raté, sans dire lequel.\n\n"
         "## Verdict\n\nÉlu : finaliste A\n", [0]),
        ("## Finaliste A\n\n1. Tenu.\nCritère 3 raté.\n\n## Verdict\n\n"
         "Élu : finaliste A\n", [3]),
    ]
    for texte, attendu in cas:
        verdict.unlink(missing_ok=True)
        if texte is not None:
            verdict.write_text(texte)
        assert gates.elected_failures(verdict) == attendu, (texte, attendu)
    print("6. verdict absent ou sans élu : rien ; section absente, sans entrée, "
          "raté sans numéro : [0] ; raté numéroté hors liste : compté ✓")


def sorties_vides(dossier: Path) -> None:
    """7. exécutable absent, délai dépassé, code non nul : sortie vide."""
    lent = dossier / "lent"
    lent.write_text("#!/bin/sh\nsleep 5\necho tard\n")
    lent.chmod(0o755)
    assert gates._command_output([str(dossier / "absent")], timeout=1) == ""
    assert gates._command_output([str(lent)], timeout=0.2) == ""
    assert gates._command_output(["sh", "-c", "echo x; exit 3"], timeout=1) == ""
    assert gates._command_output(["sh", "-c", "echo x"], timeout=1) == "x"
    print("7. exécutable absent, délai dépassé, code non nul : « » ; succès : sa sortie ✓")


def sans_conteneur(journal: Path, repo: Path) -> None:
    """8. sans conteneur, chaque service est vide et rien n'est inspecté."""
    os.environ["GATES_SHA"] = ""
    journal.write_text("")
    vus = gates.deployed_service_shas(repo)
    assert vus == {name: "" for name in gates.DEPLOYED_SERVICES}, vus
    appels = lignes(journal)
    assert len(appels) == len(gates.DEPLOYED_SERVICES), appels
    assert not any("inspect" in appel for appel in appels), appels
    os.environ["GRAPHATOM_GH"] = str(repo / "gh-absent")
    assert gates.deployed_service_shas(repo) == vus, "un gh absent a changé la sonde"
    print("8. aucun conteneur : trois services vides, zéro inspect ; gh absent "
          "toléré ✓")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graphatom-gates-"))
    workdir = data_dir_jetable("graphatom-gates-data-")
    bureau.item_workspace(0).mkdir(parents=True)
    repo = depot_git(tmp, "gates")
    checkout = gates._command_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                     timeout=5)
    journal = faux_docker(tmp)
    (tmp / "gh").write_text("#!/bin/sh\necho jeton\n")
    (tmp / "gh").chmod(0o755)
    os.environ["GRAPHATOM_DOCKER"] = str(tmp / "docker")
    os.environ["GRAPHATOM_GH"] = str(tmp / "gh")
    try:
        laisse_tranquille()
        toutes_les_discordances()
        concordance(checkout)
        rapport_inaccessible()
        verdict_elu()
        lecture_du_verdict(tmp)
        sorties_vides(tmp)
        sans_conteneur(journal, repo)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)
    print("\nportes du noyau : OK")


if __name__ == "__main__":
    main()
