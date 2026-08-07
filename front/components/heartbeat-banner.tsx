"use client";

/**
 * Le battement du worker, en tête de chaque page.
 *
 * Un rail vivant tamponne à chaque tick ; sans battement depuis deux
 * minutes, plus rien ne tourne et tout ce que la page montre est figé. Ça
 * se dit en grand : l'absence de signal est le signal.
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

import type { Heartbeat } from "@/lib/api";
import { ago, moment } from "@/lib/format";
import { cn } from "@/lib/utils";

const POLL_MS = 5000;

const fetcher = (url: string) => fetch(url).then((res) => res.json());

function text(beat: Heartbeat | null): string {
  if (!beat) {
    return "rail injoignable — l'API ne répond pas, les états affichés sont figés.";
  }
  if (!beat.stale) {
    return `rail vivant il y a ${ago(beat.ago_s)}`;
  }
  if (beat.at) {
    return `rail à l'arrêt depuis ${moment(beat.at)} — les états affichés sont figés.`;
  }
  return "rail à l'arrêt — aucun battement en base : le worker n'a jamais tamponné.";
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
  const alert = !beat || beat.stale;

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
