"use client";

/**
 * Un graph publié, et la config de ses nœuds.
 *
 * La même visionneuse que la page d'un item — pan, zoom, LR/TB, plein
 * écran —, mais sans état d'exécution : aucun nœud n'est courant, ils sont
 * tous au même titre. Le seul geste est la lecture : cliquer un nœud ouvre
 * sa config dessous, la refermer la range. Rien ne s'écrit d'ici.
 */
import { useMemo, useState } from "react";

import type { GraphBundle } from "@/lib/api";
import { toGraph } from "@/lib/graph-bundle";
import { GraphSvg } from "@/components/graph-svg";
import { NodeConfig } from "@/components/node-config";

export function GraphView({ bundle }: { bundle: GraphBundle }) {
  const graph = useMemo(() => toGraph(bundle), [bundle]);
  const [selected, setSelected] = useState<string | null>(null);
  const node = selected ? bundle.nodes[selected] : undefined;

  return (
    <div className="flex flex-col gap-3">
      <GraphSvg graph={graph} selected={selected} onSelect={setSelected} />
      {node && selected ? (
        <NodeConfig
          name={selected}
          node={node}
          graphAgent={bundle.agent}
          onClose={() => setSelected(null)}
        />
      ) : (
        <p className="text-sm text-muted-foreground">
          Clique un nœud pour lire sa config.
        </p>
      )}
    </div>
  );
}
