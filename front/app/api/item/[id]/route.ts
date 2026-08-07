/**
 * Le détail d'un item, relayé pour le navigateur.
 *
 * Les sections de `/item/<id>` sondent cette route toutes les 5 s ; elles
 * ne peuvent pas sonder l'API directement — elle vit sur le réseau du
 * compose, sous un nom de service que le navigateur ne résout pas. Le
 * front est son seul interlocuteur, ici comme pour `/api/heartbeat`.
 *
 * Ce qu'elle rend est exactement ce que la page a rendu au premier octet :
 * le détail de l'API, plus le texte de `validate.md`.
 */
import { NextResponse } from "next/server";

import { getItemView } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  try {
    return NextResponse.json(await getItemView(Number(id)));
  } catch (err) {
    // un item qui n'existe pas n'est pas une API en panne : le sondage
    // doit pouvoir faire la différence
    const status = String(err).includes("404") ? 404 : 502;
    return NextResponse.json({ error: String(err) }, { status });
  }
}
