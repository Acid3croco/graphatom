/**
 * Le dessin du graph : ses arêtes, puis ses nœuds, à l'intérieur du `<svg>`.
 *
 * La géométrie vient de `lib/graph-layout` ; ce module ne fait que la
 * peindre. Le nœud courant est en orange : c'est là qu'est l'item. Un
 * graph sans exécution — la vitrine d'une révision publiée — n'en a pas :
 * son `current` est vide, aucun nœud n'est peint, et c'est `onPick` qui
 * rend alors les nœuds cliquables pour lire leur config.
 */
import type { Graph, GraphNode } from "@/lib/api";
import { H, W, type Layout } from "@/lib/graph-layout";

/** La couleur d'un nœud : courant, terminal, ou ordinaire. */
function paint(node: GraphNode, current: string): string {
  if (node.name === current) {
    return "#ffb74d";
  }
  return node.terminal ? "#eee" : "#e3ecf7";
}

function Node({
  node,
  x,
  y,
  graph,
  candidates,
  chosen,
  onPick,
}: {
  node: GraphNode;
  x: number;
  y: number;
  graph: Graph;
  candidates: number;
  chosen: boolean;
  onPick?: (e: React.MouseEvent, name: string) => void;
}) {
  return (
    <g
      className={onPick ? "cursor-pointer" : undefined}
      role={onPick ? "button" : undefined}
      aria-label={onPick ? `config du nœud ${node.name}` : undefined}
      onClick={onPick ? (e) => onPick(e, node.name) : undefined}
    >
      <rect
        x={x}
        y={y}
        width={W}
        height={H}
        rx="7"
        fill={paint(node, graph.current)}
        // le nœud choisi s'épaissit : c'est une sélection de lecture, pas
        // un état d'exécution — la couleur ne bouge pas
        stroke={chosen ? "#333" : "#888"}
        strokeWidth={chosen ? 2 : 1}
        strokeDasharray={node.escalade ? "4 2" : undefined}
      />
      <text
        x={x + W / 2}
        y={y + H / 2 + 4}
        fontSize="11"
        textAnchor="middle"
      >
        {node.terminal ? node.name : `${node.name} · ${node.block}`}
      </text>
      {/* le fan-out se compte dans le nœud courant, et nulle part ailleurs :
          l'item est sur *un* nœud quel que soit le nombre de candidats, et
          la géométrie ne bouge pas */}
      {candidates > 1 && node.name === graph.current && (
        <text
          x={x + W - 4}
          y={y + 11}
          fontSize="8"
          fill="#7a4a00"
          textAnchor="end"
        >
          ×{candidates}
        </text>
      )}
    </g>
  );
}

export function GraphPicture({
  graph,
  plan,
  candidates,
  selected,
  onPick,
}: {
  graph: Graph;
  plan: Layout;
  /** Les candidats en course sur le nœud courant, quand il est en fan-out. */
  candidates: number;
  selected?: string | null;
  /** Le clic sur un nœud, quand la vue en fait quelque chose. */
  onPick?: (e: React.MouseEvent, name: string) => void;
}) {
  return (
    <>
      <defs>
        <marker
          id="arr"
          markerWidth="7"
          markerHeight="7"
          refX="6"
          refY="3"
          orient="auto"
        >
          <path d="M0,0 L6,3 L0,6" fill="none" stroke="#999" />
        </marker>
      </defs>
      {plan.edges.map((p) => (
        <g key={p.key}>
          <path d={p.d} fill="none" stroke="#999" markerEnd="url(#arr)" />
          <text
            x={p.lx}
            y={p.ly}
            fontSize="9"
            fill="#777"
            textAnchor={p.anchor}
          >
            {p.label}
          </text>
        </g>
      ))}
      {graph.nodes
        .filter((node) => plan.pos.has(node.name))
        .map((node) => {
          const [x, y] = plan.pos.get(node.name)!;
          return (
            <Node
              key={node.name}
              node={node}
              x={x}
              y={y}
              graph={graph}
              candidates={candidates}
              chosen={node.name === selected}
              onPick={onPick}
            />
          );
        })}
    </>
  );
}
