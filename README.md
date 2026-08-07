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
                                                     # groupe de l'agent, sans base
uv run python tests/reconnect_test.py                # couper la base sous le worker :
                                                     # il se reconnecte et reprend
uv run python tests/links_test.py                    # les liens du frontend vers
                                                     # l'issue et la PR, sans base
```

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

La boucle avec GitHub va dans les deux sens : les commentaires du rail
pointent vers le frontend, et le frontend renvoie vers GitHub. Partout où
un sujet a la forme `gh:<owner>/<repo>#<num>` — page des questions, `/items`,
en-tête de `/item/<id>` — il devient un lien vers l'issue ; et quand le cycle
a produit une PR, `/item/<id>` l'affiche à côté, lue dans le `release.json`
que le nœud release écrit dans le workspace. Tout se construit depuis la base
et le workspace : aucun appel à l'API GitHub depuis le web. Un sujet d'une
autre forme reste du texte brut — le kernel, lui, ne connaît pas GitHub.

Refusé, exprès : auth, comptes, exposition Internet, WebSocket, édition de
graphs, mutation d'items, dashboard. Voir les WAIT, répondre une fois parmi
les options, laisser le rail reprendre.

## Le canal GitHub + docker (milestone 3a)

Le rail branché sur un repo — dogfoodé sur ce repo même :

```sh
GITHUB_TOKEN=$(gh auth token) docker compose up -d --build
```

Quatre services : Postgres, l'ordonnanceur, le canal GitHub (polling), le
canal web (secours, port 8850). Ensuite tout se passe sur GitHub :

1. poser le label `graphatom` sur une issue → admission (une seule fois par issue)
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
| `GRAPHATOM_PROXY_NET` / `..._EXTERNAL` | le réseau docker du proxy que le service `web` rejoint |

Pas de secret dedans : `GITHUB_TOKEN` reste fourni par le shell et garde
son garde-fou (`${GITHUB_TOKEN:?…}`). Les défauts du `docker-compose.yml`
restent ceux d'un déploiement générique — le compose est générique, le
`.env` est cette instance.

**Le raccordement au proxy suit la même règle.** L'UI est exposée sur
`graphatom.veyxzer.com` par le Traefik de l'hôte, avec basicauth au bord
— l'app reste sans auth, refus assumé. Le routeur Traefik vit dans le
proxy, hors de ce repo ; seul le raccordement réseau est déclaré ici, dans
le compose, sur le service `web`. Un `docker network connect` à la main
n'aurait pas survécu au premier `up -d` — même panne silencieuse que
`TAKE_ALL`. Sans les deux variables du `.env`, compose crée son propre
réseau `graphatom-proxy` : un déploiement sans proxy ne casse pas.

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
n'importe quel bloc. L'agent ne voit jamais la base du rail :
`GRAPHATOM_AGENT_DSN` lui substitue une base jetable.

Une tentative crashée rend son **autopsie** dans le résultat du run —
`exit_code` (négatif = le signal qui l'a tué), `log_tail` (les 20 dernières
lignes d'`agent-<tentative>.log`, bornées à 2 000 caractères) et `timeout`
(vrai si c'est le bail du bloc qui a fauché l'agent). La table des runs de
`/item/<id>` l'affiche : le post-mortem se lit sur la page, pas en fouillant
le workspace à la main.

[`examples/code-task.json`](examples/code-task.json) est le graph qui fait
tourner ce repo : implémentation par agent, **agent de test backend**
(imports, crash-test), **agent de test frontend au navigateur headless**
(le DOM rendu et des screenshots, pas du curl), puis review humaine —
question fermée sur l'issue GitHub. La boucle se ferme ensuite toute
seule : **release** (commit, push, PR, merge surveillé jusqu'au SHA),
**deploy** (`docker compose up -d --build github-sync web`) et
**verify_deploy** (conteneurs `Up`, `/items` en 200, logs du sync
propres).

**Un modèle et un effort par atome.** Le `cmd` d'un nœud est une ligne de
shell : il porte aussi le coût. Tous les nœuds ne font pas le même travail,
donc ils ne paient pas le même tarif :

| nœuds | modèle / effort | pourquoi |
| --- | --- | --- |
| `implement` | défaut, `--effort high` | le seul vrai travail de conception |
| `test_backend`, `test_frontend` | `--model sonnet --effort medium` | procéduraux, mais avec du jugement |
| `worktree`, `release`, `deploy`, `verify_deploy` | `--model haiku --effort low` | scriptés pas à pas dans le prompt |
| `cleanup`, `cleanup_unresolved` | pas d'agent | du shell pur, qui écrit son `outcome.json` |

Le trade-off est assumé, pas gratuit : `release` et `deploy` touchent git et
docker avec le modèle le moins cher. Ils sont scriptés pas à pas et leurs
sorties d'échec (`conflict`, `failed`) mènent à l'escalade — mais si l'un
se met à rater, la marche arrière est *une ligne de JSON* : remonter d'un
cran (`haiku` → `sonnet`, `low` → `medium`) dans
[`examples/code-task.json`](examples/code-task.json). Les compteurs de
tentatives et le journal disent lequel a lâché ; c'est la donnée qui
tranche, pas l'intuition.

Même motif côté fixtures : le test frontend peuple sa base avec
[`tests/seed.py`](tests/seed.py) — publier, admettre, quelques ticks
d'ordonnanceur, ~10 s — et non plus avec le crash-test, qui coûte 90 s
parce qu'il tue l'ordonnanceur et attend l'expiration d'un bail. Peupler
une base n'est pas tester le noyau.

**Un worktree git par item** — le pendant git du workspace `data/item-N`.
`GRAPHATOM_REPO_DIR` est le clone de référence, plus l'atelier : le nœud
`worktree` crée `$GRAPHATOM_REPO_DIR/.worktrees/rail-item-N` sur la branche
`rail/issue-<num>` depuis `origin/main` fraîchement fetché — jamais depuis
l'état local. Tous les blocs de l'item partagent ce worktree (implement
écrit, les tests vérifient sur place, release commite et pushe depuis là) ;
seul **deploy** revient au clone de référence, qu'il aligne sur `origin/main`
avant de reconstruire — c'est le merge qui part en prod, pas la branche.
Deux items concurrents partent du même `origin/main` et divergent par leur
branche ; s'ils touchent les mêmes fichiers, le second merge voit le conflit,
et release a déjà sa sortie `conflict`. Le retrait (worktree + branche
locale) est un **nœud du graph** : toutes les sorties passent par `cleanup`
ou `cleanup_unresolved` avant leur terminal — le graph *est* la garantie de
cleanup, le noyau n'en sait rien. Les agents demandent un worker sur
l'hôte (voir le commentaire dans `docker-compose.yml`) ; le bail par nœud
(`config.lease_s`) couvre leur durée, et l'ordonnanceur exécute chaque
bloc dans son propre thread — un agent de dix minutes ne bloque ni le
faucheur ni les autres items.

## Ce qu'on ne fera jamais

Périmètre négatif, assumé — ces refus *sont* le design :

- **Pas de fan-out ni de jointure** dans un item — le parallélisme, c'est plusieurs items. Un état unique ne représente pas plusieurs prédécesseurs actifs.
- **Pas de conversation inter-agents** — la coordination est le graph. N agents qui discutent produisent un transcript inauditable.
- **Pas de langage de workflow** — la déclaration reste de la configuration étroite au-dessus de blocs typés. Expressions et conditions arbitraires : non.
- **Pas de question ouverte** aux humains — toute question est fermée, avec des options et une deadline.
- **Pas d'exactement-une-fois** promis — au-moins-une-fois avec réconciliation, et un état « incertain » honnête là où la cible ne sait pas dédupliquer.
- **Pas de mutation d'état hors des verbes officiels** — pas d'UPDATE de dépannage, jamais.
- **Pas de multi-organisation** — un opérateur, ses graphs, ses agents.
