# Test frontend — gh:Acid3croco/graphatom#216

## Préparation

- `uv run python tests/seed.py` a échoué deux fois pendant le second `admit` : la voie était occupée par un item actif. Après attente du scheduler, les items existants `#1` et `#2` étaient utilisables ; l’item `#3` est resté actif. Aucun crash-test n’a été lancé.
- `scripts/front-env.sh` a réussi. Commande : `scripts/front-env.sh`. Durée mesurée : **13 s**. Aucun install de secours.
- API lancée sur `http://127.0.0.1:8899`, puis front lancé depuis `front/` avec `GRAPHATOM_API_URL=http://127.0.0.1:8899 PORT=3899`.

## Pages et preuves navigateur

| Route | HTTP | DOM rendu | Screenshot |
|---|---:|---|---|
| `/` | 200 | Page `questions`, avec état vide explicite `Aucune` | `frontend-evidence/root.png`, 28 499 octets |
| `/items` | 200 | Table `items`, états `close` et `ingest`, liens `/item/1`, `/item/2`, `/item/3` | `frontend-evidence/_items.png`, 47 744 octets |
| `/item/1` | 200 | État courant `close`, sections `graph`, `journal`, `runs`, `effets`, et `<svg role="img" aria-label="graph supervision, courant close, orientation TB">` | `frontend-evidence/_item_1.png`, 154 575 octets |

Les liens ont été contrôlés sur leur `href` complet dans le DOM rendu. Les PNG sont valides et non vides (`1440x1200`).

## Critères

1. **Vérifié côté backend, non directement vérifiable dans le navigateur.** Le front rend la route d’item et ses données ; les refus item/run incohérents sont couverts par le test API déjà passé par `test_backend`.
2. **Vérifié côté backend, non directement vérifiable dans le navigateur.** Le navigateur ne choisit pas de chemin de trace ; la sécurité du workspace est couverte par le test API déjà passé.
3. **Partiellement visible dans le DOM.** `/item/1` rend les sections `journal` et `runs`, ainsi que les états `close`, `ingest` et `WAIT`. Le type et l’état de chaque source absente ou vide restent un contrat API, couvert par le test API déjà passé.
4. **Non directement vérifiable dans le navigateur.** Le curseur octet-à-octet et l’absence d’écriture sont des propriétés API, couvertes par le test API déjà passé.
5. **Visible pour les données rendues.** La page `/item/1` affiche un run terminal et son état courant `close`; les lectures actif/terminal du contrat sont couvertes par le test API déjà passé.

Résultat frontend : **pass**. Les trois routes obligatoires répondent 200, le graphe SVG est présent sur `/item/1`, les états et sections attendus sont rendus, et les captures sont non vides.
