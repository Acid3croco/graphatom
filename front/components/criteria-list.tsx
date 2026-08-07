/**
 * Les critères de succès de l'item, en cases à cocher.
 *
 * Deux fichiers du workspace se rencontrent ici. `criteria.md` porte la
 * liste fermée et numérotée figée par le nœud `scope` : le contrat du
 * cycle. `validate.md`, quand le nœud `validate` est passé, coche chaque
 * numéro et écrit le constaté qui le tranche — une ligne par critère,
 * `- [x] <n>. <constaté>`. La coche affichée est celle du validate, la
 * preuve affichée est son constaté : le front montre le verdict, il ne le
 * prononce pas. Sans `validate.md`, les carrés restent vides, honnêtes.
 *
 * Un critère cite souvent une commande ou un sha : le texte se coupe au
 * milieu d'un mot plutôt que de pousser la page hors de l'écran.
 */
import { Square, SquareCheck } from "lucide-react";

type Criterion = { number: string; text: string; checked: boolean };

type Verdict = { checked: boolean; evidence: string };

// « 1. … » ouvre un critère ; les lignes suivantes le prolongent
const OPENS = /^\s*(\d+)[.)]\s+(.*)$/;

// « - [x] 1. … » : la coche du validate sur un critère, et sa preuve
const VERDICT = /^\s*[-*]\s*\[([ xX])\]\s*(\d+)[.)]\s*(.*)$/;

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

/**
 * Les verdicts de `validate.md`, par numéro de critère.
 *
 * Le nœud `validate` écrit une ligne par critère, jamais un paragraphe :
 * une ligne qui ne s'ouvre pas sur une case n'est pas de lui — un titre,
 * un `outcome:`, une note de fin — et ne prolonge donc aucun constaté.
 *
 * Un même numéro peut revenir quand le validate repasse au cycle suivant.
 * La dernière occurrence fait foi : elle écrase la précédente.
 */
export function verdicts(validate: string): Map<string, Verdict> {
  const said = new Map<string, Verdict>();
  for (const line of validate.split("\n")) {
    const opened = VERDICT.exec(line);
    if (opened) {
      const checked = opened[1].toLowerCase() === "x";
      said.set(opened[2], { checked, evidence: opened[3].trim() });
    }
  }
  return said;
}

export function CriteriaList({
  criteria,
  validate,
}: {
  criteria: string;
  validate?: string | null;
}) {
  const items = parse(criteria);
  if (!items.length) {
    return (
      <pre className="overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap">
        {criteria}
      </pre>
    );
  }
  const said = validate ? verdicts(validate) : new Map<string, Verdict>();
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => {
        // un numéro que le validate n'a pas vu garde ce que le texte dit
        const verdict = said.get(item.number);
        const checked = verdict ? verdict.checked : item.checked;
        return (
          <li key={item.number} className="flex items-start gap-2 text-sm">
            {checked ? (
              <SquareCheck className="mt-0.5 size-4 shrink-0 text-emerald-600" />
            ) : (
              <Square className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            )}
            <span className="flex min-w-0 flex-col gap-0.5 break-words">
              <span>
                <b className="text-muted-foreground">{item.number}.</b>{" "}
                {item.text}
              </span>
              {verdict?.evidence && (
                <span className="text-xs text-muted-foreground">
                  constaté : {verdict.evidence}
                </span>
              )}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
