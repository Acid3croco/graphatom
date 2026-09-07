/** L'API injoignable : la page le dit, elle ne tombe pas et n'invente rien. */
import type { ReactNode } from "react";
import { TriangleAlert } from "lucide-react";

import type { Loaded } from "@/lib/load";

export function ApiDown({ error }: { error: unknown }) {
  return (
    <p className="flex items-start gap-2 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-800">
      <TriangleAlert className="mt-0.5 size-4 shrink-0" />
      <span>
        API injoignable — rien à afficher.
        <br />
        <code className="text-xs">{String(error)}</code>
      </span>
    </p>
  );
}

/** La page rendue avec ce qui a été chargé — ou, à sa place, la panne. */
export function shown<T>(
  got: Loaded<T>,
  page: (value: T) => ReactNode,
): ReactNode {
  return got.ok ? page(got.value) : <ApiDown error={got.error} />;
}
