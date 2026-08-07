/**
 * Les questions ouvertes — la surface d'écriture du front.
 *
 * Une carte par question : le contexte de son item, le texte, et un bouton
 * par option. Le clic passe par `/api/answer`, jamais par l'API en direct.
 */
import Link from "next/link";

import { getQuestions } from "@/lib/api";
import { moment } from "@/lib/format";
import { AnswerForm } from "@/components/answer-form";
import { ApiDown } from "@/components/api-down";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export const dynamic = "force-dynamic";

export default async function QuestionsPage() {
  let open;
  try {
    open = await getQuestions();
  } catch (err) {
    return <ApiDown error={err} />;
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold sm:text-xl">questions</h1>
      {!open.questions.length && (
        <p className="text-muted-foreground">
          Aucune question ouverte. La page se rafraîchit toute seule.
        </p>
      )}
      {open.questions.map((question) => (
        <Card key={question.question_id}>
          <CardHeader>
            <CardTitle className="text-sm font-normal break-words text-muted-foreground">
              [{question.question_id}]{" "}
              {question.issue_url ? (
                <a href={question.issue_url} className="underline">
                  {question.subject_key}
                </a>
              ) : (
                question.subject_key
              )}{" "}
              ·{" "}
              <Link href={`/item/${question.item_id}`} className="underline">
                item {question.item_id}
              </Link>
              {question.item_title ? ` « ${question.item_title} »` : ""} en{" "}
              <Badge variant="active">{question.item_state}</Badge> · pour{" "}
              {question.owner} · avant {moment(question.deadline)}
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="whitespace-pre-wrap text-sm">{question.text}</p>
            <AnswerForm question={question} />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
