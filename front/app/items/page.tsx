/**
 * Tous les items : le titre vers la page de l'item, l'état courant, la
 * trajectoire.
 *
 * Le titre vient de la base — le canal l'y a rangé à l'admission — et mène
 * au détail de l'item, son chemin naturel. Le lien vers l'issue GitHub est
 * porté par le numéro, dans sa propre colonne, comme sur les pages stdlib.
 * Un sujet d'un autre canal n'a ni titre ni numéro : la cellule du titre
 * montre alors la clé du sujet, celle du numéro reste vide.
 */
import Link from "next/link";

import { getItems } from "@/lib/api";
import { issueNumber, moment } from "@/lib/format";
import { ApiDown } from "@/components/api-down";
import { Badge, tone } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export const dynamic = "force-dynamic";

export default async function ItemsPage() {
  let items;
  try {
    items = await getItems();
  } catch (err) {
    return <ApiDown error={err} />;
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">items</h1>
      {!items.length && <p className="text-muted-foreground">Aucun item admis.</p>}
      {items.length > 0 && (
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
                <TableCell className="text-muted-foreground">
                  {item.graph}
                </TableCell>
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
      )}
    </div>
  );
}
