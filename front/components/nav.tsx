/** La barre commune : les vues du rail, questions d'abord. */
import Link from "next/link";
import { CircleQuestionMark, List, Workflow } from "lucide-react";

export function Nav() {
  return (
    <nav className="flex items-center gap-4 text-sm text-muted-foreground">
      <Link href="/" className="flex items-center gap-1.5 hover:text-foreground">
        <CircleQuestionMark className="size-4" />
        questions
      </Link>
      <Link
        href="/items"
        className="flex items-center gap-1.5 hover:text-foreground"
      >
        <List className="size-4" />
        items
      </Link>
      <Link
        href="/graphs"
        className="flex items-center gap-1.5 hover:text-foreground"
      >
        <Workflow className="size-4" />
        graphs
      </Link>
    </nav>
  );
}
