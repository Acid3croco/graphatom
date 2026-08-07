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

Toutes les pages portent le bandeau heartbeat, et lui seul sonde : un
`fetch` SWR toutes les 5 s sur `/api/heartbeat`, puis un rendu serveur
rafraîchi. Il montre les deux battements — « rail vivant il y a 3 s · canal
GitHub il y a 2 s » — et passe au rouge dès qu'un seul des deux se tait. Rien n'est poussé par le serveur, rien n'est mis en cache. Pas
de state manager, pas d'auth applicative.

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
