#!/usr/bin/env bash
#
# Prépare l'environnement front d'un worktree : dépendances, puis build.
#
# C'est le chemin rapide du test frontend. Chaque worktree d'item repart
# d'un `front/` sans `node_modules` ni `.next` — les deux sont ignorés par
# git, rien ne les transporte. Ce qui se partage entre worktrees, c'est le
# cache npm : une fois les paquets tirés du registre, `npm ci
# --prefer-offline` les relie depuis ce cache sans ressortir sur le réseau.
# D'où le cache épinglé ici sur un chemin stable de l'hôte, au lieu de
# dépendre de ce que l'environnement de l'agent traîne comme HOME.
#
# Mesuré sur ce dépôt, cache chaud : ~4 s d'install, ~10 s de build. Le
# premier amorçage — cache vide — paie les téléchargements, une fois.
#
# Le script ne démarre rien : `npm run start` reste à l'appelant, lui seul
# connaît son port et son GRAPHATOM_API_URL.
#
# Usage : scripts/front-env.sh — depuis n'importe quel cwd, le script
# travaille sur le `front/` de son propre worktree.
#
#   2  front/ introuvable
#   3  install refusée
#   4  build refusé

set -u

ROOT="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)"
FRONT="$ROOT/front"
[ -n "$FRONT" ] || { echo "front-env: front/ introuvable depuis $0" >&2; exit 2; }

# L'installation et le build forment une seule dépense. Le second passage
# est lancé par le fournisseur de quota et ne tente pas de reprendre une
# autre place.
if [ "${1:-}" != "--sous-quota" ]; then
    exec uv run --project "$ROOT" graphatom build-quota -- "$0" --sous-quota
fi

export npm_config_cache="${GRAPHATOM_NPM_CACHE:-$HOME/.npm}"
export NEXT_TELEMETRY_DISABLED=1
mkdir -p "$npm_config_cache" || exit 2

cd "$FRONT" || exit 2
DEBUT=$SECONDS

echo "front-env: $FRONT — cache npm $npm_config_cache"
npm ci --prefer-offline --no-audit --no-fund || exit 3
npm run build || exit 4

echo "front-env: environnement prêt en $((SECONDS - DEBUT)) s"
