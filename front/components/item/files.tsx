"use client";

/** Le workspace : les screenshots des agents en preview, puis la liste. */
import { useItem } from "@/lib/live";
import type { WorkspaceFile } from "@/lib/api";

export function ItemFiles({
  id,
  initial,
}: {
  id: number;
  initial: WorkspaceFile[];
}) {
  const files = useItem(id, (view) => view.files, initial);
  if (!files.length) {
    return null;
  }
  const images = files.filter((f) => f.name.endsWith(".png"));

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-base font-semibold sm:text-lg">workspace</h2>
      {images.map((file) => (
        // les screenshots des agents = la preview ; le `href` vient de
        // l'API et le front monte le même chemin, rien à réécrire
        <figure key={file.name} className="flex flex-col gap-1">
          <figcaption className="text-sm break-all text-muted-foreground">
            {file.name}
          </figcaption>
          <a href={file.href}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={file.href}
              alt={file.name}
              className="h-auto w-full rounded-md border md:w-auto md:max-w-full"
            />
          </a>
        </figure>
      ))}
      <ul className="flex flex-col gap-1 text-sm break-words">
        {files.map((file) => (
          <li key={file.name}>
            <a href={file.href} className="underline">
              {file.name}
            </a>{" "}
            <span className="text-muted-foreground">({file.size} o)</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
