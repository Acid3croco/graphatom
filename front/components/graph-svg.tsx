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
 * section du graph sonde ses données toutes les cinq secondes et `graph`
 * arrive alors en nouvel objet — sans que la visionneuse soit remontée :
 * rien de tout ça ne doit toucher à la vue. D'où `view` à `null`, qui veut
 * dire « l'ajustement initial » : il se recalcule à chaque rendu depuis la
 * géométrie du moment, donc il suit les données sans jamais remettre à zéro
 * un cadrage choisi. Un rendu de plus n'est plus un événement.
 *
 * Ce choix se range dans `localStorage`, une entrée par item, sous
 * `graph-view:<item>` : l'orientation et le cadrage. C'est ce qui le fait
 * survivre au rechargement de la page — le seul moment où la mémoire du
 * composant s'en va, puisque le sondage, lui, ne le remonte pas. Une entrée
 * absente, illisible ou d'une autre forme est ignorée en silence — la
 * visionneuse repart de l'ajustement initial, ce n'est pas une panne.
 *
 * `item` est ce qui nomme cette entrée, et il est facultatif : une vue qui
 * n'appartient à aucun item — la vitrine d'un graph publié — n'a pas de nom
 * de rangement, donc elle ne lit ni n'écrit rien. Tout le reste marche
 * pareil ; seul le cadrage ne survit pas au rechargement.
 *
 * Le nœud courant est peint en orange : c'est là qu'est l'item. Un graph
 * sans exécution — la vitrine d'une révision publiée — n'en a pas : son
 * `current` est vide, aucun nœud n'est peint, et c'est `onSelect` qui rend
 * alors les nœuds cliquables pour lire leur config.
 *
 * L'orientation par défaut est TB : de haut en bas, un graph tient dans la
 * largeur d'un téléphone là où LR le couche sur plusieurs écrans. Ce n'est
 * que le défaut d'une vue neuve — un choix déjà rangé revient tel quel.
 *
 * Le plein écran passe par l'API du navigateur quand elle est là. Safari
 * iOS ne la donne pas sur un `<div>` : `requestFullscreen` n'y existe pas,
 * et l'appeler à l'aveugle jetait une erreur, donc le bouton ne faisait
 * rien. À défaut d'API — ou si elle refuse — le conteneur se met lui-même
 * en `fixed inset-0`, ce qui donne la même vue sans rien demander à
 * personne. Le bouton Minimize en sort, la touche Échap aussi.
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
  // plein écran de l'API du navigateur, suivi par son propre événement
  const [native, setNative] = useState(false);
  // plein écran du repli CSS, quand l'API n'est pas là ou qu'elle refuse
  const [css, setCss] = useState(false);
  const full = native || css;
  // le cadrage choisi, ou `null` pour l'ajustement initial
  const [view, setView] = useState<View | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  // les pointeurs en cours : un pour le glisser, deux pour le pincement
  const points = useRef(new Map<number, { x: number; y: number }>());
  // d'où est parti le pointeur : ce qui distingue un clic d'un glisser
  const start = useRef({ x: 0, y: 0 });

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

  useEffect(() => {
    const sync = () => setNative(document.fullscreenElement === box.current);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  // le repli n'est qu'un jeu de classes : Échap n'en sort que si on l'écoute.
  // L'API native, elle, a déjà sa touche Échap — il n'y a rien à y ajouter.
  useEffect(() => {
    if (!css) {
      return;
    }
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setCss(false);
      }
    };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [css]);

  /** Entrer en plein écran, ou en sortir — par l'API si elle est là. */
  function fullscreen() {
    if (css) {
      setCss(false);
      return;
    }
    if (document.fullscreenElement) {
      document.exitFullscreen();
      return;
    }
    const el = box.current;
    if (!el?.requestFullscreen) {
      // Safari iOS ne met pas un `<div>` en plein écran : l'API n'y est pas
      setCss(true);
      return;
    }
    // le navigateur refuse le plein écran hors geste utilisateur : c'est
    // son droit, pas une panne du front, et le repli prend alors la main
    el.requestFullscreen().catch(() => setCss(true));
  }

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

  /** Le pointeur a-t-il quitté son point de départ — glisser, ou simple clic ? */
  function moved(e: React.MouseEvent | React.PointerEvent): boolean {
    return (
      Math.hypot(e.clientX - start.current.x, e.clientY - start.current.y) >= 4
    );
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
            const chosen = node.name === selected;
            return (
              <g
                key={node.name}
                className={onSelect ? "cursor-pointer" : undefined}
                role={onSelect ? "button" : undefined}
                aria-label={onSelect ? `config du nœud ${node.name}` : undefined}
                onClick={onSelect ? (e) => pick(e, node.name) : undefined}
              >
                <rect
                  x={x}
                  y={y}
                  width={W}
                  height={H}
                  rx="7"
                  fill={fill}
                  // le nœud choisi s'épaissit : c'est une sélection de
                  // lecture, pas un état d'exécution — la couleur ne bouge pas
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
                {/* le fan-out se compte dans le nœud courant, et nulle part
                    ailleurs : l'item est sur *un* nœud quel que soit le
                    nombre de candidats, et la géométrie ne bouge pas */}
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
          })}
      </svg>
    </div>
  );
}
