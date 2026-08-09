/** Polling and DOM contracts for the read-only run trace viewer. */
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
const { TracePanels, TraceViewer } = require("../front/components/run-trace.tsx");
const {
  createTracePoller,
  emptyTraceContent,
} = require("../front/lib/run-trace.ts");

function source(state, content = "", type = null) {
  return {
    type,
    state,
    content,
    offset: 0,
    next_offset: Buffer.byteLength(content),
    media_type: "text/plain",
  };
}

function trace(run, status, contents, cursor) {
  return {
    item_id: 9,
    run_id: run,
    status,
    events: source(contents.events ? "available" : "missing", contents.events, "codex"),
    log: source(contents.log === undefined ? "empty" : "available", contents.log || "", "log"),
    command: source(contents.command ? "available" : "missing", contents.command, "command"),
    cursor,
  };
}

function harness(responses, running = true) {
  const urls = [];
  const data = [];
  const errors = [];
  const scheduled = [];
  const poller = createTracePoller({
    item: 9,
    run: responses[0].run,
    running,
    async fetchTrace(url) {
      urls.push(url);
      const answer = responses.shift();
      if (answer.error) throw new Error(answer.error);
      return answer.trace;
    },
    onData(value, content) {
      data.push({ value, content });
    },
    onError(error) {
      errors.push(error);
    },
    schedule(load) {
      scheduled.push(load);
      return load;
    },
    cancel() {},
  });
  return { poller, urls, data, errors, scheduled };
}

async function main() {
  const first = trace(20, "running", { events: '{"step":1}\n', log: "a" }, {
    events: 11, log: 1, command: 0,
  });
  const second = trace(20, "applied", { events: '{"step":2}\n', log: "b" }, {
    events: 22, log: 2, command: 0,
  });
  const incremental = harness([{ run: 20, trace: first }, { run: 20, trace: second }]);
  await incremental.poller.load();
  assert.equal(incremental.scheduled.length, 1);
  await incremental.scheduled.shift()();
  assert.equal(incremental.urls[0], "/api/item/9/run/20/trace");
  assert.match(incremental.urls[1], /cursor=/);
  assert.deepEqual(incremental.data[1].content, {
    events: '{"step":1}\n{"step":2}\n', log: "ab", command: "",
  });
  assert.equal(incremental.scheduled.length, 0, "un run terminal ne se resonde pas");

  const retry = harness([
    { run: 21, error: "réseau temporaire" },
    { run: 21, trace: trace(21, "applied", { log: "revenu" }, { events: 0, log: 6, command: 0 }) },
  ]);
  await retry.poller.load();
  assert.match(retry.errors[0], /réseau temporaire/);
  assert.equal(retry.scheduled.length, 1, "une erreur transitoire se retente");
  await retry.scheduled.shift()();
  assert.equal(retry.data[0].content.log, "revenu");
  assert.equal(retry.scheduled.length, 0);

  const viewers = [30, 31, 32].map((run) =>
    harness([{ run, trace: trace(run, "applied", { log: `run-${run}` }, { events: 0, log: 6, command: 0 }) }], false),
  );
  await Promise.all(viewers.map((viewer) => viewer.poller.load()));
  assert.deepEqual(
    viewers.map((viewer) => viewer.urls[0]),
    [
      "/api/item/9/run/30/trace",
      "/api/item/9/run/31/trace",
      "/api/item/9/run/32/trace",
    ],
  );
  assert.deepEqual(viewers.map((viewer) => viewer.data[0].content.log), ["run-30", "run-31", "run-32"]);

  const viewerDom = renderToStaticMarkup(
    React.createElement(
      React.Fragment,
      null,
      ...[
        { id: 30, candidate: null, variant: null },
        { id: 31, candidate: 0, variant: "codex" },
        { id: 32, candidate: 1, variant: "opencode" },
      ].map((run) =>
        React.createElement(TraceViewer, {
          key: run.id,
          item: 9,
          run: { ...run, status: "running" },
        }),
      ),
    ),
  );
  assert.deepEqual(
    [...viewerDom.matchAll(/data-run-trace="(\d+)"/g)].map((match) => Number(match[1])),
    [30, 31, 32],
  );

  const states = {
    ...trace(40, "applied", { events: "", command: '{"kind":"model"}' }, { events: 0, log: 0, command: 16 }),
    events: source("missing", "", null),
    log: source("empty", "", "log"),
  };
  const html = renderToStaticMarkup(
    React.createElement(TracePanels, {
      trace: states,
      content: { events: "", log: "", command: '{"kind":"model"}' },
      error: "lecture temporaire",
    }),
  );
  assert.match(html, /événements JSONL structurés/);
  assert.match(html, /journal texte du wrapper/);
  assert.match(html, /commande effective/);
  assert.match(html, /source absente/);
  assert.match(html, /source vide/);
  assert.match(html, /erreur de lecture/);
  assert.match(html, /kind/);

  const errorOnly = renderToStaticMarkup(
    React.createElement(TracePanels, {
      trace: null,
      content: emptyTraceContent(),
      error: "API indisponible",
    }),
  );
  assert.match(errorOnly, /role="alert"/);
  assert.match(errorOnly, /API indisponible/);

  console.log("front run trace: viewers, cursor, retry, terminal stop and DOM states ✓");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
