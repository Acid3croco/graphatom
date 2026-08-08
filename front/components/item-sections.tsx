"use client";

/**
 * Les sections de la page d'un item, chacune vivante pour son compte.
 *
 * Une section = un composant client qui sonde `/api/item/<id>` et n'y lit
 * que sa tranche. Le tour de sondage est commun — une requête toutes les
 * cinq secondes pour toute la page — mais le rendu ne l'est pas : une
 * transition repeint le journal et le graph, et laisse les autres
 * sections exactement où elles étaient, sans les remonter. C'est ce qui
 * fait survivre le cadrage du graph, le focus d'un bouton et une saisie en
 * cours, sans rien avoir à sauver ni à restaurer.
 *
 * Chaque section reçoit sa tranche déjà rendue par le serveur (`initial`)
 * : la page est juste au premier octet, avant le premier tour.
 *
 * Une section vide ne rend rien — et se met à exister toute seule quand sa
 * tranche arrive, un premier run par exemple.
 *
 * C'est la page la plus large du front, et elle doit tenir dans 360 px :
 * le graph et les tables glissent chacun dans leur bloc, jamais la page.
 */
import { Fragment, useState } from "react";

import { useItem, itemFeed } from "@/lib/live";
import type {
  Effect,
  Graph,
  ItemHead,
  JournalEntry,
  Question,
  Run,
  Usage,
  Variant,
  WorkspaceFile,
} from "@/lib/api";
import { running, steps, type Candidate } from "@/lib/fanout";
import { cost, count, duration, moment, tokens } from "@/lib/format";
import { AnswerForm } from "@/components/answer-form";
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
    <div className="flex flex-col gap-1 break-words">
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

/** L'en-tête : l'identité de l'item, son état, ses totaux. */
export function ItemHeader({ id, initial }: { id: number; initial: ItemHead }) {
  const item = useItem(id, (view) => view.item, initial);
  const total = tokens(item.usage);

  return (
    <section className="flex flex-col gap-2">
      <h1 className="text-lg font-semibold break-words sm:text-xl">
        item {item.id}
        <span className="ml-2 text-xs font-normal text-muted-foreground sm:text-sm">
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
  );
}

/** Les questions ouvertes de l'item — la seule chose à faire ici. */
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
        <Card key={question.question_id}>
          <CardHeader>
            <CardTitle className="text-sm font-normal text-muted-foreground">
              [{question.question_id}] {question.node} · pour {question.owner} ·
              avant {moment(question.deadline)}
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="whitespace-pre-wrap text-sm">{question.text}</p>
            <AnswerForm question={question} feed={itemFeed(id)} />
          </CardContent>
        </Card>
      ))}
    </section>
  );
}

/** Le graph, avec le nœud courant marqué. */
export function ItemGraph({
  id,
  initial,
}: {
  id: number;
  initial: { graph: Graph; runs: Run[] };
}) {
  // le graph change à chaque transition ; la visionneuse, elle, n'est
  // jamais remontée — le cadrage choisi par l'œil ne bouge donc pas.
  // Les runs sont là pour compter les candidats en vol, rien de plus :
  // l'item reste sur *un* nœud, et la géométrie ne bouge pas d'un pixel.
  const { graph, runs } = useItem(
    id,
    (view) => ({ graph: view.graph, runs: view.runs }),
    initial,
  );

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">graph</h2>
      <GraphSvg graph={graph} item={id} candidates={running(runs, graph.current)} />
    </section>
  );
}

/** Une variante, telle qu'elle est déclarée : ses clés, et rien d'inventé. */
function VariantFields({ variant }: { variant: Variant }) {
  // un objet — un `agent` surchargé — ne se met pas dans une cellule : ce
  // qu'il change se lit déjà dans les colonnes modèle et CLI
  const fields = Object.entries(variant).filter(
    ([, value]) => typeof value !== "object" || value === null,
  );
  return (
    <span className="flex flex-col gap-0.5">
      {fields.map(([key, value]) => (
        <span key={key}>
          <span className="text-muted-foreground">{key}</span>{" "}
          <b>{String(value)}</b>
        </span>
      ))}
    </span>
  );
}

/**
 * Les candidats d'une étape en fan-out, dépliés.
 *
 * Une table à elle, sous la ligne de l'étape : dix champs par candidat ne
 * tiennent pas dans les colonnes du journal, et les aligner de force les
 * rendrait illisibles. Le gagnant porte sa marque — un fond, un badge —
 * qu'aucun perdant ne porte : il se voit sans comparer les colonnes.
 */
function Candidates({ candidates }: { candidates: Candidate[] }) {
  return (
    <Table className="text-xs">
      <TableHeader>
        <TableRow>
          <TableHead>candidat</TableHead>
          <TableHead>variante</TableHead>
          <TableHead>modèle</TableHead>
          <TableHead>CLI</TableHead>
          <TableHead>issue</TableHead>
          <TableHead>sort</TableHead>
          <TableHead>durée</TableHead>
          <TableHead>tokens in</TableHead>
          <TableHead>tokens out</TableHead>
          <TableHead>coût $</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {candidates.map((candidate) => (
          <TableRow
            key={candidate.run.id}
            data-winner={candidate.winner || undefined}
            className={candidate.winner ? "bg-emerald-50" : undefined}
          >
            <TableCell className="whitespace-nowrap">
              c{candidate.index}
            </TableCell>
            <TableCell>
              <VariantFields variant={candidate.variant} />
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {candidate.model ?? ""}
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {candidate.cli ?? ""}
            </TableCell>
            <TableCell>{candidate.run.outcome ?? ""}</TableCell>
            <TableCell>
              {candidate.winner ? (
                <Badge variant="active">gagnant</Badge>
              ) : candidate.loss ? (
                <Badge variant={tone(candidate.run.status)}>
                  {candidate.loss}
                </Badge>
              ) : (
                <Badge variant="warn">en course</Badge>
              )}
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {duration(candidate.duration_s)}
            </TableCell>
            <TableCell
              className="whitespace-nowrap"
              title={tokens(candidate.run.usage) || undefined}
            >
              {count(candidate.run.usage?.input_tokens)}
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {count(candidate.run.usage?.output_tokens)}
            </TableCell>
            <TableCell className="whitespace-nowrap">
              {cost(candidate.run.usage?.total_cost_usd)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/**
 * Le journal, une ligne par version, et ce que chaque step a coûté.
 *
 * Une étape en fan-out garde sa ligne — l'item n'a fait qu'une transition
 * — mais elle dit alors combien de candidats l'ont courue, et se déplie
 * sur eux. Sa durée reste celle du mur, celle du séjour dans le nœud ; ses
 * jetons et son coût, eux, sont ceux de tous les candidats. Le pied de
 * table additionne ce que les lignes montrent, donc le total de la page
 * compte les perdants comme le gagnant.
 */
export function ItemJournal({
  id,
  initial,
}: {
  id: number;
  initial: { journal: JournalEntry[]; runs: Run[]; graph: Graph };
}) {
  // les runs sont dans la tranche parce que les colonnes de tokens les
  // lisent : le journal dit quel run a produit le step, le run porte son
  // usage — la jointure se fait ici, une fois. Le graph nomme les
  // candidats d'un nœud en fan-out ; il vient donc avec.
  const { journal, runs, graph } = useItem(
    id,
    (view) => ({ journal: view.journal, runs: view.runs, graph: view.graph }),
    initial,
  );
  // les étapes dépliées ; l'ouverture vit ici et survit aux tours de sondage
  const [open, setOpen] = useState<number[]>([]);
  const rows = steps(journal, runs, graph);
  // les totaux du pied de table sont la somme de ce que les lignes montrent,
  // pas un second calcul : un step sans LLM y pèse zéro, et ça se voit
  const sum = (key: string) =>
    rows.reduce(
      (acc, step: { usage: Usage | undefined }) => acc + (step.usage?.[key] ?? 0),
      0,
    );

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">journal</h2>
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
          {rows.map(({ entry, usage, candidates }) => {
            const shown = open.includes(entry.version);
            return (
              <Fragment key={entry.version}>
                <TableRow>
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
                  {/* le dépliant tient dans la colonne du run : une colonne
                      de plus décorerait toutes les étapes pour les rares qui
                      sont en fan-out — celles-ci ne changent pas d'un pixel */}
                  <TableCell>
                    {candidates.length ? (
                      <button
                        type="button"
                        className="cursor-pointer whitespace-nowrap underline"
                        aria-expanded={shown}
                        onClick={() =>
                          setOpen((was) =>
                            was.includes(entry.version)
                              ? was.filter((v) => v !== entry.version)
                              : [...was, entry.version],
                          )
                        }
                      >
                        {shown ? "▾" : "▸"} {candidates.length} candidats
                      </button>
                    ) : (
                      (entry.run_id ?? "")
                    )}
                  </TableCell>
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
                {shown && candidates.length > 0 && (
                  <TableRow>
                    <TableCell colSpan={10} className="bg-muted/40">
                      <Candidates candidates={candidates} />
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
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
  );
}

/** Les runs : leur statut, leur durée, leurs tokens, leur résultat. */
export function ItemRuns({ id, initial }: { id: number; initial: Run[] }) {
  const runs = useItem(id, (view) => view.runs, initial);
  if (!runs.length) {
    return null;
  }

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">runs</h2>
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
              {/* K candidats partagent une tentative : sans leur numéro, la
                  table montrerait K lignes identiques */}
              <TableCell>
                {run.attempt}
                {run.candidate !== null && (
                  <span className="ml-1 text-xs text-muted-foreground">
                    c{run.candidate}
                  </span>
                )}
              </TableCell>
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
              {/* le post-mortem est le plus large de la table : on le
                  borne au doigt pour raccourcir le glissement */}
              <TableCell className="max-w-80 text-xs md:max-w-none">
                <Result run={run} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

/** Les critères du cycle, tels que le nœud `validate` les a cochés. */
export function ItemCriteria({
  id,
  initial,
}: {
  id: number;
  initial: { criteria: string | null; validate: string | null };
}) {
  // les deux fichiers vont ensemble : la liste vient de `criteria.md`, la
  // coche de `validate.md`, et le validate arrive au tour où il est écrit
  const { criteria, validate } = useItem(
    id,
    (view) => ({ criteria: view.criteria, validate: view.validate }),
    initial,
  );
  if (!criteria) {
    return null;
  }

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">critères de succès</h2>
      <CriteriaList criteria={criteria} validate={validate} />
    </section>
  );
}

/** Les effets réconciliés de l'item. */
export function ItemEffects({ id, initial }: { id: number; initial: Effect[] }) {
  const effects = useItem(id, (view) => view.effects, initial);
  if (!effects.length) {
    return null;
  }

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold sm:text-lg">effets</h2>
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
  );
}

/** Le workspace : les screenshots des agents en preview, puis la liste. */
export function ItemFiles({
  id,
  initial,
}: {
  id: number;
  initial: WorkspaceFile[];
}) {
  const files = useItem(id, (view) => view.files, initial);
  if (!files.length) {
    return null;
  }
  const images = files.filter((f) => f.name.endsWith(".png"));

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-base font-semibold sm:text-lg">workspace</h2>
      {images.map((file) => (
        // les screenshots des agents = la preview ; le `href` vient de
        // l'API et le front monte le même chemin, rien à réécrire
        <figure key={file.name} className="flex flex-col gap-1">
          <figcaption className="text-sm break-all text-muted-foreground">
            {file.name}
          </figcaption>
          <a href={file.href}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={file.href}
              alt={file.name}
              className="h-auto w-full rounded-md border md:w-auto md:max-w-full"
            />
          </a>
        </figure>
      ))}
      <ul className="flex flex-col gap-1 text-sm break-words">
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
  );
}
