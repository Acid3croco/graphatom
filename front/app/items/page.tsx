/**
 * Tous les items : le titre vers la page de l'item, l'état courant, la
 * trajectoire.
 *
 * Le titre vient de la base — le canal l'y a rangé à l'admission — et mène
 * au détail de l'item, son chemin naturel. Le lien vers l'issue GitHub est
 * porté par le numéro, dans sa propre colonne, comme sur les pages stdlib.
 * Un sujet d'un autre canal n'a ni titre ni numéro : la cellule du titre
 * montre alors la clé du sujet, celle du numéro reste vide.
 *
 * La page pose la première liste, le tableau vit ensuite tout seul.
 */
import { getItems } from "@/lib/api";
import { ApiDown } from "@/components/api-down";
import { ItemsTable } from "@/components/items-table";

export const dynamic = "force-dynamic";

export default async function ItemsPage() {
  let items;
  try {
    items = await getItems();
  } catch (err) {
    return <ApiDown error={err} />;
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">items</h1>
      <ItemsTable initial={items} />
    </div>
  );
}
