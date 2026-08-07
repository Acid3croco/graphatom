"use client";

/**
 * Les questions ouvertes du rail, vivantes pour leur compte.
 *
 * La liste sonde `/api/questions` toutes les cinq secondes : une question
 * posée pendant qu'on lit la page apparaît, une question répondue s'en va,
 * et rien d'autre de la page ne bouge — la carte qu'on est en train de
 * lire garde son focus et sa position.
 *
 * L'en-tête d'une carte cite un sujet et un titre d'item : le texte se
 * coupe au milieu d'un mot plutôt que de pousser la page hors de l'écran.
 */
import Link from "next/link";

import type { Question } from "@/lib/api";
import { moment } from "@/lib/format";
import { QUESTIONS_FEED, useQuestions } from "@/lib/live";
import { AnswerForm } from "@/components/answer-form";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function QuestionsList({ initial }: { initial: Question[] }) {
  const questions = useQuestions(initial);
  if (!questions.length) {
    return (
      <p className="text-muted-foreground">
        Aucune question ouverte. La page se rafraîchit toute seule.
      </p>
    );
  }

  return (
    <>
      {questions.map((question) => (
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
            <AnswerForm question={question} feed={QUESTIONS_FEED} />
          </CardContent>
        </Card>
      ))}
    </>
  );
}
