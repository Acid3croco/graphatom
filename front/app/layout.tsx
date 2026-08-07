/**
 * La coquille commune : la barre de navigation, et le bandeau heartbeat.
 *
 * Le bandeau est ici et nulle part ailleurs — c'est ce qui le met sur
 * toutes les pages, à l'identique. Il sonde son battement pour son compte,
 * et ne commande le rendu de personne.
 *
 * La coquille se rend une fois, au chargement, et plus jamais : aucun
 * sondage ne passe par ici, donc la nav garde son DOM pendant que les
 * sections de données se repeignent sous elle.
 *
 * Tout est rendu à la demande : ces pages montrent un rail qui bouge, rien
 * ne se pré-rend au build (où l'API n'existe pas).
 */
import type { Metadata } from "next";

import "./globals.css";
import { getHeartbeat } from "@/lib/api";
import { HeartbeatBanner } from "@/components/heartbeat-banner";
import { Nav } from "@/components/nav";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "graphatom",
  description: "Le rail : items, trajectoires, questions ouvertes.",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const beat = await getHeartbeat().catch(() => null);

  return (
    <html lang="fr">
      <body className="mx-auto max-w-5xl px-4 py-6">
        <header className="mb-6 flex flex-col gap-2">
          <Nav />
          <HeartbeatBanner initial={beat} />
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
