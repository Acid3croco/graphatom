import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium",
  {
    variants: {
      variant: {
        default: "border-transparent bg-secondary text-secondary-foreground",
        active: "border-emerald-300 bg-emerald-50 text-emerald-800",
        terminal: "border-transparent bg-muted text-muted-foreground",
        warn: "border-amber-300 bg-amber-50 text-amber-800",
        alert: "border-red-300 bg-red-50 text-red-800",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

// les statuts que le noyau écrit, rangés dans les cinq tons du badge
const TONES: Record<string, VariantProps<typeof badgeVariants>["variant"]> = {
  active: "active",
  applied: "active",
  done: "active",
  terminal: "terminal",
  succeeded: "active",
  running: "warn",
  uncertain: "warn",
  open: "warn",
  faulted: "alert",
  stale: "alert",
  superseded: "alert",
  rejected: "alert",
};

/** Le ton d'un statut — le gris neutre pour tout ce qui n'est pas listé. */
export function tone(status: string): VariantProps<typeof badgeVariants>["variant"] {
  return TONES[status] ?? "default";
}

export { Badge, badgeVariants };
