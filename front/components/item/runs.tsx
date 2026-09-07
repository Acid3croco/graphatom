"use client";

/** Les runs : leur statut, leur durée, leurs tokens, leur résultat. */
import { useState } from "react";

import { useItem } from "@/lib/live";
import type { Run } from "@/lib/api";
import { duration, tokens } from "@/lib/format";
import { toggled } from "@/lib/utils";
import { Badge, tone } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TraceViewer } from "@/components/run-trace";

// jsonb ne garde pas l'ordre des clés : le post-mortem d'abord, la trace après
const RESULT_ORDER = ["outcome", "exit_code", "timeout", "error"];
const RESULT_APART = new Set(["_retry_signature", "log_tail", "usage"]);

/** Le résultat d'un run : les champs en ligne, la queue de log en bloc. */
function Result({ run }: { run: Run }) {
  const result = run.result;
  if (!result) {
    return null;
  }
  const keys = [
    ...RESULT_ORDER.filter((k) => k in result),
    ...Object.keys(result)
      .filter((k) => !RESULT_ORDER.includes(k) && !RESULT_APART.has(k))
      .sort(),
  ];
  const tail = result.log_tail;
  return (
    <div className="flex flex-col gap-1 break-words">
      <span>
        {keys.map((key) => (
          <span key={key} className="mr-2">
            {key} <b>{String(result[key])}</b>
          </span>
        ))}
      </span>
      {typeof tail === "string" && tail && (
        <pre className="max-h-64 overflow-auto rounded-md bg-muted p-2 text-xs break-words whitespace-pre-wrap">
          {tail}
        </pre>
      )}
    </div>
  );
}

/** Un run sur sa ligne, et sa trace dépliée dessous si on l'a ouverte. */
function RunRows({
  item,
  run,
  shown,
  onToggle,
}: {
  item: number;
  run: Run;
  shown: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <TableRow>
        <TableCell className="whitespace-nowrap">
          {run.id}{" "}
          <button
            type="button"
            className="cursor-pointer underline"
            aria-expanded={shown}
            aria-label={`ouvrir la trace du run ${run.id}`}
            onClick={onToggle}
          >
            trace
          </button>
        </TableCell>
        <TableCell>{run.node}</TableCell>
        <TableCell>{run.cycle}</TableCell>
        {/* K candidats partagent une tentative : sans leur numéro, la
            table montrerait K lignes identiques */}
        <TableCell>
          {run.attempt}
          {run.candidate !== null && (
            <span className="ml-1 text-xs text-muted-foreground">
              c{run.candidate}
            </span>
          )}
        </TableCell>
        <TableCell>
          <Badge variant={tone(run.status)}>{run.status}</Badge>
        </TableCell>
        <TableCell>{run.outcome ?? ""}</TableCell>
        <TableCell className="whitespace-nowrap">
          {duration(run.duration_s)}
        </TableCell>
        <TableCell className="whitespace-nowrap">
          <span>{tokens(run.usage)}</span>
        </TableCell>
        {/* le post-mortem est le plus large de la table : on le borne au
            doigt pour raccourcir le glissement */}
        <TableCell className="max-w-80 text-xs md:max-w-none">
          <Result run={run} />
        </TableCell>
      </TableRow>
      {shown && (
        <TableRow>
          <TableCell colSpan={9} className="bg-muted/40">
            <TraceViewer item={item} run={run} />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

export function ItemRuns({ id, initial }: { id: number; initial: Run[] }) {
  const runs = useItem(id, (view) => view.runs, initial);
  const [open, setOpen] = useState<number[]>([]);
  if (!runs.length) {
    return null;
  }

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">runs</h2>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>run</TableHead>
            <TableHead>nœud</TableHead>
            <TableHead>passage</TableHead>
            <TableHead>tentative</TableHead>
            <TableHead>statut</TableHead>
            <TableHead>issue</TableHead>
            <TableHead>durée</TableHead>
            <TableHead>tokens</TableHead>
            <TableHead>résultat</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((run) => (
            <RunRows
              key={run.id}
              item={id}
              run={run}
              shown={open.includes(run.id)}
              onToggle={() => setOpen((was) => toggled(was, run.id))}
            />
          ))}
        </TableBody>
      </Table>
    </section>
  );
}
