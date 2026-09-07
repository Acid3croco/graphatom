"use client";

/**
 * Le graph de l'item, et sa visionneuse.
 *
 * Le dessin vient de `lib/graph-layout` et se peint dans `graph-picture` ;
 * ce composant ne fait que le regarder. Toute la lecture tient dans le
 * `viewBox` du `<svg>` : le glisser en déplace l'origine, la molette et le
 * pincement en changent la largeur, le double-clic et le bouton reset le
 * rendent à l'ajustement initial. Rien d'autre ne bouge — pas de
 * transform, pas de re-layout, et aucune dépendance de plus.
 *
 * Les conversions écran → graph passent par `getScreenCTM()`, la seule
 * mesure qui reste juste quand le SVG est mis en boîte aux lettres, ce qui
 * arrive en plein écran. Le rapport largeur/hauteur du `viewBox` ne change
 * jamais : c'est ce qui garde le point sous le curseur immobile pendant le
 * zoom.
 *
 * Ce que l'œil a choisi vit ici, côté client, et nulle part ailleurs. La
 * section du graph sonde ses données toutes les cinq secondes et `graph`
 * arrive alors en nouvel objet — sans que la visionneuse soit remontée :
 * rien de tout ça ne doit toucher à la vue. D'où `view` à `null`, qui veut
 * dire « l'ajustement initial » : il se recalcule à chaque rendu depuis la
 * géométrie du moment, donc il suit les données sans jamais remettre à zéro
 * un cadrage choisi. Un rendu de plus n'est plus un événement.
 *
 * Ce choix se range par `lib/graph-memory`, une entrée par item. `item`
 * est ce qui nomme cette entrée, et il est facultatif : une vue qui
 * n'appartient à aucun item — la vitrine d'un graph publié — n'a pas de nom
 * de rangement, donc elle ne lit ni n'écrit rien. Tout le reste marche
 * pareil ; seul le cadrage ne survit pas au rechargement.
 *
 * L'orientation par défaut est TB : de haut en bas, un graph tient dans la
 * largeur d'un téléphone là où LR le couche sur plusieurs écrans. Ce n'est
 * que le défaut d'une vue neuve — un choix déjà rangé revient tel quel.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Graph } from "@/lib/api";
import { layout, type Orient } from "@/lib/graph-layout";
import { fit, read, write, type View } from "@/lib/graph-memory";
import { useFullscreen } from "@/lib/use-fullscreen";
import { cn } from "@/lib/utils";
import { GraphPicture } from "@/components/graph-picture";
import { GraphToolbar } from "@/components/graph-toolbar";

const MIN = 0.25; // bornes du zoom, en facteur de l'ajustement initial
const MAX = 4;

/** Le `viewBox` reste lisible : deux décimales suffisent au pixel près. */
const round = (n: number) => Math.round(n * 100) / 100;

type Point = { x: number; y: number };

/** L'écart entre les deux doigts d'un pincement, ou d'un pointeur et l'autre. */
const gap = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);

export function GraphSvg({
  graph,
  item,
  candidates = 0,
  selected,
  onSelect,
}: {
  graph: Graph;
  item?: number;
  /** Les candidats en course sur le nœud courant, quand il est en fan-out. */
  candidates?: number;
  selected?: string | null;
  onSelect?: (name: string) => void;
}) {
  const [orient, setOrient] = useState<Orient>("TB");
  // le cadrage choisi, ou `null` pour l'ajustement initial
  const [view, setView] = useState<View | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const { full, css, toggle: fullscreen } = useFullscreen(box);
  // les pointeurs en cours : un pour le glisser, deux pour le pincement
  const points = useRef(new Map<number, Point>());
  // d'où est parti le pointeur : ce qui distingue un clic d'un glisser
  const start = useRef<Point>({ x: 0, y: 0 });

  const plan = useMemo(() => layout(graph, orient), [graph, orient]);

  // au montage, la vue rangée pour cet item revient. La page monte un graph
  // par item, donc `item` ne change jamais sous les pieds du composant.
  useEffect(() => {
    if (item === undefined) {
      return; // sans item, il n'y a pas d'entrée à relire
    }
    const kept = read(item);
    if (kept) {
      setOrient(kept.orient);
      setView(kept.view);
    }
  }, [item]);

  // et chaque geste range à son tour — sauf le premier tour, qui vient de lire
  const first = useRef(true);
  useEffect(() => {
    if (item === undefined) {
      return; // ni d'entrée où ranger
    }
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

  /** Un cran de zoom au bouton : centré sur le milieu du cadre. */
  function step(factor: number) {
    const rect = svg.current?.getBoundingClientRect();
    if (rect) {
      zoomAt(factor, rect.left + rect.width / 2, rect.top + rect.height / 2);
    }
  }

  function down(e: React.PointerEvent) {
    start.current = { x: e.clientX, y: e.clientY };
    points.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
  }

  /**
   * Garder le pointeur qui sort du cadre — mais seulement quand il glisse.
   *
   * Capturé dès le `pointerdown`, le pointeur re-cible aussi le `click` sur
   * le SVG : un simple clic n'atteindrait plus jamais le nœud qu'il vise.
   * La capture arrive donc au premier vrai déplacement, et pas avant.
   */
  function capture(pointerId: number) {
    try {
      svg.current?.setPointerCapture(pointerId);
    } catch {
      // un pointeur simulé n'a rien à capturer — le glisser marche quand même
    }
  }

  /** Pincement : l'écart entre les deux doigts donne le facteur. */
  function pinch(e: React.PointerEvent, from: Point, other: Point) {
    const was = gap(from, other);
    const now = gap({ x: e.clientX, y: e.clientY }, other);
    if (was > 0 && now > 0) {
      zoomAt(now / was, (e.clientX + other.x) / 2, (e.clientY + other.y) / 2);
    }
  }

  /** Glisser : le déplacement à l'écran, ramené dans le repère du graph. */
  function drag(e: React.PointerEvent, from: Point) {
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

  function move(e: React.PointerEvent) {
    const from = points.current.get(e.pointerId);
    if (!from) {
      return;
    }
    if (moved(e)) {
      capture(e.pointerId);
    }
    const others = [...points.current.values()].filter((p) => p !== from);
    points.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (others.length === 1) {
      pinch(e, from, others[0]);
    } else if (!others.length) {
      drag(e, from);
    }
    // trois doigts ou plus : on ne devine pas ce que ça veut dire
  }

  function up(e: React.PointerEvent) {
    points.current.delete(e.pointerId);
  }

  /** Le pointeur a-t-il quitté son point de départ — glisser, ou simple clic ? */
  function moved(e: React.MouseEvent | React.PointerEvent): boolean {
    return gap({ x: e.clientX, y: e.clientY }, start.current) >= 4;
  }

  /** Choisir un nœud — un clic qui finit un glisser a déplacé la vue, pas plus. */
  function pick(e: React.MouseEvent, name: string) {
    if (!moved(e)) {
      onSelect?.(name);
    }
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
        css && "fixed inset-0 z-50",
      )}
    >
      <GraphToolbar
        orient={orient}
        full={full}
        onZoom={step}
        onReset={reset}
        onTurn={turn}
        onFullscreen={fullscreen}
      />
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
        aria-label={
          `graph ${graph.name}` +
          (graph.current ? `, courant ${graph.current}` : "") +
          (candidates > 1 ? `, ${candidates} candidats en course` : "") +
          `, orientation ${orient}`
        }
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={up}
        onDoubleClick={reset}
      >
        <GraphPicture
          graph={graph}
          plan={plan}
          candidates={candidates}
          selected={selected}
          onPick={onSelect ? pick : undefined}
        />
      </svg>
    </div>
  );
}
