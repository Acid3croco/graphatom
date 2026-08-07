/**
 * Les questions ouvertes, relayées pour le navigateur.
 *
 * La page `/` et la section « questions ouvertes » d'un item la sondent
 * toutes les 5 s. Seules les questions sortent : le jeton anti-rejeu que
 * l'API rend avec elles ne quitte pas le serveur — c'est `/api/answer` qui
 * le lit, au moment de répondre.
 */
import { NextResponse } from "next/server";

import { getQuestions } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const { questions } = await getQuestions();
    return NextResponse.json(questions);
  } catch (err) {
    return NextResponse.json({ error: String(err) }, { status: 502 });
  }
}
