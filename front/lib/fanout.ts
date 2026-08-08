/**
 * Une étape du journal, ses candidats, et ce qu'elle a vraiment coûté.
 *
 * Le journal a une ligne par transition de l'item, et l'item n'en fait
 * qu'une par étape — même quand K candidats l'ont courue. Sans rien de
 * plus, cette ligne montrerait le seul run qui l'a routée : le gagnant, et
 * rien du prix payé. Ce module recolle la tentative entière derrière sa
 * ligne, à partir de ce que l'API rend déjà.
 *
 * Ce qui fait une étape en fan-out : un run à `candidate` non nul. Les
 * candidats d'une même tentative partagent `(nœud, passage, tentative)` —
 * c'est la clé du noyau, c'est celle d'ici.
 *
 * Deux comptes qui ne se confondent pas :
 *
 *   - la **durée** de l'étape est celle du mur, du premier départ à la
 *     décision — celle que le journal porte déjà. Elle n'est pas la somme
 *     des durées des candidats, sinon le parallélisme ne se lirait nulle
 *     part ;
 *   - les **jetons** et le **coût**, eux, s'additionnent sur tous les
 *     candidats, perdants compris. C'est le prix réel de l'étape, et une
 *     vue qui ne compterait que le gagnant mentirait par omission.
 *
 * Un item sans aucun candidat ne rencontre rien de tout ceci : chaque
 * étape garde son run unique et son usage, exactement comme avant.
 */
import type { Graph, JournalEntry, Run, Usage, Variant } from "@/lib/api";
import { execution } from "@/lib/agent-model";

// les issues que le noyau nomme lui-même : un run qui en rend une a raté.
// Miroir de `KERNEL_OUTCOMES` dans `src/graphatom/graph.py`.
const KERNEL_OUTCOMES = new Set([
  "crashed",
  "starved",
  "stalled",
  "timed_out",
  "invalid_result",
  "budget_exhausted",
  "wall_deadline",
]);

/** Un candidat de l'étape : ce qui le distingue, et ce qu'il a donné. */
export type Candidate = {
  run: Run;
  /** Son numéro, celui de son workspace `c<k>` et de sa variante. */
  index: number;
  variant: Variant;
  model: string | null;
  cli: string | null;
  duration_s: number | null;
  winner: boolean;
  /** Pourquoi il a perdu — `null` s'il a gagné, ou s'il court encore. */
  loss: string | null;
};

/** Une ligne du journal, et les candidats de son étape s'il y en a. */
export type Step = {
  entry: JournalEntry;
  usage: Usage | undefined;
  /** Les candidats, dans l'ordre de leur numéro — vide hors fan-out. */
  candidates: Candidate[];
};

/** La clé d'une tentative : celle que les candidats partagent. */
const attemptKey = (run: Run) => `${run.node}/${run.cycle}/${run.attempt}`;

/**
 * Pourquoi ce candidat a perdu — `null` tant qu'il n'a pas perdu.
 *
 * Trois façons de perdre, et le run les porte toutes : la décision l'a
 * révoqué avant qu'il rende, son bail a expiré sur un agent encore vivant,
 * ou il a rendu une issue que le noyau refuse — une porte interne.
 *
 * La raison nomme la catégorie, pas l'issue : celle-ci est déjà dans sa
 * colonne, et la répéter ici ne dirait rien de plus.
 */
function loss(run: Run): string | null {
  if (run.status === "running") {
    return null;
  }
  if (run.status === "superseded") {
    return "révoqué par la décision";
  }
  if (run.status === "stale") {
    return "périmé";
  }
  if (run.outcome === "timed_out") {
    return "timeout";
  }
  if (run.outcome === "starved") {
    return "fournisseur";
  }
  return run.outcome && KERNEL_OUTCOMES.has(run.outcome)
    ? "porte interne"
    : "perdu";
}

/** La somme des usages, clé à clé — les types tels qu'ils viennent. */
function total(usages: (Usage | undefined)[]): Usage | undefined {
  const sum: Usage = {};
  let seen = false;
  for (const usage of usages) {
    for (const [key, value] of Object.entries(usage ?? {})) {
      if (typeof value === "number") {
        sum[key] = (sum[key] ?? 0) + value;
        seen = true;
      }
    }
  }
  return seen ? sum : undefined;
}

/**
 * Le journal de l'item, étape par étape, fan-out recollé.
 *
 * `graph` nomme les candidats : un nœud en fan-out y porte sa liste, et
 * l'entrée `k` est celle du candidat `k`. Un nœud qui n'en porte pas laisse
 * simplement les variantes vides — la course se lit quand même.
 */
export function steps(
  journal: JournalEntry[],
  runs: Run[],
  graph: Graph,
): Step[] {
  const byRun = new Map(runs.map((run) => [run.id, run]));
  const batches = new Map<string, Run[]>();
  for (const run of runs) {
    if (run.candidate !== null) {
      const key = attemptKey(run);
      batches.set(key, [...(batches.get(key) ?? []), run]);
    }
  }
  const fanouts = new Map(graph.nodes.map((node) => [node.name, node.fanout]));

  return journal.map((entry) => {
    const run = entry.run_id === null ? undefined : byRun.get(entry.run_id);
    const batch = run && run.candidate !== null ? batches.get(attemptKey(run)) : undefined;
    if (!run || !batch) {
      return { entry, usage: run?.usage, candidates: [] };
    }
    // le gagnant est le run qui a routé l'étape, et seulement s'il l'a
    // routée sur une arête du nœud : une étape que tous ont ratée n'a pas
    // de gagnant, et il ne faut pas en peindre un
    const won =
      entry.outcome !== null && !KERNEL_OUTCOMES.has(entry.outcome)
        ? entry.run_id
        : null;
    // l'étape a commencé quand l'item est entré dans le nœud : la durée que
    // le journal porte est justement celle de ce séjour
    const start =
      entry.duration_s === null
        ? null
        : new Date(entry.at).getTime() - entry.duration_s * 1000;
    const declared = fanouts.get(run.node)?.candidates ?? [];

    const candidates = [...batch]
      .sort((a, b) => a.candidate! - b.candidate!)
      .map((candidate) => {
        const cmd = declared[candidate.candidate!]?.cmd ?? null;
        const end = candidate.finished_at;
        // la commande dit la CLI et le modèle ensemble, ou ne dit ni l'une
        // ni l'autre : un candidat en shell pur n'a aucun des deux
        const exec = execution(cmd);
        return {
          run: candidate,
          index: candidate.candidate!,
          variant: declared[candidate.candidate!]?.variant ?? {},
          model: exec.kind === "model" ? exec.model : null,
          cli: exec.kind === "model" ? exec.cli : null,
          duration_s:
            start === null || end === null
              ? candidate.duration_s
              : (new Date(end).getTime() - start) / 1000,
          winner: candidate.id === won,
          loss: candidate.id === won ? null : loss(candidate),
        };
      });
    return { entry, usage: total(candidates.map((c) => c.run.usage)), candidates };
  });
}

/** Les candidats encore en course sur un nœud — 0 quand rien n'y court. */
export function running(runs: Run[], node: string): number {
  return runs.filter(
    (run) =>
      run.node === node && run.candidate !== null && run.status === "running",
  ).length;
}
