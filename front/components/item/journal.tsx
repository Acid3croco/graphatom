"use client";

/**
 * Le journal, une ligne par version, et ce que chaque step a coûté.
 *
 * Une étape en fan-out garde sa ligne — l'item n'a fait qu'une transition
 * — mais elle dit alors combien de candidats l'ont courue, et se déplie
 * sur eux. Sa durée reste celle du mur, celle du séjour dans le nœud ; ses
 * jetons et son coût, eux, sont ceux de tous les candidats. Le pied de
 * table additionne ce que les lignes montrent, donc le total de la page
 * compte les perdants comme le gagnant.
 */
import { useState } from "react";

import { useItem } from "@/lib/live";
import type { Graph, JudgeDecision, JournalEntry, Run, Usage } from "@/lib/api";
import { steps, type Step } from "@/lib/fanout";
import { cost, count, duration, moment } from "@/lib/format";
import { toggled } from "@/lib/utils";
import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Candidates, Decision } from "@/components/item/candidates";
import { UsageCells, UsageHeads } from "@/components/item/usage-cells";

/** Une étape du journal, et ses candidats dépliés dessous si on l'a ouverte. */
function StepRows({
  step: { entry, usage, candidates },
  decision,
  shown,
  onToggle,
}: {
  step: Step;
  decision: JudgeDecision | null;
  shown: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <TableRow>
        <TableCell>v{entry.version}</TableCell>
        <TableCell className="whitespace-nowrap">
          {moment(entry.at, true)}
        </TableCell>
        <TableCell className="whitespace-nowrap">
          {duration(entry.duration_s)}
        </TableCell>
        <TableCell>{entry.kind}</TableCell>
        <TableCell>
          {entry.from_state ? `${entry.from_state} → ` : ""}
          {entry.to_state}
        </TableCell>
        <TableCell>{entry.outcome ?? ""}</TableCell>
        {/* le dépliant tient dans la colonne du run : une colonne de plus
            décorerait toutes les étapes pour les rares qui sont en fan-out
            — celles-ci ne changent pas d'un pixel */}
        <TableCell>
          {candidates.length ? (
            <button
              type="button"
              className="cursor-pointer whitespace-nowrap underline"
              aria-expanded={shown}
              onClick={onToggle}
            >
              {shown ? "▾" : "▸"} {candidates.length} candidats
            </button>
          ) : (
            (entry.run_id ?? "")
          )}
        </TableCell>
        <UsageCells usage={usage} />
      </TableRow>
      {shown && candidates.length > 0 && (
        <TableRow>
          <TableCell colSpan={10} className="bg-muted/40">
            <Candidates candidates={candidates} />
            {decision?.source === candidates[0].run.node && (
              <Decision decision={decision} />
            )}
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

export function ItemJournal({
  id,
  initial,
}: {
  id: number;
  initial: {
    journal: JournalEntry[];
    runs: Run[];
    graph: Graph;
    decision: JudgeDecision | null;
  };
}) {
  // les runs sont dans la tranche parce que les colonnes de tokens les
  // lisent : le journal dit quel run a produit le step, le run porte son
  // usage — la jointure se fait ici, une fois. Le graph nomme les
  // candidats d'un nœud en fan-out ; il vient donc avec.
  const { journal, runs, graph, decision } = useItem(
    id,
    (view) => ({
      journal: view.journal,
      runs: view.runs,
      graph: view.graph,
      decision: view.decision,
    }),
    initial,
  );
  // les étapes dépliées ; l'ouverture vit ici et survit aux tours de sondage
  const [open, setOpen] = useState<number[]>(
    decision ? journal.map((entry) => entry.version) : [],
  );
  const rows = steps(journal, runs, graph, decision);
  // les totaux du pied de table sont la somme de ce que les lignes montrent,
  // pas un second calcul : un step sans LLM y pèse zéro, et ça se voit
  const sum = (key: string) =>
    rows.reduce(
      (acc, step: { usage: Usage | undefined }) => acc + (step.usage?.[key] ?? 0),
      0,
    );

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">journal</h2>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>v</TableHead>
            <TableHead>à</TableHead>
            <TableHead>durée</TableHead>
            <TableHead>événement</TableHead>
            <TableHead>transition</TableHead>
            <TableHead>issue</TableHead>
            <TableHead>run</TableHead>
            <UsageHeads />
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((step) => (
            <StepRows
              key={step.entry.version}
              step={step}
              decision={decision}
              shown={open.includes(step.entry.version)}
              onToggle={() =>
                setOpen((was) => toggled(was, step.entry.version))
              }
            />
          ))}
        </TableBody>
        <TableFooter>
          <TableRow>
            <TableCell colSpan={7}>total</TableCell>
            <TableCell className="whitespace-nowrap">
              {count(sum("input_tokens"))}
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {count(sum("output_tokens"))}
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {cost(sum("total_cost_usd"))}
            </TableCell>
          </TableRow>
        </TableFooter>
      </Table>
    </section>
  );
}
