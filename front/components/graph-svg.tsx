"use client";

/**
 * Le graph de l'item, et sa visionneuse.
 *
 * Le dessin vient de `lib/graph-layout` ; ce composant ne fait que le
 * regarder. Toute la lecture tient dans le `viewBox` du `<svg>` : le
 * glisser en déplace l'origine, la molette et le pincement en changent la
 * largeur, le double-clic et le bouton reset le rendent à l'ajustement
 * initial. Rien d'autre ne bouge — pas de transform, pas de re-layout, et
 * aucune dépendance de plus.
 *
 * Les conversions écran → graph passent par `getScreenCTM()`, la seule
 * mesure qui reste juste quand le SVG est mis en boîte aux lettres, ce qui
 * arrive en plein écran. Le rapport largeur/hauteur du `viewBox` ne change
 * jamais : c'est ce qui garde le point sous le curseur immobile pendant le
 * zoom.
 *
 * Ce que l'œil a choisi vit ici, côté client, et nulle part ailleurs. La
 * page se refait rendre toutes les cinq secondes — le sondage du bandeau de
 * battement — et `graph` arrive alors en nouvel objet : rien de tout ça ne
 * doit toucher à la vue. D'où `view` à `null`, qui veut dire « l'ajustement
 * initial » : il se recalcule à chaque rendu depuis la géométrie du moment,
 * donc il suit les données sans jamais remettre à zéro un cadrage choisi.
 * Un rendu de plus n'est plus un événement.
 *
 * Ce choix se range dans `localStorage`, une entrée par item, sous
 * `graph-view:<item>` : l'orientation et le cadrage. Une entrée absente,
 * illisible ou d'une autre forme est ignorée en silence — la visionneuse
 * repart de l'ajustement initial, ce n'est pas une panne.
 *
 * Le nœud courant est peint en orange : c'est là qu'est l'item.
 *
 * Sur téléphone le dessin se met à la largeur du bloc, comme partout : un
 * graph de six couches y tient en entier mais en tout petit, et c'est le
 * zoom qui le rend lisible. Les boutons de la visionneuse sont donc la
 * seule façon de s'en servir au doigt : ils passent à 36 px sous `sm:`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Expand, Minimize, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";

import type { Graph } from "@/lib/api";
import { H, W, layout, type Layout, type Orient } from "@/lib/graph-layout";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

const MIN = 0.25; // bornes du zoom, en facteur de l'ajustement initial
const MAX = 4;
const STEP = 1.3; // un cran de bouton

// cible tactile sous `sm:`, taille d'origine au-dessus
const ICON = "h-9 w-9 p-0 sm:h-7 sm:w-7";

type View = { x: number; y: number; w: number; h: number };
/** Ce qu'un item garde : son orientation, et son cadrage s'il en a un. */
type Kept = { orient: Orient; view: View | null };

/** Le `viewBox` reste lisible : deux décimales suffisent au pixel près. */
const round = (n: number) => Math.round(n * 100) / 100;

/** L'ajustement initial : tout le dessin, et rien de plus. */
const fit = (plan: Layout): View => ({
  x: 0,
  y: 0,
  w: plan.width,
  h: plan.height,
});

/** L'entrée de rangement d'un item. */
const slot = (item: number) => `graph-view:${item}`;

const num = (n: unknown): n is number =>
  typeof n === "number" && Number.isFinite(n);

/** Ce qui est rangé pour cet item, ou `null` si rien de lisible n'y est. */
function read(item: number): Kept | null {
  let kept: Record<string, unknown> | null = null;
  try {
    kept = JSON.parse(localStorage.getItem(slot(item)) ?? "");
  } catch {
    // rien de rangé, ou du texte qui n'est pas du json : on repart de zéro
    return null;
  }
  const orient = kept?.orient;
  if (orient !== "LR" && orient !== "TB") {
    return null;
  }
  const { x, y, w, h } = kept ?? {};
  const view = num(x) && num(y) && num(w) && num(h) ? { x, y, w, h } : null;
  return { orient, view };
}

/** Ranger la vue de cet item. Un stockage qui refuse ne casse rien. */
function write(item: number, kept: Kept) {
  try {
    localStorage.setItem(
      slot(item),
      JSON.stringify({ orient: kept.orient, ...kept.view }),
    );
  } catch {
    // stockage plein ou interdit : la vue reste juste pour cette page-ci
  }
}

export function GraphSvg({ graph, item }: { graph: Graph; item: number }) {
  const [orient, setOrient] = useState<Orient>("LR");
  const [full, setFull] = useState(false);
  // le cadrage choisi, ou `null` pour l'ajustement initial
  const [view, setView] = useState<View | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  // les pointeurs en cours : un pour le glisser, deux pour le pincement
  const points = useRef(new Map<number, { x: number; y: number }>());

  const plan = useMemo(() => layout(graph, orient), [graph, orient]);

  // au montage, la vue rangée pour cet item revient. La page monte un graph
  // par item, donc `item` ne change jamais sous les pieds du composant.
  useEffect(() => {
    const kept = read(item);
    if (kept) {
      setOrient(kept.orient);
      setView(kept.view);
    }
  }, [item]);

  // et chaque geste range à son tour — sauf le premier tour, qui vient de lire
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    write(item, { orient, view });
  }, [item, orient, view]);

  /** Revenir à l'ajustement initial : la vue n'a plus de choix à garder. */
  const reset = useCallback(() => setView(null), []);

  /** L'autre orientation, c'est une autre géométrie : la vue repart de zéro. */
  function turn() {
    setOrient(orient === "LR" ? "TB" : "LR");
    setView(null);
  }

  const zoomAt = useCallback(
    (factor: number, cx: number, cy: number) => {
      const ctm = svg.current?.getScreenCTM();
      if (!plan || !ctm) {
        return;
      }
      const point = new DOMPoint(cx, cy).matrixTransform(ctm.inverse());
      setView((seen) => {
        const v = seen ?? fit(plan);
        const w = Math.min(
          Math.max(v.w / factor, plan.width / MAX),
          plan.width / MIN,
        );
        // le rapport ne bouge pas, donc le point sous le curseur non plus
        const k = w / v.w;
        return {
          x: point.x - (point.x - v.x) * k,
          y: point.y - (point.y - v.y) * k,
          w,
          h: v.h * k,
        };
      });
    },
    [plan],
  );

  useEffect(() => {
    const el = svg.current;
    if (!el) {
      return;
    }
    // React pose `wheel` en passif : la molette zoomerait *et* ferait
    // défiler la page. L'écouteur natif est le seul qui peut refuser.
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      zoomAt(Math.exp(-e.deltaY / 300), e.clientX, e.clientY);
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [zoomAt]);

  useEffect(() => {
    const sync = () => setFull(document.fullscreenElement === box.current);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  function fullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen();
      return;
    }
    // le navigateur refuse le plein écran hors geste utilisateur : c'est
    // son droit, pas une panne du front, et il n'y a rien à en dire
    box.current?.requestFullscreen().catch(() => undefined);
  }

  /** Un cran de zoom au bouton : centré sur le milieu du cadre. */
  function step(factor: number) {
    const rect = svg.current?.getBoundingClientRect();
    if (rect) {
      zoomAt(factor, rect.left + rect.width / 2, rect.top + rect.height / 2);
    }
  }

  function down(e: React.PointerEvent) {
    points.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    try {
      svg.current?.setPointerCapture(e.pointerId);
    } catch {
      // un pointeur simulé n'a rien à capturer — le glisser marche quand même
    }
  }

  function move(e: React.PointerEvent) {
    const from = points.current.get(e.pointerId);
    if (!from) {
      return;
    }
    const others = [...points.current.values()].filter((p) => p !== from);
    points.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (others.length === 1) {
      // pincement : l'écart entre les deux doigts donne le facteur
      const other = others[0];
      const was = Math.hypot(from.x - other.x, from.y - other.y);
      const now = Math.hypot(e.clientX - other.x, e.clientY - other.y);
      if (was > 0 && now > 0) {
        zoomAt(now / was, (e.clientX + other.x) / 2, (e.clientY + other.y) / 2);
      }
      return;
    }
    if (others.length) {
      return; // trois doigts ou plus : on ne devine pas ce que ça veut dire
    }
    const ctm = svg.current?.getScreenCTM();
    if (!ctm || !plan) {
      return;
    }
    const [dx, dy] = [(e.clientX - from.x) / ctm.a, (e.clientY - from.y) / ctm.d];
    setView((seen) => {
      const v = seen ?? fit(plan);
      return { ...v, x: v.x - dx, y: v.y - dy };
    });
  }

  function up(e: React.PointerEvent) {
    points.current.delete(e.pointerId);
  }

  if (!plan) {
    return null;
  }

  // ce que le `viewBox` montre : le choix de l'œil, sinon l'ajustement
  const seen = view ?? fit(plan);

  return (
    <div
      ref={box}
      className={cn(
        "relative rounded-md border bg-background",
        full && "flex h-full w-full items-center justify-center",
      )}
    >
      <div className="absolute right-2 top-2 z-10 flex gap-1">
        <Button
          variant="outline"
          size="sm"
          className={ICON}
          aria-label="zoom avant"
          title="zoom avant"
          onClick={() => step(STEP)}
        >
          <ZoomIn className="h-4 w-4" aria-hidden />
        </Button>
        <Button
          variant="outline"
          size="sm"
          className={ICON}
          aria-label="zoom arrière"
          title="zoom arrière"
          onClick={() => step(1 / STEP)}
        >
          <ZoomOut className="h-4 w-4" aria-hidden />
        </Button>
        <Button
          variant="outline"
          size="sm"
          className={ICON}
          aria-label="reset de la vue"
          title="reset de la vue"
          onClick={reset}
        >
          <RotateCcw className="h-4 w-4" aria-hidden />
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-9 px-3 sm:h-7 sm:px-2"
          aria-label={`orientation ${orient}, basculer en ${
            orient === "LR" ? "TB" : "LR"
          }`}
          title={`orientation ${orient} — basculer`}
          onClick={turn}
        >
          {orient}
        </Button>
        <Button
          variant="outline"
          size="sm"
          className={ICON}
          aria-label={full ? "quitter le plein écran" : "plein écran"}
          title={full ? "quitter le plein écran" : "plein écran"}
          onClick={fullscreen}
        >
          {full ? (
            <Minimize className="h-4 w-4" aria-hidden />
          ) : (
            <Expand className="h-4 w-4" aria-hidden />
          )}
        </Button>
      </div>
      <svg
        ref={svg}
        viewBox={`${round(seen.x)} ${round(seen.y)} ${round(seen.w)} ${round(
          seen.h,
        )}`}
        xmlns="http://www.w3.org/2000/svg"
        className={cn(
          "w-full touch-none select-none",
          full ? "h-full" : "h-auto",
        )}
        role="img"
        aria-label={`graph ${graph.name}, courant ${graph.current}, orientation ${orient}`}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={up}
        onDoubleClick={reset}
      >
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
            const fill =
              node.name === graph.current
                ? "#ffb74d"
                : node.terminal
                  ? "#eee"
                  : "#e3ecf7";
            return (
              <g key={node.name}>
                <rect
                  x={x}
                  y={y}
                  width={W}
                  height={H}
                  rx="7"
                  fill={fill}
                  stroke="#888"
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
              </g>
            );
          })}
      </svg>
    </div>
  );
}
