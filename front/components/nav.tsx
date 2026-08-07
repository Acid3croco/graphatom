/**
 * La barre commune : les deux vues du rail, questions d'abord.
 *
 * Sur téléphone elle se resserre — police plus petite, écart plus court —
 * mais les deux liens gardent 40 px de haut : c'est la taille d'un doigt,
 * et une barre qu'on rate au doigt ne sert à rien.
 */
import Link from "next/link";
import { CircleQuestionMark, List } from "lucide-react";

// hauteur de cible tactile sous `sm:`, hauteur du texte au-dessus
const LINK = "flex min-h-10 items-center gap-1.5 hover:text-foreground sm:min-h-0";

export function Nav() {
  return (
    <nav className="flex items-center gap-3 text-xs text-muted-foreground sm:gap-4 sm:text-sm">
      <Link href="/" className={LINK}>
        <CircleQuestionMark className="size-4" />
        questions
      </Link>
      <Link href="/items" className={LINK}>
        <List className="size-4" />
        items
      </Link>
    </nav>
  );
}
