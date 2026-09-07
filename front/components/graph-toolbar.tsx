"use client";

/**
 * Les boutons de la visionneuse : zoom, reset, orientation, plein écran.
 *
 * Sur téléphone le dessin se met à la largeur du bloc, comme partout : un
 * graph de six couches y tient en entier mais en tout petit, et c'est le
 * zoom qui le rend lisible. Les boutons sont donc la seule façon de s'en
 * servir au doigt : ils passent à 36 px sous `sm:`.
 */
import type { ReactNode } from "react";
import { Expand, Minimize, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";

import type { Orient } from "@/lib/graph-layout";
import { Button } from "@/components/ui/button";

/** Un cran de bouton, en facteur de zoom. */
const STEP = 1.3;

// cible tactile sous `sm:`, taille d'origine au-dessus
const ICON = "h-9 w-9 p-0 sm:h-7 sm:w-7";

/** Un bouton à icône : son libellé sert à la fois d'infobulle et de nom. */
function Tool({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <Button
      variant="outline"
      size="sm"
      className={ICON}
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      {children}
    </Button>
  );
}

export function GraphToolbar({
  orient,
  full,
  onZoom,
  onReset,
  onTurn,
  onFullscreen,
}: {
  orient: Orient;
  full: boolean;
  /** Un cran de zoom : `STEP` pour avancer, `1 / STEP` pour reculer. */
  onZoom: (factor: number) => void;
  onReset: () => void;
  onTurn: () => void;
  onFullscreen: () => void;
}) {
  const other = orient === "LR" ? "TB" : "LR";
  return (
    <div className="absolute right-2 top-2 z-10 flex gap-1">
      <Tool label="zoom avant" onClick={() => onZoom(STEP)}>
        <ZoomIn className="h-4 w-4" aria-hidden />
      </Tool>
      <Tool label="zoom arrière" onClick={() => onZoom(1 / STEP)}>
        <ZoomOut className="h-4 w-4" aria-hidden />
      </Tool>
      <Tool label="reset de la vue" onClick={onReset}>
        <RotateCcw className="h-4 w-4" aria-hidden />
      </Tool>
      <Button
        variant="outline"
        size="sm"
        className="h-9 px-3 sm:h-7 sm:px-2"
        aria-label={`orientation ${orient}, basculer en ${other}`}
        title={`orientation ${orient} — basculer`}
        onClick={onTurn}
      >
        {orient}
      </Button>
      <Tool
        label={full ? "quitter le plein écran" : "plein écran"}
        onClick={onFullscreen}
      >
        {full ? (
          <Minimize className="h-4 w-4" aria-hidden />
        ) : (
          <Expand className="h-4 w-4" aria-hidden />
        )}
      </Tool>
    </div>
  );
}
