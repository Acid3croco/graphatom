# front — les vues du rail, en Next.js

Une app Next.js (App Router, TypeScript, Tailwind, shadcn/ui) qui rend les
mêmes vues que le canal web stdlib, en lisant son API JSON. Rien du côté
Python ne bouge : `src/graphatom/web.py` reste l'API et l'unique porte
d'écriture ; `front/` est un service séparé qui la consomme.

## Ce qu'il rend

| route        | ce qu'on y voit                                                     |
| ------------ | ------------------------------------------------------------------- |
| `/`          | les questions ouvertes, un bouton par option                         |
| `/items`     | tous les items : titre vers l'issue, état courant, lien vers l'item  |
| `/item/<id>` | graph, journal, runs, critères, effets, workspace, questions         |

Toutes les pages portent le bandeau heartbeat : il montre les deux
battements — « rail vivant il y a 3 s · canal GitHub il y a 2 s » — et
passe au rouge dès qu'un seul des deux se tait.

## Comment ça reste à jour

Rien ne se rafraîchit en entier. Chaque section de données est un composant
client qui sonde sa route de relais et n'en lit qu'une tranche :

| route de relais   | qui la sonde                                | tour  |
| ----------------- | ------------------------------------------- | ----- |
| `/api/item/<id>`  | les sections de `/item/<id>`                | 5 s   |
| `/api/items`      | le tableau de `/items`                      | 5 s   |
| `/api/questions`  | les cartes de `/`                           | 5 s   |
| `/api/heartbeat`  | le bandeau                                  | 2 s   |

Le tout tient dans `lib/live.ts` : un `useSWR` par section, et un `compare`
qui ne remplace l'état d'un composant que si *sa* tranche a bougé. Deux
sections de la même page partagent une requête, pas un rendu. Le premier
rendu vient du serveur, donc la page est juste au premier octet.

Conséquences, qui sont le but : la coquille (nav, bandeau, titres) ne se
rend qu'une fois ; le pan/zoom du graph, le focus d'un bouton et une saisie
en cours survivent aux tours de sondage sans rien avoir à sauver ; l'onglet
caché ne demande rien, et rattrape au retour. Aucun `router.refresh()`,
rien n'est poussé par le serveur, rien n'est mis en cache. Pas de state
manager, pas d'auth applicative.

## Ce qu'il écrit

Une seule route : `POST /api/answer`, qui lit le jeton anti-rejeu de
`GET /api/questions` de l'API et relaie `POST /answer`.

```sh
curl -X POST localhost:3000/api/answer \
  -H 'Content-Type: application/json' \
  -d '{"question_id": 1, "option": "retry"}'
```

## Le lancer

L'adresse de l'API est une variable d'environnement, déclarée et lue dans
`lib/config.ts` — seul endroit du front qui la connaît. Son défaut est le
nom de service du compose ; en local, on la pointe sur le port qu'écoute
`graphatom serve`.

```sh
uv run graphatom serve --port 8899 &      # depuis la racine du dépôt
cd front && npm install
npm run dev                               # http://localhost:3000
```

Et l'image, telle que la construit `tests/front_test.sh` :

```sh
bash tests/front_test.sh                  # build + contrôle de taille
docker run --rm --network host graphatom-front
```
