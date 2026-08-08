#!/usr/bin/env bash
#
# Retrait du worktree de l'item, de sa branche locale et de sa base jetable.
#
# Appelé par les trois sorties d'un cycle — `cleanup` (le merge a réussi),
# `cleanup_unresolved` (abandon, escalade ou expiration) et `cleanup_split`
# (l'issue a été découpée, le worktree n'a jamais servi) — avec le même
# shell : le retrait ne dépend pas de la raison qui y mène, seulement du
# sujet et de son atelier.
#
# Le script vérifie sa cible avant de détruire : le worktree doit être
# enregistré dans $GRAPHATOM_REPO_DIR, sous son `.worktrees/`, et porter la
# branche `rail/issue-<num>` du sujet de CET item. Une cible qui ne matche
# pas — un rail de test qui aurait hérité du REPO_DIR de production — n'est
# pas touchée, et cleanup.md le dit.
#
# Pas d'agent ici : un shell suffit, et il écrit toujours son outcome.json
# — c'est ce qui autorise le graph à faire passer toutes ses sorties par ce
# nœud sans jamais s'y coincer.
#
# Usage : scripts/cleanup.sh — tout vient des variables GRAPHATOM_*.

WT="$GRAPHATOM_REPO_DIR/.worktrees/rail-$(basename "$GRAPHATOM_WORKSPACE")"
NUM=$(printf '%s' "${GRAPHATOM_SUBJECT_KEY##*#}" | tr -cd '0-9')
BR=$(git -C "$GRAPHATOM_REPO_DIR" worktree list --porcelain 2>/dev/null | grep -FxA2 "worktree $WT" | sed -n 's|^branch refs/heads/||p')
MIEN=""
case "$GRAPHATOM_REPO_DIR" in /*) [ -n "$NUM" ] && [ "$BR" = "rail/issue-$NUM" ] && MIEN=1;; esac
{ echo "sujet ${GRAPHATOM_SUBJECT_KEY:-?} → worktree $WT (branche ${BR:-aucune})"
  if [ -n "$MIEN" ]; then
    git -C "$GRAPHATOM_REPO_DIR" worktree remove --force "$WT" && git -C "$GRAPHATOM_REPO_DIR" branch -D "$BR"
  else
    echo "cible étrangère : hors de $GRAPHATOM_REPO_DIR/.worktrees/, ou branche ${BR:-aucune} ≠ rail/issue-${NUM:-?} — on ne touche à rien"
  fi
  "$GRAPHATOM_REPO_DIR/.venv/bin/graphatom" drop-agent-db
} > cleanup.md 2>&1
if [ -n "$MIEN" ] && [ ! -d "$WT" ]; then S="worktree $WT et branche rail/issue-$NUM retirés"; else S="worktree $WT laissé en place — voir cleanup.md"; fi
printf '{"outcome": "done", "summary": "%s"}\n' "$S" > outcome.json
