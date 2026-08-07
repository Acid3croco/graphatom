/**
 * Le battement, relayé pour le navigateur.
 *
 * Le bandeau sonde toutes les 5 s ; il ne peut pas sonder l'API
 * directement — elle vit sur le réseau du compose, sous un nom de service
 * que le navigateur ne résout pas. Le front est donc son seul
 * interlocuteur, ici comme pour `/api/answer`.
 */
import { NextResponse } from "next/server";

import { getHeartbeat } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(await getHeartbeat());
  } catch (err) {
    return NextResponse.json({ error: String(err) }, { status: 502 });
  }
}
