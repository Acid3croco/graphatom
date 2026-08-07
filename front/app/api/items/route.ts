/**
 * La liste des items, relayée pour le navigateur.
 *
 * Le tableau de `/items` la sonde toutes les 5 s. Comme partout, le
 * navigateur ne joint que le front ; l'API reste derrière.
 */
import { NextResponse } from "next/server";

import { getItems } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(await getItems());
  } catch (err) {
    return NextResponse.json({ error: String(err) }, { status: 502 });
  }
}
