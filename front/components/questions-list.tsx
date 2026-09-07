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
import { QuestionCard } from "@/components/question-card";
import { Badge } from "@/components/ui/badge";

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
        <QuestionCard
          key={question.question_id}
          question={question}
          feed={QUESTIONS_FEED}
          wrap
        >
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
        </QuestionCard>
      ))}
    </>
  );
}
