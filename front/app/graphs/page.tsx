/**
 * Les graphs publiés : une ligne par révision, la plus récente en tête.
 *
 * Ce qui est configuré, pas ce qui tourne. Les anciennes révisions restent
 * listées et consultables : un item en vol épingle la sienne, et cette
 * révision-là reste sa vérité même après une republication. Le compte
 * d'items dit lesquelles sont encore portées.
 */
import Link from "next/link";

import { getGraphs } from "@/lib/api";
import { moment } from "@/lib/format";
import { ApiDown } from "@/components/api-down";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export const dynamic = "force-dynamic";

export default async function GraphsPage() {
  let graphs;
  try {
    graphs = await getGraphs();
  } catch (err) {
    return <ApiDown error={err} />;
  }

  // l'API rend les révisions par nom, la plus récente d'abord : la première
  // de chaque nom est donc celle qu'une nouvelle admission prendrait
  const seen = new Set<string>();
  const rows = graphs.map((graph) => {
    const latest = !seen.has(graph.name);
    seen.add(graph.name);
    return { ...graph, latest };
  });

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">graphs</h1>
      {!rows.length && (
        <p className="text-muted-foreground">Aucun graph publié.</p>
      )}
      {rows.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>graph</TableHead>
              <TableHead>révision</TableHead>
              <TableHead>publiée</TableHead>
              <TableHead>items</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((graph) => (
              <TableRow key={graph.id}>
                <TableCell>
                  <Link href={`/graphs/${graph.id}`} className="underline">
                    {graph.name}
                  </Link>
                  {graph.latest && (
                    <Badge variant="active" className="ml-2">
                      dernière
                    </Badge>
                  )}
                </TableCell>
                <TableCell>
                  <Link
                    href={`/graphs/${graph.id}`}
                    className="text-muted-foreground underline"
                  >
                    <code className="text-xs">{graph.id.slice(0, 12)}…</code>
                  </Link>
                </TableCell>
                <TableCell className="text-muted-foreground whitespace-nowrap">
                  {moment(graph.published_at)}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {graph.items}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
