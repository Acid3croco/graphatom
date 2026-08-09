#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [ ! -d "$ROOT/front/node_modules" ]; then
    npm --prefix "$ROOT/front" ci --prefer-offline --ignore-scripts
fi

NODE_PATH="$ROOT/front/node_modules" node "$ROOT/tests/front_run_trace_test.cjs"
