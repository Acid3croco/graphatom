/**
 * La config d'un nœud, telle qu'elle est publiée.
 *
 * Le bundle est la vérité : ce panneau ne résume rien et n'interprète rien
 * — le bloc, le bail, les bornes, les arêtes issue → cible, et pour un
 * agent sa commande et son prompt entier. Ce qui n'entre dans aucune de
 * ces cases sort quand même, en JSON : une clé qu'un bloc range dans sa
 * config se montre sous son nom plutôt que de disparaître de la vue.
 */
import type { ReactNode } from "react";
import { X } from "lucide-react";

import type { BundleNode } from "@/lib/api";
import { agentModel } from "@/lib/agent-model";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

// les clés que le panneau montre en clair ; le reste part dans le bloc JSON
const KNOWN = new Set([
  "lease_s",
  "agent",
  "question",
  "options",
  "owner",
  "deadline_minutes",
]);

/** Un champ du bundle : sa clé, telle qu'elle s'écrit, et sa valeur. */
function Field({ name, value }: { name: string; value: ReactNode }) {
  return (
    <span className="whitespace-nowrap">
      <span className="text-muted-foreground">{name}</span> <b>{value}</b>
    </span>
  );
}

/** Un pavé du panneau : son titre, et ce qu'il montre. */
function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-1">
      <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
        {title}
      </h3>
      {children}
    </section>
  );
}

export function NodeConfig({
  name,
  node,
  onClose,
}: {
  name: string;
  node: BundleNode;
  onClose: () => void;
}) {
  const config = node.config ?? {};
  const agent = config.agent;
  const model = agentModel(agent?.cmd);
  const edges = Object.entries(node.edges ?? {});
  const rest = Object.entries(config).filter(([key]) => !KNOWN.has(key));

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-2">
        <CardTitle className="flex items-center gap-2">
          {name}
          <Badge variant={node.terminal ? "terminal" : "default"}>
            {node.terminal ? "terminal" : (node.block ?? "?")}
          </Badge>
          {node.escalade && <Badge variant="warn">escalade</Badge>}
        </CardTitle>
        <Button
          variant="outline"
          size="sm"
          className="h-7 w-7 p-0"
          aria-label="fermer la config"
          title="fermer la config"
          onClick={onClose}
        >
          <X className="h-4 w-4" aria-hidden />
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-sm">
        <p className="flex flex-wrap gap-x-4 gap-y-1">
          <Field name="bloc" value={node.block ?? "—"} />
          {model !== null && <Field name="model" value={model} />}
          {config.lease_s !== undefined && (
            <Field name="lease_s" value={`${config.lease_s} s`} />
          )}
          {agent?.timeout_s !== undefined && (
            <Field name="timeout_s" value={`${agent.timeout_s} s`} />
          )}
          <Field name="escalade" value={node.escalade ? "oui" : "non"} />
          <Field name="terminal" value={node.terminal ? "oui" : "non"} />
        </p>

        {edges.length > 0 && (
          <Block title="arêtes">
            <ul className="flex flex-col gap-0.5">
              {edges.map(([outcome, target]) => (
                <li key={outcome}>
                  <code className="text-xs">{outcome}</code> → {target}
                </li>
              ))}
            </ul>
          </Block>
        )}

        {config.question !== undefined && (
          <Block title="question">
            <p className="whitespace-pre-wrap">{config.question}</p>
            <p className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1">
              <Field name="owner" value={config.owner ?? "—"} />
              <Field
                name="deadline_minutes"
                value={config.deadline_minutes ?? "—"}
              />
            </p>
            {config.options && (
              <p className="mt-1 flex flex-wrap items-center gap-1">
                <span className="text-muted-foreground">options</span>
                {config.options.map((option) => (
                  <Badge key={option}>{option}</Badge>
                ))}
              </p>
            )}
          </Block>
        )}

        {agent?.cmd && (
          <Block title="cmd">
            {/* la commande garde sa mise en forme : elle défile plutôt que
                de se replier, un script replié ne se lit plus */}
            <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre">
              {agent.cmd}
            </pre>
          </Block>
        )}

        {agent?.prompt && (
          <Block title="prompt">
            {/* le prompt entier, jamais coupé : c'est ce qui dit ce que le
                nœud demande vraiment à son agent */}
            <pre className="max-h-[32rem] overflow-auto rounded-md bg-muted p-2 text-xs break-words whitespace-pre-wrap">
              {agent.prompt}
            </pre>
          </Block>
        )}

        {rest.length > 0 && (
          <Block title="reste de la config">
            <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs break-words whitespace-pre-wrap">
              {JSON.stringify(Object.fromEntries(rest), null, 2)}
            </pre>
          </Block>
        )}
      </CardContent>
    </Card>
  );
}
