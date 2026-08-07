/**
 * L'unique endroit qui sait où joindre l'API du rail.
 *
 * L'API, c'est le canal web stdlib — `graphatom serve` — et ses routes
 * `/api/…`. Son adresse est une variable d'environnement, jamais une URL
 * en dur : le défaut est le nom de service du compose, pour qu'un
 * conteneur lancé sans rien la trouve quand même ; en local, on pointe la
 * variable sur le port que `graphatom serve` écoute.
 *
 * Rien d'autre dans `front/` ne lit l'environnement : tout ce qui parle à
 * l'API passe par `lib/api.ts`, qui passe par ici.
 */
export const API_URL = (
  process.env.GRAPHATOM_API_URL ?? "http://web:8848"
).replace(/\/+$/, "");
