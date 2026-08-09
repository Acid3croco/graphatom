import type { RunTrace } from "@/lib/api";

export type TraceContent = {
  events: string;
  log: string;
  command: string;
};

export const emptyTraceContent = (): TraceContent => ({
  events: "",
  log: "",
  command: "",
});

type Timer = ReturnType<typeof setTimeout>;

type PollerOptions = {
  item: number;
  run: number;
  running: boolean;
  fetchTrace: (url: string) => Promise<RunTrace>;
  onData: (trace: RunTrace, content: TraceContent) => void;
  onError: (error: string) => void;
  delay?: number;
  schedule?: (load: () => void, delay: number) => Timer;
  cancel?: (timer: Timer) => void;
};

/**
 * A small state machine for one trace viewer.
 *
 * It owns its cursor and accumulated content. A failed request does not lose
 * either one. An active run retries after the normal delay. A terminal answer
 * ends the loop.
 */
export function createTracePoller(options: PollerOptions) {
  let cursor: RunTrace["cursor"] | undefined;
  let content = emptyTraceContent();
  let running = options.running;
  let stopped = false;
  let timer: Timer | undefined;
  const delay = options.delay ?? 2000;
  const schedule = options.schedule ?? setTimeout;
  const cancel = options.cancel ?? clearTimeout;

  const queue = () => {
    if (running && !stopped) {
      timer = schedule(() => void load(), delay);
    }
  };

  async function load() {
    const search = cursor
      ? `?cursor=${encodeURIComponent(JSON.stringify(cursor))}`
      : "";
    try {
      const next = await options.fetchTrace(
        `/api/item/${options.item}/run/${options.run}/trace${search}`,
      );
      if (stopped) {
        return;
      }
      content = {
        events: content.events + next.events.content,
        log: content.log + next.log.content,
        command: content.command + next.command.content,
      };
      cursor = next.cursor;
      running = next.status === "running";
      options.onData(next, content);
      queue();
    } catch (error) {
      if (stopped) {
        return;
      }
      options.onError(String(error));
      queue();
    }
  }

  return {
    load,
    stop() {
      stopped = true;
      if (timer !== undefined) {
        cancel(timer);
      }
    },
  };
}
