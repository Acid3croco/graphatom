"use client";

/**
 * Le tableau de tous les items, vivant pour son compte.
 *
 * Il sonde `/api/items` toutes les cinq secondes et ne repeint que
 * lui-même : un item qui change d'état voit sa ligne bouger, la coquille
 * de la page ne bouge pas. Le rendu serveur pose la première liste
 * (`initial`), donc rien n'attend le premier tour.
 */
import Link from "next/link";

import type { Item } from "@/lib/api";
import { issueNumber, moment } from "@/lib/format";
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
          <TableHead>état</TableHead>
          <TableHead>version</TableHead>
          <TableHead>mis à jour</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => (
          <TableRow key={item.id}>
            <TableCell>
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
            <TableCell>
              <Link href={`/item/${item.id}`} className="underline">
                {item.title ?? item.subject_key}
              </Link>
            </TableCell>
            <TableCell className="text-muted-foreground">{item.graph}</TableCell>
            <TableCell className="text-muted-foreground">
              g{item.generation}
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
        ))}
      </TableBody>
    </Table>
  );
}
