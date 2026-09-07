/**
 * Les sections de la page d'un item, chacune vivante pour son compte.
 *
 * Une section = un composant client qui sonde `/api/item/<id>` et n'y lit
 * que sa tranche. Le tour de sondage est commun — une requête toutes les
 * cinq secondes pour toute la page — mais le rendu ne l'est pas : une
 * transition repeint le journal et le graph, et laisse les autres
 * sections exactement où elles étaient, sans les remonter. C'est ce qui
 * fait survivre le cadrage du graph, le focus d'un bouton et une saisie en
 * cours, sans rien avoir à sauver ni à restaurer.
 *
 * Chaque section reçoit sa tranche déjà rendue par le serveur (`initial`)
 * : la page est juste au premier octet, avant le premier tour.
 *
 * Une section vide ne rend rien — et se met à exister toute seule quand sa
 * tranche arrive, un premier run par exemple.
 *
 * C'est la page la plus large du front, et elle doit tenir dans 360 px :
 * le graph et les tables glissent chacun dans leur bloc, jamais la page.
 *
 * Chaque section vit dans son module sous `components/item/` ; ce fichier
 * est la porte par laquelle la page les prend, dans l'ordre où on les lit.
 */
export { ItemHeader } from "@/components/item/header";
export { ItemQuestions } from "@/components/item/questions";
export { ItemGraph } from "@/components/item/graph";
export { ItemJournal } from "@/components/item/journal";
export { ItemRuns } from "@/components/item/runs";
export { ItemCriteria } from "@/components/item/criteria";
export { ItemEffects } from "@/components/item/effects";
export { ItemFiles } from "@/components/item/files";
