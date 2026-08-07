/**
 * La seule écriture du front : le relais de `POST /answer`.
 *
 * Le front ne décide rien — il transporte. Le jeton anti-rejeu que l'API
 * attend est celui de `GET /api/questions` : on le lit au moment de
 * répondre, donc il est toujours celui du serveur en cours, même après un
 * redémarrage de l'API qui en tire un nouveau.
 *
 * Deux formats en entrée, comme le formulaire stdlib : du JSON, ou de
 * l'urlencodé. Un seul chemin derrière, et la réponse de l'API rendue
 * telle quelle — `{ok, message}`, avec son statut.
 */
import { NextResponse } from "next/server";

import { getQuestions } from "@/lib/api";
import { API_URL } from "@/lib/config";

export const dynamic = "force-dynamic";

async function fields(request: Request): Promise<Record<string, string>> {
  const type = request.headers.get("Content-Type") ?? "";
  if (type.includes("application/json")) {
    return await request.json();
  }
  const form = await request.formData();
  return Object.fromEntries(
    [...form.entries()].map(([k, v]) => [k, String(v)]),
  );
}

export async function POST(request: Request) {
  let question_id: number;
  let option: string;
  try {
    const posted = await fields(request);
    question_id = Number(posted.question_id);
    option = String(posted.option);
    if (!Number.isInteger(question_id) || !option || option === "undefined") {
      throw new Error("question_id et option sont requis");
    }
  } catch (err) {
    return NextResponse.json(
      { ok: false, message: `requête illisible : ${err}` },
      { status: 400 },
    );
  }

  try {
    const { token } = await getQuestions();
    const res = await fetch(`${API_URL}/answer`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({ question_id, option, token }),
    });
    return NextResponse.json(await res.json(), { status: res.status });
  } catch (err) {
    return NextResponse.json(
      { ok: false, message: `l'API n'a pas répondu : ${err}` },
      { status: 502 },
    );
  }
}
