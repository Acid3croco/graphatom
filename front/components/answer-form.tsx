"use client";

/**
 * Répondre à une question ouverte : un bouton par option.
 *
 * Le clic POSTe vers `/api/answer`, la route serveur du front, qui relaie
 * l'unique porte d'écriture de l'API avec son jeton. Le navigateur ne
 * parle jamais à l'API directement — il ne connaît que le front.
 *
 * Une fois la réponse enregistrée, la route que la page sonde (`feed`) est
 * relue tout de suite, sans attendre le tour suivant : la question passe
 * d'ouverte à répondue sous les yeux, et rien d'autre de la page ne bouge.
 */
import { useState } from "react";
import { useSWRConfig } from "swr";

import type { Question } from "@/lib/api";
import { Button } from "@/components/ui/button";

export function AnswerForm({
  question,
  feed,
}: {
  question: Question;
  feed: string;
}) {
  const { mutate } = useSWRConfig();
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function answer(option: string) {
    setMessage(null);
    setPending(true);
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
    } finally {
      setPending(false);
    }
    mutate(feed);
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      {question.options.map((option) => (
        <Button
          key={option}
          variant="outline"
          size="sm"
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
