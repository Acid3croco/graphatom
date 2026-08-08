/**
 * Le modèle qui exécute un nœud, lu dans la commande de son agent.
 *
 * Le bundle n'a pas de champ `model` : le modèle est un argument de la
 * commande, `claude … --model sonnet …`. La commande reste la vérité — on
 * la lit, on n'en déduit rien d'autre.
 *
 * Trois cas, et un seul par commande : elle appelle `claude` avec
 * `--model`, et c'est ce modèle ; elle appelle `claude` sans le préciser,
 * et c'est le modèle par défaut ; elle n'appelle pas `claude` du tout —
 * du shell pur — et aucun modèle ne l'exécute.
 */

// `claude` en tête de commande, pas le `.claude/` d'un chemin : le mot
// n'ouvre une commande qu'après un début de ligne ou un séparateur shell
const CALLS_CLAUDE = /(^|[\s|&;(])claude\s/;

/** Le modèle d'une commande d'agent, ou `null` si elle n'en lance aucun. */
export function agentModel(cmd: string | undefined): string | null {
  if (!cmd || !CALLS_CLAUDE.test(cmd)) {
    return null;
  }
  const flag = cmd.match(/--model[\s=]+(\S+)/);
  return flag ? flag[1] : "défaut";
}
