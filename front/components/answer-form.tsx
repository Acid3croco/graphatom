"use client";

/**
 * Répondre à une question ouverte : un bouton par option.
 *
 * Le clic POSTe vers `/api/answer`, la route serveur du front, qui relaie
 * l'unique porte d'écriture de l'API avec son jeton. Le navigateur ne
 * parle jamais à l'API directement — il ne connaît que le front.
 *
 * Une fois la réponse enregistrée, `router.refresh()` refait rendre la
 * page : la question passe d'ouverte à répondue sans rechargement.
 *
 * C'est le seul geste du front, et il se fait souvent au téléphone : les
 * boutons font 40 px de haut sous `md:` — la cible d'un doigt — et
 * retrouvent les 32 px de la variante `sm` au-delà.
 */
import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

import type { Question } from "@/lib/api";
import { Button } from "@/components/ui/button";

export function AnswerForm({ question }: { question: Question }) {
  const router = useRouter();
  const [message, setMessage] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  async function answer(option: string) {
    setMessage(null);
    try {
      const res = await fetch("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: question.question_id, option }),
      });
      const payload = await res.json();
      setMessage(payload.message ?? `réponse refusée (${res.status})`);
    } catch (err) {
      setMessage(`réponse impossible : ${err}`);
      return;
    }
    startTransition(() => router.refresh());
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      {question.options.map((option) => (
        <Button
          key={option}
          variant="outline"
          size="sm"
          className="h-10 px-4 md:h-8 md:px-3"
          disabled={pending}
          onClick={() => answer(option)}
        >
          {option}
        </Button>
      ))}
      {message && (
        <span className="text-sm text-muted-foreground">{message}</span>
      )}
    </div>
  );
}
