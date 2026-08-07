/**
 * Le client de l'API du rail : les quatre lectures JSON, plus les fichiers
 * du workspace que l'API sert en texte, et rien d'autre.
 *
 * Une projection, pas un second modèle — les types ci-dessous décrivent ce
 * que `src/graphatom/web.py` rend, tel quel. Aucune écriture ici : répondre
 * à une question passe par la route serveur `/api/answer` du front, qui
 * relaie l'unique porte d'écriture de l'API.
 *
 * Tout se lit sans cache : ces pages montrent un rail qui bouge.
 */
import { API_URL } from "@/lib/config";

export type Beat = {
  at: string | null;
  ago_s: number;
  stale: boolean;
};

/** Un battement par producteur, sous son identité en base. */
export type Heartbeat = {
  rail: Beat;
  "github-sync": Beat;
};

export type Item = {
  id: number;
  subject_key: string;
  title: string | null;
  graph: string;
  generation: number;
  state: string;
  status: "active" | "terminal";
  version: number;
  cycle: number;
  escalations: number;
  issue_url: string | null;
  pr_url: string | null;
  terminal_at: string | null;
  updated_at: string | null;
};

export type Usage = Record<string, number>;

export type ItemHead = Item & {
  revision: string;
  lineage_budget: number;
  wall_deadline: string | null;
  duration_s: number;
  usage: Usage;
};

export type GraphNode = {
  name: string;
  block: string | null;
  terminal: boolean;
  escalade: boolean;
};

export type GraphEdge = { from: string; outcome: string; to: string };

export type Graph = {
  name: string;
  entry: string;
  on_kernel: Record<string, string>;
  current: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
};

export type JournalEntry = {
  version: number;
  at: string;
  kind: string;
  from_state: string | null;
  to_state: string;
  outcome: string | null;
  run_id: number | null;
  duration_s: number | null;
};

export type Run = {
  id: number;
  node: string;
  cycle: number;
  attempt: number;
  status: string;
  outcome: string | null;
  fence: number;
  lease_expires_at: string | null;
  duration_s: number | null;
  usage: Usage;
  result: Record<string, unknown> | null;
};

export type Effect = {
  op_id: string;
  logical_key: string;
  target_uri: string;
  observation: string;
};

export type Question = {
  question_id: number;
  item_id: number;
  node: string;
  text: string;
  options: string[];
  owner: string;
  deadline: string;
  state: string;
  answer: string | null;
  answered_by: string | null;
  subject_key?: string;
  item_title?: string | null;
  item_state?: string;
  issue_url?: string | null;
};

export type WorkspaceFile = { name: string; size: number; href: string };

export type ItemDetail = {
  item: ItemHead;
  graph: Graph;
  journal: JournalEntry[];
  runs: Run[];
  effects: Effect[];
  questions: Question[];
  criteria: string | null;
  files: WorkspaceFile[];
};

export type OpenQuestions = { token: string; questions: Question[] };

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new Error(`${path} : l'API a répondu ${res.status}`);
  }
  return res.json() as Promise<T>;
}

/**
 * Le contenu d'un fichier du workspace, en texte.
 *
 * Le `href` vient de la liste `files` de l'item — on ne le fabrique pas.
 * Un fichier que l'API refuse de servir rend `null` : la page se passe de
 * ce qu'elle voulait y lire plutôt que de disparaître pour autant.
 */
export async function getFileText(href: string): Promise<string | null> {
  const res = await fetch(`${API_URL}${href}`, { cache: "no-store" });
  return res.ok ? res.text() : null;
}

export const getItems = () => get<Item[]>("/api/items");

export const getItem = (id: number) => get<ItemDetail>(`/api/item/${id}`);

export const getQuestions = () => get<OpenQuestions>("/api/questions");

export const getHeartbeat = () => get<Heartbeat>("/api/heartbeat");
