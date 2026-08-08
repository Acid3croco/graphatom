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

**Deux compteurs, jamais confondus.** Les tentatives par nœud
(`MAX_ATTEMPTS`) sont un amortisseur local : elles se comptent sur le
*passage* courant de l'item. Une réponse humaine sur un nœud d'escalade —
`retry` — ouvre le passage suivant : en aval, les nœuds repartent à la
tentative 1, pleine marge, parce que l'humain vient précisément de juger
qu'un nouvel essai complet valait le coup. Le budget d'escalades, lui, ne
se régénère jamais : c'est lui, et lui seul, qui garantit la terminaison.
Il compte les tours de boucle, pas les traversées — la première visite
d'un nœud d'escalade dans le passage courant est gratuite, la re-entrée
décompte. L'histoire n'est pas réécrite — les tentatives des passages précédents
restent dans `node_run`, et `/item/<id>` donne le passage de chaque run.

Hors noyau, en modules : EVAL, ADMIT, dialogue durable, gouverneur de flotte.

## Lancer le squelette (milestone 1)

```sh
docker run -d --name graphatom-pg -e POSTGRES_PASSWORD=graphatom \
  -e POSTGRES_USER=graphatom -e POSTGRES_DB=graphatom \
  -p 127.0.0.1:54321:5432 postgres:17

uv run graphatom init-db
uv run graphatom publish examples/supervision.json   # → révision
uv run graphatom admit <révision> "pipeline-x:oom"
uv run graphatom run                                 # l'ordonnanceur
uv run graphatom journal 1                           # la trajectoire
uv run graphatom questions && uv run graphatom answer 1 retry

uv run python tests/crash_test.py                    # le critère du milestone :
                                                     # SIGKILL en plein vol = cas nominal
uv run python tests/validate_test.py                 # la validation statique, sans base
uv run python tests/orphans_test.py                  # un bail expiré tue tout le
                                                     # groupe de l'agent, et le
                                                     # faucheur tue l'orphelin d'un
                                                     # worker mort, sans base
uv run python tests/reconnect_test.py                # couper la base sous le worker :
                                                     # il se reconnecte et reprend
uv run python tests/links_test.py                    # les liens du frontend vers
                                                     # l'issue et la PR, et le titre
                                                     # dans la table, sans base
uv run python tests/depends_test.py                  # `Depends-on: #N` : l'admission
                                                     # attend, sans base ni réseau
uv run python tests/hermetic_test.py                 # ce qu'un agent lance ne voit
                                                     # ni la base ni le dépôt de la
                                                     # production
uv run python tests/passage_test.py                  # un retry d'escalade rend la
                                                     # marge de tentatives des nœuds,
                                                     # jamais le budget d'escalades
uv run python tests/heartbeat_test.py                # les battements du worker et du
                                                     # canal GitHub : le front dit
                                                     # quand plus rien ne tourne
uv run python tests/live_test.py                     # le marqueur de fraîcheur des
                                                     # pages : stable à données
                                                     # égales, sans base
uv run python tests/shell_test.py                    # les nœuds shell de code-task,
                                                     # joués tels quels : sans base,
                                                     # sans modèle, sans docker
uv run python tests/checklist_test.py                # le nœud validate : le routage
                                                     # du graph, et la checklist citée
                                                     # dans la question de review
uv run python tests/criteria_test.py                 # le nœud scope qui parle : les
                                                     # critères postés sur l'issue,
                                                     # et la sortie `unclear`
uv run python tests/api_test.py                      # l'API JSON du canal web : les
                                                     # mêmes vues en données, sans
                                                     # base ni serveur
uv run python tests/answer_test.py                   # `/answer` : la première ligne
                                                     # décide, la prose passe, et la
                                                     # commande mal formée se dit
```

Les tests ne touchent jamais au `data/` du repo : chacun travaille dans un
répertoire temporaire à lui, effacé à la sortie. Le `data/` d'un checkout,
c'est le workspace vivant d'un rail — les items en cours y écrivent.

## Le canal humain (milestone 2)

```sh
uv run graphatom serve                               # http://127.0.0.1:8848
uv run graphatom serve --notify-cmd 'mon-hook.sh'    # JSON de la question sur stdin
```

Un web local en stdlib, zéro dépendance : les questions ouvertes, un bouton
par option, et rien d'autre. Le canal n'écrit jamais l'état d'un item — il
enregistre la réponse, l'ordonnanceur route au tick suivant. Une page
disponible n'est pas un oncall notifié : `--notify-cmd` lance une commande
à chaque question ouverte (au-moins-une-fois — au redémarrage, on renotifie).

L'en-tête de chaque page porte le battement du worker — « rail vivant il y
a 3 s », ou un bandeau rouge « rail à l'arrêt depuis HH:MM — les états
affichés sont figés ». Une page qui montre des états doit dire quand plus
personne ne les fait avancer : voir [le battement](#le-battement-du-worker--railstalled).

Chaque page se suit sans rechargement : elle porte un marqueur de fraîcheur
en meta (`graphatom-version` — la version de l'item, ou la plus haute des
items listés, et l'état du battement), et une quinzaine de lignes de JS
vanilla refont un `fetch` de la même URL toutes les 5 s. Marqueur identique :
rien ne bouge. Marqueur différent : seul le contenu du conteneur `#live` est
remplacé, donc le scroll reste où il est. Le polling s'arrête quand l'onglet
est caché, et ne patche jamais pendant qu'un élément du conteneur a le focus
— le formulaire de réponse ne bouge pas sous la souris. Le serveur, lui, ne
change pas : toujours du rendu complet côté serveur, et sans JavaScript le
rechargement d'avant tient toujours, dans un `noscript`.

La boucle avec GitHub va dans les deux sens : les commentaires du rail
pointent vers le frontend, et le frontend renvoie vers GitHub. Partout où
un sujet a la forme `gh:<owner>/<repo>#<num>` — page des questions, `/items`,
en-tête de `/item/<id>` — il devient un lien vers l'issue ; et quand le cycle
a produit une PR, `/item/<id>` l'affiche à côté, lue dans le `release.json`
que le nœud release écrit dans le workspace. Tout se construit depuis la base
et le workspace : aucun appel à l'API GitHub depuis le web. Un sujet d'une
autre forme reste du texte brut — le kernel, lui, ne connaît pas GitHub.

Le titre de l'issue se lit partout, et un numéro nu ne dit plus rien tout
seul : le canal GitHub le range sur le sujet (`subject.title`) au moment de
l'admission — il l'a sous la main — et le repose à chaque tick si quelqu'un
l'édite. `/items` porte donc une colonne `issue` (le numéro, vers GitHub) et
une colonne `titre` (vers `/item/<id>`), l'en-tête de `/item/<id>` l'affiche
à côté du sujet, et chaque question dit de quel travail elle parle. Un sujet
d'un autre canal n'a pas de titre : cellule vide, rien de cassé.

### Les mêmes vues en JSON

```sh
curl -s localhost:8848/api/items | jq '.[0]'
curl -s localhost:8848/api/item/1 | jq 'keys'
curl -s localhost:8848/api/questions | jq '.token'
curl -s localhost:8848/api/graphs | jq '.[0]'
curl -s localhost:8848/api/graph/<rév> | jq '.nodes | keys'
curl -s -H 'Accept: application/json' \
     -d "question_id=1&option=retry&token=<jeton>" localhost:8848/answer
```

Six lectures, pour un client qui rend les pages lui-même : `/api/items`
(la table, avec l'état, le statut et les liens issue et PR), `/api/item/<id>`
(l'item entier : `item`, `graph`, `journal`, `runs`, `effects`, `questions`,
`criteria`, `files`), `/api/questions` (les questions ouvertes),
`/api/heartbeat` (les deux battements bruts, `rail` et `github-sync`, chacun
avec son horodatage, son âge et son état périmé), `/api/graphs` (les
révisions publiées : nom, date, nombre d'items qui la portent) et
`/api/graph/<rév>` (le bundle entier de cette révision, config des nœuds et
prompts compris). Une projection, pas un second modèle :
mêmes requêtes, mêmes durées tirées du journal, mêmes totaux de tokens que
les pages — seul le rendu change. Le `graph` porte les nœuds, les arêtes et
le nœud courant : le SVG se redessine sans relire la base. Toujours zéro
dépendance : `json.dumps` et le `http.server` de la stdlib.

Le jeton anti-rejeu, jusqu'ici enfoui dans le formulaire rendu, se lit dans
`/api/questions` — c'est ce qui permet de répondre sans charger la page.
`POST /answer` reste l'unique porte d'écriture et sert les deux clients :
avec `Accept: application/json` il rend `{"ok": …, "message": "…"}`, sans lui
la redirection 303 du formulaire, inchangée. Une route `/api/` qui rate rend
un objet à `error` — jamais une page, jamais une trace.

Refusé, exprès : auth, comptes, exposition Internet, WebSocket, édition de
graphs, mutation d'items, dashboard. Voir les WAIT, répondre une fois parmi
les options, laisser le rail reprendre.

## Le canal GitHub + docker (milestone 3a)

Le rail branché sur un repo — dogfoodé sur ce repo même :

```sh
GITHUB_TOKEN=$(gh auth token) docker compose up -d --build
```

Cinq services : Postgres, l'ordonnanceur, le canal GitHub (polling), le
canal web (API et secours, port 8850) et le front Next.js (public, port
8851). Ensuite tout se passe sur GitHub :

1. poser le label `graphatom` sur une issue → admission (une seule fois par
   issue ; différée si le corps déclare une dépendance encore ouverte)
2. le rail accuse la prise en charge en commentaire : item, graph, lien
   trajectoire — et pose le label d'état `rail:<état>`
3. le rail pose sa question fermée en commentaire
4. répondre par un commentaire `/answer <id> <option>` (auteurs autorisés :
   `GRAPHATOM_ANSWERERS`, défaut : le propriétaire du repo)
5. le rail route — le label suit chaque transition, et l'accusé est réécrit
   pour porter la trajectoire en direct — puis poste le rapport terminal,
   le journal en table, et retire le label d'état

Règles : GitHub est l'interface humaine et la cible des effets, Postgres
reste l'unique autorité d'exécution. Chaque prise de parole du rail est un
effet — clé logique, intention commise avant, réconciliation par marqueur
dans les commentaires : un crash entre le POST et le marquage ne produit
jamais de doublon. Le label `rail:<état>` n'est pas un état : c'est une
projection possédée par le rail, comme la colonne d'un board — jamais lue,
repeinte à chaque tick depuis la base ; un label bricolé à la main revient
à sa place tout seul. Le repeint ne voit que les issues ouvertes, mais le
retrait au terminal vise l'issue par son numéro : une issue fermée par le
merge de sa propre PR ne garde pas son label. La trajectoire est une
projection de même nature — le commentaire d'accusé est réécrit à chaque
transition, reconstruit depuis la base, jamais complété à l'aveugle ; une
édition ne notifie personne, donc l'issue est un tableau de bord en direct
sans un seul mail. Aucun parsing de langage naturel, aucune lecture de
GitHub comme état d'item. La démo : issues [#7](https://github.com/Acid3croco/graphatom/issues/7)
et [#8](https://github.com/Acid3croco/graphatom/issues/8).

### Dépendances entre tâches : `Depends-on: #N`

Une tâche qui ne doit démarrer qu'après une autre le déclare dans le corps
de son issue — une ligne par dépendance, grammaire fermée comme `/answer` :

```
Depends-on: #29
```

Le corps est lu au moment de l'admission seulement, jamais relu en cours de
route. Tant qu'une des issues visées est ouverte, l'admission est différée :
aucun item n'est créé, l'issue reçoit un commentaire « en attente de #29 »
— une seule fois, à clé logique — et porte le label `rail:blocked`. Quand la
dernière dépendance se ferme, l'admission part au tick suivant, par le chemin
normal. Une dépendance vers une issue inexistante ou vers elle-même est
ignorée, mais dite en commentaire : rien ne passe en silence.

Rien de tout ça n'entre en base : pas de graphe de dépendances, la condition
se réévalue à chaque tick sur les issues non admises, et le kernel ne change
pas — c'est de l'admission, donc du ressort du canal. GitHub ne gate rien
nativement, les task lists et les sub-issues sont de la visualisation : on
peut poser en plus `- [ ] #29` dans le corps pour la lisibilité, mais la
vérité du rail reste la ligne `Depends-on:`. Deux issues ouvertes qui
dépendent l'une de l'autre se bloquent pour toujours — c'est visible (deux
`rail:blocked`), et c'est à l'humain de casser le cycle en éditant un corps.

### Le battement du worker : `rail:stalled`

Un worker mort ne dit rien, et c'est le problème : les items gardent leurs
états actifs — `test_frontend`, `review`… — alors que plus rien ne tourne.
Pas de faucheur, donc pas de classement des agents expirés, donc pas
d'escalade : aucune question, aucun signal, nulle part. C'est arrivé quatre
fois en un jour, jusqu'à quarante minutes de stase invisible. L'absence de
signal doit devenir un signal.

L'ordonnanceur tamponne donc un battement à chaque tick — une ligne en base
(`heartbeat`, `who = 'rail'`, UPSERT), écrite dans le tick comme le reste :
pas de thread dédié, pas de timer. Plusieurs workers tamponnent la même
ligne : c'est « au moins un vivant » qu'on mesure, jamais qui est vivant.

Le canal GitHub est un second processus, et son silence à lui ne se voyait
nulle part : il tamponne donc sa propre ligne (`who = 'github-sync'`) à
chaque tour de sa boucle, hors du tick, pour dire que la boucle tourne — et
non que GitHub répond.

Deux surfaces les lisent, et deux suffisent :

- **le frontend**, dans l'en-tête commun de chaque page — « rail vivant il y
  a 3 s · canal GitHub il y a 2 s », et le bandeau rouge dès qu'un seul des
  deux dépasse deux minutes : « … — les états affichés sont figés » ; une
  requête d'une ligne par batteur ;
- **le canal GitHub**, qui pose `rail:stalled` sur les issues des items
  actifs et le retire au retour du battement, comme les autres
  labels-projections. C'est le point clé : le sync est un processus séparé
  du worker et survit à sa mort — le problème se voit sur GitHub
  précisément quand le worker ne peut plus parler.

Pas d'alerte externe de plus : l'opérateur regarde déjà l'une des deux. Et
le sync ne lit le battement que pour cette projection — il ne prend aucune
décision d'état avec : GitHub reste une projection, la base reste l'autorité.

### Config de déploiement : épinglée dans le repo

Un redéploiement (`docker compose up -d`) ne doit jamais dépendre des
variables du shell qui l'invoque — le token excepté. Sinon le rail se
redéploie lui-même sans elles et perd sa config sans un mot : c'est
arrivé, `GRAPHATOM_TAKE_ALL` vidé, admission éteinte neuf heures.

La config de cette instance vit donc dans le [`.env`](.env) commité à la
racine, que compose lit tout seul :

| variable | rôle |
| --- | --- |
| `GRAPHATOM_TAKE_ALL=1` | prendre en charge toute issue ouverte, sans attendre le label — pour un repo dont le rail est le seul mainteneur |
| `GRAPHATOM_ANSWERERS` | les auteurs autorisés à répondre `/answer` |
| `GRAPHATOM_WEB_URL` | l'URL publique de l'UI, celle des liens « Trajectoire » postés sur GitHub |
| `GRAPHATOM_PROXY_NET` / `..._EXTERNAL` | le réseau docker du proxy que le service `front` rejoint |

Pas de secret dedans : `GITHUB_TOKEN` reste fourni par le shell et garde
son garde-fou (`${GITHUB_TOKEN:?…}`). Les défauts du `docker-compose.yml`
restent ceux d'un déploiement générique — le compose est générique, le
`.env` est cette instance.

**Le raccordement au proxy suit la même règle.** L'UI est exposée sur
`graphatom.veyxzer.com` par le Traefik de l'hôte, avec basicauth au bord
— l'app reste sans auth, refus assumé. Le raccordement réseau est déclaré
dans le compose, jamais par un `docker network connect` à la main : celui-là
n'aurait pas survécu au premier `up -d` — même panne silencieuse que
`TAKE_ALL`. Sans les deux variables du `.env`, compose crée son propre
réseau `graphatom-proxy` : un déploiement sans proxy ne casse pas.

### La bordure : `front` public, `web` interne

Un seul service est à la bordure, et c'est le **front Next.js** :

| service | réseaux | port local | qui le joint |
| --- | --- | --- | --- |
| `front` | `default`, `proxy` | `127.0.0.1:8851` | le Traefik de l'hôte, sur `graphatom.veyxzer.com` |
| `web` | `default` | `127.0.0.1:8850` | `front`, par le réseau interne (`http://web:8848`) |

`web` ne rejoint plus le réseau du proxy : l'API ne sort pas. Elle garde
tout ce qu'elle avait — les six lectures `/api/…`, l'unique porte
d'écriture `POST /answer`, la route des fichiers de workspace
(`/item/<id>/file/<nom>`, les screenshots des agents) et ses pages HTML
complètes. Le front ne fait que la consommer : il relaie `POST /answer` par
sa route serveur `/api/answer`, et les previews par sa route
`/item/<id>/file/<nom>`, montée au même chemin que celle de l'API — le
navigateur ne joint donc que le front, `web` sert derrière. **Et c'est le
canal de secours** : le jour où le front ne builde pas,
`http://127.0.0.1:8850/items` répond toujours, depuis la même base.

Le routeur de bordure est déclaré en labels sur `front` dans le
[`docker-compose.yml`](docker-compose.yml) — `traefik.enable`, la règle
``Host(`graphatom.veyxzer.com`)``, le port `3000` du conteneur, et
`traefik.docker.network` sur `${GRAPHATOM_PROXY_NET}`, sans quoi Traefik
choisirait un réseau au hasard parmi les deux.

**Ces labels ne servent qu'à un Traefik qui lit les labels docker.** Si le
routeur de l'hôte est configuré par fichier — c'est le cas ici, le Traefik
de Coolify porte la basicauth dans sa config dynamique —, les labels sont
inertes et il reste **un pas à la main, une fois** : dans le fichier de
config dynamique de l'hôte, faire pointer le service du routeur
`graphatom.veyxzer.com` vers `http://<conteneur front>:3000` au lieu de
`http://<conteneur web>:8848`. Tant que ce pas n'est pas fait, la bordure
sert encore les pages du web stdlib : tout répond 200, et rien ne le dit —
la preuve qui tranche est un `_next` dans le HTML. La basicauth, elle, n'a
pas à bouger : elle est sur le routeur, pas sur le service.

**Où lire cette preuve.** Pas depuis Internet sans les identifiants : la
basicauth du routeur rend `401` à toute requête anonyme, sur `/` comme sur
`/items` ou `/item/<id>` — un `curl` nu ne distingue donc pas un front en
place d'un stdlib en place. La preuve se lit en local, sur les deux ports
que publie le compose, et c'est ce que fait `verify_deploy` après chaque
déploiement :

```sh
curl -s http://127.0.0.1:8851/items | grep -c _next   # le front rendu
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8850/items  # le secours
```

## De vrais agents dans les blocs (milestone 3b)

Un nœud ACT / CHECK / JUDGE peut déclarer `config.agent` — le bloc écrit
alors `prompt.md` dans le workspace, lance la commande configurée, et lit
`outcome.json` :

```json
"agent": {
  "cmd": "claude --dangerously-skip-permissions -p \"$(cat prompt.md)\"",
  "timeout_s": 540,
  "prompt": "Agent de test frontend… chromium --headless=new…"
}
```

Le contrat est minuscule et agnostique : n'importe quel agent CLI (claude,
codex, pi…) fait l'affaire ; le kernel n'en connaît aucun. Pas
d'`outcome.json` valide → `crashed`, retenté puis escaladé — comme
n'importe quel bloc.

**Une extension optionnelle : `usage.json`.** Si la tentative en laisse
un dans le workspace, le bloc le fusionne dans le résultat du run, sous
la clé `usage` — les types de tokens tels que l'agent les rapporte
(`input_tokens`, `output_tokens`, les caches, le coût), sans que
personne ici les interprète. Pas de `usage.json` : rien, et l'agent
reste un citoyen de première classe. C'est le `cmd` du graph qui produit
ce fichier, jamais le noyau : pour le CLI claude, `--output-format json`
sort un JSON final dont un `jq` extrait l'usage, le texte de la réponse
partant dans le log comme avant.

```sh
claude … --output-format json -p "$(cat prompt.md)" > agent.json; RC=$?
jq -r '.result // .' agent.json || cat agent.json
jq -e 'select(.usage|objects) | .usage + {total_cost_usd}' agent.json > usage.json 2>/dev/null || rm -f usage.json
rm -f agent.json
exit $RC
```

**Les traces sont auditables, jamais écrasées.** Le journal, le prompt et
l'usage d'une tentative portent le nœud, le passage et la tentative dans
leur nom — `agent-<nœud>-<passage>-<tentative>.log`, `prompt-…md`,
`usage-…json` : le nœud suivant n'écrase plus rien, et la section
workspace de `/item/<id>` montre l'histoire complète d'un item. Seul
`outcome.json` reste transitoire, purgé avant chaque tentative — son
contenu vit déjà dans le résultat du run en base.

**Un processus lancé par un agent ne voit jamais ni la base ni le dépôt de
la production.** C'est la règle. Elle a coûté une implémentation : un agent
de test avait lancé un ordonnanceur sur la base jetable *partagée*, en
gardant le `GRAPHATOM_REPO_DIR` de la production dans son environnement ;
le cleanup d'un item de test a retiré le worktree d'un vrai item portant le
même numéro. Trois barrières indépendantes la tiennent maintenant :

1. **Une base jetable par item.** `GRAPHATOM_AGENT_DSN` désigne une
   *instance*, pas une base : le bloc y crée `graphatom_test_item_<id>` à la
   volée (idempotent) et pose la `GRAPHATOM_DSN` de l'agent dessus. Deux
   items qui testent en même temps ne se marchent plus dessus, et un
   `init-db --drop` ne vide que son propre bac à sable. Le rôle doit avoir
   `CREATEDB` sur cette instance : c'est le cas de `graphatom`, le
   `POSTGRES_USER` du conteneur postgres.
2. **Le `REPO_DIR` ne fuit pas.** Les scripts qui lancent un ordonnanceur —
   `crash_test.py`, `reconnect_test.py`, la fixture `seed.py` — épinglent
   `GRAPHATOM_REPO_DIR` sur *leur* dépôt, et retirent l'instance jetable de
   l'environnement, avant de lancer quoi que ce soit. Un rail de test qui
   crée des worktrees les crée alors sous son propre `.worktrees/`.
3. **Le cleanup vérifie sa cible.** Avant `worktree remove --force`, le
   shell exige que le worktree soit enregistré dans *son* `REPO_DIR`, sous
   `.worktrees/`, et qu'il porte la branche `rail/issue-<num>` du sujet de
   *son* item (`GRAPHATOM_SUBJECT_KEY`). Sinon il ne touche à rien, l'écrit
   dans `cleanup.md`, et rend `done` quand même — un cleanup ne bloque
   jamais une sortie. Le même nœud drop la base jetable de l'item
   (`graphatom drop-agent-db`), qui refuse toute base ne portant pas le
   préfixe `graphatom_test_item_`.

[`tests/hermetic_test.py`](tests/hermetic_test.py) exerce les trois, cleanup
du graph joué tel quel sur un dépôt jetable. Rien de tout cela n'est dans le
noyau : c'est l'affaire du canal et des `cmd` du graph.

**La révocation a deux moitiés** : l'autorité en base, et le processus.
L'agent tourne dans sa propre session — au timeout, le bloc révoque tout
le groupe (SIGTERM, grâce, SIGKILL), pas seulement le shell : un
descendant ne survit pas au bail. Mais si c'est le *worker* qui meurt,
il n'y a plus de handle pour tuer quoi que ce soit ; le faucheur du
suivant bumpe bien le fence, et l'orphelin travaille quand même. Alors le
bloc persiste son pgid dans `data/item-N/agent.pgid` au lancement, et
l'efface à la fin normale — succès comme crash collecté. Le faucheur, en
fauchant un run, y trouve de quoi appliquer la même séquence sans handle.
Trois garde-fous, parce que tuer un innocent est pire qu'un orphelin : la
trace doit appartenir au run fauché, le chef du groupe doit toujours être
celui qu'on a lancé — un pid se recycle, pas son couple (boot, date de
naissance) — et le faucheur ne se fauche jamais lui-même. C'est du POSIX
pur (`os.killpg`), le kernel ne connaît toujours aucun agent.

Une tentative crashée rend son **autopsie** dans le résultat du run —
`exit_code` (négatif = le signal qui l'a tué), `log_tail` (les 20 dernières
lignes d'`agent-<nœud>-<passage>-<tentative>.log`, bornées à 2 000 caractères)
et `timeout` (vrai si c'est le bail du bloc qui a fauché l'agent). La table
des runs de `/item/<id>` l'affiche : le post-mortem se lit sur la page, pas en
fouillant le workspace à la main.

**Un timeout ne se relance jamais tout seul.** Une tentative qui déborde de
son budget rend l'issue `timed_out`, pas `crashed` — et le noyau ne la
retente pas sur place : elle part droit sur `escalate_to`, dès la première
tentative et quel que soit le compteur. Relancer à l'identique brûlerait un
cycle complet de plus pour retomber au même endroit ; c'est l'humain qui
tranche entre `retry` — un passage neuf, pleine marge — et `abandon`. Une
panne d'infra (`crashed`) ou une sortie malformée (`invalid_result`), elles,
gardent leur seconde chance automatique : elles se rejouent. Le faucheur
applique la même règle sans modèle : à l'expiration d'un bail, le pgid
persisté dit si l'agent travaillait encore — groupe vivant, c'est un
dépassement (`timed_out`) ; groupe déjà mort, c'est une panne (`crashed`).
La question d'escalade née d'un timeout porte le budget dépassé (le
`timeout_s` du nœud) et la queue du journal, directement sur l'issue. Rien
à déclarer dans les graphs : `timed_out` n'est l'arête de personne, c'est
le défaut central de `_route` qui change de branche.
[`tests/escalade_timeout_test.py`](tests/escalade_timeout_test.py) fige la
règle de bout en bout.

**Ce que coûte un cycle se lit sur la même page.** Chaque transition du
journal porte sa durée — l'écart avec la précédente, tentatives comprises —,
chaque run la sienne et ses tokens, et l'en-tête le temps total de l'item
avec le total par type de token. Rien de nouveau en base : les durées
sortent des horodatages du journal, les tokens du résultat des runs.

[`examples/code-task.json`](examples/code-task.json) est le graph qui fait
tourner ce repo : **jugement de la taille et des critères** (`scope`),
implémentation par agent, **agent de test backend**
(imports, crash-test), **agent de test frontend au navigateur headless**
(le DOM rendu et des screenshots, pas du curl), **validation des critères
un par un** (`validate`), puis review humaine —
question fermée sur l'issue GitHub. La boucle se ferme ensuite toute
seule : **release** (commit, push, PR, merge surveillé jusqu'au SHA),
**deploy** (`docker compose up -d --build github-sync web front`) et
**verify_deploy** (conteneurs en marche, `/items` rendu par le front, le
secours en 200, logs du sync propres) — ces deux derniers sans aucun
modèle, comme la préparation du worktree : du shell.

**Une porte de jugement avant la construction.** Toute issue partait droit
en implémentation, quelle que soit sa taille : une issue trop grande
produisait un cycle interminable — des timeouts d'`implement` en série — ou
un résultat bâclé, et les critères de succès restaient implicites, chaque
agent de test relisant l'issue à sa façon. Le nœud **`scope`**, entre
`worktree` et `implement`, tranche d'abord. Il ne code jamais, ne touche pas
au worktree : il lit, il écrit, il crée des issues.

- **Atomique** → il écrit `criteria.md` dans le workspace de l'item : la
  liste *fermée*, numérotée et falsifiable des critères de succès, chacun
  avec la preuve qui le tranche — une commande, un fichier, un élément du
  DOM rendu. Fermée veut dire : ce qui n'y est pas n'est pas demandé.
  Outcome `ready`, l'implémentation part.
- **Trop grande** (plusieurs livrables indépendants, ou infaisable en un
  cycle) → il la **découpe** : une issue fille par livrable (`gh issue
  create --label graphatom`), chacune atomique avec ses critères dans son
  corps, chaînées par `Depends-on: #N` quand l'ordre compte — deux filles
  qui touchent les mêmes fichiers se sérialisent —, et une task list
  `- [ ] #fille` dans la mère pour l'œil. Puis il **ferme la mère** sur
  GitHub (`gh issue close`, commentaire *Découpée en #A, #B — suivi sur les
  filles.*) : son cycle s'arrête là, aucune pull request ne viendra la
  fermer. La fermeture est idempotente, un rejeu du nœud ne la casse pas.
  Outcome `split`, vers le terminal
  dédié `close_split` ; les filles suivent le pipeline normal, admises par
  le sync comme n'importe quelle issue.
- **Vraiment ambiguë** (plusieurs lectures incompatibles, objectif qu'aucune
  preuve ne peut trancher) → il écrit quand même `criteria.md` — sa
  meilleure lecture — et sort `unclear`, vers le nœud d'attente
  **`clarify`** : une question fermée sur l'issue, `go` (on part sur cette
  lecture, l'implémentation démarre) ou `reformuler` (l'humain réécrit le
  corps de l'issue, et `scope` rejoue depuis zéro au passage suivant).
  `clarify` porte `escalade` — l'arête de retour vers `scope` est bornée par
  le budget d'escalades de l'item —, et l'expiration part en `escalate`
  comme celle de la review.

`unclear` est **l'exception, pas la paresse** : s'il existe une lecture
raisonnable unique, `scope` la prend et ne pose aucune question. Ses
critères étant publiés sur l'issue (ci-dessous), un contresens y coûte un
commentaire de l'humain, pas un cycle.

`criteria.md` est **contractuel** pour la suite du cycle : `implement` le
lit comme cahier des charges, les deux agents de test comme checklist en
plus de l'issue, et le nœud `validate` le coche formellement. Une
fille porte déjà ses critères dans son corps — figés au découpage : `scope`
les reprend tels quels au lieu de les réinventer.

**Le rail dit sa lecture avant d'écrire une ligne.** Dès que `criteria.md`
existe dans le workspace d'un item actif, le canal GitHub le publie en
commentaire de l'issue — une prise de parole comme les autres : intention
commise en base avant le POST, réconciliation par marqueur. La clé logique
porte le graph, la génération et l'**empreinte du contenu** : deux ticks sur
des critères inchangés ne laissent qu'un commentaire, et des critères
réécrits — après un `reformuler`, après une escalade — se redisent au lieu
de rester muets. L'humain voit ce que le rail a compris pendant que ça coûte
encore un commentaire, et non un cycle entier découvert à la review.

Le **corps de l'issue n'est jamais édité** par le rail — c'est le territoire
de l'humain ; la seule exception reste le bloc `## Découpée en` de la
découpe. La spécification du rail vit en commentaire, et `implement` lit
titre, corps et commentaires : rien de plus à câbler.

Quand le corps de l'issue est **vide ou squelettique** — un titre seul —,
les critères ne suffisent plus à dire ce qui a été compris : `criteria.md`
devient une **spécification proposée**, ouverte de trois sections courtes
(*Compris*, *Contexte technique*, *Approche*) avant la liste numérotée, qui
reste là dans tous les cas. Même fichier, même mécanique, juste plus étoffé
quand l'issue l'exige.

**Une porte de constat avant la review.** Les critères étaient figés, mais
rien ne les cochait : chaque agent de test relisait `criteria.md` à sa
façon, au milieu de son propre travail, et l'humain jugeait sur « testé, on
garde ? ». Le nœud **`validate`**, entre `test_frontend` et `review`, ne
fait que ça. C'est un JUDGE à contexte neuf — il n'a pas implémenté, il ne
lit le worktree qu'en lecture, et il ne corrige **jamais** rien : il reprend
`criteria.md` critère par critère, rejoue la preuve que chacun nomme (une
commande, un fichier, un élément du DOM déjà capturé par `test_frontend`)
et écrit `validate.md` dans le workspace — une ligne par critère, la case
`[x]`/`[ ]` et la preuve constatée.

- toutes les cases cochées → `pass`, la review s'ouvre ;
- une seule case vide → `fail`, retour en `implement`, qui lit le workspace
  et prend `validate.md` comme la liste de ce qui reste à tenir.

L'intelligence de correction reste donc dans `implement` : `validate`
constate et route. L'arête `fail` referme un cycle `validate → implement →
test → validate` — `validate` porte donc `escalade`, comme `release`, et le
budget d'escalades de l'item le borne. Le traverser une fois ne coûte
rien : le budget ne paie que les tours de boucle, donc les **cinq** de
`code-task` restent pour de vraies reprises.

Pas de critères — `criteria.md` absent ou vide, cas d'un cycle antérieur à
`scope` — → `pass`, et `validate.md` dit « aucun critère formalisé ». Une
checklist qui n'existe pas ne bloque rien rétroactivement ; la review reste
le juge.

**Toute question du rail embarque les deux fichiers.** Le canal GitHub cite
`criteria.md` — ce qui est demandé — puis `validate.md` — ce qui est tenu —
dans le commentaire de la question, en bloc de citation, bornés
respectivement aux quatre-vingts et aux quarante premières lignes, avec le
lien vers la preview du fichier entier. L'humain voit les critères et leurs
preuves, pas seulement une option à choisir : c'est vrai de la review, et
c'est ce qui rend la question de `clarify` lisible — elle porte la lecture
que le rail propose. Le sync lit les fichiers dans le workspace de
l'item, comme le frontend : `./data` est donc monté en lecture seule dans
le conteneur `github-sync`, comme il l'était déjà dans `web`. Pas de
fichier — question posée avant `validate`, cycle plus ancien, `data/` hors
de portée — pas de citation, et la question reste exactement ce qu'elle
était.

**La découpe est bornée.** Une ligne `Lineage-budget: <n>` dans le corps dit
combien de découpes restent permises sous cette issue : le pendant lisible
du `lineage_budget` du sujet, dans la même grammaire fermée que
`Depends-on:` — le canal ne lit que ce qu'il déclare lire. Absente, elle
vaut 3, le défaut de la colonne ; chaque fille reçoit celui de sa mère moins
un ; à `0`, `scope` traite l'issue comme atomique quelle que soit sa taille,
et son rapport le dit. Rien ne se découpe à l'infini.

Créer une issue est un effet, et un nœud se rejoue — tentative retentée,
passage rouvert par une escalade. Avant tout `gh issue create`, `scope`
liste les issues du dépôt et compare les titres **exactement** : une fille
déjà créée par une exécution précédente est reprise par son numéro, jamais
dupliquée. Le worktree que la mère avait préparé n'a jamais servi, mais il
existe : la sortie `split` passe par `cleanup_split` — le même shell que les
deux autres retraits — avant son terminal.

**Un modèle et un effort par atome.** Le `cmd` d'un nœud est une ligne de
shell : il porte aussi le coût. Tous les nœuds ne font pas le même travail,
donc ils ne paient pas le même tarif :

| nœuds | modèle / effort | pourquoi |
| --- | --- | --- |
| `scope`, `implement` | défaut, `--effort high` | le jugement d'avant la construction, puis le seul vrai travail de conception |
| `test_backend`, `test_frontend`, `validate` | `--model sonnet --effort medium` | procéduraux, mais avec du jugement — `validate` est du constat outillé, pas de la conception |
| `release` | `--model haiku --effort low`, script-first | le script fait le nominal ; l'agent ne sert qu'à la panne |
| `worktree`, `deploy`, `verify_deploy`, `cleanup`, `cleanup_unresolved`, `cleanup_split` | pas d'agent | du shell pur, qui écrit son `outcome.json` |

**Les nœuds mécaniques n'ont pas d'agent.** `worktree`, `deploy` et
`verify_deploy` étaient des agents qui suivaient un prompt scripté pas à
pas : on payait un modèle — du temps, des tokens, de la variance
d'interprétation — pour ce qu'un shell fait à l'identique, plus vite et
sans surprise. Ce sont maintenant des `cmd`, comme `cleanup` depuis
toujours : le numéro d'issue tiré du sujet et un `worktree add` idempotent
qui attend les verrous `.lock` des items voisins ; `docker compose up -d
--build` et son code de retour ; trois portes binaires (les deux services
en marche, `/items` en 200, aucun `Traceback` dans les 50 dernières lignes
du sync). Chacun écrit toujours son `outcome.json`, échec compris — c'est
ce qui interdit au graph de se coincer dans un nœud shell — et son compte
rendu nomme le pas qui a lâché avec son code : un shell qui échoue doit
être *plus* lisible qu'un agent qui improvise, pas moins.
[`tests/shell_test.py`](tests/shell_test.py) joue ces `cmd` tels quels sur
un dépôt jetable, sans modèle ni docker. Un `cmd` sans modèle ne laisse pas
d'`usage.json` : ces nœuds ne coûtent aucun token, et la page de l'item le
montre — c'est exactement le gain que l'on cherchait.

**`release` est le seul hybride : un agent script-first.** Tout le nominal
vit dans [`scripts/release.sh`](scripts/release.sh) — commit (titre de
l'issue, puis `Closes #<num>`), push, PR ouverte ou retrouvée, merge
surveillé jusqu'au SHA, un code de retour par pas. Le prompt du nœud tient
en une phrase : lance le script ; sortie 0 → `done`, rien d'autre — le
nominal coûte un aller-retour de modèle. Si le script lâche, l'agent a le
droit d'agir, dans une frontière stricte : réparer la mécanique — relancer
un push, recréer une PR obsolète d'un cycle passé, rebaser quand le rebase
passe tout seul — puis relancer le script, oui ; merger du code qu'il a
modifié, jamais. D'où trois issues fermées : `done`, `conflict` (l'agent
n'y arrive pas, l'humain reprend) et **`rebased`** — il a fallu résoudre de
vrais conflits, donc pas de merge : l'arête renvoie la branche à
`test_backend`, parce qu'une fusion est une combinaison que personne n'a
testée. Cette arête referme un cycle release → test → … → release :
`release` porte donc `escalade`, et le budget d'escalades de l'item borne
la boucle. Le noyau débite ce budget sur les **tours de boucle**, pas sur
les traversées : entrer dans un nœud d'escalade que l'item n'a pas encore
visité dans le passage courant est gratuit, y re-entrer décompte. Le chemin
nominal `review → release` ne coûte donc rien, et un item au budget épuisé
finit sa route — il ne peut juste plus boucler. Les nœuds d'escalade
humaine (`escalate`, `clarify`, des `WAIT`) restent décomptés à chaque
entrée : demander à un humain de relancer, c'est un tour par définition.
Le budget de `code-task` reste à **cinq**, tous disponibles pour de vraies
reprises.

Le trade-off du modèle le moins cher reste assumé, mais il ne porte plus
que sur la panne de release. Si elle se met à rater, la marche arrière est
*une ligne de JSON* : remonter d'un cran (`haiku` → `sonnet`, `low` →
`medium`) dans [`examples/code-task.json`](examples/code-task.json). Les
compteurs de tentatives et le journal disent ce qui a lâché ; c'est la
donnée qui tranche, pas l'intuition.

**Une porte de pertinence avant chaque test.** Le test le plus cher du
cycle est le test frontend (~8 min de navigateur) et il tournait même pour
une issue qui ne touche que du JSON de graph. Les deux `cmd` de test
commencent donc par quelques lignes de shell — pas du jugement d'agent —
qui lisent le diff de l'item (`git diff --name-only origin/main` plus les
fichiers neufs non encore suivis) et décident :

- le diff ne touche aucun fichier front (`src/graphatom/web.py` et `front/`,
  liste en tête du `cmd`) → `outcome` `pass`, « diff sans src/graphatom/web.py
  ni front/ — test frontend non concerné », sans lancer l'agent ;
- en miroir côté backend : ni `src/`, ni `tests/`, ni `schema.sql` → même
  court-circuit ;
- le diff **vide** n'est pas un skip, c'est un symptôme — implémentation
  perdue, worktree absent → `outcome` `fail`, avec le chemin du worktree
  dans le résumé. L'incident de l'item 10 aurait été vu dès `test_backend`.

Le résumé dit toujours pourquoi le test n'a pas tourné : le journal et la
page de l'item le lisent comme n'importe quel autre. Un chemin que la
porte ne comprend pas ne court-circuite rien — l'agent tourne.

Même motif côté fixtures : le test frontend peuple sa base avec
[`tests/seed.py`](tests/seed.py) — publier, admettre, quelques ticks
d'ordonnanceur, ~10 s — et non plus avec le crash-test, qui coûte 90 s
parce qu'il tue l'ordonnanceur et attend l'expiration d'un bail. Peupler
une base n'est pas tester le noyau.

Même motif pour l'environnement du front. `node_modules` et `.next` sont
ignorés par git : chaque worktree d'item repart à zéro, et une install à
froid coûtait plusieurs minutes sur un bail de test déjà court. Ce qui se
partage entre worktrees, c'est le **cache npm** —
[`scripts/front-env.sh`](scripts/front-env.sh) l'épingle sur un chemin
stable de l'hôte (`GRAPHATOM_NPM_CACHE`, `$HOME/.npm` par défaut), installe
en `npm ci --prefer-offline` puis builde : ~4 s d'install et ~10 s de build
cache chaud, contre plusieurs minutes à froid. C'est la voie normale du
prompt de `test_frontend` ; l'install à froid reste le secours. Le bail du
nœud est passé à **30 min** (`timeout_s: 1740`) : un test front réel paie
l'environnement, un serveur et des captures multi-viewports, là où 20 min
étaient calibrées sur du Python.

**Un worktree git par item** — le pendant git du workspace `data/item-N`.
`GRAPHATOM_REPO_DIR` est le clone de référence, plus l'atelier : le nœud
`worktree` crée `$GRAPHATOM_REPO_DIR/.worktrees/rail-item-N` sur la branche
`rail/issue-<num>` depuis `origin/main` fraîchement fetché — jamais depuis
l'état local. Tous les blocs de l'item partagent ce worktree (implement
écrit, les tests vérifient sur place, release commite et pushe depuis là) ;
seul **deploy** revient au clone de référence, qu'il aligne sur `origin/main`
avant de reconstruire — c'est le merge qui part en prod, pas la branche.
Deux items concurrents partent du même `origin/main` et divergent par leur
branche ; s'ils touchent les mêmes fichiers, le second merge voit le conflit :
release rebase quand le rebase passe tout seul, sort en `rebased` — retour aux
tests — quand il a fallu résoudre à la main, et en `conflict` quand elle n'y
arrive pas. Le retrait (worktree + branche locale) est un **nœud du
graph** : toutes les sorties passent par `cleanup`,
`cleanup_unresolved` ou `cleanup_split` avant leur terminal — le graph *est* la garantie de
cleanup, le noyau n'en sait rien. Les agents demandent un worker sur
l'hôte (voir le commentaire dans `docker-compose.yml`) ; le bail par nœud
(`config.lease_s`) couvre leur durée, et l'ordonnanceur exécute chaque
bloc dans son propre thread — un agent de dix minutes ne bloque ni le
faucheur ni les autres items.

## Des myriades de modèles bon marché

Le rail a été conçu pour un agent cher et compétent par nœud. La direction
change : on veut pouvoir lancer **des myriades de modèles bon marché,
potentiellement stupides**, sur la même étape, et laisser la sélection
produire la qualité que l'intelligence individuelle ne donne pas. Rien de
ce qui suit n'est codé à ce jour — c'est la vision à laquelle les issues
suivantes se réfèrent, écrite dans le dépôt parce qu'un principe non écrit
se perd.

**Le renversement économique.** Quand le token ne coûte plus rien, la
ressource rare n'est plus l'intelligence par appel : c'est la **capacité de
vérification**. Générer vingt candidats devient gratuit ; décider lequel est
bon devient tout le problème. L'architecture se réorganise donc autour du
tri, pas de la génération.

**L'ordre des filtres.** Le build, le lint, les tests, le diff non vide sont
déterministes, gratuits, et éliminent la grande majorité des candidats
faibles. **Un juge LLM ne doit jamais trancher ce qu'un compilateur
tranche.** La doctrine « des rails et du code, pas du prompt » devient ici
une contrainte de coût mesurable : chaque critère non mécanisable est un
critère qui exige un modèle cher. Corollaire : si `criteria.md` était
entièrement exécutable, aucun juge ne serait nécessaire — le premier
candidat qui franchit toutes les portes gagne, et la course est le juge.

**L'haltère : cher aux deux bouts, gratuit au milieu.**

| étage | tarif | pourquoi |
| --- | --- | --- |
| `scope` | cher | transformer une issue en fonction de fitness exécutable devient le nœud le plus important du graph |
| `implement` | gratuit et massif | N candidats jetables |
| jugement | cher | seulement sur les finalistes, et seulement quand les portes n'ont pas suffi |

**Le fan-out de candidats n'est pas le fan-out interdit.** Le périmètre
négatif refuse la jointure, et ce refus reste entièrement valide. Mais il ne
décrivait pas ce qu'on introduit ici : sa formulation est donc
[amendée](#ce-quon-ne-fera-jamais) honnêtement, plutôt que contredite en
silence.

- l'item conserve **un seul état, une seule révision, une seule issue de
  nœud** ; il n'est jamais sur deux nœuds à la fois et ne représente jamais
  plusieurs prédécesseurs actifs ;
- ce qui se multiplie, ce sont les **runs** d'un même nœud — exactement
  comme les tentatives se multiplient déjà aujourd'hui, mais en parallèle
  plutôt qu'en série ;
- il n'y a **aucune jointure** : les candidats ne fusionnent jamais, un seul
  survit et les autres sont tués et détruits ;
- la réduction produit **une seule issue** avant que l'item n'avance : vu du
  noyau, un nœud en fan-out se comporte comme un nœud ordinaire.

Les deux ne se confondent donc jamais : *jointure de plusieurs
prédécesseurs* — refusée pour toujours ; *candidats concurrents d'un même
nœud, réduits à un* — la nouvelle capacité.

**La diversité doit être structurelle.** N candidats du même modèle avec le
même prompt échouent de la même façon. La diversité utile ne vient pas de
l'échantillonnage mais de la **stratégie imposée** : « diff minimal »,
« réécris le composant en entier », « commence par écrire le test qui
échoue », et de la variété des modèles et des CLI. C'est ce que la
configuration devra exprimer.

**Pas de débat entre agents.** Des modèles faibles qui délibèrent convergent
vers celui qui a parlé en dernier, pas vers le vrai : ils sont complaisants
et partagent leurs angles morts. Ce qui remplace le raisonnement, ce n'est
pas la discussion mais la **sélection**. Le refus « pas de conversation
inter-agents » du périmètre négatif n'est pas affaibli par le fan-out — il
est renforcé : les candidats ne se voient jamais.

**L'état du parc, au 2026-08-08.** La vision doit rester honnête sur ses
moyens :

- la machine hôte n'a **ni `opencode`, ni `ollama`, ni runtime d'inférence
  local, ni poids de modèle** ;
- ce qui existe : la CLI `claude` (abonnement) et la CLI `codex`
  (`@openai/codex`, abonnement) ;
- le tier « bon marché » démarre donc avec des variantes
  `claude --model haiku` et une variante `codex`, qui apporte une vraie
  diversité de fournisseur ;
- un modèle local réellement gratuit demanderait d'installer un runtime et
  de télécharger des poids — c'est une option ouverte, pas un prérequis.
  **Le modèle est un paramètre de configuration : l'architecture ne doit
  dépendre d'aucun fournisseur.**

## Ce qu'on ne fera jamais

Périmètre négatif, assumé — ces refus *sont* le design :

- **Pas de jointure, ni de fan-out de chemins** dans un item — le parallélisme entre travaux, c'est plusieurs items. Un état unique ne représente pas plusieurs prédécesseurs actifs, et deux branches d'un graph ne se rejoignent jamais : la jointure est refusée pour toujours. Ce qui est permis, en revanche, c'est le **fan-out de candidats** — plusieurs runs concurrents d'un *même* nœud, réduits à une seule issue avant que l'item n'avance (voir [des myriades de modèles bon marché](#des-myriades-de-modèles-bon-marché)). L'invariant d'état unique tient parce que rien ne fusionne : l'item garde un seul état, une seule révision, une seule issue de nœud, il n'est jamais sur deux nœuds à la fois ; les candidats ne se voient jamais, un seul survit et les autres sont tués et détruits. Vu du noyau, un nœud en fan-out se comporte comme un nœud ordinaire.
- **Pas de conversation inter-agents** — la coordination est le graph. N agents qui discutent produisent un transcript inauditable.
- **Pas de langage de workflow** — la déclaration reste de la configuration étroite au-dessus de blocs typés. Expressions et conditions arbitraires : non.
- **Pas de question ouverte** aux humains — toute question est fermée, avec des options et une deadline.
- **Pas d'exactement-une-fois** promis — au-moins-une-fois avec réconciliation, et un état « incertain » honnête là où la cible ne sait pas dédupliquer.
- **Pas de mutation d'état hors des verbes officiels** — pas d'UPDATE de dépannage, jamais.
- **Pas de multi-organisation** — un opérateur, ses graphs, ses agents.
