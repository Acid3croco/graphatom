"use client";

/** Le graph de l'item, avec le nœud courant marqué. */
import { useItem } from "@/lib/live";
import type { Graph, Run } from "@/lib/api";
import { running } from "@/lib/fanout";
import { GraphSvg } from "@/components/graph-svg";

export function ItemGraph({
  id,
  initial,
}: {
  id: number;
  initial: { graph: Graph; runs: Run[] };
}) {
  // le graph change à chaque transition ; la visionneuse, elle, n'est
  // jamais remontée — le cadrage choisi par l'œil ne bouge donc pas.
  // Les runs sont là pour compter les candidats en vol, rien de plus :
  // l'item reste sur *un* nœud, et la géométrie ne bouge pas d'un pixel.
  const { graph, runs } = useItem(
    id,
    (view) => ({ graph: view.graph, runs: view.runs }),
    initial,
  );

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">graph</h2>
      <GraphSvg graph={graph} item={id} candidates={running(runs, graph.current)} />
    </section>
  );
}
