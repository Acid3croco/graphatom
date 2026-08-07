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
 * Le rafraîchissement du front tient entièrement ici : un sondage SWR
 * toutes les 5 s sur la route `/api/heartbeat` du front, et un
 * `router.refresh()` derrière, qui refait rendre les composants serveur de
 * la page courante sans la recharger. Rien n'est poussé par le serveur —
 * le client demande, c'est tout. Le bandeau arrive rendu côté serveur
 * (`initial`), donc il est déjà juste au premier octet, avant le premier
 * tour de sondage.
 */
import useSWR from "swr";
import { useRouter } from "next/navigation";
import { Activity, TriangleAlert } from "lucide-react";

import type { Beat, Heartbeat } from "@/lib/api";
import { ago, moment } from "@/lib/format";
import { cn } from "@/lib/utils";

const POLL_MS = 5000;

// une API muette rend un 502 à corps d'erreur, qui n'a aucun battement :
// c'est une erreur pour SWR, pas un objet à lire — sinon le bandeau tombe
const fetcher = async (url: string) => {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`heartbeat : ${res.status}`);
  }
  return res.json();
};

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
  const both = `${one("rail", beat.rail)} · ${one("canal GitHub", beat["github-sync"])}`;
  return alarm(beat) ? `${both} — les états affichés sont figés.` : both;
}

export function HeartbeatBanner({ initial }: { initial: Heartbeat | null }) {
  const router = useRouter();
  const { data } = useSWR<Heartbeat>("/api/heartbeat", fetcher, {
    refreshInterval: POLL_MS,
    fallbackData: initial ?? undefined,
    // le battement rythme aussi le contenu : chaque tour repeint la page
    onSuccess: () => router.refresh(),
  });
  const beat = data ?? null;
  const alert = alarm(beat);

  return (
    <p
      id="heartbeat"
      data-stale={alert ? "true" : "false"}
      className={cn(
        "flex items-center gap-2 rounded-md px-3 py-1.5 text-sm",
        alert
          ? "border border-red-300 bg-red-50 font-semibold text-red-800"
          : "text-emerald-700",
      )}
    >
      {alert ? (
        <TriangleAlert className="size-4 shrink-0" />
      ) : (
        <Activity className="size-4 shrink-0" />
      )}
      {text(beat)}
    </p>
  );
}
