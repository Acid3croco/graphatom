/**
 * Le bundle publié, mis dans la forme que la visionneuse attend.
 *
 * `/api/graph/<rév>` rend le bundle tel qu'il est en base : les nœuds par
 * nom, leur config entière, leurs arêtes par issue. `GraphSvg` lit des
 * listes à plat — les mêmes que `/api/item/<id>` rend pour un item. Cette
 * fonction fait la traduction, et rien d'autre : aucune coordonnée, aucun
 * dessin, la géométrie reste dans `lib/graph-layout`.
 *
 * Un graph publié n'a pas d'état d'exécution : `current` est vide, donc
 * aucun nœud n'est peint en orange — ils sont tous au même titre.
 */
import type { Graph, GraphBundle } from "@/lib/api";

export function toGraph(bundle: GraphBundle): Graph {
  const nodes = Object.entries(bundle.nodes);
  return {
    name: bundle.name,
    entry: bundle.entry,
    on_kernel: bundle.on_kernel,
    current: "",
    nodes: nodes.map(([name, spec]) => ({
      name,
      block: spec.block ?? null,
      terminal: Boolean(spec.terminal),
      escalade: Boolean(spec.escalade),
    })),
    edges: nodes.flatMap(([name, spec]) =>
      Object.entries(spec.edges ?? {}).map(([outcome, to]) => ({
        from: name,
        outcome,
        to,
      })),
    ),
  };
}
