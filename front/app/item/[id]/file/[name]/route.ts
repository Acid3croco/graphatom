/**
 * Les fichiers du workspace, relayés depuis l'API.
 *
 * L'API rend pour chaque fichier un `href` de la forme
 * `/item/<id>/file/<nom>` : le front monte exactement le même chemin, donc
 * la page pose ce `href` tel quel dans ses `<img>` et ses liens, sans rien
 * réécrire. Le navigateur ne joint que le front, l'API reste derrière.
 *
 * Lecture seule et sans cache : un screenshot d'agent change entre deux
 * tentatives du même nœud.
 */
import { API_URL } from "@/lib/config";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string; name: string }> },
) {
  const { id, name } = await params;
  const res = await fetch(
    `${API_URL}/item/${encodeURIComponent(id)}/file/${encodeURIComponent(name)}`,
    { cache: "no-store" },
  ).catch(() => null);

  if (!res || !res.ok) {
    return new Response("introuvable", { status: res?.status ?? 502 });
  }
  return new Response(res.body, {
    status: 200,
    headers: {
      "Content-Type": res.headers.get("Content-Type") ?? "text/plain",
      "Cache-Control": "no-store",
    },
  });
}
