/**
 * La trajectoire d'un item, en une seule page.
 *
 * Tout ce que l'API rend de l'item y tient, dans l'ordre où on le lit :
 * l'en-tête et ses totaux, les questions ouvertes — la seule chose à faire
 * ici —, le graph avec le nœud courant marqué, le journal, les runs avec
 * leur durée et leurs tokens, les critères de succès du cycle tels que le
 * nœud `validate` les a cochés — sa preuve avec —, les effets,
 * et le workspace, dont les screenshots des agents s'affichent en preview.
 *
 * La page ne fait que poser les sections et leur donner ce que le serveur a
 * lu : chacune sonde ensuite `/api/item/<id>` pour son compte, et se repeint
 * seule quand sa tranche bouge. La coquille, elle, ne se rend qu'une fois.
 *
 * C'est la page la plus large du front, et elle doit tenir dans 360 px :
 * le graph et les tables glissent chacun dans leur bloc, jamais la page.
 */
import Link from "next/link";
import { notFound } from "next/navigation";

import { getItemView } from "@/lib/api";
import { ApiDown } from "@/components/api-down";
import {
  ItemCriteria,
  ItemEffects,
  ItemFiles,
  ItemGraph,
  ItemHeader,
  ItemJournal,
  ItemQuestions,
  ItemRuns,
} from "@/components/item-sections";

export const dynamic = "force-dynamic";

export default async function ItemPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  let view;
  try {
    view = await getItemView(Number(id));
  } catch (err) {
    if (String(err).includes("404")) {
      notFound();
    }
    return <ApiDown error={err} />;
  }

  const item = Number(id);

  return (
    <div className="flex flex-col gap-6">
      <ItemHeader id={item} initial={view.item} />
      <ItemQuestions id={item} initial={view.questions} />
      <ItemGraph id={item} initial={view.graph} />
      <ItemJournal
        id={item}
        initial={{ journal: view.journal, runs: view.runs }}
      />
      <ItemRuns id={item} initial={view.runs} />
      <ItemCriteria
        id={item}
        initial={{ criteria: view.criteria, validate: view.validate }}
      />
      <ItemEffects id={item} initial={view.effects} />
      <ItemFiles id={item} initial={view.files} />

      <p className="text-sm">
        <Link href="/items" className="underline">
          ← tous les items
        </Link>
      </p>
    </div>
  );
}
