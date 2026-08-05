# GraphAtom

**📄 Spec rendue : [acid3croco.github.io/graphatom](https://acid3croco.github.io/graphatom/)** · [dérivation v1](https://acid3croco.github.io/graphatom/archive/graph-runner-original.html)

Un noyau d'exécution durable pour orchestrer des agents LLM : une machine à états persistante qui exécute des tâches isolées et externalise les effets de façon réconciliable.

L'idée : des portes successives dont l'exécution est certaine — du code atomique qui guide, trace et corrige des agents au cours de leur cycle de vie. Pas de conversation inter-agents : la coordination *est* le graph.

## Documents

- [`index.html`](index.html) — **le noyau** (v2, simplifié) : six pièces, sept concepts utilisateur, quatre gardes de frontière, sept tables.
- [`archive/graph-runner-original.html`](archive/graph-runner-original.html) — la dérivation complète (v1) : algèbre de capacités, particules élémentaires, forces d'interaction, les douze trous de frontière. Le raisonnement, pas la spécification.

## Le noyau en six pièces

1. **Révision immuable** — un graph publié est une donnée adressée par contenu, épinglée sur chaque item et run.
2. **`apply()` unique** — toute mutation d'état d'item passe par une seule fonction transactionnelle.
3. **Issues fermées** — chaque nœud déclare son énuméré, chaque issue a exactement une arête.
4. **Passerelle d'effets** — intention commise avant tout accès au monde ; `uncertain` est un état de première classe.
5. **Baux et fencing** — un bail expiré révoque l'autorité, pas seulement l'acceptation du résultat.
6. **Terminaison structurelle** — budget d'escalade fini que rien ne régénère, deadlines partout.

Hors noyau, en modules : EVAL, ADMIT, dialogue durable, gouverneur de flotte.
