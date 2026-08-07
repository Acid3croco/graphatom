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
import { H, W, layout, type Orient } from "@/lib/graph-layout";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

const MIN = 0.25; // bornes du zoom, en facteur de l'ajustement initial
const MAX = 4;
const STEP = 1.3; // un cran de bouton

// cible tactile sous `sm:`, taille d'origine au-dessus
const ICON = "h-9 w-9 p-0 sm:h-7 sm:w-7";

type View = { x: number; y: number; w: number; h: number };

/** Le `viewBox` reste lisible : deux décimales suffisent au pixel près. */
const round = (n: number) => Math.round(n * 100) / 100;

export function GraphSvg({ graph }: { graph: Graph }) {
  const [orient, setOrient] = useState<Orient>("LR");
  const [full, setFull] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  // les pointeurs en cours : un pour le glisser, deux pour le pincement
  const points = useRef(new Map<number, { x: number; y: number }>());

  const plan = useMemo(() => layout(graph, orient), [graph, orient]);
  const [view, setView] = useState<View>(() => ({
    x: 0,
    y: 0,
    w: plan?.width ?? 1,
    h: plan?.height ?? 1,
  }));

  const reset = useCallback(() => {
    if (plan) {
      setView({ x: 0, y: 0, w: plan.width, h: plan.height });
    }
  }, [plan]);

  // changer d'orientation refait la géométrie : la vue repart de l'ajustement
  useEffect(reset, [reset]);

  const zoomAt = useCallback(
    (factor: number, cx: number, cy: number) => {
      const ctm = svg.current?.getScreenCTM();
      if (!plan || !ctm) {
        return;
      }
      const point = new DOMPoint(cx, cy).matrixTransform(ctm.inverse());
      setView((v) => {
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
    if (!ctm) {
      return;
    }
    const [dx, dy] = [(e.clientX - from.x) / ctm.a, (e.clientY - from.y) / ctm.d];
    setView((v) => ({ ...v, x: v.x - dx, y: v.y - dy }));
  }

  function up(e: React.PointerEvent) {
    points.current.delete(e.pointerId);
  }

  if (!plan) {
    return null;
  }

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
          onClick={() => setOrient(orient === "LR" ? "TB" : "LR")}
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
        viewBox={`${round(view.x)} ${round(view.y)} ${round(view.w)} ${round(
          view.h,
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
