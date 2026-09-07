"use client";

/** Les questions ouvertes de l'item — la seule chose à faire ici. */
import { useItem, itemFeed } from "@/lib/live";
import type { Question } from "@/lib/api";
import { moment } from "@/lib/format";
import { QuestionCard } from "@/components/question-card";

export function ItemQuestions({
  id,
  initial,
}: {
  id: number;
  initial: Question[];
}) {
  const questions = useItem(id, (view) => view.questions, initial);
  const open = questions.filter((q) => q.state === "open");
  if (!open.length) {
    return null;
  }

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-base font-semibold sm:text-lg">questions ouvertes</h2>
      {open.map((question) => (
        <QuestionCard
          key={question.question_id}
          question={question}
          feed={itemFeed(id)}
        >
          [{question.question_id}] {question.node} · pour {question.owner} ·
          avant {moment(question.deadline)}
        </QuestionCard>
      ))}
    </section>
  );
}
