#!/usr/bin/env bash
#
# Voie rapide du nœud release : le script déterministe passe avant le modèle.
# Le modèle n'est utile que si `release.sh` nomme une panne à interpréter ou
# un conflit à résoudre. Une release nominale ne consomme donc aucun tour.
#
# Usage : scripts/release-node.sh — tout vient des variables GRAPHATOM_*.

set -u

WT="${GRAPHATOM_WORKTREE:-}"
if [ -z "$WT" ] || [ ! -d "$WT" ]; then
    printf '%s\n' '{"outcome":"conflict","summary":"release : GRAPHATOM_WORKTREE absent ou invalide"}' > outcome.json
    exit 0
fi

if bash "$WT/scripts/release.sh"; then
    printf '%s\n' '{"outcome":"done","summary":"release.sh a suffi : branche publiée, PR mergée et SHA observé"}' > outcome.json
    exit 0
fi

CODEX_MODEL="${CODEX_MODEL:-gpt-5.6-luna}" \
CODEX_REASONING_EFFORT="${CODEX_REASONING_EFFORT:-low}" \
CODEX_TIMEOUT_S="${CODEX_TIMEOUT_S:-540}" \
bash "$WT/scripts/agent-codex.sh"
