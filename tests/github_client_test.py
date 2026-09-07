"""Le test du client GitHub : chaque appel sortant, vu du serveur.

Scénario, sans réseau ni base — un vrai serveur HTTP local tient le rôle
d'api.github.com et note tout ce qu'il reçoit :

  1. chaque appel porte le jeton, l'Accept et la version de l'API ; une
     lecture n'a pas de corps, une écriture porte du JSON
  2. la liste des issues filtre sur le label et écarte les pull requests ;
     `GRAPHATOM_TAKE_ALL` lève le filtre du label, pas celui des PR
  3. une issue absente vaut None, une panne du serveur lève — la tolérance
     ne couvre qu'un code précis
  4. un label naît à la volée, en 422 sans bruit, rouge pour la seule alarme
  5. retirer un label absent est sans erreur, et le nom voyage encodé
  6. fermer, réécrire, éditer, commenter, lire : la méthode et le chemin
     attendus, un corps vide se lit comme `None`
  7. un serveur muet finit en `TimeoutError` — jamais en attente infinie
  8. `from_env` refuse de partir sans jeton, et le lit quand il est là

Usage : uv run python tests/github_client_test.py
"""

import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from github_outils import REPO, Serveur  # noqa: E402
from graphatom import github_client  # noqa: E402

JETON = "ghp_jeton_de_test"
ENTETES = {"authorization": f"Bearer {JETON}",    # urllib normalise la casse
           "accept": "application/vnd.github+json",
           "x-github-api-version": "2022-11-28"}
ISSUES = f"/repos/{REPO}/issues"


def corps_json(requete: dict) -> dict:
    return json.loads(requete["body"].decode())


def entetes_portes(serveur: Serveur, gh: github_client.GitHub) -> None:
    """1. Le jeton et les en-têtes GitHub sur chaque appel, le JSON sur les écritures."""
    serveur.programme("GET", f"{ISSUES}/1", 200, {"number": 1})
    gh.issue(1)
    lecture = serveur.derniere()
    for cle, valeur in ENTETES.items():
        assert lecture["headers"][cle] == valeur, (cle, lecture["headers"])
    assert lecture["body"] == b"", "une lecture ne porte aucun corps"

    gh.post_comment(1, "bonjour")
    ecriture = serveur.derniere()
    for cle, valeur in ENTETES.items():
        assert ecriture["headers"][cle] == valeur, (cle, ecriture["headers"])
    assert ecriture["method"] == "POST" and ecriture["path"] == f"{ISSUES}/1/comments"
    assert corps_json(ecriture) == {"body": "bonjour"}, ecriture["body"]
    print("1. jeton, Accept et version sur chaque appel ; JSON sur les écritures ✓")


def liste_filtree(serveur: Serveur, gh: github_client.GitHub) -> None:
    """2. Le filtre du label dans l'URL, les PR écartées, `TAKE_ALL` lève le label."""
    chemin = f"{ISSUES}?labels={github_client.LABEL}&state=open&per_page=100"
    serveur.programme("GET", chemin, 200, [
        {"number": 1}, {"number": 2, "pull_request": {"url": "…"}}, {"number": 3}])
    assert [r["number"] for r in gh.labeled_issues()] == [1, 3]
    assert serveur.derniere()["path"] == chemin, serveur.derniere()["path"]

    os.environ["GRAPHATOM_TAKE_ALL"] = "1"
    try:
        tout = f"{ISSUES}?state=open&per_page=100"
        serveur.programme("GET", tout, 200, [{"number": 9, "pull_request": {}},
                                             {"number": 10}])
        assert [r["number"] for r in gh.labeled_issues()] == [10]
        assert serveur.derniere()["path"] == tout, serveur.derniere()["path"]
    finally:
        del os.environ["GRAPHATOM_TAKE_ALL"]
    print("2. label dans l'URL, PR écartées, `GRAPHATOM_TAKE_ALL` lève le label ✓")


def tolerance_precise(serveur: Serveur, gh: github_client.GitHub) -> None:
    """3. Une issue absente vaut None ; toute autre erreur HTTP lève."""
    serveur.programme("GET", f"{ISSUES}/404", 404, {"message": "Not Found"})
    assert gh.issue(404) is None
    serveur.programme("GET", f"{ISSUES}/500", 500, {"message": "boom"})
    try:
        gh.issue(500)
        raise AssertionError("un 500 n'est pas une absence d'issue")
    except urllib.error.HTTPError as exc:
        assert exc.code == 500, exc
    serveur.programme("GET", f"{ISSUES}/7", 200, {"number": 7, "state": "closed"})
    assert gh.issue(7) == {"number": 7, "state": "closed"}
    print("3. issue absente → None, panne du serveur → l'erreur monte ✓")


def naissance_des_labels(serveur: Serveur, gh: github_client.GitHub) -> None:
    """4. Un label est créé en bleu, l'alarme en rouge ; « existe déjà » ne dit rien."""
    chemin = f"/repos/{REPO}/labels"
    serveur.programme("POST", chemin, 201, {})
    gh.create_label("rail:implement")
    assert corps_json(serveur.derniere()) == {
        "name": "rail:implement", "color": github_client.RAIL_COLOR}
    gh.create_label(github_client.STALLED)
    assert corps_json(serveur.derniere())["color"] == github_client.STALLED_COLOR
    serveur.programme("POST", chemin, 422, {"message": "already_exists"})
    assert gh.create_label("rail:implement") is None, "422 : il existe, tant mieux"

    gh.add_label(4, "rail:blocked")
    pose = serveur.derniere()
    assert (pose["method"], pose["path"]) == ("POST", f"{ISSUES}/4/labels"), pose
    assert corps_json(pose) == {"labels": ["rail:blocked"]}, pose["body"]
    print("4. label créé à la volée, rouge pour la seule alarme, 422 toléré ✓")


def retrait_encode(serveur: Serveur, gh: github_client.GitHub) -> None:
    """5. Le nom du label voyage encodé ; déjà retiré n'est pas une erreur."""
    chemin = f"{ISSUES}/4/labels/rail%3Aimplement"
    serveur.programme("DELETE", chemin, 200, [])
    gh.remove_label(4, "rail:implement")
    retrait = serveur.derniere()
    assert (retrait["method"], retrait["path"]) == ("DELETE", chemin), retrait
    serveur.programme("DELETE", chemin, 404, {"message": "Label does not exist"})
    assert gh.remove_label(4, "rail:implement") is None
    assert len([r for r in serveur.journal if r["method"] == "DELETE"]) == 2
    print("5. `:` encodé dans le chemin du retrait, 404 toléré ✓")


def gestes_restants(serveur: Serveur, gh: github_client.GitHub) -> None:
    """6. Fermer, réécrire, éditer, lire : méthode et chemin ; un corps vide vaut None."""
    serveur.programme("PATCH", f"{ISSUES}/5", 200, {})
    gh.close_issue(5)
    assert corps_json(serveur.derniere()) == {"state": "closed"}
    gh.edit_issue(5, "Depends-on: #9")
    assert corps_json(serveur.derniere()) == {"body": "Depends-on: #9"}
    assert serveur.derniere()["method"] == "PATCH"

    serveur.programme("PATCH", f"/repos/{REPO}/issues/comments/77", 200, {})
    gh.edit_comment(77, "trajectoire")
    edition = serveur.derniere()
    assert edition["path"] == f"/repos/{REPO}/issues/comments/77", edition
    assert corps_json(edition) == {"body": "trajectoire"}

    serveur.programme("GET", f"{ISSUES}/5/labels?per_page=100", 200,
                      [{"name": "graphatom"}, {"name": "rail:scope"}])
    assert gh.issue_labels(5) == ["graphatom", "rail:scope"]
    serveur.programme("GET", f"{ISSUES}/5/comments?per_page=100", 200,
                      [{"id": 1, "body": "x"}])
    assert gh.comments(5) == [{"id": 1, "body": "x"}]

    serveur.programme("DELETE", f"{ISSUES}/5/labels/rail%3Ascope", 204, None)
    assert gh.remove_label(5, "rail:scope") is None, "204 sans corps se lit"
    print("6. PATCH/GET aux chemins attendus, un 204 sans corps se lit ✓")


def serveur_muet(serveur: Serveur, gh: github_client.GitHub) -> None:
    """7. Un serveur qui accepte et se tait finit en `TimeoutError`, une OSError."""
    borne = github_client.TIMEOUT_S
    github_client.TIMEOUT_S = 0.3
    serveur.httpd.attente_s = 5.0
    try:
        gh.issue(8)
        raise AssertionError("l'appel aurait dû expirer")
    except TimeoutError as exc:
        assert isinstance(exc, OSError), "l'incident réseau du tick"
    finally:
        github_client.TIMEOUT_S = borne
        serveur.httpd.attente_s = 0.0
        serveur.httpd.mute.set()
    print("7. serveur muet : `TimeoutError` après la borne, jamais l'attente infinie ✓")


def jeton_d_environnement() -> None:
    """8. Sans `GITHUB_TOKEN`, le canal refuse de partir ; avec, il le porte."""
    os.environ.pop("GITHUB_TOKEN", None)
    try:
        github_client.from_env(REPO)
        raise AssertionError("sans jeton, pas de client")
    except SystemExit as exc:
        assert "GITHUB_TOKEN" in str(exc), exc
    os.environ["GITHUB_TOKEN"] = JETON
    gh = github_client.from_env(REPO)
    assert (gh.repo, gh.token) == (REPO, JETON), (gh.repo, gh.token)
    print("8. `from_env` : SystemExit sans jeton, le client le porte sinon ✓")


def main() -> None:
    serveur = Serveur()
    github_client.API = serveur.url
    gh = github_client.GitHub(REPO, JETON)
    try:
        entetes_portes(serveur, gh)
        liste_filtree(serveur, gh)
        tolerance_precise(serveur, gh)
        naissance_des_labels(serveur, gh)
        retrait_encode(serveur, gh)
        gestes_restants(serveur, gh)
        serveur_muet(serveur, gh)
        jeton_d_environnement()
    finally:
        serveur.fermer()
    print("\nclient github : OK — chaque appel sortant est celui qu'on attend")


if __name__ == "__main__":
    main()
