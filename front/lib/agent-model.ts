/**
 * Ce qui exécute un nœud, lu dans ses réglages structurés.
 *
 * Le graph porte les valeurs par défaut. Le nœud puis la variante les
 * surchargent, clé par clé, comme le noyau. Une commande explicite garde la
 * priorité sur la CLI déclarée. `cmd_uses_executor` distingue un wrapper qui
 * lance cette CLI d'un shell déterministe. Sans commande ni CLI, le nœud ne
 * lance rien.
 */

/** Les champs nécessaires pour identifier un exécuteur. */
export type AgentExecution = {
  cli?: string;
  model?: string;
  effort?: string | null;
  cmd?: string;
  cmd_uses_executor?: boolean;
};

/** Le choix fermé du wagon, avant résolution des valeurs de l'agent. */
export type DeclaredExecution = {
  kind: "agent" | "command";
  cmd?: string;
};

/** Ce qu'un nœud lance : une CLI et son modèle, du shell, ou rien. */
export type Execution =
  | {
      kind: "model" | "composed";
      cli: string;
      model: string;
      effort: string | null;
    }
  | { kind: "shell" }
  | { kind: "none" };

/**
 * L'exécuteur effectif du graph, du nœud et, si elle existe, de la variante.
 *
 * Les trois objets restent séparés à l'appel : la vue ne reçoit pas une
 * commande reconstruite à analyser et la règle d'héritage reste visible.
 */
export function execution(
  graph: AgentExecution | null | undefined,
  node?: AgentExecution | null,
  variant?: AgentExecution | null,
  declared?: DeclaredExecution | null,
): Execution {
  if (declared?.kind === "command") {
    return { kind: "shell" };
  }
  const effective = { ...(graph ?? {}), ...(node ?? {}), ...(variant ?? {}) };
  if (declared?.kind === "agent" && declared.cmd !== undefined) {
    effective.cmd = declared.cmd;
    effective.cmd_uses_executor = true;
  }
  if (effective.cmd !== undefined && effective.cmd_uses_executor !== true) {
    return { kind: "shell" };
  }
  if (!effective.cli) {
    return { kind: "none" };
  }
  return {
    kind: effective.cmd === undefined ? "model" : "composed",
    cli: effective.cli,
    // opencode nomme le fournisseur dans la valeur structurée. La CLI est
    // déjà affichée à gauche : le libellé garde seulement le nom du modèle,
    // comme avant la migration des commandes.
    model: effective.model?.split("/").pop() ?? "défaut",
    effort: effective.effort ?? null,
  };
}

/** Le libellé d'une exécution — une CLI et son modèle, ou le cas nommé. */
export function executionLabel(exec: Execution): string {
  switch (exec.kind) {
    case "composed":
    case "model":
      return [exec.cli, exec.model, exec.effort]
        .filter((value) => value !== null)
        .join(" · ") + (exec.kind === "composed" ? " + shell" : "");
    case "shell":
      return "shell déterministe";
    case "none":
      return "sans exécution";
  }
}
