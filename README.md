# GraphAtom

**📄 Spec rendue : [acid3croco.github.io/graphatom](https://acid3croco.github.io/graphatom/)** · [dérivation v1](https://acid3croco.github.io/graphatom/archive/graph-runner-original.html)

Un noyau d'exécution durable pour orchestrer des agents LLM : une machine à états persistante qui exécute des tâches isolées et externalise les effets de façon réconciliable.

L'idée : des portes successives dont l'exécution est certaine — du code atomique qui guide, trace et corrige des agents au cours de leur cycle de vie. Pas de conversation inter-agents : la coordination *est* le graph.

## Documents

- [`index.html`](index.html) — **le noyau** (v2, simplifié) : six pièces, sept concepts utilisateur, quatre gardes de frontière, sept tables.
- [`cas-usage.html`](cas-usage.html) — **le cas d'usage pilote** : d'une carte Notion à la prod. Trois gestes humains, le reste est le rail. C'est ce scénario qui pilote les choix.
- [`pourquoi.html`](pourquoi.html) — **pourquoi pas Temporal/Restate/LangGraph** : les cinq garanties qu'aucun moteur existant ne donne. Le moteur est une commodité, les portes sont le produit.
- [`archive/graph-runner-original.html`](archive/graph-runner-original.html) — la dérivation complète (v1) : algèbre de capacités, particules élémentaires, forces d'interaction, les douze trous de frontière. Le raisonnement, pas la spécification.

## Le noyau en six pièces

1. **Révision immuable** — un graph publié est une donnée adressée par contenu, épinglée sur chaque item et run.
2. **`apply()` unique** — toute mutation d'état d'item passe par une seule fonction transactionnelle.
3. **Issues fermées** — chaque nœud déclare son énuméré, chaque issue a exactement une arête.
4. **Passerelle d'effets** — intention commise avant tout accès au monde ; `uncertain` est un état de première classe.
5. **Baux et fencing** — un bail expiré révoque l'autorité, pas seulement l'acceptation du résultat.
6. **Terminaison structurelle** — budget d'escalade fini que rien ne régénère, deadlines partout.

Hors noyau, en modules : EVAL, ADMIT, dialogue durable, gouverneur de flotte.

## Ce qu'on ne fera jamais

Périmètre négatif, assumé — ces refus *sont* le design :

- **Pas de fan-out ni de jointure** dans un item — le parallélisme, c'est plusieurs items. Un état unique ne représente pas plusieurs prédécesseurs actifs.
- **Pas de conversation inter-agents** — la coordination est le graph. N agents qui discutent produisent un transcript inauditable.
- **Pas de langage de workflow** — la déclaration reste de la configuration étroite au-dessus de blocs typés. Expressions et conditions arbitraires : non.
- **Pas de question ouverte** aux humains — toute question est fermée, avec des options et une deadline.
- **Pas d'exactement-une-fois** promis — au-moins-une-fois avec réconciliation, et un état « incertain » honnête là où la cible ne sait pas dédupliquer.
- **Pas de mutation d'état hors des verbes officiels** — pas d'UPDATE de dépannage, jamais.
- **Pas de multi-organisation** — un opérateur, ses graphs, ses agents.
