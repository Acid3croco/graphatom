#!/usr/bin/env bash
# Adaptateur Claude : prompt.md en entrée, outcome.json et usage.json en sortie.

set -u

MODELE="${CLAUDE_MODEL:-}"
BORNE="${CLAUDE_TIMEOUT_S:-300}"
CL="${CLAUDE_BIN:-claude}"
DIR="${CLAUDE_DIR:-${GRAPHATOM_WORKTREE:-$PWD}}"
REPONSE="claude.json"

[ -f prompt.md ] || {
    echo "agent-claude: pas de prompt.md dans $PWD — rien à donner au modèle" >&2
    exit 2
}
command -v "$CL" > /dev/null 2>&1 || {
    echo "agent-claude: commande « $CL » introuvable dans le PATH." >&2
    exit 3
}

PARAMETRES=(--dangerously-skip-permissions --output-format json)
if [ -n "$MODELE" ]; then PARAMETRES+=(--model "$MODELE"); fi
PARAMETRES+=(-p "$(cat prompt.md)")

echo "agent-claude: modèle ${MODELE:-défaut de la session} — répertoire $DIR — borne ${BORNE} s"
(cd "$DIR" && timeout -k 5 "$BORNE" "$CL" "${PARAMETRES[@]}") > "$REPONSE"
RC=$?

jq -r '.result // empty' "$REPONSE" 2>/dev/null
jq -e 'select((.usage // {}) | length > 0) |
    .usage + (if .total_cost_usd == null then {} else {total_cost_usd: .total_cost_usd} end)' \
    "$REPONSE" > usage.json 2>/dev/null || rm -f usage.json

if [ "$DIR" != "$PWD" ]; then
    [ -f "$DIR/outcome.json" ] && mv "$DIR/outcome.json" outcome.json 2>/dev/null
    [ -f "$DIR/usage.json" ] && mv "$DIR/usage.json" usage.json 2>/dev/null
fi

case $RC in 124 | 137)
    echo "agent-claude: le modèle « ${MODELE:-défaut de la session} » n'a rien rendu en ${BORNE} s" >&2
    exit 4
    ;;
esac

if [ ! -f outcome.json ]; then
    if [ "$RC" != 0 ]; then
        echo "agent-claude: claude a échoué (code $RC), sans rendre d'issue" >&2
        exit 5
    fi
    echo "agent-claude: le modèle a fini sans écrire outcome.json" >&2
    exit 6
fi

echo "agent-claude: issue rendue — $(cat outcome.json)"
