#!/usr/bin/env bash
#
# Adaptateur opencode : un candidat gratuit qui respecte le contrat de bloc.
#
# Le contrat d'un bloc agent est minuscule et agnostique — `prompt.md` en
# entrée, `outcome.json` en sortie, `usage.json` si on veut. Un adaptateur
# de CLI n'a donc rien d'autre à faire que le pont entre deux formes : ce
# que `opencode run` rend sur sa sortie, et ce que le workspace attend.
#
# Déterministe, sans jugement : le script ne choisit jamais une issue. Si
# le modèle a écrit `outcome.json` lui-même, il n'y touche pas ; si le
# modèle a dicté son issue dans son texte au lieu de l'écrire, le script
# la recopie telle quelle ; si le modèle n'a rien dit, le script n'invente
# rien et le noyau classe la tentative `crashed`, comme d'habitude.
#
# Un modèle qui ne rend rien est un échec net, pas un blocage : l'attente
# est bornée, et le dépassement nomme le modèle fautif. La borne est celle
# du script, plus courte que le `timeout_s` du nœud — c'est l'adaptateur
# qui doit parler du modèle, pas le chien de garde du bloc.
#
# Les modèles visés sont les variantes gratuites d'opencode, sans aucun
# identifiant : rien à configurer, rien à mettre dans le dépôt. Seul
# `deepseek-v4-flash-free` a un fonctionnement établi, écriture de fichier
# comprise — d'où le défaut. `north-mini-code-free`, lui, reste muet.
#
# Usage : scripts/agent-opencode.sh [provider/model]
#
#   OPENCODE_MODEL      le modèle, quand l'argument est absent
#   OPENCODE_TIMEOUT_S  la borne d'attente, en secondes (défaut 300)
#   OPENCODE_BIN        le binaire opencode, quand le PATH ne suffit pas
#   OPENCODE_DIR        le répertoire de travail du modèle (défaut : le workspace)
#   OPENCODE_STATE_DIR  le répertoire de sa base locale (défaut : propre au run)
#
# Le script travaille dans le répertoire courant, sans jamais en changer :
# le bloc lance déjà son `cmd` depuis le workspace de l'item, et c'est là
# que `prompt.md` attend comme `outcome.json` est relu.
#
#   2  pas de prompt.md dans le répertoire courant : rien à donner au modèle
#   3  opencode introuvable
#   4  borne d'attente dépassée : le modèle n'a rien rendu
#   5  opencode a échoué, et aucune issue n'a été rendue
#   6  opencode a fini sans rendre la moindre issue
#   7  le répertoire de la base locale n'a pas pu être créé

set -u

MODELE="${1:-${OPENCODE_MODEL:-opencode/deepseek-v4-flash-free}}"
BORNE="${OPENCODE_TIMEOUT_S:-300}"
OC="${OPENCODE_BIN:-opencode}"
DIR="${OPENCODE_DIR:-$PWD}"
EVENTS="opencode-events.jsonl"   # la sortie brute d'opencode, dans le workspace

[ -f prompt.md ] || {
    echo "agent-opencode: pas de prompt.md dans $PWD — rien à donner au modèle" >&2
    exit 2
}
command -v "$OC" > /dev/null 2>&1 || {
    echo "agent-opencode: commande « $OC » introuvable dans le PATH." >&2
    echo "agent-opencode: installe opencode (npm i -g opencode-ai), ou pose" >&2
    echo "agent-opencode: OPENCODE_BIN sur le chemin du binaire." >&2
    exit 3
}

# OpenCode garde ses sessions dans une base SQLite globale. Deux candidats
# parallèles peuvent alors écrire dans le même fichier et l'un d'eux meurt
# avec `database is locked`. Le workspace d'un run est déjà isolé par le
# noyau et persiste pendant ses reprises : sa sous-arborescence est la bonne
# durée de vie pour cette base. Seule la base change de place ; config,
# identifiants et cache restent ceux de la session OpenCode de l'hôte.
ETAT="${OPENCODE_STATE_DIR:-${GRAPHATOM_WORKSPACE:-$PWD}/.opencode}"
mkdir -p "$ETAT" || {
    echo "agent-opencode: impossible de créer l'état local dans $ETAT" >&2
    exit 7
}
ETAT=$(cd "$ETAT" && pwd -P) || {
    echo "agent-opencode: impossible de résoudre l'état local dans $ETAT" >&2
    exit 7
}
export OPENCODE_DB="$ETAT/opencode.db"

texte() {  # le texte du modèle, extrait du flux d'événements d'opencode
    jq -r 'select(.type == "text") | .part.text' "$EVENTS" 2>/dev/null
}

usage() {  # la consommation, telle qu'opencode la rapporte : personne ne l'interprète
    jq -se '[.[] | select(.type == "step_finish") | .part] | select(length > 0) | {
        input_tokens: (map(.tokens.input // 0) | add),
        output_tokens: (map(.tokens.output // 0) | add),
        reasoning_tokens: (map(.tokens.reasoning // 0) | add),
        cache_read_tokens: (map(.tokens.cache.read // 0) | add),
        cache_write_tokens: (map(.tokens.cache.write // 0) | add),
        total_tokens: (map(.tokens.total // 0) | add),
        total_cost_usd: (map(.cost // 0) | add),
        model: $m,
    }' --arg m "$MODELE" "$EVENTS" > usage.json 2>/dev/null || rm -f usage.json
    [ -s usage.json ] || rm -f usage.json  # pas de quoi le remplir : rien du tout
}

recopie() {  # l'issue dictée dans le texte, quand le modèle a parlé au lieu d'écrire
    [ -f outcome.json ] && return 0
    CANDIDAT=$(texte | tr '\n' ' ' | grep -o '{[^{}]*"outcome"[^{}]*}' | tail -1)
    [ -n "$CANDIDAT" ] || return 1
    printf '%s\n' "$CANDIDAT" \
        | jq -e 'select((.outcome | type) == "string")' > outcome.json 2>/dev/null \
        || { rm -f outcome.json; return 1; }
    echo "agent-opencode: issue recopiée du texte du modèle vers outcome.json"
}

# `--auto` n'est pas un détail : sans lui, opencode attend une approbation
# humaine avant d'écrire un fichier, et un bloc de rail n'a personne pour
# la donner. Le prix est connu et assumé — l'agent tourne déjà dans le
# worktree de son item et sur sa base jetable, jamais sur la production.
echo "agent-opencode: modèle $MODELE — répertoire $DIR — base $OPENCODE_DB — borne ${BORNE} s"
timeout -k 5 "$BORNE" \
    "$OC" run -m "$MODELE" --format json --auto --dir "$DIR" "$(cat prompt.md)" > "$EVENTS"
RC=$?

texte || cat "$EVENTS"   # le texte du modèle part dans le journal de la tentative
usage

case $RC in 124 | 137)
    echo "agent-opencode: le modèle « $MODELE » n'a rien rendu en ${BORNE} s —" >&2
    echo "agent-opencode: borne d'attente dépassée, tentative abandonnée." >&2
    exit 4
    ;;
esac

recopie
if [ ! -f outcome.json ]; then
    if [ "$RC" != 0 ]; then
        echo "agent-opencode: opencode a échoué (code $RC) avec « $MODELE »," >&2
        echo "agent-opencode: et aucune issue n'a été rendue." >&2
        exit 5
    fi
    echo "agent-opencode: le modèle « $MODELE » a fini sans écrire outcome.json" >&2
    echo "agent-opencode: ni dicter d'issue dans son texte — tentative sans issue." >&2
    exit 6
fi

echo "agent-opencode: issue rendue — $(cat outcome.json)"
