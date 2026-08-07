/**
 * Les questions ouvertes — la surface d'écriture du front.
 *
 * Une carte par question : le contexte de son item, le texte, et un bouton
 * par option. Le clic passe par `/api/answer`, jamais par l'API en direct.
 *
 * La page pose la première liste, les cartes vivent ensuite toutes seules.
 */
import { getQuestions } from "@/lib/api";
import { ApiDown } from "@/components/api-down";
import { QuestionsList } from "@/components/questions-list";

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
      <QuestionsList initial={open.questions} />
    </div>
  );
}
