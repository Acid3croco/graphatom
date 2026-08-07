/**
 * Un graph publié, à l'adresse de sa révision.
 *
 * L'URL porte la révision, jamais le nom : c'est ce qui garde une ancienne
 * révision consultable telle qu'elle a été publiée, pendant que le même nom
 * en a déjà une plus récente.
 *
 * En tête, ce qui vaut pour tout le graph — les budgets et les cibles du
 * noyau — puis le graph lui-même et la config de chaque nœud.
 */
import Link from "next/link";
import { notFound } from "next/navigation";

import { getGraph } from "@/lib/api";
import { moment } from "@/lib/format";
import { ApiDown } from "@/components/api-down";
import { GraphView } from "@/components/graph-view";

export const dynamic = "force-dynamic";

export default async function GraphPage({
  params,
}: {
  params: Promise<{ revision: string }>;
}) {
  const { revision } = await params;
  let bundle;
  try {
    bundle = await getGraph(revision);
  } catch (err) {
    if (String(err).includes("404")) {
      notFound();
    }
    return <ApiDown error={err} />;
  }

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-2">
        <h1 className="text-lg font-semibold break-words sm:text-xl">
          {bundle.name}
          <span className="ml-2 text-xs font-normal text-muted-foreground sm:text-sm">
            rév. {bundle.revision.slice(0, 12)}… · publiée{" "}
            {moment(bundle.published_at)} · {bundle.items} item
            {bundle.items > 1 ? "s" : ""} · entrée {bundle.entry}
          </span>
        </h1>
        <p className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
          {/* les budgets tels que le bundle les nomme : un budget qu'on
              ajoute demain s'affiche sans qu'on touche à cette page */}
          {Object.entries(bundle.budgets).map(([key, value]) => (
            <span key={key} className="whitespace-nowrap">
              {key} <b>{String(value)}</b>
            </span>
          ))}
        </p>
        <p className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
          {Object.entries(bundle.on_kernel).map(([key, target]) => (
            <span key={key} className="whitespace-nowrap">
              on_kernel.{key} → <b>{target}</b>
            </span>
          ))}
        </p>
      </section>

      <GraphView bundle={bundle} />

      <p className="text-sm">
        <Link href="/graphs" className="underline">
          ← tous les graphs
        </Link>
      </p>
    </div>
  );
}
