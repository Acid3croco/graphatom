/**
 * Le DOM réel du panneau de l'onglet Graphs, rendu sans navigateur.
 *
 * TypeScript compile le JSX comme pendant le build. React rend ensuite le
 * vrai composant `NodeConfig` en HTML statique : les assertions portent
 * sur les libellés que reçoit le DOM, pas sur une copie de leur logique.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const modules = path.join(root, "front", "node_modules");
process.env.NODE_PATH = [modules, process.env.NODE_PATH].filter(Boolean).join(path.delimiter);
Module._initPaths();

const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, isMain, options) {
  const actual = request.startsWith("@/")
    ? path.join(root, "front", request.slice(2))
    : request;
  return resolve.call(this, actual, parent, isMain, options);
};

const ts = require("typescript");
function typescript(module, filename) {
  const source = fs.readFileSync(filename, "utf8");
  const output = ts.transpileModule(source, {
    fileName: filename,
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
  });
  module._compile(output.outputText, filename);
}
require.extensions[".ts"] = typescript;
require.extensions[".tsx"] = typescript;

const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { NodeConfig } = require("../front/components/node-config.tsx");

function render(node, graphAgent) {
  return renderToStaticMarkup(
    React.createElement(NodeConfig, {
      name: "test",
      node,
      graphAgent,
      onClose() {},
    }),
  );
}

function executions(html) {
  return [...html.matchAll(/exécution<\/span> <b>([^<]+)<\/b>/g)].map(
    (match) => match[1],
  );
}

const declared = render(
  { block: "ACT", config: { agent: { model: "sonnet" } } },
  { cli: "claude", model: "opus", effort: "high" },
);
assert.deepEqual(executions(declared), ["claude · sonnet · high"]);

const shell = render(
  { block: "CHECK", config: { agent: { cmd: "printf explicite" } } },
  { cli: "codex", model: "gpt-5.6" },
);
assert.deepEqual(executions(shell), ["shell déterministe"]);

// Un bloc sans objet `agent` garde son exécuteur déterministe interne. Les
// défauts du graph ne l'activent pas implicitement.
const stub = render(
  { block: "ACT", config: {} },
  { cli: "claude", model: "sonnet" },
);
assert.deepEqual(executions(stub), ["sans exécution"]);

const none = render({ terminal: true }, { cli: "codex", model: "gpt-5.6" });
assert.deepEqual(executions(none), ["sans exécution"]);

const fanout = render(
  {
    block: "ACT",
    config: {
      agent: { model: "gpt-nœud" },
      fanout: {
        variants: [
          { label: "héritée", agent: {} },
          { label: "cli", agent: { cli: "claude" } },
          { label: "modèle", agent: { model: "gpt-variante" } },
          { label: "shell", agent: { cmd: "printf variante" } },
        ],
        reduce: "first_pass",
      },
    },
  },
  { cli: "codex", model: "gpt-graph" },
);
assert.deepEqual(executions(fanout), [
  "codex · gpt-nœud",
  "claude · gpt-nœud",
  "codex · gpt-variante",
  "shell déterministe",
]);

// Le vrai graph d'exemple migré doit garder les libellés visibles de sa
// version antérieure : son agent `work`, puis ses deux sorties terminales.
const migrated = JSON.parse(
  fs.readFileSync(path.join(root, "examples", "executor.json"), "utf8"),
);
const apresMigration = Object.values(migrated.nodes).flatMap((node) =>
  executions(render(node, migrated.agent)),
);
const avantMigration = ["claude · sonnet", "sans exécution", "sans exécution"];
assert.deepEqual(apresMigration, avantMigration);

// Le vrai graph de production a un wrapper shell, mais ce wrapper lance
// bien chaque exécuteur déclaré. Les trois candidats doivent rester visibles.
const codeTask = JSON.parse(
  fs.readFileSync(path.join(root, "examples", "code-task.json"), "utf8"),
);
const implement = executions(
  render(codeTask.nodes.implement, codeTask.agent),
);
assert.deepEqual(implement, [
  "codex · gpt-5.6-luna · medium + shell",
  "codex · gpt-5.6-sol · high + shell",
  "opencode · deepseek-v4-flash-free + shell",
]);
assert.equal(implement.includes("shell déterministe"), false);

console.log(
  "front graphs DOM : modèle, shell, absence, fan-out et rendu migré ✓",
);
