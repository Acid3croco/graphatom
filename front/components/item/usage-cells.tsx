/**
 * Les trois colonnes d'usage d'une table : tokens in, tokens out, coût.
 *
 * Le journal et la table des candidats les montrent à l'identique — les
 * en-têtes comme les cellules. Le détail du cache tient dans l'infobulle
 * de la première : une colonne de plus dirait la même chose en prenant la
 * place.
 */
import type { Usage } from "@/lib/api";
import { cost, count, tokens } from "@/lib/format";
import { TableCell, TableHead } from "@/components/ui/table";

export function UsageHeads() {
  return (
    <>
      <TableHead>tokens in</TableHead>
      <TableHead>tokens out</TableHead>
      <TableHead>rapporté $</TableHead>
    </>
  );
}

export function UsageCells({ usage }: { usage: Usage | null | undefined }) {
  return (
    <>
      <TableCell
        className="whitespace-nowrap"
        title={tokens(usage) || undefined}
      >
        {count(usage?.input_tokens)}
      </TableCell>
      <TableCell className="whitespace-nowrap">
        {count(usage?.output_tokens)}
      </TableCell>
      <TableCell className="whitespace-nowrap">
        {cost(usage?.total_cost_usd)}
      </TableCell>
    </>
  );
}
