"use client";

/**
 * Le tableau de tous les items, vivant pour son compte.
 *
 * Il sonde `/api/items` toutes les cinq secondes et ne repeint que
 * lui-même : un item qui change d'état voit sa ligne bouger, la coquille
 * de la page ne bouge pas. Le rendu serveur pose la première liste
 * (`initial`), donc rien n'attend le premier tour.
 *
 * Sur téléphone la table glisse dans son conteneur et le titre se coupe
 * en une ligne : une ligne par item, le texte complet dans l'infobulle.
 * Au-delà de `md:` le titre revient sur autant de lignes qu'il en veut.
 */
import Link from "next/link";

import type { Item } from "@/lib/api";
import { cost, issueNumber, moment } from "@/lib/format";
import { useItems } from "@/lib/live";
import { Badge, tone } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// coupé au doigt, entier au bureau
const TITLE = "block truncate md:overflow-visible md:whitespace-normal";

export function ItemsTable({ initial }: { initial: Item[] }) {
  const items = useItems(initial);
  if (!items.length) {
    return <p className="text-muted-foreground">Aucun item admis.</p>;
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>item</TableHead>
          <TableHead>issue</TableHead>
          <TableHead>titre</TableHead>
          <TableHead>graph</TableHead>
          <TableHead>gén.</TableHead>
          <TableHead>rapporté $</TableHead>
          <TableHead>API estimé $</TableHead>
          <TableHead>état</TableHead>
          <TableHead>version</TableHead>
          <TableHead>mis à jour</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => {
          const title = item.title ?? item.subject_key;
          return (
            <TableRow key={item.id}>
              {/* « item 40 » sur une ligne tant que la place manque ;
                  au-delà de `lg:` la table respire et on la laisse faire */}
              <TableCell className="whitespace-nowrap lg:whitespace-normal">
                <Link href={`/item/${item.id}`} className="underline">
                  item {item.id}
                </Link>
              </TableCell>
              <TableCell>
                {item.issue_url && (
                  <a href={item.issue_url} className="underline">
                    {issueNumber(item.issue_url)}
                  </a>
                )}
              </TableCell>
              <TableCell className="max-w-56 md:max-w-none">
                <Link
                  href={`/item/${item.id}`}
                  title={title}
                  className={`${TITLE} underline`}
                >
                  {title}
                </Link>
              </TableCell>
              <TableCell className="text-muted-foreground">
                {item.graph}
              </TableCell>
              <TableCell className="text-muted-foreground">
                g{item.generation}
              </TableCell>
              <TableCell className="text-muted-foreground whitespace-nowrap">
                {cost(item.reported_cost_usd)}
              </TableCell>
              <TableCell
                className="text-muted-foreground whitespace-nowrap"
                title={`${item.cost_estimated_runs} run(s) chiffré(s), ${item.cost_unestimated_runs} sans tarif`}
              >
                {cost(item.estimated_cost_usd)}
                {item.cost_unestimated_runs ? " *" : ""}
              </TableCell>
              <TableCell>
                <Badge variant={tone(item.status)}>{item.state}</Badge>
              </TableCell>
              <TableCell className="text-muted-foreground">
                v{item.version}
              </TableCell>
              <TableCell className="text-muted-foreground whitespace-nowrap">
                {item.terminal_at
                  ? moment(item.terminal_at)
                  : (moment(item.updated_at) || "actif")}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
