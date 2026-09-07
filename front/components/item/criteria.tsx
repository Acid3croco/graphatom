"use client";

/** Les critères du cycle, tels que le nœud `validate` les a cochés. */
import { useItem } from "@/lib/live";
import { CriteriaList } from "@/components/criteria-list";

export function ItemCriteria({
  id,
  initial,
}: {
  id: number;
  initial: { criteria: string | null; validate: string | null };
}) {
  // les deux fichiers vont ensemble : la liste vient de `criteria.md`, la
  // coche de `validate.md`, et le validate arrive au tour où il est écrit
  const { criteria, validate } = useItem(
    id,
    (view) => ({ criteria: view.criteria, validate: view.validate }),
    initial,
  );
  if (!criteria) {
    return null;
  }

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">critères de succès</h2>
      <CriteriaList criteria={criteria} validate={validate} />
    </section>
  );
}
