/**
 * Les critères de succès de l'item, en cases à cocher.
 *
 * La source est `criteria.md` du workspace, que l'API rend tel quel : la
 * liste fermée et numérotée figée par le nœud `scope`. Une case cochée est
 * une case que le texte coche lui-même (`[x]`) — le front montre le
 * contrat, il ne prononce pas le verdict.
 */
import { Square, SquareCheck } from "lucide-react";

type Criterion = { number: string; text: string; checked: boolean };

// « 1. … » ouvre un critère ; les lignes suivantes le prolongent
const OPENS = /^\s*(\d+)[.)]\s+(.*)$/;

export function parse(criteria: string): Criterion[] {
  const items: Criterion[] = [];
  for (const line of criteria.split("\n")) {
    const opened = OPENS.exec(line);
    if (opened) {
      items.push({ number: opened[1], text: opened[2], checked: false });
    } else if (items.length && line.trim()) {
      items[items.length - 1].text += ` ${line.trim()}`;
    }
  }
  for (const item of items) {
    item.checked = /\[[xX]\]/.test(item.text);
    item.text = item.text.replace(/\[[ xX]\]\s*/, "");
  }
  return items;
}

export function CriteriaList({ criteria }: { criteria: string }) {
  const items = parse(criteria);
  if (!items.length) {
    return (
      <pre className="overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap">
        {criteria}
      </pre>
    );
  }
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => (
        <li key={item.number} className="flex items-start gap-2 text-sm">
          {item.checked ? (
            <SquareCheck className="mt-0.5 size-4 shrink-0 text-emerald-600" />
          ) : (
            <Square className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          )}
          <span>
            <b className="text-muted-foreground">{item.number}.</b> {item.text}
          </span>
        </li>
      ))}
    </ul>
  );
}
