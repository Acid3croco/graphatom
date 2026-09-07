/**
 * Ce qu'une page charge de l'API avant de se rendre.
 *
 * Une panne de l'API n'est pas une exception de rendu : elle revient en
 * `error`, et la page la montre à la place de son contenu, sans tomber ni
 * rien inventer.
 */
import { notFound } from "next/navigation";

export type Loaded<T> = { ok: true; value: T } | { ok: false; error: unknown };

/** Charger ce que la page montre, ou ce qui l'en a empêchée. */
export async function load<T>(fetch: () => Promise<T>): Promise<Loaded<T>> {
  try {
    return { ok: true, value: await fetch() };
  } catch (error) {
    return { ok: false, error };
  }
}

/**
 * Charger la chose que l'URL nomme. Un 404 de l'API n'est pas une panne :
 * c'est la page qui n'existe pas, et Next le dit à sa façon.
 */
export async function find<T>(fetch: () => Promise<T>): Promise<Loaded<T>> {
  const got = await load(fetch);
  if (!got.ok && String(got.error).includes("404")) {
    notFound();
  }
  return got;
}
