# GraphAtom

**📄 Spec rendue : [acid3croco.github.io/graphatom](https://acid3croco.github.io/graphatom/)** · [dérivation v1](https://acid3croco.github.io/graphatom/archive/graph-runner-original.html)

Un noyau d'exécution durable pour orchestrer des agents LLM : une machine à états persistante qui exécute des tâches isolées et externalise les effets de façon réconciliable.

L'idée : des portes successives dont l'exécution est certaine — du code atomique qui guide, trace et corrige des agents au cours de leur cycle de vie. Pas de conversation inter-agents : la coordination *est* le graph.

## Documents

- [`index.html`](index.html) — **le noyau** (v2, simplifié) : six pièces, sept concepts utilisateur, quatre gardes de frontière, sept tables.
- [`cas-usage.html`](cas-usage.html) — **le cas d'usage pilote** : d'une carte Notion à la prod. Trois gestes humains, le reste est le rail. C'est ce scénario qui pilote les choix.
- [`pourquoi.html`](pourquoi.html) — **pourquoi pas Temporal/Restate/LangGraph** : les cinq garanties qu'aucun moteur existant ne donne. Le moteur est une commodité, les portes sont le produit.
- [`DECOUPE.md`](DECOUPE.md) — **découper un travail en N morceaux différents, et le recoller** : la forme de configuration, les agrégations retenues bloc par bloc, le sort d'un morceau en échec, et pourquoi l'invariant d'état unique tient. Conception seule — rien n'en est encore exécutable.
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

**Une troisième boucle, et une seule : la file.** Un nœud peut déclarer
`"file": true` ; il gagne alors le droit de se renvoyer sur lui-même, et
c'est la seule boucle que la validation tolère hors escalade. Elle ne
décompte aucun budget, parce qu'elle n'est pas un tour de reprise : le nœud
attend une ressource unique que personne ne lui rendra plus vite, et chaque
tour lui coûte son propre délai d'attente — la borne est le `wall_deadline`
de l'item, qui ne se régénère pas davantage. L'exception ne porte que sur
l'arête réflexive : une boucle plus longue qui passerait par la file reste
refusée, et une file sans arête sur elle-même aussi. `deploy` est la seule
à ce jour (voir « un seul déploiement à la fois »).

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
uv run python tests/migration_test.py                # migrer le schéma sous le
                                                     # worker : ses plans cachés
                                                     # périment, il encaisse et le dit
uv run python tests/verrou_test.py                   # la transaction oubliée d'un
                                                     # tiers : elle meurt seule, et
                                                     # la migration bloquée le dit
uv run python tests/links_test.py                    # les liens du frontend vers
                                                     # l'issue et la PR, et le titre
                                                     # dans la table, sans base
uv run python tests/depends_test.py                  # `Depends-on: #N` : l'admission
                                                     # attend, sans base ni réseau
uv run python tests/split_deps_test.py               # une découpe reporte les
                                                     # dépendances de la mère sur la
                                                     # dernière fille, puis la ferme
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
                                                     # joués tels quels : sans modèle,
                                                     # sans docker — mais avec une
                                                     # base, où `deploy` pose le
                                                     # verrou de la file
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
uv run python tests/silence_test.py                  # le chien de garde : un agent
                                                     # muet est coupé tôt, et le
                                                     # progrès constaté fait l'issue
uv run python tests/judge_test.py                    # le nœud arbitre : un finaliste
                                                     # unique ne coûte pas un jeton,
                                                     # plusieurs se départagent à
                                                     # l'aveugle, aucun repart en amont
uv run python tests/cycle_test.py                    # le profil code-task de bout en
                                                     # bout : keep_n, puis judge, puis
                                                     # close — le vrai ordonnanceur,
                                                     # des doublures pour les modèles
uv run python tests/opencode_test.py                 # l'adaptateur opencode : un
                                                     # nœud réel tourne sous un
                                                     # modèle gratuit (demande
                                                     # `opencode` et le réseau)
uv run python tests/portes_test.py                   # les portes d'un candidat
                                                     # d'implement : un succès ne
                                                     # compte qu'une fois prouvé
uv run python tests/fanout_opencode_test.py          # le candidat gratuit
                                                     # d'implement : sa CLI absente
                                                     # dit son nom dans le run
uv run python tests/plafond_test.py                  # les deux plafonds du dispatch :
                                                     # la charge est bornée, et ce
                                                     # que le plafond retient attend
                                                     # sans bail ni tentative
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
curl -s localhost:8848/api/load | jq
curl -s localhost:8848/api/graphs | jq '.[0]'
curl -s localhost:8848/api/graph/<rév> | jq '.nodes | keys'
curl -s -H 'Accept: application/json' \
     -d "question_id=1&option=retry&token=<jeton>" localhost:8848/answer
```

Sept lectures, pour un client qui rend les pages lui-même : `/api/items`
(la table, avec l'état, le statut et les liens issue et PR), `/api/item/<id>`
(l'item entier : `item`, `graph`, `journal`, `runs`, `effects`, `questions`,
`criteria`, `files`), `/api/questions` (les questions ouvertes),
`/api/heartbeat` (les deux battements bruts, `rail` et `github-sync`, chacun
avec son horodatage, son âge et son état périmé), `/api/load` (la charge de
l'ordonnanceur : les runs en vol, et les deux plafonds qui les bornent — voir
[la file du dispatch](#la-charge-a-un-plafond--le-dispatch-est-une-file)),
`/api/graphs` (les
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

**Une porte attend avant de conclure.** `docker compose up` rend la main
dès que les conteneurs sont créés, pas quand ils écoutent : un front Next
reconstruit ouvre son port plusieurs secondes plus tard. Mesuré à cet
instant-là, le déploiement le plus sain rend `000` — connexion refusée —,
l'item escalade, et un humain répond `retry` pour constater que tout allait
bien. Une porte qui échoue à tort finit par être lue comme du bruit : c'est
exactement ce qu'une porte ne doit jamais devenir. Les portes sondent donc
toutes les 2 s jusqu'à une réponse, et deux cas restent nets :

- **rien qui répond est une attente** — le `000` de curl, ou un service pas
  encore en marche : c'est l'état normal juste après un redémarrage ;
- **une réponse fausse est un échec immédiat** — un `500`, un corps sans
  `_next`, un `docker compose ps` en erreur : attendre ne les améliorerait
  pas, cela ne ferait que retarder le diagnostic. La porte 4 n'attend
  jamais non plus : un `Traceback` déjà écrit ne s'efface pas.

**Le budget d'attente est commun aux quatre portes : 60 s au total**, et
non 60 s chacune (`GRAPHATOM_PORTES_DELAI_S` le déplace). C'est ce qui rend
le pire cas du nœud calculable et indépendant du nombre de portes : au
maximum **60 s d'attente**, plus une sonde en cours (5 s de `--max-time`)
et les deux appels à `docker compose` — moins de 80 s en tout. Le nœud
garde donc son `timeout_s` de 120 s et son bail de 180 s inchangés, là où
60 s par porte auraient demandé de tripler les deux. Le partage n'enlève
rien au cas qui a motivé la mesure : quand seul le front traîne, les autres
portes concluent en une sonde et lui laissent le budget entier.
`verify_deploy.md` porte, porte par porte, le temps attendu avant de
conclure — c'est ce qui permettra de savoir si 60 s est bien réglé.

### Une transaction oubliée ne met pas le site à terre

Le 2026-08-08, le site a rendu 502 sur toutes ses pages pendant un quart
d'heure, base et worker en parfait état. La chaîne : un client externe en
lecture seule — un script de surveillance, pas le rail — laisse une
transaction ouverte et inactive, qui tient un `AccessShareLock` sur
`subject` ; `init-db` demande derrière elle l'`AccessExclusiveLock` de son
`ALTER TABLE` et attend **sans limite** ; la file d'attente des verrous
étant FIFO, toutes les lectures s'empilent derrière ce DDL ; et `web`,
`front` et `github-sync` dépendant d'`init` en
`service_completed_successfully`, la porte ne s'ouvre jamais. Il a fallu un
`pg_terminate_backend` à la main. Le code du rail n'y était pour rien : le
défaut est qu'**un client tiers mal élevé suffisait à tout arrêter**.

Trois protections indépendantes, toutes posées par le passage d'`init-db`
(`db.init_db`), toutes locales — pas un démon de plus :

1. **une transaction inactive meurt d'elle-même.**
   `idle_in_transaction_session_timeout`, 5 min par défaut
   (`GRAPHATOM_IDLE_TX_TIMEOUT`), posé sur le rôle applicatif pour la base
   du passage. C'est la protection qui, seule, aurait évité toute la
   panne : la session fautive disparaît avant d'avoir des conséquences. Le
   rail lui-même n'en voit jamais la couleur — `db.connect` ouvre en
   `autocommit`, et chaque `conn.transaction()` est bornée.
2. **une migration n'attend jamais sans limite.** `lock_timeout`, 3 s par
   défaut (`GRAPHATOM_LOCK_TIMEOUT`), posé avant le DDL, avec 3 essais
   espacés de 2 s. Bloquée, la migration échoue en moins de 10 s **en
   nommant le bloqueur** — pid, état et requête, lus dans `pg_locks` joint
   à `pg_stat_activity`. « Bloqué » sans dire par qui n'aide personne à
   trancher entre attendre et intervenir.
3. **la porte de démarrage reste ce qu'elle est.** Des deux pistes
   ouvertes — `init` échoue vite, ou les services de lecture démarrent
   malgré tout —, **c'est la première qui est retenue** : `web`, `front` et
   `github-sync` gardent leur `depends_on: init: service_completed_successfully`
   dans `docker-compose.yml`. La raison : la protection 2 supprime le cas
   qui rendait cette porte dangereuse, l'attente sans limite. Une porte qui
   ne peut plus rester fermée qu'une dizaine de secondes n'est plus une
   panne, et le déploiement suivant reprend tout seul. Laisser démarrer des
   services de lecture sur une base dont on ne sait pas si le schéma est
   celui du code, c'est en revanche servir des pages fausses au lieu d'une
   page absente — plus de complexité pour une garantie plus faible.

[`tests/verrou_test.py`](tests/verrou_test.py) rejoue la panne en entier,
dans une base à lui : la transaction tierce inactive est terminée par la
base, `init-db` échoue en nommant le pid et la requête, puis le bloqueur
parti de lui-même, un simple nouveau démarrage passe — le test n'appelle
`pg_terminate_backend` nulle part.

### Un seul déploiement à la fois : la concurrence est une file

Le rail travaille couramment à quatre ou six items en parallèle, et cela
marche partout **sauf sur `deploy`** : tous les autres nœuds agissent chacun
sur son atelier, `deploy` est le seul à agir sur une cible unique, la
production. Deux `docker compose up` concurrents sur le même projet se
disputent les noms de conteneurs, et docker refuse le second — un faux
échec, qui escaladait chez l'humain alors que le déploiement était bon.

**Le nœud prend un verrou de session postgres.** `pg_advisory_lock`, ni
fichier ni démon : la mort de la session le libère, donc un shell tué en
plein vol n'en laisse jamais un orphelin — un verrou qui survivrait à un
crash serait pire que pas de verrou. La clé est la somme de contrôle du
chemin de `GRAPHATOM_REPO_DIR` : c'est la cible qu'on sérialise, pas le
rail, et deux rails sur deux clones ne se gênent pas. La base où il vit est
`GRAPHATOM_VERROU_DSN`, à défaut `GRAPHATOM_AGENT_DSN` — l'instance que tous
les items partagent ; la `GRAPHATOM_DSN` du bloc, elle, est la base jetable
de l'item, propre à lui, donc sans effet sur le voisin. L'interprète qui
tient la session est celui du clone de référence,
`$GRAPHATOM_REPO_DIR/.venv/bin/python3`, avant celui du `PATH` : le worker
est lancé par chemin absolu, son `PATH` n'a donc pas le venv, et le
`python3` du système n'a pas psycopg — le prendre ferait taire le verrou
sans rien dire.

**Exclusion mutuelle *et* attente bornée**, les deux, parce qu'elles
répondent à deux questions différentes : la première dit qui passe, la
seconde combien de temps on patiente avant de rendre la main. L'exclusion
seule laisserait le second item pendu au bail d'un déploiement qui traîne —
le couperet le tuerait en `timed_out`, qui escalade sans compter les
tentatives, c'est-à-dire exactement la panne qu'on veut supprimer.
L'attente bornée seule ne sérialiserait rien. Passé
`GRAPHATOM_VERROU_DELAI_S`, soit **300 s d'attente**, le nœud rend
`waiting` et le graph le renvoie sur `deploy` : la file avance, personne
n'escalade. Ces 300 s et le build tiennent ensemble dans le `timeout_s` du
nœud, passé à 1260 s, donc dans son bail, passé à 1320 s.

**Deux effets de bord du même mécanisme.** Le verrou obtenu, le shell
compare le SHA visé à celui que portent les conteneurs — l'étiquette
`com.graphatom.sha`, que le compose pose sur les trois services déployés :
si un voisin a déployé le même `main` pendant l'attente, il n'y a rien à
reconstruire et l'issue est un succès. La vérité est ainsi lue sur le
déploiement lui-même, jamais sur un fichier tenu à côté. Et si un `up`
interrompu a laissé un conteneur bâtard, le message `The container name …
is already in use` nomme le coupable : le shell le retire et rejoue le
`up`, une fois — plus d'humain dans la boucle.

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

### Le contrat d'un bloc agent, noir sur blanc

C'est lui qui rend le rail agnostique du fournisseur, et tout adaptateur
de CLI s'y adosse. Il tient en trois fichiers, tous dans le workspace de
l'item (`data/item-<N>/`), qui est aussi le répertoire courant du `cmd`.

**Ce que la commande reçoit.**

- `prompt.md`, écrit par le bloc dans le workspace avant chaque tentative :
  le `prompt` du nœud, puis le contrat rappelé en clair — le workspace, le
  chemin d'`outcome.json`, les issues permises par les `edges` du nœud —,
  puis l'état laissé par la tentative précédente s'il y en a une.
- son répertoire courant : le workspace, toujours. Un adaptateur n'a donc
  rien à résoudre pour trouver `prompt.md` ni où déposer sa sortie.
- l'environnement du worker, où le bloc pose `GRAPHATOM_WORKSPACE` (le
  workspace), `GRAPHATOM_SUBJECT_KEY` (la clé du sujet, d'où le cleanup
  reconnaît son worktree) et, quand l'instance jetable est configurée,
  `GRAPHATOM_DSN` (la base de l'item, jamais celle du rail).
  `GRAPHATOM_REPO_DIR` — le clone de référence — vient du worker et passe
  tel quel.

**Ce que la commande doit écrire.** `outcome.json`, dans le workspace,
un objet JSON à deux clés :

```json
{"outcome": "done", "summary": "ce que la tentative a fait, en une phrase"}
```

`outcome` est obligatoire, et sa valeur est l'une des issues déclarées
dans les `edges` du nœud — c'est elle qui route. `summary` est libre :
absente, elle vaut la chaîne vide, et le run n'en souffre pas. Le fichier
est transitoire, purgé avant chaque tentative ; son contenu vit ensuite
dans le résultat du run en base.

**Ce qui est facultatif.** `usage.json`, dans le workspace : s'il est là et
lisible, le bloc le fusionne dans le résultat du run sous la clé `usage`.
Absent, illisible ou vide : rien, et l'agent reste un citoyen de première
classe.

**Ce qui ne compte pas.** Le code de sortie du `cmd` ne décide de rien :
seul `outcome.json` fait l'issue. Une commande qui sort 0 sans écrire le
fichier est `crashed` ; une commande qui sort 3 après l'avoir écrit est
routée par ce qu'il dit. Écrire le fichier même en échec est donc la
bonne manière — c'est ce que font tous les nœuds shell du graph.

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

### Un adaptateur pour un candidat gratuit : `scripts/agent-opencode.sh`

Le contrat ci-dessus se suffit à lui-même : un adaptateur de CLI n'est
qu'un pont entre deux formes. `scripts/agent-opencode.sh` en est la
démonstration — il lance `opencode run` sur le `prompt.md` du workspace et
traduit ce qu'opencode rend en `outcome.json` conforme :

```sh
"cmd": "$GRAPHATOM_REPO_DIR/scripts/agent-opencode.sh opencode/deepseek-v4-flash-free"
```

Le chemin est absolu parce que le `cmd` tourne depuis le workspace de
l'item, jamais depuis le dépôt : le clone de référence se nomme par
`GRAPHATOM_REPO_DIR`, comme le font déjà les nœuds shell du graph.

La course d'`implement` s'en sert pour son candidat gratuit, à un détail
près : un candidat a son propre atelier, et c'est celui-là qu'il nomme —
`GRAPHATOM_WORKTREE` pour lire le script comme pour le donner au modèle en
`OPENCODE_DIR`. Le clone de référence est partagé par tous les items ; un
candidat n'y écrit jamais.

Le modèle se donne en argument, ou par `OPENCODE_MODEL` ; à défaut c'est
`opencode/deepseek-v4-flash-free`, le seul dont le fonctionnement est
établi, écriture de fichier comprise. Les variantes gratuites d'opencode
ne demandent **aucun identifiant** — rien à configurer, rien à mettre dans
le dépôt. `OPENCODE_TIMEOUT_S` borne l'attente (300 s par défaut),
`OPENCODE_BIN` désigne le binaire quand le PATH ne suffit pas, et
`OPENCODE_DIR` le répertoire de travail du modèle.

Le script est déterministe, et ne juge jamais : si le modèle a écrit
`outcome.json`, il n'y touche pas ; si le modèle a dicté son issue dans
son texte au lieu de l'écrire, il la recopie telle quelle ; s'il n'a rien
dit, le script n'invente rien et la tentative est `crashed`, comme
d'habitude. Il écrit `usage.json` à partir des `step_finish` du flux
d'opencode, et n'écrit rien quand il n'y a pas de quoi le remplir.

Ses échecs sont nets et nommés, jamais silencieux — chacun son code de
sortie et son message sur `stderr` :

| code | ce qui a lâché |
| --- | --- |
| 2 | pas de `prompt.md` dans le répertoire courant |
| 3 | `opencode` introuvable — la commande manquante est nommée |
| 4 | borne d'attente dépassée — le modèle fautif est nommé |
| 5 | opencode a échoué, et aucune issue n'a été rendue |
| 6 | opencode a fini sans rendre la moindre issue |

Le code 4 n'est pas théorique : `opencode/north-mini-code-free` ne rend
rien du tout. Un candidat muet ne doit jamais retenir quoi que ce soit —
le chien de garde du bloc le couperait de toute façon, mais un adaptateur
qui se tait est un adaptateur cassé. La borne du script est donc plus
courte que le `timeout_s` du nœud : c'est l'adaptateur qui doit parler du
modèle, pas le couperet.

**La mesure, pas l'opinion.** `uv run python tests/opencode_test.py` fait
tourner un nœud réel du graph sous `opencode/deepseek-v4-flash-free` et
relit son issue en base : le modèle écrit son `outcome.json`, le noyau
route, et l'`usage.json` de l'adaptateur rejoint le résultat du run. Le
test se saute en le disant quand `opencode` n'est pas là. Le modèle muet,
lui, coûte sa borne d'attente entière et se vérifie donc à la main :

```sh
OPENCODE_TIMEOUT_S=45 scripts/agent-opencode.sh opencode/north-mini-code-free
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

**Un agent muet ne consomme plus son budget entier.** Un budget unique
recouvrait deux morts très différentes : la tâche qui déborde vraiment, et le
processus qui n'a jamais démarré. Un **chien de garde** sépare les deux. Il
relève trois signaux mécaniques — la taille du journal de la tentative, le
mtime le plus récent du workspace de l'item, celui de son worktree — et coupe
dès que les trois n'ont pas bougé pendant `silence_s` (180 s par défaut,
réglable par nœud à côté de `timeout_s`). Aucun modèle, aucune interprétation.

Au couperet — chien de garde ou budget total, peu importe lequel est tombé —
c'est le **progrès constaté** qui fait l'issue : des octets dans le journal,
ou un fichier du workspace ou du worktree touché depuis le lancement.

- **du progrès** → `timed_out`, escalade directe, comme ci-dessus ;
- **aucun progrès** → `stalled` : l'agent était pendu, c'est de l'infra et
  pas de la tâche. Le noyau relance sur place jusqu'à `MAX_ATTEMPTS`, puis
  escalade — c'est exactement le traitement de `crashed`.

Et **une reprise est une reprise, jamais un recommencement**. La question
n'est pas « est-ce la première tentative ? » mais « y a-t-il quelque chose à
reprendre ? » : le `retry` d'un humain sur une escalade ouvre un passage
neuf, donc une tentative 1, sur le worktree que le passage d'avant a rempli.
Dès qu'une tentative antérieure du même nœud a laissé quelque chose, le
prompt porte l'état déjà là — le `git status`, le `git diff` et les commits
face à `origin/main` du worktree de l'item, et la liste des fichiers de son
workspace. Deux signaux mécaniques, aucun modèle : du travail dans le
worktree, ou un fichier d'agent dans le workspace. Sans l'un ni l'autre,
aucun bloc — un agent qui recommence vraiment de zéro ne doit pas lire un
état imaginaire. Le bloc nomme enfin la mort dont il hérite — budget dépassé,
pendaison, panne —, parce qu'un état sans provenance se lit comme du travail
étranger. Repartir à l'aveugle, c'est payer le trajet deux fois. La question
d'escalade, elle, dit laquelle des deux morts l'a amenée : budget dépassé
avec la queue du journal, ou pendaison que les relances n'ont pas réveillée.
[`tests/silence_test.py`](tests/silence_test.py) fige les deux couperets, les
deux issues et le prompt de reprise.

**Ce qui n'est pas commité se perd au couperet.** Le prompt d'`implement`
demande donc de commiter au fil de l'eau, morceau par morceau — c'est une
règle du graph, pas un commentaire écrit à la main sur l'issue au troisième
dépassement. Le budget de l'agent monte du même coup à **25 min** : 840 s
étaient calibrées sur une issue de surface, et deux changements de noyau
d'affilée les ont dépassées pour aboutir en une seconde passe — un
aller-retour par l'humain à chaque fois. `release` n'en est pas gênée : elle
commite ce qui reste quand il reste quelque chose, et le corps de la PR porte
`Closes #<num>` de toute façon.

**`implement` est une course.** Le nœud déclare un `fanout` de trois
variantes — *minimal* sur Luna medium, *test d'abord* sur Sol high, et
*gratuit* sur DeepSeek V4 Flash — réduit par `keep_n: 2`. Trois candidats
implémentent la même issue en même temps, chacun dans son atelier et avec son
angle imposé. Les deux premiers qui rendent `done` après leurs portes vont au
juge ; le troisième est révoqué en vol. Trois runs laissent assez de place,
sous le plafond de huit, pour deux courses et deux portes concurrentes.

**Un candidat qui ne coûte rien.** La variante *gratuit* ne change que sa
commande : elle passe par [`scripts/agent-opencode.sh`](scripts/agent-opencode.sh)
sur `opencode/deepseek-v4-flash-free`, et hérite du prompt, des budgets et
des portes de tout le monde. C'est une mesure, pas une économie : si le
harnais fait le travail de fiabilité, un modèle gratuit suffit parfois, et
c'est la course qui le dit. Son `OPENCODE_DIR` est l'atelier du candidat —
rien à configurer, aucun identifiant, le modèle visé est sans
authentification.

Un candidat qui perdrait en silence fausserait justement cette mesure.
L'adaptateur sort donc en **code 3** quand `opencode` est introuvable, en
nommant la commande manquante ; la commande du candidat s'arrête là — les
portes ne tournent pas quand il n'y a pas d'issue à garder —, et le
post-mortem du `node_run` porte ce message dans son `log_tail`. Une CLI
absente se lit comme telle dans le résultat du run, pas comme du code qui ne
compile pas : celui-là, lui, laisse un `portes.md`.

**Un candidat porte ses propres portes.** Sans elles, « le premier qui
réussit » ne voudrait dire que « le premier qui s'est déclaré fini » : on
sélectionnerait le plus rapide à prétendre, pas le plus correct. Le `cmd` du
candidat lance donc [`scripts/portes.sh`](scripts/portes.sh) dès que l'agent
rend la main — le projet doit s'importer, et les tests sans base concernés
par son diff doivent passer — et **retire son `outcome.json` quand une porte
lâche**. Le noyau ne voit alors aucun succès : le candidat sort en `crashed`
comme n'importe quel raté, et la course continue sans lui.

Un agent qui veut rejouer ces portes à la main appelle
[`scripts/portes-ici.sh`](scripts/portes-ici.sh), qui crée le workspace
jetable, lance les portes dedans et nettoie derrière lui. Ce détour n'est pas
du confort : composer soi-même un `mktemp -d` avec son `trap 'rm -rf …'` est
le réflexe naturel, et la CLI `codex` **refuse ce motif** — « rm -f style
commands are not permitted » —, filtre de contenu que même le contournement
de son bac à sable ne lève pas. Six exécutions de `test_backend` y sont
mortes. Une opération mécanique et répétée n'a rien à faire dans la tête d'un
modèle : on lui donne l'outil, il ne compose plus de shell, et il cesse d'être
exposé aux refus de son fournisseur.

Les K candidats partagent la base jetable de leur item : une porte qui la
détruit ou la recrée les ferait tomber les uns les autres. Le script coupe
donc `GRAPHATOM_DSN` et `GRAPHATOM_AGENT_DSN` d'entrée et épingle
`GRAPHATOM_REPO_DIR` sur l'atelier du candidat — aucune porte ne *peut*
toucher une base ni le clone de référence partagé, quelle que soit la
distraction de qui éditera la liste. `tests/crash_test.py`, qui drope la base
nommée par `GRAPHATOM_DSN`, n'y est donc pas ; `tests/shell_test.py` non plus,
depuis que le verrou de la file du `deploy` lui en demande une, ni
`tests/plafond_test.py`, qui compte les runs en vol de toute la base et ne se
mesure donc que seul. `test_backend`
les joue après la course, une fois seul. Des jeux de portes lancés en même temps ne coûtent
presque rien de plus qu'un seul — trois mettaient **53 s**, une seconde de
plus qu'un seul ; le budget du nœud passe de 25 à
**28 min** (`timeout_s: 1680`, bail `lease_s: 1740`) pour que l'agent garde
les siennes entières.

Le `cmd` des nœuds à modèle de `code-task` diffuse pour cette raison :
`--output-format stream-json --verbose` écrit au fil de l'eau dans le
workspace, là où `--output-format json` ne rendait rien avant la toute fin —
un agent parfaitement sain y serait resté muet aux yeux du chien de garde.
Le `usage.json` et le texte final se lisent à la fin du flux, sans rien
changer au contrat du bloc.

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
  `- [ ] #fille` dans la mère pour l'œil. Puis il **ferme la mère** par
  `graphatom split-close --repo … --mother … --children A B C`, qui reporte
  d'abord les dépendances : toute issue encore en attente qui porte
  `Depends-on: #<mère>` voit cette ligne — et elle seule — réécrite vers la
  **dernière fille** de la chaîne, avec le commentaire qui nomme l'ancienne
  cible et la nouvelle. Sans ce report, la fermeture de la mère satisfait
  une dépendance sans livrer le travail attendu, et le dépendant part pour
  rien. L'ordre fait la sûreté : tant qu'un dépendant n'est pas reporté, la
  mère reste ouverte, donc personne n'est libéré — une réécriture
  impossible arrête la découpe en nommant l'issue, plutôt que d'admettre
  trop tôt en silence. Puis la mère est fermée (*Découpée en #A, #B — suivi
  sur les filles.*) : son cycle s'arrête là, aucune pull request ne viendra
  la fermer. Tout est idempotent, un rejeu du nœud ne casse rien.
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

**Un modèle et un effort explicites par atome.** Le `cmd` d'un nœud est une
ligne de shell. Les nœuds Codex passent par `agent-codex.sh` avec
`CODEX_MODEL` et `CODEX_REASONING_EFFORT` : le routage du graph ne dépend pas
de la configuration locale. Les nœuds mécaniques restent du shell pur. Le
seul mélange de fournisseurs est la course d'implémentation :

| nœuds | fournisseur | pourquoi |
| --- | --- | --- |
| `scope`, `judge` | Codex `gpt-5.6-sol`, effort high | les deux jugements qui découpent ou choisissent gardent le modèle fort |
| `test_backend`, `validate` | Codex `gpt-5.6-luna`, effort low | constat borné, preuves déjà nommées |
| `test_frontend` | Codex `gpt-5.6-luna`, effort medium | le navigateur demande plus de lecture, pas un Sol |
| `release` | `release.sh` d'abord ; Luna low seulement sur panne | le nominal ne consomme aucun tour de modèle |
| `implement` | Luna medium, Sol high, DeepSeek V4 Flash gratuit | deux abonnements Codex et un fournisseur gratuit, trois stratégies |
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

**`release` est le seul hybride : shell-first, agent sur panne.**
[`scripts/release-node.sh`](scripts/release-node.sh) lance d'abord tout le
nominal, qui vit dans [`scripts/release.sh`](scripts/release.sh) — rapprochement
d'`origin/main`, commit (titre de l'issue, puis `Closes #<num>`), push, PR
ouverte ou retrouvée, merge surveillé jusqu'au SHA, un code de retour par
pas. Le rapprochement est le pas le plus récent, et il dit la règle du
script entière : `git fetch origin` puis `git merge origin/main` dans le
worktree de l'item, avant le commit. Main n'a pas bougé, ou a bougé
ailleurs — le cas fréquent — et personne n'est dérangé ; le conflit
textuel, lui, annule proprement le merge, nomme ses fichiers dans
`release.md` et sort en code 9. Le pas tourne avant le commit, comme
l'issue le demande : le travail de l'item est donc encore à nu, et si main
a bougé sur un fichier qu'il tient ouvert, git refuse le merge d'entrée —
même sans conflit de contenu. C'est le second visage du code 9, et il coûte
une reprise que le même pas placé après le commit s'épargnerait. Un merge, jamais un rebase : la branche est
publique dès son premier push, son histoire ne se réécrit pas.

Le rapprochement supprime les conflits textuels, pas les ruptures de sens :
un merge sans le moindre marqueur peut casser la construction. C'est arrivé
— deux branches se compilaient chacune de son côté, l'une rendait une
propriété obligatoire, l'autre appelait le composant sans elle, et `main`
est sortie cassée (`graph-view.tsx(25,8) TS2741`). **Une porte de construction**
suit donc toute absorption non vide, avant le push :
`python3 -m compileall` sur `src/`, puis `npm run build` dans `front/` si le
contenu absorbé y touche — la porte chère ne tourne que quand le front est
concerné. Une porte qui lâche arrête la release en **code 11**, avec la
sortie de la commande fautive dans `release.md` et le worktree laissé sur sa
fusion : c'est un état à réparer, pas à effacer. Absorption vide, aucune
porte : ce contenu-là a déjà été testé tel quel par les nœuds de test, et le
cas fréquent ne paie rien. On ne fait jamais juger par un modèle ce qu'un
compilateur tranche, et on ne paie pas un cycle d'agents — des minutes —
pour ce qu'une commande déterministe décide en secondes.

Le prompt du
nœud tient en une phrase : lance le script ; sortie 0 → `done`, rien
d'autre — le nominal coûte un aller-retour de modèle. Si le script lâche,
l'agent a le droit d'agir, dans une frontière stricte : réparer la
mécanique — relancer un push, recréer une PR obsolète d'un cycle passé,
résoudre le conflit du code 9, réparer la rupture d'intégration du code 11 —
puis relancer le script, oui ; merger du
code qu'il a modifié, jamais. D'où trois issues fermées : `done`, `conflict` (l'agent
n'y arrive pas, l'humain reprend) et **`rebased`** — il a fallu résoudre de
vrais conflits, ou réparer ce que la porte de construction a arrêté, donc
pas de merge : l'arête renvoie la branche à
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

Le trade-off du modèle le plus rapide reste assumé, mais il ne porte plus
que sur la panne de release. Si elle se met à rater, la marche arrière est
*une ligne de JSON* : remonter Luna de `low` à `medium` dans
[`examples/code-task.json`](examples/code-task.json). Les
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
- le diff **vide** n'est pas un skip, c'est un symptôme → `outcome` `fail`,
  « rien à tester dans cet atelier », avec le chemin du worktree dans le
  résumé. Le message ne nomme aucune cause : la porte constate, elle ne
  devine pas. Un atelier lu trop tôt, lui, n'en est plus une — la promotion
  du gagnant d'une course est dans la transaction de la réduction, donc
  faite avant que le nœud suivant ne réserve quoi que ce soit. L'incident de
  l'item 10 aurait été vu dès `test_backend`.

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
release merge `origin/main` quand le merge passe tout seul, sort en `rebased`
— retour aux tests — quand il a fallu résoudre à la main, et en `conflict`
quand elle n'y arrive pas. Jamais un rebase : la branche est publique, seul
le nom de l'issue est resté. Le retrait (worktree + branche locale) est un **nœud du
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
produire la qualité que l'intelligence individuelle ne donne pas. Le premier
étage est en place — `implement` court en fan-out de trois candidats, Luna
medium, Sol high et DeepSeek V4 Flash gratuit, et chacun porte ses portes
déterministes ; le reste
de ce qui suit est la vision à laquelle les issues suivantes se réfèrent,
écrite dans le dépôt parce qu'un principe non écrit se perd.

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

Le premier pas est fait : `implement` court en fan-out de trois candidats —
deux tiers Codex d'abonnement, un modèle gratuit — et chacun porte ses portes
déterministes ([`scripts/portes.sh`](scripts/portes.sh)). Un juge Sol high
compare les deux finalistes ; `criteria.md` reste la grille commune.

**L'haltère : cher aux deux bouts, gratuit au milieu.**

| étage | tarif | pourquoi |
| --- | --- | --- |
| `scope` | cher | transformer une issue en fonction de fitness exécutable devient le nœud le plus important du graph |
| `implement` | abonnement et gratuit | trois candidats jetables, deux finalistes |
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
  travail finit sur la branche de l'item et les autres sont détruits ;
- la réduction produit **une seule issue** avant que l'item n'avance : vu du
  noyau, un nœud en fan-out se comporte comme un nœud ordinaire.

**Deux réductions, et elles ne coûtent pas la même chose.**

| réduction | ce qu'elle fait | ce qu'elle coûte |
| --- | --- | --- |
| `first_pass` | le premier candidat dont l'issue passe gagne, les autres sont tués sur place | rien : personne n'attend |
| `keep_n` | **attend tout le monde**, puis laisse passer les `n` premières réussites — les *finalistes* — au nœud d'aval | l'attente du plus lent, et un juge derrière |

**La promotion du gagnant est dans la transaction de la réduction.** Élire,
promouvoir, router : trois gestes, un seul verrou — celui de l'item, que
`claim` prend aussi. Le premier run du nœud suivant ne peut donc pas être
réservé avant que le travail du gagnant soit sur la branche de l'item.
L'ordre est garanti, pas probable : c'est la même propriété que « un
résultat qui arrive après la décision ne la change jamais ». Le premier
usage réel du fan-out avait perdu trois items sur quatre là-dessus — la
porte d'aval lisait un atelier deux secondes avant que le gagnant n'y
arrive. Et une promotion qui échoue — merge non-ff, atelier disparu, git en
erreur — est un **échec du nœud**, nommé comme tel dans le run : l'item ne
part pas en avant sur un atelier vide en croyant que tout va bien.

`keep_n` ne choisit pas : c'est un nœud `JUDGE` **arbitre** qui départage,
déclaré par `finalists_from: <nœud de fan-out>`. Sa borne sur `n` est dure —
`2 ≤ n ≤ 3` — parce que le juge lit `n` diffs entiers : au-delà son contexte
explose, en deçà il n'a rien à départager. Trois issues fermées :

- `sole` — un seul finaliste : le nœud est traversé **sans dépenser un seul
  jeton**. Un juge à qui l'on présente une option unique dit toujours oui ;
  ce serait un tampon payant, qui fabriquerait de la fausse confiance. Le
  court-circuit est mécanique, décidé avant d'appeler quoi que ce soit ;
- `chosen` — plusieurs finalistes : le juge lit les diffs, élit, et dit
  pourquoi dans `verdict.md`, là où `validate` écrit le sien ;
- `none` — aucun finaliste : retour en amont, comme un échec de `validate`,
  et borné par le même budget d'escalade.

**Le juge ne voit jamais qui a produit quoi.** Ni le modèle, ni la CLI, ni
l'étiquette de variante ne lui sont présentés : uniquement les diffs, sous
des lettres, et `criteria.md`. Un juge qui saurait qu'un candidat vient d'un
« gros » modèle le préfèrerait, et la mesure entière perdrait son sens — or
c'est précisément la question qu'on cherche à trancher. Il est cher, et
c'est voulu : c'est l'un des deux bouts de l'haltère, et la page d'un item
montre son prix **à part** de celui des candidats, pour qu'on puisse
comparer ce que coûte le jugement face à ce que coûte la génération.

`validate` reste en place et garde son rôle : le juge **choisit entre des
candidats**, `validate` **vérifie le résultat retenu** contre les critères.
Deux questions différentes, deux nœuds différents. Et le juge n'écrit jamais
dans les ateliers des candidats : il lit leurs diffs par les noms de
branches, depuis le dépôt.

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

- la machine hôte a les CLI `opencode`, `codex` et `claude`, mais aucun
  runtime d'inférence local ni poids de modèle ;
- le rail nominal ne dépend plus du quota claude : ses nœuds à modèle
  épinglent un tier Codex et son effort ;
- la course utilise trois variantes : une variante
  `opencode/deepseek-v4-flash-free`, une variante Luna medium et
  une variante Sol high, qui apportent une vraie diversité de fournisseur ;
- aucun modèle local ne tourne sur cet hôte. Tous les nœuds utilisent un
  abonnement CLI ou un modèle distant gratuit, sans clé API.
  **Le modèle est un paramètre de configuration : l'architecture ne doit
  dépendre d'aucun fournisseur.**

### La charge a un plafond : le dispatch est une file

Le fan-out multiplie la charge, et rien ne la bornait. Mesuré le 2026-08-08,
quelques minutes après la première mise en service réelle : 7 items actifs ×
4 candidats = 28 runs en vol, 27 processus `claude` sur une machine à 12
cœurs, load average 22 au pic, et **Postgres tombé deux fois** dans la
journée. Tant qu'un item n'avait qu'un run par nœud, le nombre d'items
bornait la charge tout seul ; le fan-out a supprimé cette borne implicite
sans la remplacer.

Deux plafonds la remplacent, dans l'ordonnanceur. Tous deux dérivés du nombre
de cœurs de la machine — aucun chiffre magique —, tous deux surchargeables,
et tous deux plafonnés par en bas à `FANOUT_MAX_CANDIDATES` (8), la largeur
du plus grand fan-out qu'un graph puisse publier :

| plafond | défaut | sur 12 cœurs | surcharge |
| --- | --- | --- | --- |
| runs en vol, tous items confondus | `max(FANOUT_MAX_CANDIDATES, cœurs // 2)` | 8 | `GRAPHATOM_MAX_RUNS` |
| runs en vol d'un même item | `max(FANOUT_MAX_CANDIDATES, plafond // 2)` | 8 | `GRAPHATOM_MAX_RUNS_PER_ITEM` |

**Pourquoi la moitié des cœurs.** Un candidat ne coûte pas un agent qui écrit
du texte : il coûte un agent **plus ses portes** — une construction et une
suite de tests, qui saturent un cœur chacune pendant qu'elles tournent. Un
run vaut donc plus qu'un cœur, et huit runs sur douze cœurs laissent peu de
marge à Postgres, au canal GitHub et au front. Le défaut est volontairement
prudent : mieux vaut un rail un peu lent qu'une base qui tombe.

**Pourquoi un plancher à la largeur du fan-out.** Une course se réserve
entière ou pas du tout (voir plus bas) : un plafond plus serré que
`FANOUT_MAX_CANDIDATES` ne différerait pas la course la plus large qu'un
graph publie, il l'empêcherait pour toujours — aucun tick suivant ne
libérerait jamais assez de place. Le plancher garantit l'inverse : sur cette
machine, les deux plafonds valent leur moitié de cœurs *ou* huit, le plus
grand des deux, et une course n'est donc jamais plus large que ce que le
dispatch peut accueillir d'un coup. Le plafond borne le nombre de courses
simultanées, jamais la largeur d'une seule.

**Pourquoi un plafond par item.** Sans lui, un item en fan-out large occupe
toute la capacité et affame les autres items sur des nœuds bon marché. La
moitié du plafond global le tient sous celui-ci quand cette moitié dépasse le
plancher ; en dessous, les deux coïncident (8 sur 12 cœurs) et le plafond par
item ne fait plus que garantir qu'une course complète tient toujours — la
place pour un autre item vient alors du tick suivant, pas d'une réserve
strictement plus étroite.

**Ce que le plafond retient attend — rien n'échoue.** Un run retenu n'est pas
réservé du tout : aucune ligne `node_run`, donc aucun bail posé, aucune
tentative consommée, aucune issue d'échec, rien à annuler. Le tick suivant le
prend. C'est la file du déploiement, appliquée au dispatch.

**Une course se réserve entière, ou elle attend.** Les candidats de la
même issue construisent le projet et lancent la même suite de tests, et
c'est ce qui coûte — on ne la sérialise pas et on ne la partage pas : une
porte jouée une fois pour tous ne prouverait plus rien sur le diff d'un
candidat en particulier, et c'est précisément ce que la course sélectionne.
Le dispatch (`_dispatch`) ne réserve donc les K candidats d'un item que si la
place en accueille tous : une réservation partielle ne tourne pas, car la
réduction trancherait sur une course amputée — les autres candidats ne
naîtraient jamais. Une course trop large pour attendre son tour reste
entière ou attend, jamais coupée en deux ; le plancher ci-dessus garantit que
ce cas n'arrive pas tant que les plafonds gardent leur défaut. **Dernier
recours**, si une surcharge (`GRAPHATOM_MAX_RUNS` ou
`GRAPHATOM_MAX_RUNS_PER_ITEM`) resserre l'un des deux plafonds sous la
largeur d'une course : celle-ci passe quand même, entière, mais seulement
quand plus rien d'autre ne peut avancer et que le rail est vide — sinon elle
attendrait indéfiniment derrière des items plus étroits.

**La charge se lit hors de la base.** `GET /api/load` rend les runs en vol et
les deux plafonds effectifs — `{"running": 4, "max_runs": 8,
"max_runs_per_item": 8}` sur cette machine à 12 cœurs : une saturation ne se
diagnostique plus à coups de `ps`.

## Ce qu'on ne fera jamais

Périmètre négatif, assumé — ces refus *sont* le design :

- **Pas de jointure, ni de fan-out de chemins** dans un item — le parallélisme entre travaux, c'est plusieurs items. Un état unique ne représente pas plusieurs prédécesseurs actifs, et deux branches d'un graph ne se rejoignent jamais : la jointure est refusée pour toujours. Ce qui est permis, en revanche, c'est le **fan-out de candidats** — plusieurs runs concurrents d'un *même* nœud, réduits à une seule issue avant que l'item n'avance (voir [des myriades de modèles bon marché](#des-myriades-de-modèles-bon-marché)). L'invariant d'état unique tient parce que rien ne fusionne : l'item garde un seul état, une seule révision, une seule issue de nœud, il n'est jamais sur deux nœuds à la fois ; les candidats ne se voient jamais, un seul travail finit sur la branche de l'item et les autres sont détruits. Quand la réduction est `keep_n`, plusieurs candidats survivent le temps qu'un nœud arbitre les départage — mais il en élit un, et un seul : c'est une sélection différée, jamais une jointure. Vu du noyau, un nœud en fan-out se comporte comme un nœud ordinaire.
- **Pas de conversation inter-agents** — la coordination est le graph. N agents qui discutent produisent un transcript inauditable.
- **Pas de langage de workflow** — la déclaration reste de la configuration étroite au-dessus de blocs typés. Expressions et conditions arbitraires : non.
- **Pas de question ouverte** aux humains — toute question est fermée, avec des options et une deadline.
- **Pas d'exactement-une-fois** promis — au-moins-une-fois avec réconciliation, et un état « incertain » honnête là où la cible ne sait pas dédupliquer.
- **Pas de mutation d'état hors des verbes officiels** — pas d'UPDATE de dépannage, jamais.
- **Pas de multi-organisation** — un opérateur, ses graphs, ses agents.
