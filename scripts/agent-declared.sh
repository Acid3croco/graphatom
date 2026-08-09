#!/usr/bin/env bash
# Lance l'adaptateur choisi par la configuration structurée du graph.

set -u

WT="${GRAPHATOM_WORKTREE:-.}"
case "${GRAPHATOM_AGENT_CLI:-}" in
    claude) bash "$WT/scripts/agent-claude.sh" ;;
    codex) bash "$WT/scripts/agent-codex.sh" ;;
    opencode) OPENCODE_DIR="${OPENCODE_DIR:-$WT}" \
        bash "$WT/scripts/agent-opencode.sh" ;;
    *)
        echo "agent-declared: CLI d'agent absente ou inconnue : ${GRAPHATOM_AGENT_CLI:-vide}" >&2
        exit 2
        ;;
esac
