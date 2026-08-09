/** La tranche nouvelle de la trace d'un run, relayée pour le navigateur. */
import { NextResponse } from "next/server";

import { getRunTrace } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string; run: string }> },
) {
  const { id, run } = await params;
  try {
    return NextResponse.json(
      await getRunTrace(Number(id), Number(run), new URL(request.url).search),
    );
  } catch (err) {
    const status = String(err).includes("404") ? 404 : 502;
    return NextResponse.json({ error: String(err) }, { status });
  }
}
