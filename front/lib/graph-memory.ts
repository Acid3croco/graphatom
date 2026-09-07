/**
 * Ce que la visionneuse du graph garde d'un item : son orientation, et son
 * cadrage s'il en a choisi un.
 *
 * Ce choix se range dans `localStorage`, une entrée par item, sous
 * `graph-view:<item>`. C'est ce qui le fait survivre au rechargement de la
 * page — le seul moment où la mémoire du composant s'en va, puisque le
 * sondage, lui, ne le remonte pas. Une entrée absente, illisible ou d'une
 * autre forme est ignorée en silence — la visionneuse repart de
 * l'ajustement initial, ce n'est pas une panne.
 */
import type { Layout, Orient } from "@/lib/graph-layout";

/** Le `viewBox` du `<svg>` : ce que l'œil regarde. */
export type View = { x: number; y: number; w: number; h: number };
/** Ce qu'un item garde : son orientation, et son cadrage s'il en a un. */
type Kept = { orient: Orient; view: View | null };

/** L'ajustement initial : tout le dessin, et rien de plus. */
export const fit = (plan: Layout): View => ({
  x: 0,
  y: 0,
  w: plan.width,
  h: plan.height,
});

/** L'entrée de rangement d'un item. */
const slot = (item: number) => `graph-view:${item}`;

const num = (n: unknown): n is number =>
  typeof n === "number" && Number.isFinite(n);

/** Ce qui est rangé pour cet item, ou `null` si rien de lisible n'y est. */
export function read(item: number): Kept | null {
  let kept: Record<string, unknown> | null = null;
  try {
    kept = JSON.parse(localStorage.getItem(slot(item)) ?? "");
  } catch {
    // rien de rangé, ou du texte qui n'est pas du json : on repart de zéro
    return null;
  }
  const orient = kept?.orient;
  if (orient !== "LR" && orient !== "TB") {
    return null;
  }
  const { x, y, w, h } = kept ?? {};
  const view = num(x) && num(y) && num(w) && num(h) ? { x, y, w, h } : null;
  return { orient, view };
}

/** Ranger la vue de cet item. Un stockage qui refuse ne casse rien. */
export function write(item: number, kept: Kept) {
  try {
    localStorage.setItem(
      slot(item),
      JSON.stringify({ orient: kept.orient, ...kept.view }),
    );
  } catch {
    // stockage plein ou interdit : la vue reste juste pour cette page-ci
  }
}
