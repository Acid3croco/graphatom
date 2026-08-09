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
  /** Coût réellement rapporté par le fournisseur, quand il en donne un. */
  total_cost_usd: number;
  reported_cost_usd: number | null;
  /** Même usage aux tarifs API publics épinglés par run. */
  estimated_cost_usd: number | null;
  cost_estimated_runs: number;
  cost_unestimated_runs: number;
  issue_url: string | null;
  pr_url: string | null;
  terminal_at: string | null;
  updated_at: string | null;
};

export type Usage = Record<string, number>;

/**
 * Le prix de l'item, coupé en trois parts disjointes.
 *
 * Les deux bouts de l'haltère se lisent l'un à côté de l'autre : ce que le
 * jugement a coûté, et ce que la génération des candidats a coûté. `other`
 * est tout le reste — les nœuds ordinaires, ni juges ni candidats.
 */
export type UsageSplit = {
  judgement: Usage;
  candidates: Usage;
  other: Usage;
};

export type ItemHead = Item & {
  revision: string;
  lineage_budget: number;
  wall_deadline: string | null;
  duration_s: number;
  usage: Usage;
  usage_split: UsageSplit;
};

/**
 * Une variante de fan-out : le fragment de config qu'elle surcharge.
 *
 * Ses clés sont celles que le graph a choisies — `label`, `strategy`, un
 * `agent` surchargé. On les montre telles quelles ; rien n'est imposé ici.
 */
export type Variant = Record<string, unknown>;

/** Un candidat d'un nœud en fan-out : sa variante et son agent effectif. */
export type FanoutCandidate = {
  variant: Variant;
  agent?: BundleAgent;
  cmd: string | null;
};

/** Le fan-out d'un nœud tel que l'API le projette, un candidat par entrée. */
export type NodeFanout = {
  reduce: string;
  repeat: number;
  candidates: FanoutCandidate[];
};

export type GraphNode = {
  name: string;
  block: string | null;
  terminal: boolean;
  escalade: boolean;
  fanout?: NodeFanout;
  /** Le nœud de fan-out dont ce nœud départage les finalistes — un arbitre. */
  finalists_from?: string;
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
  /** Le candidat du fan-out, ou `null` quand le nœud n'en porte pas. */
  candidate: number | null;
  status: string;
  outcome: string | null;
  fence: number;
  lease_expires_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  usage: Usage;
  cost_estimate: {
    provider: string;
    model: string;
    model_source: "usage" | "graph" | "legacy_default";
    pricing_basis: "base_standard";
    source_url: string;
    price_fetched_at: string;
    input_cost_usd: number;
    cache_read_cost_usd: number;
    cache_write_cost_usd: number;
    output_cost_usd: number;
    total_cost_usd: number;
  } | null;
  result: Record<string, unknown> | null;
};

export type TraceSource = {
  type: string | null;
  state: "missing" | "empty" | "available";
  content: string;
  offset: number;
  next_offset: number;
  media_type: string;
};

export type RunTrace = {
  item_id: number;
  run_id: number;
  status: string;
  events: TraceSource;
  log: TraceSource;
  command: TraceSource;
  cursor: Record<"events" | "log" | "command", number>;
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

/** Le détail d'un item tel que les pages le lisent, `validate.md` compris. */
export type ItemView = ItemDetail & { validate: string | null };

export type OpenQuestions = { token: string; questions: Question[] };

/** Une révision publiée, telle que `/api/graphs` la liste. */
export type GraphRevision = {
  id: string;
  name: string;
  published_at: string;
  items: number;
};

/** Les réglages structurés et explicites d'un agent. */
export type BundleAgent = {
  cli?: string;
  model?: string;
  effort?: string | null;
  cmd?: string;
  cmd_uses_executor?: boolean;
  cmd_reason?: string;
  prompt?: string;
  timeout_s?: number;
  silence_s?: number;
};

/**
 * La config d'un nœud, telle qu'elle est publiée.
 *
 * Les clés connues sont nommées ; le reste passe quand même — un bloc qui
 * range sa propre clé dans sa config se montre sous son nom plutôt que de
 * disparaître de la vue.
 */
export type BundleConfig = {
  lease_s?: number;
  agent?: BundleAgent;
  question?: string;
  options?: string[];
  owner?: string;
  deadline_minutes?: number;
  fanout?: BundleFanout;
  [key: string]: unknown;
};

/** Le fan-out d'un nœud, tel qu'il est déclaré dans le bundle. */
export type BundleFanout = {
  variants: Variant[];
  repeat?: number;
  reduce: string;
  /** Le nombre de finalistes gardés — `keep_n` seule le déclare. */
  n?: number;
};

export type BundleNode = {
  block?: string;
  terminal?: boolean;
  escalade?: boolean;
  config?: BundleConfig;
  edges?: Record<string, string>;
};

/** Le bundle entier d'une révision, plus ce que la base sait d'elle. */
export type GraphBundle = {
  revision: string;
  published_at: string;
  items: number;
  name: string;
  entry: string;
  budgets: Record<string, number>;
  on_kernel: Record<string, string>;
  agent?: BundleAgent;
  nodes: Record<string, BundleNode>;
};

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

export const getRunTrace = (item: number, run: number, search = "") =>
  get<RunTrace>(`/api/item/${item}/run/${run}/trace${search}`);

/**
 * Le détail d'un item, plus le texte de `validate.md`.
 *
 * L'API inline `criteria.md` mais pas `validate.md` : il arrive par son
 * `href` de workspace, lu ici — le serveur parle au serveur. La section
 * des critères de succès en a besoin à chaque tour de sondage : la
 * jointure se fait donc une fois, ici, et la page comme la route de relais
 * lisent le même objet.
 */
export async function getItemView(id: number): Promise<ItemView> {
  const detail = await getItem(id);
  const validated = detail.files.find((f) => f.name === "validate.md");
  return {
    ...detail,
    validate: validated ? await getFileText(validated.href) : null,
  };
}

export const getQuestions = () => get<OpenQuestions>("/api/questions");

export const getHeartbeat = () => get<Heartbeat>("/api/heartbeat");

export const getGraphs = () => get<GraphRevision[]>("/api/graphs");

export const getGraph = (revision: string) =>
  get<GraphBundle>(`/api/graph/${encodeURIComponent(revision)}`);
