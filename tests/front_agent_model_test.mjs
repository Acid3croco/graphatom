/**
 * Contrat du front pour l'exécuteur structuré d'un nœud.
 *
 * Usage : node --experimental-strip-types tests/front_agent_model_test.mjs
 */
import assert from "node:assert/strict";

import {
  execution,
  executionLabel,
} from "../front/lib/agent-model.ts";

const graph = { cli: "codex", model: "gpt-par-defaut", effort: "high" };

assert.deepEqual(execution(graph), {
  kind: "model",
  cli: "codex",
  model: "gpt-par-defaut",
  effort: "high",
});
assert.deepEqual(execution(graph, { cli: "claude" }), {
  kind: "model",
  cli: "claude",
  model: "gpt-par-defaut",
  effort: "high",
});
assert.deepEqual(execution(graph, { model: "sonnet" }), {
  kind: "model",
  cli: "codex",
  model: "sonnet",
  effort: "high",
});
assert.deepEqual(
  execution(graph, { model: "sonnet" }, {
    cli: "opencode",
    model: "opencode/deepseek-v4-flash-free",
  }),
  { kind: "model", cli: "opencode", model: "deepseek-v4-flash-free", effort: "high" },
);
assert.deepEqual(
  execution(graph, { model: "sonnet" }, { model: "deepseek-v4" }),
  { kind: "model", cli: "codex", model: "deepseek-v4", effort: "high" },
);

assert.deepEqual(execution(graph, { cmd: "printf explicite" }), {
  kind: "shell",
});
assert.deepEqual(
  execution(graph, { cli: "claude" }, { cmd: "printf variante" }),
  { kind: "shell" },
);
assert.deepEqual(
  execution(graph, {
    cmd: "bash agent-declared.sh",
    cmd_uses_executor: true,
    effort: "medium",
  }),
  {
    kind: "composed",
    cli: "codex",
    model: "gpt-par-defaut",
    effort: "medium",
  },
);
assert.deepEqual(execution(undefined, undefined, undefined), { kind: "none" });
assert.deepEqual(execution({ cli: "claude" }), {
  kind: "model",
  cli: "claude",
  model: "défaut",
  effort: null,
});

assert.equal(
  executionLabel(execution(graph, {}, { model: "gpt-variante" })),
  "codex · gpt-variante · high",
);
assert.equal(
  executionLabel(execution(graph, {
    cmd: "bash agent-declared.sh",
    cmd_uses_executor: true,
    effort: "medium",
  })),
  "codex · gpt-par-defaut · medium + shell",
);
assert.equal(executionLabel(execution(undefined)), "sans exécution");

console.log(
  "front agent model : héritage graph/nœud/variante, shell et absence ✓",
);
