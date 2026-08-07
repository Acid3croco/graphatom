/**
 * La table du rail, et son conteneur qui scrolle.
 *
 * Le scroll horizontal est porté par le `div`, jamais par la page : une
 * table de dix colonnes reste lisible sur un téléphone en glissant du
 * doigt dans son bloc, et rien ne déborde autour. Les cellules se
 * resserrent sous `md:` pour que ce glissement soit court.
 */
import * as React from "react";

import { cn } from "@/lib/utils";

function Table({ className, ...props }: React.ComponentProps<"table">) {
  return (
    <div className="w-full overflow-x-auto">
      <table
        className={cn("w-full caption-bottom text-sm", className)}
        {...props}
      />
    </div>
  );
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return <thead className={cn("[&_tr]:border-b", className)} {...props} />;
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return <tbody className={className} {...props} />;
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      className={cn("border-t font-medium [&_tr]:border-0", className)}
      {...props}
    />
  );
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr className={cn("border-b last:border-0", className)} {...props} />
  );
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      className={cn(
        "px-2 py-2 text-left align-middle font-semibold text-muted-foreground whitespace-nowrap md:px-3",
        className,
      )}
      {...props}
    />
  );
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td className={cn("px-2 py-2 align-top md:px-3", className)} {...props} />
  );
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableRow,
  TableHead,
  TableCell,
};
