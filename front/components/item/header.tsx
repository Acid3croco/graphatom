"use client";

/** L'en-tête de l'item : son identité, son état, ses totaux. */
import { useItem } from "@/lib/live";
import type { ItemHead, UsageSplit } from "@/lib/api";
import { cost, duration, moment, tokens } from "@/lib/format";
import { Badge, tone } from "@/components/ui/badge";

export function ItemHeader({ id, initial }: { id: number; initial: ItemHead }) {
  const item = useItem(id, (view) => view.item, initial);
  const total = tokens(item.usage);

  return (
    <section className="flex flex-col gap-2">
      <h1 className="text-lg font-semibold break-words sm:text-xl">
        item {item.id}
        <span className="ml-2 text-xs font-normal text-muted-foreground sm:text-sm">
          {item.issue_url ? (
            <a href={item.issue_url} className="underline">
              {item.subject_key}
            </a>
          ) : (
            item.subject_key
          )}
          {item.pr_url && (
            <>
              {" · "}
              <a href={item.pr_url} className="underline">
                PR
              </a>
            </>
          )}
          {item.title ? ` · « ${item.title} »` : ""} · g{item.generation} ·{" "}
          {item.graph} · rév. {item.revision.slice(0, 12)}…
        </span>
      </h1>
      <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
        <Badge variant={tone(item.status)}>{item.state}</Badge>
        <span>v{item.version}</span>
        <span>· passage {item.cycle}</span>
        <span>· escalades restantes {item.escalations}</span>
        <span>· lignée restante {item.lineage_budget}</span>
        <span>
          ·{" "}
          {item.terminal_at
            ? `terminé ${moment(item.terminal_at, true)}`
            : `wall deadline ${moment(item.wall_deadline)}`}
        </span>
      </p>
      <p className="text-sm text-muted-foreground">
        temps total {duration(item.duration_s)}
        {item.terminal_at ? "" : " (en cours)"} ·{" "}
        {total || "aucun usage rapporté"}
      </p>
      <p className="text-sm text-muted-foreground">
        coût rapporté {cost(item.reported_cost_usd) ? `$${cost(item.reported_cost_usd)}` : "inconnu"}
      </p>
      <CostSplit split={item.usage_split} />
    </section>
  );
}

/**
 * Les deux bouts de l'haltère, l'un en face de l'autre.
 *
 * Le total d'un item mélange ce qu'on paie pour *produire* — les candidats
 * d'un fan-out, tous, perdants compris — et ce qu'on paie pour *choisir* —
 * le nœud arbitre. Mêlés, la question qui compte n'a plus de réponse : le
 * jugement vaut-il ce qu'il coûte ? Séparés, elle se lit d'un coup d'œil.
 *
 * Un item qui n'est passé ni par un fan-out ni par un juge n'a rien à
 * comparer : la ligne n'existe pas.
 */
function CostSplit({ split }: { split: UsageSplit | undefined }) {
  const judgement = tokens(split?.judgement);
  const candidates = tokens(split?.candidates);
  if (!judgement && !candidates) {
    return null;
  }
  return (
    <p
      data-testid="cout-jugement"
      className="text-sm text-muted-foreground"
    >
      jugement {judgement || "aucun usage rapporté"} · candidats{" "}
      {candidates || "aucun usage rapporté"}
    </p>
  );
}
