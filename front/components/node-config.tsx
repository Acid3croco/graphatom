/**
 * La config d'un nœud, telle qu'elle est publiée.
 *
 * Le bundle est la vérité : ce panneau ne résume rien et n'interprète rien
 * — le bloc, le bail, les bornes, les arêtes issue → cible, et pour un
 * agent sa commande et son prompt entier. Ce qui n'entre dans aucune de
 * ces cases sort quand même, en JSON : une clé qu'un bloc range dans sa
 * config se montre sous son nom plutôt que de disparaître de la vue.
 *
 * **Ce qui exécute le nœud** se lit dans les champs structurés du graph, du
 * nœud et de la variante. Il est toujours dit — une CLI et son modèle, du
 * shell déterministe, ou rien du tout. Un nœud sans modèle n'a pas un champ
 * vide : il a une nature qui se nomme.
 *
 * Un nœud en fan-out n'a pas d'exécutant unique et le panneau n'en invente
 * pas : son en-tête compte les candidats et dit la réduction qui les
 * départage, et chaque variante porte ensuite la sienne.
 */
import type { ReactNode } from "react";
import { X } from "lucide-react";

import type { BundleAgent, BundleFanout, BundleNode, Variant } from "@/lib/api";
import { execution, executionLabel } from "@/lib/agent-model";
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
  "fanout",
]);

// les clés d'une variante que le panneau nomme lui-même ; le reste sort
// comme le reste de la config — en clair si c'est un scalaire, en JSON
// sinon, l'`agent` surchargé compris
const VARIANT_KNOWN = new Set(["label", "strategy"]);

/** Un champ du bundle : sa clé, telle qu'elle s'écrit, et sa valeur. */
function Field({ name, value }: { name: string; value: ReactNode }) {
  return (
    <span className="whitespace-nowrap">
      <span className="text-muted-foreground">{name}</span> <b>{value}</b>
    </span>
  );
}

/** Un champ dont la valeur est un texte : il se replie au lieu de défiler. */
function Text({ name, value }: { name: string; value: string }) {
  return (
    <p>
      <span className="text-muted-foreground">{name}</span>{" "}
      <span className="whitespace-pre-wrap">{value}</span>
    </p>
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

/** Ce qui exécute un agent effectif, en toutes lettres — jamais vide. */
function execField(
  graph: BundleAgent | undefined,
  node?: BundleAgent,
  variant?: BundleAgent,
) {
  return (
    <Field
      name="exécution"
      value={executionLabel(execution(graph, node, variant))}
    />
  );
}

/**
 * La réduction, avec son paramètre quand elle en a un.
 *
 * `keep_n` garde n finalistes et le dit — `keep_n n=2` ; `first_pass` n'a
 * rien à paramétrer et se lit seule.
 */
function reduction(fanout: BundleFanout): string {
  return fanout.n === undefined
    ? fanout.reduce
    : `${fanout.reduce} n=${fanout.n}`;
}

/**
 * Une variante déclarée : ce qui la distingue, et ce qui l'exécute.
 *
 * Trois choses la décrivent, et le fan-out les met toutes les trois dans la
 * config : son étiquette, la stratégie qu'on lui impose, et son exécuteur
 * effectif. Le reste de ce qu'elle surcharge sort ensuite tel qu'il est
 * déclaré — c'est bien là que se lit ce que cette variante change.
 */
function VariantConfig({
  variant,
  index,
  graphAgent,
  nodeAgent,
}: {
  variant: Variant;
  index: number;
  graphAgent: BundleAgent | undefined;
  nodeAgent: BundleAgent | undefined;
}) {
  const variantAgent = variant.agent as BundleAgent | undefined;
  const inherited =
    nodeAgent === undefined && variantAgent === undefined ? undefined : graphAgent;
  const rest = Object.entries(variant).filter(([key]) => !VARIANT_KNOWN.has(key));
  const flat = rest.filter(
    ([, value]) => typeof value !== "object" || value === null,
  );
  const nested = rest.filter(
    ([, value]) => typeof value === "object" && value !== null,
  );
  return (
    <>
      <p className="flex flex-wrap gap-x-4 gap-y-1">
        <Field
          name="étiquette"
          value={variant.label === undefined ? `c${index}` : String(variant.label)}
        />
        {execField(inherited, nodeAgent, variantAgent)}
        {flat.map(([key, value]) => (
          <Field key={key} name={key} value={String(value)} />
        ))}
      </p>
      {variant.strategy !== undefined && (
        <Text name="stratégie" value={String(variant.strategy)} />
      )}
      {nested.length > 0 && (
        <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs break-words whitespace-pre-wrap">
          {JSON.stringify(Object.fromEntries(nested), null, 2)}
        </pre>
      )}
    </>
  );
}

export function NodeConfig({
  name,
  node,
  graphAgent,
  onClose,
}: {
  name: string;
  node: BundleNode;
  graphAgent?: BundleAgent;
  onClose: () => void;
}) {
  const config = node.config ?? {};
  const agent = config.agent;
  const fanout = config.fanout;
  const repeat = fanout?.repeat ?? 1;
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
          {/* un nœud en fan-out n'a pas d'exécutant unique : il a K
              candidats et une réduction qui les départage, et c'est cela
              que son en-tête doit dire */}
          {fanout ? (
            <>
              <Field name="candidats" value={fanout.variants.length * repeat} />
              <Field name="réduction" value={reduction(fanout)} />
              {repeat > 1 && <Field name="répétition" value={repeat} />}
            </>
          ) : (
            execField(agent === undefined ? undefined : graphAgent, agent)
          )}
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

        {fanout && (
          <Block title={`variantes (${fanout.variants.length})`}>
            <ul className="flex flex-col gap-2">
              {fanout.variants.map((variant, index) => (
                <li key={index} className="flex flex-col gap-1 border-l pl-2">
                  <VariantConfig
                    variant={variant}
                    index={index}
                    graphAgent={graphAgent}
                    nodeAgent={agent}
                  />
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
