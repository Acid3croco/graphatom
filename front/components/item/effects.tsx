"use client";

/** Les effets réconciliés de l'item. */
import { useItem } from "@/lib/live";
import type { Effect } from "@/lib/api";
import { Badge, tone } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export function ItemEffects({ id, initial }: { id: number; initial: Effect[] }) {
  const effects = useItem(id, (view) => view.effects, initial);
  if (!effects.length) {
    return null;
  }

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">effets</h2>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>clé logique</TableHead>
            <TableHead>cible</TableHead>
            <TableHead>observation</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {effects.map((effect) => (
            <TableRow key={effect.op_id}>
              <TableCell>
                <code className="text-xs">{effect.logical_key}</code>
              </TableCell>
              <TableCell className="text-xs break-all">
                {effect.target_uri}
              </TableCell>
              <TableCell>
                <Badge variant={tone(effect.observation)}>
                  {effect.observation}
                </Badge>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}
