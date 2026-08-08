/**
 * Ce qui exécute un nœud, lu dans la commande de son agent.
 *
 * Le bundle n'a ni champ `cli` ni champ `model` : les deux sont des
 * morceaux de la commande, `claude … --model sonnet …`. La commande reste
 * la vérité — on la lit, on n'en déduit rien d'autre.
 *
 * Trois natures, et une seule par commande :
 *
 *   - **`model`** — la commande appelle une CLI d'agent : c'est elle qui
 *     exécute le nœud, avec le modèle qu'elle nomme, ou celui par défaut
 *     de la CLI quand elle n'en nomme aucun ;
 *   - **`shell`** — la commande n'appelle aucune CLI d'agent : du shell
 *     pur, déterministe. C'est une propriété remarquable du nœud et non
 *     une lacune — le mécanique au script, le jugement au modèle ;
 *   - **`none`** — il n'y a pas de commande : un nœud `WAIT` ou terminal
 *     ne lance rien.
 *
 * Une nature est toujours rendue, et chacune a son libellé : rien d'ici ne
 * peut valoir « vide », et une vue qui n'afficherait rien mentirait par
 * omission — l'absence de modèle se lirait comme une information manquante.
 */

/** Ce qu'une commande lance : une CLI et son modèle, du shell, ou rien. */
export type Execution =
  | { kind: "model"; cli: string; model: string }
  | { kind: "shell" }
  | { kind: "none" };

// un mot n'ouvre une commande qu'après un début de ligne ou un séparateur
// shell : `claude` en tête de commande, pas le `.claude/` d'un chemin
const DEBUT = String.raw`(^|[\s|&;(])`;

// ce qu'un argument de commande peut porter — il s'arrête au premier
// séparateur shell, `opencode/deepseek-v4-flash-free; RC=$?` sur le premier
const ARGUMENT = String.raw`([^\s"';|&)]+)`;

/**
 * Les CLI d'agent connues : comment on les reconnaît, où est leur modèle.
 *
 * Une CLI est reconnue à son appel, jamais à son nom croisé quelque part —
 * un chemin qui contient `claude` n'appelle rien. Son modèle se lit ensuite
 * dans la commande, au premier motif qui répond : la CLI a souvent plus
 * d'une façon de le nommer, et l'adaptateur d'`opencode` le prend même en
 * argument de script.
 */
const CLIS = [
  {
    name: "claude",
    calls: new RegExp(`${DEBUT}claude\\s`),
    models: [/--model[\s=]+"?([^\s"';|&)]+)/],
  },
  {
    name: "opencode",
    calls: new RegExp(`${DEBUT}(\\S*agent-opencode\\.sh|opencode\\s)`),
    models: [
      // `scripts/agent-opencode.sh <fournisseur/modèle>` — l'adaptateur du
      // projet prend son modèle en argument, et c'est le seul qu'il joue
      new RegExp(String.raw`agent-opencode\.sh"?\s+"?${ARGUMENT}`),
      // `opencode run -m <fournisseur/modèle>`, la CLI appelée directement
      new RegExp(String.raw`(?:^|\s)(?:-m|--model)[\s=]+"?${ARGUMENT}`),
      // `OPENCODE_MODEL=…` — l'adaptateur le lit quand l'argument manque
      new RegExp(String.raw`OPENCODE_MODEL=["']?${ARGUMENT}`),
    ],
  },
];

/** Le modèle nommé par la commande — `défaut` quand elle n'en nomme aucun. */
function modele(cmd: string, motifs: RegExp[]): string {
  for (const motif of motifs) {
    const trouve = cmd.match(motif);
    // `opencode/deepseek-v4-flash-free` nomme un fournisseur puis un
    // modèle : c'est le modèle qu'on montre, le fournisseur est déjà dit
    // par la CLI
    if (trouve) {
      return trouve[1].split("/").pop()!;
    }
  }
  return "défaut";
}

/** Ce qu'exécute une commande d'agent, ou son absence d'exécution. */
export function execution(cmd: string | null | undefined): Execution {
  if (!cmd || !cmd.trim()) {
    return { kind: "none" };
  }
  for (const cli of CLIS) {
    if (cli.calls.test(cmd)) {
      return { kind: "model", cli: cli.name, model: modele(cmd, cli.models) };
    }
  }
  return { kind: "shell" };
}

/** Le libellé d'une exécution — une CLI et son modèle, ou le cas nommé. */
export function executionLabel(exec: Execution): string {
  switch (exec.kind) {
    case "model":
      return `${exec.cli} · ${exec.model}`;
    case "shell":
      return "shell déterministe";
    case "none":
      return "sans exécution";
  }
}
