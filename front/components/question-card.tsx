/**
 * Une carte de question : son en-tête, son texte, puis ses boutons.
 *
 * L'en-tête est ce que la page en dit — le rail cite l'item, la page de
 * l'item cite le nœud — ; le reste est pareil partout. `wrap` coupe un
 * en-tête long au milieu d'un mot plutôt que de pousser la page hors de
 * l'écran : c'est celui du rail, qui cite un sujet et un titre d'item.
 */
import type { ReactNode } from "react";

import type { Question } from "@/lib/api";
import { cn } from "@/lib/utils";
import { AnswerForm } from "@/components/answer-form";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function QuestionCard({
  question,
  feed,
  wrap = false,
  children,
}: {
  question: Question;
  /** La route que la page sonde, relue dès que la réponse est enregistrée. */
  feed: string;
  wrap?: boolean;
  children: ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle
          className={cn(
            "text-sm font-normal",
            wrap && "break-words",
            "text-muted-foreground",
          )}
        >
          {children}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="whitespace-pre-wrap text-sm">{question.text}</p>
        <AnswerForm question={question} feed={feed} />
      </CardContent>
    </Card>
  );
}
