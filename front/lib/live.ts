"use client";

/**
 * Le sondage du front : une route de données, une tranche, un rendu local.
 *
 * Rien ne se rafraîchit en entier ici. Chaque section de page est un
 * composant client qui sonde la route de relais qui la nourrit et n'en
 * lit qu'une tranche — le journal, les runs, le graph, les critères. Deux
 * sections de la même page partagent la même route : SWR les sert d'une
 * seule requête par tour, c'est son cache par clé qui le fait.
 *
 * `compare` est ce qui rend le rendu local : SWR ne remplace l'état d'un
 * composant que si *sa* tranche a bougé. Une transition change le journal
 * et le graph ; la liste des fichiers, elle, ne rend rien de nouveau.
 *
 * Deux règles de plus, écrites même si SWR les tient par défaut, parce que
 * ce sont des contrats du front et non des détails : rien n'est demandé
 * quand l'onglet est caché — la page cachée ne montre rien à personne —,
 * et le retour de l'onglet rattrape sans attendre le tour suivant.
 */
import useSWR from "swr";

import type { Item, ItemView, Question } from "@/lib/api";

/** Le tour de sondage des données, et celui du battement — plus court. */
export const POLL_MS = 5000;
export const BEAT_MS = 2000;

/** Les routes de relais du front, sondées par les pages. */
export const ITEMS_FEED = "/api/items";
export const QUESTIONS_FEED = "/api/questions";
export const BEAT_FEED = "/api/heartbeat";
export const itemFeed = (id: number) => `/api/item/${id}`;

// une API muette rend un 502 à corps d'erreur, qui n'est pas la donnée
// attendue : c'est une erreur pour SWR, pas un objet à lire — sinon la
// section affiche le message d'erreur à la place de ce qu'elle montrait
const fetcher = async (url: string) => {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`${url} : ${res.status}`);
  }
  return res.json();
};

/** Deux tranches sont la même quand elles s'écrivent pareil en JSON. */
const same = (a: unknown, b: unknown) =>
  JSON.stringify(a) === JSON.stringify(b);

/**
 * La tranche `select` d'une route sondée, ou `initial` avant le premier
 * tour — le rendu serveur l'a déjà posée, la page n'a donc rien de vide à
 * montrer, ni au premier octet ni à l'hydratation.
 */
export function useSlice<T, S>(
  url: string,
  select: (data: T) => S,
  initial: S,
  ms: number = POLL_MS,
): S {
  const { data } = useSWR<T>(url, fetcher, {
    refreshInterval: ms,
    refreshWhenHidden: false,
    revalidateOnFocus: true,
    compare: (a, b) => same(a && select(a), b && select(b)),
  });
  return data === undefined ? initial : select(data);
}

/** Une tranche du détail d'un item. */
export function useItem<S>(
  id: number,
  select: (view: ItemView) => S,
  initial: S,
): S {
  return useSlice<ItemView, S>(itemFeed(id), select, initial);
}

/** Tous les items. */
export function useItems(initial: Item[]): Item[] {
  return useSlice<Item[], Item[]>(ITEMS_FEED, (items) => items, initial);
}

/** Les questions ouvertes, sans le jeton — il ne quitte pas le serveur. */
export function useQuestions(initial: Question[]): Question[] {
  return useSlice<Question[], Question[]>(
    QUESTIONS_FEED,
    (questions) => questions,
    initial,
  );
}
