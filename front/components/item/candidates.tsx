/**
 * Une étape en fan-out, dépliée sous sa ligne du journal : ses candidats,
 * puis la décision du juge quand il a tranché.
 */
import type { JudgeDecision, Variant } from "@/lib/api";
import type { Candidate } from "@/lib/fanout";
import { duration } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { UsageCells, UsageHeads } from "@/components/item/usage-cells";

/** Une variante, telle qu'elle est déclarée : ses clés, et rien d'inventé. */
function VariantFields({ variant }: { variant: Variant }) {
  // un objet — un `agent` surchargé — ne se met pas dans une cellule : ce
  // qu'il change se lit déjà dans les colonnes modèle et CLI
  const fields = Object.entries(variant).filter(
    ([, value]) => typeof value !== "object" || value === null,
  );
  return (
    <span className="flex flex-col gap-0.5">
      {fields.map(([key, value]) => (
        <span key={key}>
          <span className="text-muted-foreground">{key}</span>{" "}
          <b>{String(value)}</b>
        </span>
      ))}
    </span>
  );
}

/** Le sort d'un candidat : le vert est celui du gagnant, et de lui seul. */
function Fate({ candidate }: { candidate: Candidate }) {
  // un perdant qui porterait le vert rendrait la lecture ambiguë
  if (candidate.winner) {
    return <Badge variant="active">gagnant</Badge>;
  }
  if (candidate.loss) {
    return <Badge variant="alert">{candidate.loss}</Badge>;
  }
  return <Badge variant="warn">en course</Badge>;
}

/**
 * Les candidats d'une étape en fan-out, dépliés.
 *
 * Une table à elle, sous la ligne de l'étape : les champs d'un candidat ne
 * tiennent pas dans les colonnes du journal, et les aligner de force les
 * rendrait illisibles. Le gagnant porte sa marque — un fond, un badge —
 * qu'aucun perdant ne porte : il se voit sans comparer les colonnes.
 */
export function Candidates({ candidates }: { candidates: Candidate[] }) {
  return (
    <Table className="text-xs">
      <TableHeader>
        <TableRow>
          <TableHead>candidat</TableHead>
          <TableHead>variante</TableHead>
          <TableHead>modèle</TableHead>
          <TableHead>CLI</TableHead>
          <TableHead>effort</TableHead>
          <TableHead>issue</TableHead>
          <TableHead>sort</TableHead>
          <TableHead>durée</TableHead>
          <UsageHeads />
        </TableRow>
      </TableHeader>
      <TableBody>
        {candidates.map((candidate) => (
          <TableRow
            key={candidate.run.id}
            data-winner={candidate.winner || undefined}
            className={candidate.winner ? "bg-emerald-50" : undefined}
          >
            <TableCell className="whitespace-nowrap">
              c{candidate.index}
            </TableCell>
            <TableCell>
              <VariantFields variant={candidate.variant} />
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {candidate.model ?? ""}
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {candidate.cli ?? ""}
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {candidate.effort ?? ""}
            </TableCell>
            <TableCell>{candidate.run.outcome ?? ""}</TableCell>
            <TableCell>
              <Fate candidate={candidate} />
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {duration(candidate.duration_s)}
            </TableCell>
            <UsageCells usage={candidate.run.usage} />
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/** Ce que le juge a tranché, selon la forme de sa décision. */
function Verdict({ decision }: { decision: JudgeDecision }) {
  const selected = decision.selected;
  if (decision.type === "chosen") {
    return (
      <>
        <b>jugement de plusieurs finalistes</b>
        <p>finaliste {selected?.letter} → c{selected?.candidate}</p>
        <p>raison du choix : {decision.summary || "aucune raison rendue"}</p>
        {decision.judge && (
          <p className="text-muted-foreground">
            juge : modèle {decision.judge.model || "inconnu"} · CLI{" "}
            {decision.judge.cli || "inconnue"} · effort{" "}
            {decision.judge.effort || "inconnu"}
          </p>
        )}
      </>
    );
  }
  if (decision.type === "sole") {
    return (
      <>
        <b>finaliste unique choisi mécaniquement</b>
        {selected && <p>finaliste {selected.letter} → c{selected.candidate}</p>}
        <p>aucun modèle juge n’a été appelé.</p>
      </>
    );
  }
  return (
    <>
      <b>aucun finaliste</b>
      <p>aucun choix n’a été possible.</p>
    </>
  );
}

/** La décision du juge, rendue seulement après son choix. */
export function Decision({ decision }: { decision: JudgeDecision }) {
  return (
    <div data-testid="decision-juge" className="mt-3 flex flex-col gap-1 text-sm">
      <Verdict decision={decision} />
      {decision.verdict && (
        <details>
          <summary className="cursor-pointer underline">
            comparaison détaillée du juge
          </summary>
          <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap">
            {decision.verdict}
          </pre>
        </details>
      )}
    </div>
  );
}
