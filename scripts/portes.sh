#!/usr/bin/env bash
#
# Les portes déterministes d'un candidat d'`implement`.
#
# `implement` est une course : K candidats implémentent la même issue avec
# des stratégies différentes, et `first_pass` promeut le premier qui rend une
# issue de succès. Sans porte, « le premier qui réussit » ne voudrait dire
# que « le premier qui s'est déclaré fini » — on sélectionnerait le plus
# rapide à prétendre, pas le plus correct. Le candidat porte donc ses propres
# vérifications, et son `cmd` retire son `outcome.json` quand elles ne
# passent pas : le noyau ne voit alors aucun succès, et la course continue
# sans lui.
#
# Deux portes, dans cet ordre :
#
#   1. le projet s'importe — les onze modules du paquet, depuis l'atelier
#      du candidat ;
#   2. les tests sans base concernés par son diff passent.
#
# La sûreté en parallèle est tenue par le script, pas par la discipline de
# celui qui l'édite :
#
#   - `GRAPHATOM_DSN` et `GRAPHATOM_AGENT_DSN` sont coupées d'entrée. Les K
#     candidats partagent la base jetable de leur item : une porte qui la
#     détruit ou la recrée — `tests/crash_test.py` le fait — ferait tomber
#     ses frères en vol. Aucune porte d'ici ne touche une base, et sans DSN
#     aucune ne le pourrait même si elle changeait d'avis.
#   - `GRAPHATOM_REPO_DIR` est épinglé sur l'atelier du candidat. Le clone de
#     référence est partagé par tous les items du rail : un test qui y
#     retirerait un worktree emporterait le travail d'un autre item.
#   - tout le reste est déjà à un seul candidat : son atelier, donc son
#     `.venv`, ses `data/` et les dépôts jetables que ses tests fabriquent.
#     Les serveurs de test, eux, écoutent sur un port éphémère.
#
# Codes de retour :
#
#   2  environnement incomplet : pas d'atelier à examiner
#   3  une porte a échoué — le pas et sa sortie sont dans `portes.md`
#
# Usage : bash scripts/portes.sh — tout vient des variables GRAPHATOM_*.

set -u

WT="${GRAPHATOM_WORKTREE:-}"
WS="${GRAPHATOM_WORKSPACE:-$PWD}"
MD=""            # portes.md, connu dès que le workspace l'est

# Les onze modules du paquet : une erreur de syntaxe n'importe où dans
# `src/` tombe ici, avant même qu'un test soit choisi.
MODULES="import graphatom.blocks, graphatom.channel, graphatom.cli, \
graphatom.db, graphatom.github_sync, graphatom.graph, graphatom.heartbeat, \
graphatom.kernel, graphatom.scheduler, graphatom.web, graphatom.worktree"

# Ce qui rend la suite concernée : le diff du candidat touche du code, un
# bundle, le schéma ou la déclaration du projet. Un diff qui n'a que de la
# documentation ne fait passer que la porte d'import.
CONCERNE="src/
tests/
examples/
scripts/
schema.sql
pyproject.toml"

# Les tests sans base, dans l'ordre du moins cher au plus cher. La règle
# d'entrée dans cette liste est mécanique : un test qui ouvre une connexion
# n'y est pas, parce que les K candidats partagent une base. Sont donc
# dehors crash, migration, escalade_timeout, passage, fanout, fanout_worktree,
# reconnect, opencode, silence, hermetic, plafond — qui compte les runs en vol
# de toute la base, donc ne se mesure que seul —, shell — qui demande une base
# depuis que `deploy` y pose le verrou de sa file — et fanout_opencode.
# `test_backend` les joue après la course, une fois seul.
TESTS="tests/validate_test.py
tests/fanout_config_test.py
tests/answer_test.py
tests/api_test.py
tests/checklist_test.py
tests/criteria_test.py
tests/depends_test.py
tests/heartbeat_test.py
tests/links_test.py
tests/live_test.py
tests/orphans_test.py
tests/timeout_test.py"

dit() {  # une ligne de journal, à l'écran et dans le compte rendu
    printf '%s\n' "$*"
    if [ -n "$MD" ]; then printf '%s\n' "$*" >> "$MD"; fi
}

lance() {  # exécute, journalise la commande et sa sortie, rend son code
    dit "\$ $*"
    SORTIE=$("$@" 2>&1)
    CODE=$?
    if [ -n "$SORTIE" ]; then dit "$SORTIE"; fi
    return $CODE
}

echoue() {  # dernier mot du script : la porte qui a lâché, puis son code
    dit "ÉCHEC — porte « $1 » → code $2"
    exit "$2"
}

# 0. l'atelier du candidat : sans lui, il n'y a rien à vérifier, et un
#    candidat qu'on ne peut pas vérifier n'est pas un candidat qui passe
if [ ! -d "$WS" ] || [ -z "$WT" ] || [ ! -d "$WT" ]; then
    echo "GRAPHATOM_WORKSPACE et GRAPHATOM_WORKTREE (deux répertoires) sont requis" >&2
    exit 2
fi
MD="$WS/portes.md"
: > "$MD"
dit "portes du candidat — atelier $WT"

unset GRAPHATOM_DSN GRAPHATOM_AGENT_DSN
export GRAPHATOM_REPO_DIR="$WT"
cd "$WT" || echoue "atelier" 2

# 1. le projet s'importe. C'est la porte de tous les diffs : un candidat qui
#    a cassé un import a cassé le rail entier, quoi qu'il ait touché.
DEBUT=$SECONDS
lance uv run python -c "$MODULES" || echoue "import" 3
dit "porte « import » passée en $((SECONDS - DEBUT)) s"

# 2. les tests concernés par le diff. Le diff est celui de tout l'atelier
#    face à `origin/main` — ce que le candidat a commité et ce qu'il n'a pas
#    encore commité —, lu comme `test_backend` et `test_frontend` le lisent.
DIFF=$({ git diff --name-only origin/main; git ls-files --others --exclude-standard; } 2>/dev/null)
if ! printf '%s\n' "$DIFF" | grep -qF "$CONCERNE"; then
    dit "diff sans code, bundle, schéma ni script — suite de tests non concernée"
    dit "PORTES OK — le candidat peut rendre son issue de succès"
    exit 0
fi

for TEST in $TESTS; do
    DEBUT=$SECONDS
    lance uv run python "$TEST" || echoue "$TEST" 3
    dit "porte « $TEST » passée en $((SECONDS - DEBUT)) s"
done

dit "PORTES OK — le candidat peut rendre son issue de succès"
