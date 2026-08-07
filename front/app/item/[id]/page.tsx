/**
 * La trajectoire d'un item, en une seule page.
 *
 * Tout ce que l'API rend de l'item y tient, dans l'ordre où on le lit :
 * l'en-tête et ses totaux, les questions ouvertes — la seule chose à faire
 * ici —, le graph avec le nœud courant marqué, le journal, les runs avec
 * leur durée et leurs tokens, les critères de succès du cycle, les effets,
 * et le workspace, dont les screenshots des agents s'affichent en preview.
 */
import Link from "next/link";
import { notFound } from "next/navigation";

import { getItem, type Run, type Usage } from "@/lib/api";
import { cost, count, duration, moment, tokens } from "@/lib/format";
import { AnswerForm } from "@/components/answer-form";
import { ApiDown } from "@/components/api-down";
import { CriteriaList } from "@/components/criteria-list";
import { GraphSvg } from "@/components/graph-svg";
import { Badge, tone } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export const dynamic = "force-dynamic";

// jsonb ne garde pas l'ordre des clés : le post-mortem d'abord, la trace après
const RESULT_ORDER = ["outcome", "exit_code", "timeout", "error"];
const RESULT_APART = new Set(["log_tail", "usage"]);

/** Le résultat d'un run : les champs en ligne, la queue de log en bloc. */
function Result({ run }: { run: Run }) {
  const result = run.result;
  if (!result) {
    return null;
  }
  const keys = [
    ...RESULT_ORDER.filter((k) => k in result),
    ...Object.keys(result)
      .filter((k) => !RESULT_ORDER.includes(k) && !RESULT_APART.has(k))
      .sort(),
  ];
  const tail = result.log_tail;
  return (
    <div className="flex flex-col gap-1">
      <span>
        {keys.map((key) => (
          <span key={key} className="mr-2">
            {key} <b>{String(result[key])}</b>
          </span>
        ))}
      </span>
      {typeof tail === "string" && tail && (
        <pre className="max-h-64 overflow-auto rounded-md bg-muted p-2 text-xs break-words whitespace-pre-wrap">
          {tail}
        </pre>
      )}
    </div>
  );
}

export default async function ItemPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  let detail;
  try {
    detail = await getItem(Number(id));
  } catch (err) {
    if (String(err).includes("404")) {
      notFound();
    }
    return <ApiDown error={err} />;
  }

  const { item, graph, journal, runs, effects, questions, criteria, files } =
    detail;
  const open = questions.filter((q) => q.state === "open");
  const images = files.filter((f) => f.name.endsWith(".png"));
  const total = tokens(item.usage);
  // le journal dit quel run a produit le step, le run porte son usage : la
  // jointure se fait ici, une fois, et les lignes n'ont plus qu'à lire
  const byRun = new Map(runs.map((run) => [run.id, run.usage]));
  const usages = journal.map((entry) =>
    entry.run_id === null ? undefined : byRun.get(entry.run_id),
  );
  // les totaux du pied de table sont la somme de ce que les lignes montrent,
  // pas un second calcul : un step sans LLM y pèse zéro, et ça se voit
  const sum = (key: string) =>
    usages.reduce((acc, usage: Usage | undefined) => acc + (usage?.[key] ?? 0), 0);

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-2">
        <h1 className="text-xl font-semibold">
          item {item.id}
          <span className="ml-2 text-sm font-normal text-muted-foreground">
            {item.issue_url ? (
              <a href={item.issue_url} className="underline">
                {item.subject_key}
              </a>
            ) : (
              item.subject_key
            )}
            {item.pr_url && (
              <>
                {" · "}
                <a href={item.pr_url} className="underline">
                  PR
                </a>
              </>
            )}
            {item.title ? ` · « ${item.title} »` : ""} · g{item.generation} ·{" "}
            {item.graph} · rév. {item.revision.slice(0, 12)}…
          </span>
        </h1>
        <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <Badge variant={tone(item.status)}>{item.state}</Badge>
          <span>v{item.version}</span>
          <span>· passage {item.cycle}</span>
          <span>· escalades restantes {item.escalations}</span>
          <span>· lignée restante {item.lineage_budget}</span>
          <span>
            ·{" "}
            {item.terminal_at
              ? `terminé ${moment(item.terminal_at, true)}`
              : `wall deadline ${moment(item.wall_deadline)}`}
          </span>
        </p>
        <p className="text-sm text-muted-foreground">
          temps total {duration(item.duration_s)}
          {item.terminal_at ? "" : " (en cours)"} ·{" "}
          {total || "aucun usage rapporté"}
        </p>
      </section>

      {open.length > 0 && (
        <section className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">questions ouvertes</h2>
          {open.map((question) => (
            <Card key={question.question_id}>
              <CardHeader>
                <CardTitle className="text-sm font-normal text-muted-foreground">
                  [{question.question_id}] {question.node} · pour{" "}
                  {question.owner} · avant {moment(question.deadline)}
                </CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                <p className="whitespace-pre-wrap text-sm">{question.text}</p>
                <AnswerForm question={question} />
              </CardContent>
            </Card>
          ))}
        </section>
      )}

      <section className="flex flex-col gap-2">
        <h2 className="text-lg font-semibold">graph</h2>
        {/* la clé est l'item, pas les données : le sondage repeint le graph
            toutes les cinq secondes et ne doit pas remonter la visionneuse,
            qui garde le pan, le zoom et l'orientation de cet item-là */}
        <GraphSvg key={item.id} graph={graph} item={item.id} />
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-lg font-semibold">journal</h2>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>v</TableHead>
              <TableHead>à</TableHead>
              <TableHead>durée</TableHead>
              <TableHead>événement</TableHead>
              <TableHead>transition</TableHead>
              <TableHead>issue</TableHead>
              <TableHead>run</TableHead>
              <TableHead>tokens in</TableHead>
              <TableHead>tokens out</TableHead>
              <TableHead>coût $</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {journal.map((entry, index) => {
              const usage = usages[index];
              return (
                <TableRow key={entry.version}>
                  <TableCell>v{entry.version}</TableCell>
                  <TableCell className="whitespace-nowrap">
                    {moment(entry.at, true)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    {duration(entry.duration_s)}
                  </TableCell>
                  <TableCell>{entry.kind}</TableCell>
                  <TableCell>
                    {entry.from_state ? `${entry.from_state} → ` : ""}
                    {entry.to_state}
                  </TableCell>
                  <TableCell>{entry.outcome ?? ""}</TableCell>
                  <TableCell>{entry.run_id ?? ""}</TableCell>
                  {/* le détail du cache tient dans l'infobulle : une colonne
                      de plus dirait la même chose en prenant la place */}
                  <TableCell
                    className="whitespace-nowrap"
                    title={tokens(usage) || undefined}
                  >
                    {count(usage?.input_tokens)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    {count(usage?.output_tokens)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    {cost(usage?.total_cost_usd)}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
          <TableFooter>
            <TableRow>
              <TableCell colSpan={7}>total</TableCell>
              <TableCell className="whitespace-nowrap">
                {count(sum("input_tokens"))}
              </TableCell>
              <TableCell className="whitespace-nowrap">
                {count(sum("output_tokens"))}
              </TableCell>
              <TableCell className="whitespace-nowrap">
                {cost(sum("total_cost_usd"))}
              </TableCell>
            </TableRow>
          </TableFooter>
        </Table>
      </section>

      {runs.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="text-lg font-semibold">runs</h2>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>run</TableHead>
                <TableHead>nœud</TableHead>
                <TableHead>passage</TableHead>
                <TableHead>tentative</TableHead>
                <TableHead>statut</TableHead>
                <TableHead>issue</TableHead>
                <TableHead>durée</TableHead>
                <TableHead>tokens</TableHead>
                <TableHead>résultat</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.id}>
                  <TableCell>{run.id}</TableCell>
                  <TableCell>{run.node}</TableCell>
                  <TableCell>{run.cycle}</TableCell>
                  <TableCell>{run.attempt}</TableCell>
                  <TableCell>
                    <Badge variant={tone(run.status)}>{run.status}</Badge>
                  </TableCell>
                  <TableCell>{run.outcome ?? ""}</TableCell>
                  <TableCell className="whitespace-nowrap">
                    {duration(run.duration_s)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    {tokens(run.usage)}
                  </TableCell>
                  <TableCell className="text-xs">
                    <Result run={run} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>
      )}

      {criteria && (
        <section className="flex flex-col gap-2">
          <h2 className="text-lg font-semibold">critères de succès</h2>
          <CriteriaList criteria={criteria} />
        </section>
      )}

      {effects.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="text-lg font-semibold">effets</h2>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>clé logique</TableHead>
                <TableHead>cible</TableHead>
                <TableHead>observation</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {effects.map((effect) => (
                <TableRow key={effect.op_id}>
                  <TableCell>
                    <code className="text-xs">{effect.logical_key}</code>
                  </TableCell>
                  <TableCell className="text-xs break-all">
                    {effect.target_uri}
                  </TableCell>
                  <TableCell>
                    <Badge variant={tone(effect.observation)}>
                      {effect.observation}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>
      )}

      {files.length > 0 && (
        <section className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">workspace</h2>
          {images.map((file) => (
            // les screenshots des agents = la preview ; le `href` vient de
            // l'API et le front monte le même chemin, rien à réécrire
            <figure key={file.name} className="flex flex-col gap-1">
              <figcaption className="text-sm text-muted-foreground">
                {file.name}
              </figcaption>
              <a href={file.href}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={file.href}
                  alt={file.name}
                  className="max-w-full rounded-md border"
                />
              </a>
            </figure>
          ))}
          <ul className="flex flex-col gap-1 text-sm">
            {files.map((file) => (
              <li key={file.name}>
                <a href={file.href} className="underline">
                  {file.name}
                </a>{" "}
                <span className="text-muted-foreground">({file.size} o)</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="text-sm">
        <Link href="/items" className="underline">
          ← tous les items
        </Link>
      </p>
    </div>
  );
}
