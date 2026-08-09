#!/usr/bin/env bash
# Le composant réel, avec les dépendances exactes du front déjà installées.
# Usage : bash tests/front_graph_dom_test.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)

node --no-warnings --experimental-strip-types \
    "$ROOT/tests/front_agent_model_test.mjs"

# L'ancienne reconnaissance de CLI et de modèle ne doit pas revenir ailleurs
# dans le front sous un autre nom.
if rg -n 'CALLS_CLAUDE|const CLIS|new RegExp|agent-opencode\\\.sh|--model\\\[' \
        "$ROOT/front" --glob '*.{ts,tsx}'; then
    echo "ÉCHEC : reconnaissance de l'exécuteur dans une commande shell" >&2
    exit 1
fi

if [ ! -d "$ROOT/front/node_modules" ]; then
    npm --prefix "$ROOT/front" ci --offline --omit=optional --ignore-scripts
fi

NODE_PATH="$ROOT/front/node_modules" \
    node "$ROOT/tests/front_graph_dom_test.cjs"
