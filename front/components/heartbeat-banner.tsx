"use client";

/**
 * Les deux battements du rail, en tête de chaque page.
 *
 * Le worker tamponne à chaque tick, le canal GitHub à chaque tour de sa
 * boucle ; sans battement depuis deux minutes, le processus concerné ne
 * tourne plus et ce que la page montre est figé. Ça se dit en grand :
 * l'absence de signal est le signal. Les deux processus sont séparés,
 * chacun peut mourir seul — l'alarme sonne donc dès qu'un seul se tait.
 *
 * Le bandeau sonde sa propre route, `/api/heartbeat`, plus vite que les
 * sections de données — deux secondes — et ne repeint que lui-même. Il ne
 * commande plus rien à personne : chaque section de page a son sondage et
 * son rendu. Le bandeau arrive rendu côté serveur (`initial`), donc il est
 * déjà juste au premier octet, avant le premier tour de sondage.
 */
import { Activity, TriangleAlert } from "lucide-react";

import type { Beat, Heartbeat } from "@/lib/api";
import { ago, moment } from "@/lib/format";
import { BEAT_FEED, BEAT_MS, useSlice } from "@/lib/live";
import { cn } from "@/lib/utils";

/** Un battement en toutes lettres : son âge, ou l'heure où il s'est tu. */
function one(name: string, beat: Beat): string {
  if (!beat.stale) {
    return `${name} vivant il y a ${ago(beat.ago_s)}`;
  }
  if (beat.at) {
    return `${name} à l'arrêt depuis ${moment(beat.at)}`;
  }
  return `${name} à l'arrêt — jamais tamponné`;
}

/** L'alarme sonne dès qu'un seul des deux battements est périmé. */
function alarm(beat: Heartbeat | null): boolean {
  return !beat || beat.rail.stale || beat["github-sync"].stale;
}

function text(beat: Heartbeat | null): string {
  if (!beat) {
    return "rail injoignable — l'API ne répond pas, les états affichés sont figés.";
  }
  const worker = beat.rail.sha
    ? ` · worker ${beat.rail.sha}${beat.rail.started_at ? ` redémarré ${moment(beat.rail.started_at)}` : ""}`
    : "";
  const both = `${one("rail", beat.rail)}${worker} · ${one("canal GitHub", beat["github-sync"])}`;
  return alarm(beat) ? `${both} — les états affichés sont figés.` : both;
}

export function HeartbeatBanner({ initial }: { initial: Heartbeat | null }) {
  const beat = useSlice<Heartbeat, Heartbeat | null>(
    BEAT_FEED,
    (data) => data,
    initial,
    BEAT_MS,
  );
  const alert = alarm(beat);

  return (
    <p
      id="heartbeat"
      data-stale={alert ? "true" : "false"}
      className={cn(
        // sur téléphone le texte tient sur deux ou trois lignes : il passe
        // en entier, jamais tronqué, et l'icône s'aligne sur la première
        "flex items-start gap-2 rounded-md px-3 py-1.5 text-xs sm:items-center sm:text-sm",
        alert
          ? "border border-red-300 bg-red-50 font-semibold text-red-800"
          : "text-emerald-700",
      )}
    >
      {alert ? (
        <TriangleAlert className="mt-0.5 size-4 shrink-0 sm:mt-0" />
      ) : (
        <Activity className="mt-0.5 size-4 shrink-0 sm:mt-0" />
      )}
      {text(beat)}
    </p>
  );
}
