#!/usr/bin/env bash
#
# Adaptateur codex : un fournisseur à part entière, dans le même contrat que
# l'adaptateur opencode.
#
# Le contrat d'un bloc agent est minuscule et agnostique — `prompt.md` en
# entrée, `outcome.json` en sortie, `usage.json` si on veut. C'est ce qui
# fait du rail autre chose qu'un seul fournisseur : quand un nœud appelle
# `codex exec`, c'est la CLI qui rend la réponse du modèle, et le bloc n'a
# rien à savoir de codex. Ce script est le pont entre les deux formes : ce
# que `codex exec` rend, et ce que le workspace attend.
#
# Déterministe, sans jugement : le script ne choisit jamais une issue. Si le
# modèle a écrit `outcome.json`, il n'y touche pas ; si le modèle n'a rien
# écrit, le script n'invente rien et le noyau classe la tentative `crashed`,
# comme d'habitude.
#
# Un modèle qui ne rend rien est un échec net, pas un blocage : l'attente
# est bornée, et le dépassement nomme le modèle fautif. La borne est celle
# du script, plus courte que le `timeout_s` du nœud — c'est l'adaptateur
# qui doit parler du modèle, pas le chien de garde du bloc.
#
# Aucun secret dans le dépôt : `codex` s'authentifie par sa propre session,
# comme `claude`. La consommation, quand `codex exec` la rapporte en fin de
# course, part telle quelle dans `usage.json` ; sinon rien, ce que le
# contrat autorise explicitement.
#
# Usage : scripts/agent-codex.sh
#
#   CODEX_MODEL         le modèle à passer à codex (défaut : celui que la
#                       session codex lit dans sa configuration)
#   CODEX_REASONING_EFFORT l'effort à passer à codex (`low`, `medium`,
#                       `high`...) ; absent, la configuration de la session
#                       reste l'autorité
#   CODEX_TIMEOUT_S     la borne d'attente, en secondes (défaut 300)
#   CODEX_BIN           le binaire codex, quand le PATH ne suffit pas
#   CODEX_DIR           le répertoire de travail du modèle (défaut : l'atelier
#                       du candidat, `GRAPHATOM_WORKTREE`, ou le workspace)
#
# Le script travaille dans le répertoire courant, sans jamais en changer :
# le bloc lance déjà son `cmd` depuis le workspace de l'item, et c'est là
# que `prompt.md` attend comme `outcome.json` est relu. Le modèle, lui,
# travaille dans son atelier — souvent un autre répertoire que le workspace —
# ; ce qu'il y écrit est rapatrié à la fin, pour que le bloc le lise.
#
#   2  pas de prompt.md dans le répertoire courant : rien à donner au modèle
#   3  codex introuvable
#   4  borne d'attente dépassée : le modèle n'a rien rendu
#   5  codex a échoué, et aucune issue n'a été rendue
#   6  codex a fini sans rendre la moindre issue

set -u

MODELE="${CODEX_MODEL:-}"
EFFORT="${CODEX_REASONING_EFFORT:-}"
BORNE="${CODEX_TIMEOUT_S:-300}"
CX="${CODEX_BIN:-codex}"
DIR="${CODEX_DIR:-${GRAPHATOM_WORKTREE:-$PWD}}"
LOG="codex.jsonl"   # le flux d'événements de codex, dans le workspace

[ -f prompt.md ] || {
    echo "agent-codex: pas de prompt.md dans $PWD — rien à donner au modèle" >&2
    exit 2
}
command -v "$CX" > /dev/null 2>&1 || {
    echo "agent-codex: commande « $CX » introuvable dans le PATH." >&2
    echo "agent-codex: installe codex (npm i -g @openai/codex), ou pose" >&2
    echo "agent-codex: CODEX_BIN sur le chemin du binaire." >&2
    exit 3
}

# le modèle employé, nommé dans les messages d'échec : celui qu'on dit,
# celui qu'on passe — la session ne peut pas rendre un modèle qu'on ne lui
# a pas demandé. Sans configuration, codex use son défaut et le script le
# dit tel quel.
if [ -z "$MODELE" ]; then
    MODELE=$(sed -n 's/^model *= *"\([^"]*\)"/\1/p' \
        "${CODEX_HOME:-$HOME/.codex}/config.toml" 2>/dev/null | head -1)
fi

usage() {  # la consommation, telle que codex la rapporte : personne ne l'interprète
    jq -es '[.[] | select(.type == "turn.completed")] | .[-1] | .usage' \
        "$LOG" > usage.json 2>/dev/null || rm -f usage.json
    [ -s usage.json ] || rm -f usage.json  # pas de quoi le remplir : rien du tout
}

echo "agent-codex: modèle ${MODELE:-défaut de la session} — effort ${EFFORT:-défaut de la session} — répertoire $DIR — borne ${BORNE} s"
# `--dangerously-bypass-approvals-and-sandbox` n'est pas un détail : sans
# lui, codex attend une approbation humaine avant d'écrire un fichier, et
# un bloc de rail n'a personne pour la donner. Le prix est connu et assumé
# — l'agent tourne déjà dans l'atelier de son item et sur sa base jetable,
# jamais sur la production.
PARAMETRES=(exec --json --dangerously-bypass-approvals-and-sandbox)
if [ -n "$MODELE" ]; then PARAMETRES+=(-m "$MODELE"); fi
if [ -n "$EFFORT" ]; then
    PARAMETRES+=(-c "model_reasoning_effort=\"$EFFORT\"")
fi
PARAMETRES+=(-C "$DIR" "$(cat prompt.md)")
timeout -k 5 "$BORNE" "$CX" "${PARAMETRES[@]}" < /dev/null > "$LOG"
RC=$?

jq -rse 'map(select(.type=="item.completed" and .item.type=="agent_message") | .item.text)[]' \
    "$LOG" 2>/dev/null   # le texte du modèle part dans le journal de la tentative
usage

# le modèle écrit dans son répertoire de travail ; le bloc relit le
# workspace. Deux répertoires distincts, deux chemins : on rapatrie ce que
# l'agent a rendu, sans le dédoubler quand il travaille déjà au même endroit.
if [ "$DIR" != "$PWD" ]; then
    [ -f "$DIR/outcome.json" ] && mv "$DIR/outcome.json" outcome.json 2>/dev/null
    [ -f "$DIR/usage.json" ] && mv "$DIR/usage.json" usage.json 2>/dev/null
fi

case $RC in 124 | 137)
    echo "agent-codex: le modèle « ${MODELE:-défaut de la session} » n'a rien" >&2
    echo "agent-codex: rendu en ${BORNE} s — borne d'attente dépassée, tentative" >&2
    echo "agent-codex: abandonnée." >&2
    exit 4
    ;;
esac

if [ ! -f outcome.json ]; then
    if [ "$RC" != 0 ]; then
        echo "agent-codex: codex a échoué (code $RC) avec" >&2
        echo "agent-codex: « ${MODELE:-défaut de la session} », et aucune issue" >&2
        echo "agent-codex: n'a été rendue." >&2
        exit 5
    fi
    echo "agent-codex: le modèle « ${MODELE:-défaut de la session} » a fini" >&2
    echo "agent-codex: sans écrire outcome.json — tentative sans issue." >&2
    exit 6
fi

echo "agent-codex: issue rendue — $(cat outcome.json)"
