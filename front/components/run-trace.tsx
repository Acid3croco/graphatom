"use client";

import { useEffect, useState } from "react";

import type { Run, RunTrace } from "@/lib/api";
import {
  createTracePoller,
  emptyTraceContent,
  type TraceContent,
} from "@/lib/run-trace";

const sourceState = (value: "missing" | "empty" | "available") =>
  value === "missing" ? "source absente" : "source vide";

/** The three read-only trace sources, with explicit empty and error states. */
export function TracePanels({
  trace,
  content,
  error,
}: {
  trace: RunTrace | null;
  content: TraceContent;
  error: string | null;
}) {
  if (!trace) {
    return error ? (
      <p role="alert">erreur de lecture : {error}</p>
    ) : (
      <p className="text-muted-foreground">chargement de la trace…</p>
    );
  }

  const events = content.events
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => {
      try {
        return JSON.stringify(JSON.parse(line), null, 2);
      } catch {
        return line;
      }
    });

  return (
    <div className="grid gap-4 text-xs md:grid-cols-3">
      {error && <p role="alert">erreur de lecture : {error}</p>}
      <section>
        <h3 className="mb-1 font-semibold">
          événements JSONL structurés{trace.events.type ? ` · ${trace.events.type}` : ""}
        </h3>
        {trace.events.state === "available" ? (
          events.map((event, index) => (
            <pre key={index} className="mb-2 overflow-auto rounded-md bg-muted p-2">
              {event}
            </pre>
          ))
        ) : (
          <p className="text-muted-foreground">{sourceState(trace.events.state)}</p>
        )}
      </section>
      <section>
        <h3 className="mb-1 font-semibold">journal texte du wrapper</h3>
        {trace.log.state === "available" ? (
          <pre className="max-h-96 overflow-auto rounded-md bg-muted p-2 whitespace-pre-wrap">
            {content.log}
          </pre>
        ) : (
          <p className="text-muted-foreground">{sourceState(trace.log.state)}</p>
        )}
      </section>
      <section>
        <h3 className="mb-1 font-semibold">commande effective</h3>
        {trace.command.state === "available" ? (
          <pre className="max-h-96 overflow-auto rounded-md bg-muted p-2 whitespace-pre-wrap">
            {content.command}
          </pre>
        ) : (
          <p className="text-muted-foreground">{sourceState(trace.command.state)}</p>
        )}
      </section>
    </div>
  );
}

/** A trace viewer with an independent cursor and retry loop for one run. */
export function TraceViewer({ item, run }: { item: number; run: Run }) {
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [content, setContent] = useState(emptyTraceContent);
  const [error, setError] = useState<string | null>(null);
  const [runningAtOpen] = useState(run.status === "running");

  useEffect(() => {
    setTrace(null);
    setContent(emptyTraceContent());
    setError(null);
    const poller = createTracePoller({
      item,
      run: run.id,
      running: runningAtOpen,
      async fetchTrace(url) {
        const response = await fetch(url, { cache: "no-store" });
        if (!response.ok) {
          throw new Error(`l'API a répondu ${response.status}`);
        }
        return (await response.json()) as RunTrace;
      },
      onData(next, accumulated) {
        setTrace(next);
        setContent(accumulated);
        setError(null);
      },
      onError: setError,
    });
    void poller.load();
    return poller.stop;
  }, [item, run.id, runningAtOpen]);

  return (
    <div data-run-trace={run.id}>
      <TracePanels trace={trace} content={content} error={error} />
    </div>
  );
}
