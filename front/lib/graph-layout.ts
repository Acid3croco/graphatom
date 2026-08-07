/**
 * La géométrie du graph, dans les deux orientations.
 *
 * Le placement sort d'un BFS depuis l'entrée et les cibles du noyau, une
 * arête par couple (source, cible) pour que deux issues vers le même nœud
 * partagent le trait, et une voie par arête de retour — sans quoi deux
 * retours écrivent leur étiquette au même point.
 *
 * L'API ne rend que des nœuds et des arêtes, sans aucune coordonnée : la
 * vérité de placement est ici, et nulle part ailleurs. L'orientation ne la
 * déplace pas, elle la paramètre. En LR les couches vont vers la droite et
 * les retours contournent par le bas ; en TB elles vont vers le bas et les
 * retours contournent par la droite. Tout le reste est commun.
 */
import type { Graph } from "@/lib/api";

export type Orient = "LR" | "TB";

export const W = 132;
export const H = 34;
const DX = 185;
const DY = 64;
const X0 = 12;
const Y0 = 12;
const LANE = 14; // écart entre deux voies d'arête de retour
// la clé d'une arête colle deux noms de nœud : le séparateur est le seul
// caractère qu'un nom ne peut pas porter. Écrit en échappement, pas en
// octet nul dans le fichier, sinon git tient tout le module pour binaire.
const SEP = "\u0000";

export type Drawn = {
  key: string;
  d: string;
  lx: number;
  ly: number;
  anchor: "start" | "middle" | "end";
  label: string;
};

export type Layout = {
  width: number;
  height: number;
  pos: Map<string, [number, number]>;
  edges: Drawn[];
};

/** Couches BFS depuis `entry` et les cibles `on_kernel` — pour le placement. */
function layers(graph: Graph): string[][] {
  const out = new Map<string, string[]>();
  for (const edge of graph.edges) {
    out.set(edge.from, [...(out.get(edge.from) ?? []), edge.to]);
  }
  const depth = new Map<string, number>();
  const queue: [string, number][] = [[graph.entry, 0]];
  for (const target of Object.values(graph.on_kernel)) {
    queue.push([target, 0]);
  }
  while (queue.length) {
    const [node, d] = queue.shift()!;
    const known = depth.get(node);
    if (known !== undefined && known <= d) {
      continue;
    }
    depth.set(node, d);
    for (const target of out.get(node) ?? []) {
      queue.push([target, d + 1]);
    }
  }
  if (!depth.size) {
    return [];
  }
  const rows: string[][] = Array.from(
    { length: Math.max(...depth.values()) + 1 },
    () => [],
  );
  const order = [...depth.keys()].sort(
    (a, b) => depth.get(a)! - depth.get(b)! || a.localeCompare(b),
  );
  for (const node of order) {
    rows[depth.get(node)!].push(node);
  }
  return rows;
}

/** Le graph placé dans l'orientation demandée, ou `null` s'il est vide. */
export function layout(graph: Graph, orient: Orient): Layout | null {
  const rows = layers(graph);
  if (!rows.length) {
    return null;
  }
  const lr = orient === "LR";

  const pos = new Map<string, [number, number]>();
  rows.forEach((layer, d) =>
    layer.forEach((node, i) =>
      pos.set(
        node,
        lr ? [X0 + d * DX, Y0 + i * DY] : [X0 + i * DX, Y0 + d * DY],
      ),
    ),
  );

  const merged = new Map<
    string,
    { from: string; to: string; outcomes: string[] }
  >();
  for (const edge of graph.edges) {
    if (!pos.has(edge.from) || !pos.has(edge.to)) {
      continue;
    }
    const key = `${edge.from}${SEP}${edge.to}`;
    const found = merged.get(key);
    if (found) {
      found.outcomes.push(edge.outcome);
    } else {
      merged.set(key, { from: edge.from, to: edge.to, outcomes: [edge.outcome] });
    }
  }
  const all = [...merged.entries()];
  // la position sur l'axe du flux dit si l'arête avance ou si elle revient
  const flow = (node: string) => (lr ? pos.get(node)![0] : pos.get(node)![1]);
  const backs = all.filter(([, e]) => flow(e.to) <= flow(e.from));
  const lanes = new Map(backs.map(([key], i) => [key, i]));

  const span = Math.max(...rows.map((l) => l.length));
  const margin = 10 + LANE * lanes.size; // la place que prennent les voies
  const width = X0 + (lr ? rows.length : span) * DX + (lr ? 0 : margin);
  const height = Y0 + (lr ? span : rows.length) * DY + (lr ? margin : 0);

  const used = new Set<string>();
  const edges = all.map(([key, edge]) => {
    const [x1, y1] = pos.get(edge.from)!;
    const [x2, y2] = pos.get(edge.to)!;
    const lane = lanes.get(key);
    let d: string;
    let lx: number;
    let ly: number;
    let anchor: Drawn["anchor"] = "middle";
    if (lane === undefined && lr) {
      const [sx, sy] = [x1 + W, y1 + H / 2];
      const [ex, ey] = [x2, y2 + H / 2];
      d = `M${sx},${sy} C${sx + 40},${sy} ${ex - 40},${ey} ${ex},${ey}`;
      [lx, ly] = [(sx + ex) / 2, (sy + ey) / 2 - 5];
    } else if (lane === undefined) {
      // TB : l'arête sort par le bas et entre par le haut
      const [sx, sy] = [x1 + W / 2, y1 + H];
      const [ex, ey] = [x2 + W / 2, y2];
      d = `M${sx},${sy} C${sx},${sy + 40} ${ex},${ey - 40} ${ex},${ey}`;
      [lx, ly] = [(sx + ex) / 2 + 5, (sy + ey) / 2 + 3];
      anchor = "start"; // l'étiquette se range à droite du trait, pas dessus
    } else if (lr) {
      // arête de retour : contourne par le bas, dans sa voie
      const [sx, sy] = [x1, y1 + H / 2];
      const [ex, ey] = [x2 + W, y2 + H / 2];
      const my = height - 6 - lane * LANE;
      d = `M${sx},${sy} C${sx - 60},${my} ${ex + 60},${my} ${ex},${ey}`;
      [lx, ly] = [(sx + ex) / 2, my - 4];
    } else {
      // arête de retour : contourne par la droite, dans sa voie
      const [sx, sy] = [x1 + W / 2, y1];
      const [ex, ey] = [x2 + W / 2, y2 + H];
      const mx = width - 6 - lane * LANE;
      d = `M${sx},${sy} C${mx},${sy - 60} ${mx},${ey + 60} ${ex},${ey}`;
      [lx, ly] = [mx - 4, (sy + ey) / 2];
      anchor = "end";
    }
    while (used.has(`${lx},${ly}`)) {
      ly -= 10; // deux milieux confondus : on décale, jamais on n'empile
    }
    used.add(`${lx},${ly}`);
    return { key, d, lx, ly, anchor, label: edge.outcomes.join(", ") };
  });

  return { width, height, pos, edges };
}
