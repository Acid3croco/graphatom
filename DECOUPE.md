# Découper un travail en N morceaux, et le recoller

Le fan-out livré aujourd'hui répond à une question : « plusieurs agents
tentent la *même* chose, lequel garde-t-on ? ». Les deux réductions
existantes le montrent — `first_pass` élit le premier qui prouve, `keep_n`
garde les finalistes pour l'arbitre.

Il en manque une autre : « ce travail se **découpe** en N morceaux
*différents*, comment recolle-t-on les résultats ? ». Les candidats
concurrents sont un cas particulier — N morceaux identiques dont on ne garde
qu'un. Le cas général demande deux choses que le rail n'a pas : de quoi
**décrire la découpe**, et de quoi **agréger**.

Ce document tranche la forme. Le code vient après, et ce qu'il coûte est
nommé en dernière section. Il ne change rien au comportement : aucun bundle
d'aujourd'hui ne se valide ni ne s'exécute différemment après l'avoir lu.

**Le vocabulaire, une fois pour toutes.** Un *candidat* est un run concurrent
d'un nœud, tel qu'il existe déjà — la colonne `node_run.candidate` porte son
numéro. Un *morceau* est un candidat dont le travail **diffère** de celui de
ses frères au lieu de le concurrencer. Une *sélection* réduit N candidats en
en gardant un ; une *agrégation* les réduit en les **combinant**. Le rail
n'a aujourd'hui que des sélections.

## 1. La forme : `pieces`, énumérés dans la configuration

La découpe se déclare dans la clé `fanout` du nœud, à côté de sa réduction,
par une clé `pieces` **exclusive de `variants`** :

- `variants` — N façons de faire *la même* chose, réduites par une
  **sélection** (`first_pass`, `keep_n`) ;
- `pieces` — N travaux *différents*, réduits par une **agrégation**
  (`unanimous`, `concat`).

Un morceau, comme une variante, est un fragment de config posé sur celle du
nœud (`graph.candidate_node`) : ce qu'il nomme surcharge, ce qu'il tait est
hérité. Il porte un `label`, et — sous `concat` seulement — la clé `paths`,
son **périmètre déclaré** (section 2).

Les deux clés ne se mélangent jamais dans un même nœud : un fan-out est une
course *ou* une découpe, et un nœud qui déclarerait les deux demanderait au
noyau de deviner lequel des deux sens il faut lire. La publication le refuse.

### L'exemple complet

Un graph qui refait un module en trois morceaux disjoints, puis vérifie le
résultat sous trois angles dont un seul suffit à le refuser. Le bundle est
complet : il se lit sans ouvrir le noyau.

```json
{
  "name": "refonte-par-morceaux",
  "entry": "implement",
  "budgets": {"escalations": 5, "wall_deadline_hours": 24},
  "on_kernel": {"escalate_to": "humain", "exhausted_to": "abandon"},
  "nodes": {
    "implement": {
      "block": "ACT",
      "config": {
        "lease_s": 1740,
        "fanout": {
          "pieces": [
            {"label": "noyau", "paths": ["src/graphatom/kernel.py"],
             "task": "la réduction et le routage"},
            {"label": "ateliers", "paths": ["src/graphatom/worktree.py"],
             "task": "les ateliers git et la promotion"},
            {"label": "tests", "paths": ["tests/decoupe_test.py"],
             "task": "le test sans base de la découpe"}
          ],
          "reduce": "concat"
        },
        "agent": {
          "cmd": "claude --dangerously-skip-permissions -p \"$(cat prompt.md)\"",
          "timeout_s": 1680,
          "silence_s": 300,
          "prompt": "Sujet : {subject_key}. Tu écris le morceau « {label} » : {task}.\n\nTon périmètre est fermé : tu ne touches que {paths}, et rien d'autre. Un fichier hors de cette liste fait tomber la découpe entière — tes frères écrivent en même temps que toi, dans leurs propres fichiers, et le rail recolle vos trois branches. Commite tout ce que tu produis.\n\noutcome `done` quand ton morceau est prêt."
        }
      },
      "edges": {"done": "verifier"}
    },
    "verifier": {
      "block": "CHECK",
      "config": {
        "lease_s": 600,
        "fanout": {
          "pieces": [
            {"label": "correction", "angle": "le code fait-il ce qu'il dit ?"},
            {"label": "sûreté", "angle": "que se passe-t-il si un morceau meurt ?"},
            {"label": "style", "angle": "le diff ressemble-t-il au code qui l'entoure ?"}
          ],
          "reduce": "unanimous",
          "on": "pass"
        },
        "agent": {
          "cmd": "claude --dangerously-skip-permissions -p \"$(cat prompt.md)\"",
          "timeout_s": 540,
          "silence_s": 180,
          "prompt": "Sujet : {subject_key}. Tu vérifies sous un angle et un seul — « {label} » : {angle}.\n\nTu ne corriges rien. Tu ne juges pas les autres angles : ils ont leur propre agent. Le doute l'emporte — une seule réfutation refuse le tout, donc `fail` si tu n'es pas convaincu.\n\noutcome `pass` ou `fail`."
        }
      },
      "edges": {"pass": "fini", "fail": "humain"}
    },
    "humain": {
      "block": "WAIT",
      "escalade": true,
      "config": {
        "question": "Un morceau a échoué, ou la vérification a refusé. On retente le cycle, ou on abandonne ?",
        "options": ["retenter", "abandonner"],
        "owner": "Acid3croco",
        "deadline_minutes": 1440
      },
      "edges": {"retenter": "implement", "abandonner": "abandon", "expired": "abandon"}
    },
    "fini": {"terminal": true},
    "abandon": {"terminal": true}
  }
}
```

Ce bundle décrit la forme visée : il ne se publie pas aujourd'hui,
`_validate_fanout` refusant `pieces` comme `concat`.

### Pourquoi énumérés, et non produits à l'exécution

L'issue nomme le cas le plus utile : le nœud d'amont produit la liste — des
fichiers, des modules, des sous-tâches — et le nombre de morceaux n'est connu
qu'à l'exécution. **Il est refusé ici**, et pas seulement reporté.

Trois raisons, dans l'ordre de force :

1. **Un morceau perdu rend le tout faux.** C'est la conclusion de la section
   3 : une agrégation ne tolère pas de perdant. Un nombre de morceaux
   découvert à l'exécution se heurte alors à la borne dure
   `FANOUT_MAX_CANDIDATES = 8` sans issue acceptable — tronquer à huit rend
   silencieusement un résultat incomplet, et échouer au-delà de huit fait
   sauter un item après avoir payé le nœud d'amont. Une borne dure ne peut
   protéger que ce qu'elle voit **avant** que rien ne parte.

2. **Un graph publié ne peut plus échouer sur sa structure.** C'est la
   première ligne de `graph.py`, et c'est la promesse de l'adressage par
   contenu. Une largeur dynamique la casse : le même bundle, valide à la
   publication, lance trois morceaux ce matin et deux cents ce soir.

3. **Le noyau lit la largeur dans le bundle.** `claim` alloue le candidat *k*
   avec `len(fanout_variants(node))`, et ferme la tentative quand la largeur
   est atteinte ; `_batch` et la barrière en dépendent. Rendre la largeur
   dynamique, c'est en faire de l'**état d'item** — une colonne de plus,
   écrite par un run et lue par ses frères. C'est précisément le genre d'état
   partagé que le périmètre négatif refuse.

**La borne dure ne change pas.** `FANOUT_MAX_CANDIDATES = 8` s'applique aux
morceaux comme aux variantes, et pour la même raison : une config fautive ne
doit pas lancer mille agents. Elle reste vérifiée à la publication, sur
`len(pieces)` ; `repeat` est refusé avec `pieces` — répéter un morceau
referait deux fois le même travail pour le recoller deux fois.

**Ce qui reste possible quand la liste n'est connue qu'à l'exécution.** Le
périmètre négatif donne déjà la réponse : « le parallélisme entre travaux,
c'est plusieurs items ». Un nœud d'amont qui découvre N sous-tâches les
**admet en N items**, chacun avec son sujet, son état et son budget de
lignée — c'est le mécanisme de découpe du rail, il existe, et il ne demande
aucune agrégation. La découpe intra-item de ce document sert l'autre cas :
celui où les N morceaux doivent revenir **ensemble** dans un seul diff, sous
une seule review.

## 2. Les agrégations, et ce que chaque bloc en accepte

Deux agrégations sont retenues, et deux seulement. La table est celle de
`FANOUT_REDUCERS`, prolongée : le type de bloc contraint ce qui est légal, et
la publication tranche — jamais l'exécution.

| bloc | `first_pass` | `keep_n` | `unanimous` | `concat` |
| --- | --- | --- | --- | --- |
| FETCH | ✅ | ❌ rien à départager sur une lecture | ❌ une lecture ne rend pas de verdict à mettre au vote | ❌ ses artefacts sont des fichiers de workspace, sans preuve mécanique de disjonction (voir plus bas) |
| JUDGE | ✅ | ❌ un jugement n'est pas un travail comparable | ✅ N angles, une réfutation suffit | ❌ un verdict ne se concatène pas |
| ACT | ✅ | ✅ | ❌ N travaux tous réussis mais jamais recollés : ce serait N branches et aucune règle, une jointure par omission | ✅ le seul cas où recoller a un sens |
| CHECK | ✅ | ❌ un constat n'est pas un travail comparable | ✅ le cas nominal de la vérification adversariale | ❌ un constat ne se concatène pas |
| EFFECT | ✅ | ❌ rien à départager sur un effet | ❌ voir plus bas | ❌ voir plus bas |
| WAIT | ❌ | ❌ | ❌ | ❌ une question posée à un humain ne se multiplie pas |

### `unanimous` — et pourquoi `any_fail` n'est pas une seconde fonction

L'issue nomme trois familles sur les verdicts : `unanimous`, `any_fail`,
`majority`. Les deux premières sont **la même fonction lue des deux côtés**,
et le rail n'en garde qu'un nom.

    "reduce": "unanimous", "on": "pass"

`on` nomme l'issue unanime attendue, et doit être une arête déclarée du nœud.
La règle :

- tous les morceaux rendent `on` → le nœud prend `on` ;
- **dès qu'un morceau rend autre chose**, le nœud prend cette issue-là et la
  course s'arrête : les frères encore en vol sont révoqués, exactement comme
  sous `first_pass`.

Lue depuis l'unanimité c'est `unanimous`, lue depuis la réfutation c'est
`any_fail` — et c'est bien « le doute l'emporte » que demande la vérification
adversariale. Deux noms pour un prédicat feraient deux façons d'écrire la
même chose ; il n'en faut qu'une.

La réduction est donc **monotone**, comme `first_pass` : elle décide sans
attendre le plus lent, et n'introduit aucune attente nouvelle. Une issue du
noyau — `crashed`, `timed_out`, `stalled`, `invalid_result` — n'est pas `on`
non plus : elle tombe sous la règle de la section 3.

**`majority` est refusée.** Sur une découpe, les morceaux ne répondent pas à
la même question : deux angles sur trois qui passent ne disent rien du
troisième, et le vote enterre précisément le défaut qu'on payait pour
trouver. Sur des morceaux *identiques*, ce serait une sélection, et le rail
en a déjà deux. Le vote resterait par ailleurs un juge LLM là où l'unanimité
est un prédicat mécanique — « un juge LLM ne doit jamais trancher ce qu'un
compilateur tranche ».

À ne pas confondre avec `kernel._majoritaire`, qui existe déjà : il choisit
**quelle issue d'échec router** quand toute la course a raté, une fois la
décision prise. Ce n'est pas un vote sur le fond, et rien ici ne le change.

**EFFECT est refusé sous les deux agrégations.** Un effet est commis
au-moins-une-fois avec réconciliation, et `uncertain` y est une issue de
première classe : agréger N effets demanderait de dire ce que vaut « trois
commis, un incertain », et la seule réponse honnête serait de tout défaire —
ce que le rail ne promet nulle part. N effets différents sont N nœuds
différents, chacun avec sa passerelle.

### `concat` — et la disjonction, vérifiée et non supposée

Sur un ACT, un morceau produit du code sur **sa** branche, dans **son**
atelier (`rail-item-<N>-c<k>`), comme un candidat aujourd'hui. `concat` fait
de leurs travaux celui de l'item : les branches sont fusionnées sur celle de
l'item, **dans l'ordre déclaré**, une fois la course entière terminée.

C'est là que se joue tout le risque, et la disjonction se **prouve** en deux
temps, tous deux mécaniques :

1. **À la publication** — les `paths` déclarés des morceaux sont deux à deux
   disjoints. Un préfixe de répertoire qui en couvre un autre compte comme un
   recouvrement. Une découpe qui se marche dessus est refusée avant que quoi
   que ce soit ne parte, et son message nomme les deux morceaux fautifs.
   `paths` est **obligatoire** sous `concat`, et refusé ailleurs.

2. **À la réduction** — pour chaque morceau, `git diff --name-only
   <branche de l'item>...<branche du morceau>`, sources et destinations des
   renommages comprises. Deux vérifications :
   - chaque chemin touché tombe dans le `paths` de son morceau — un agent qui
     déborde est pris, quoi qu'il ait promis ;
   - les ensembles obtenus sont deux à deux vides d'intersection.

   Les deux passent → les fusions ne peuvent pas entrer en conflit : git
   fusionne par contenu à l'intérieur d'un fichier, et aucun fichier n'est
   commun. L'une des deux échoue → **aucune fusion n'a lieu**, et le nœud
   prend son issue d'échec ; la branche de l'item n'a pas bougé d'un commit.

La déclaration seule ne suffirait pas — « disjoints » serait alors une
hypothèse sur la docilité d'un agent. Le diff réel seul ne suffirait pas non
plus : sans périmètre déclaré, deux morceaux qui ne se croisent pas *par
chance* passeraient, et la découpe deviendrait une loterie. Il faut les deux.

**Le `--ff-only` d'aujourd'hui ne survit pas, et c'est le seul garde-fou qui
change.** `worktree.promote` refuse la fusion non-ff parce qu'un seul travail
doit avancer une branche immobile. Avec N morceaux, la deuxième fusion n'est
plus droite par construction. Ce que le `--ff-only` protégeait — « pas de
fusion inventée » — est repris par la preuve de disjonction, qui est plus
forte : elle interdit non seulement le conflit, mais le recouvrement.

## 3. Un morceau en échec : tout échoue

**La règle retenue : un seul morceau en échec fait échouer le nœud entier.**
L'issue du nœud est celle de l'échec, routée par l'arête que le nœud a
déclarée, et le compte des tentatives ne change pas d'un pouce. Rien n'est
fusionné, rien n'est promu : les branches des morceaux réussis restent où
elles sont.

C'est la conséquence directe de ce qu'agréger veut dire. Une sélection tolère
ses perdants — c'est son principe, il n'en faut qu'un. Une agrégation qui
recolle N morceaux ne le peut pas : deux tiers d'une refonte, c'est un diff
qui ne compile pas, et deux angles de vérification sur trois, c'est une
garantie qu'on n'a pas.

**La tentative suivante rejoue tous les morceaux, et ce n'est pas un gâchis.**
`worktree.open_run` retrouve l'atelier et la branche de la tentative
précédente — « une reprise, pas une répétition ». Les morceaux qui avaient
réussi retrouvent donc leur travail commité et n'ont qu'à le constater ;
seul le fautif recommence vraiment. Le coût d'un « tout échoue » est celui de
K agents qui relisent leur propre diff, pas celui de K refontes.

Les deux autres règles sont écartées, et voici pourquoi :

- **Retenter le seul morceau fautif.** L'unité de reprise du rail est la
  *tentative*, pas le run : `_batch` regroupe sur `(item, nœud, passage,
  tentative)`, `claim` alloue le candidat *k* dans la tentative courante, et
  la barrière se ferme quand la largeur est atteinte. Faire avancer un
  morceau seul mêlerait dans un même lot des runs de deux tentatives — la
  barrière ne se fermerait plus, et le prédicat « tout le monde a fini »
  deviendrait indécidable. Le gain visé est déjà obtenu par la reprise
  ci-dessus, sans toucher au noyau.

- **Escalader tout de suite.** Un morceau meurt souvent de `crashed` ou de
  `stalled` — de l'infra, pas du travail. Rien ne justifie qu'une découpe
  soit moins tolérante à une panne qu'un nœud ordinaire, ni qu'elle réveille
  un humain pour un worker qui est tombé. Le budget de tentatives, puis le
  budget d'escalades, s'appliquent inchangés : `timed_out` escalade tout de
  suite, comme partout ailleurs.

## 4. L'invariant d'état unique tient — au prix d'une phrase amendée

Le périmètre négatif refuse la jointure « pour toujours » : « deux branches
d'un graph ne se rejoignent jamais », « l'item garde un seul état, une seule
révision, une seule issue de nœud, il n'est jamais sur deux nœuds à la
fois ». Cette section conclut **oui**, l'invariant tient — et dit exactement
où la formulation actuelle du README doit être reprise.

**Ce qui ne change pas, et qui *est* l'invariant.** Les morceaux sont des
**runs d'un même nœud**, exactement comme les candidats aujourd'hui : même
ligne `work_item`, même colonne `state`, même révision. L'agrégation est une
fonction du noyau qui les ramène à **une seule issue** avant que l'item
n'avance, et c'est cette issue-là que `_route` traite comme celle d'un nœud
ordinaire. L'item ne représente jamais plusieurs prédécesseurs actifs : il
n'a qu'un prédécesseur, le nœud découpé, qui a une issue et une seule. Vu du
noyau, un nœud en découpe se comporte comme un nœud ordinaire — et
`apply()` reste la seule porte de mutation.

**La jointure refusée n'est pas celle-ci.** Ce que le périmètre négatif
interdit, c'est la jointure de **chemins** : deux arêtes qui se rejoignent,
donc deux états simultanés d'un même item, donc un état qui n'est plus un
état. Une découpe n'a qu'un chemin. La réduction est un point unique, sous le
verrou de l'item, décidé sur des runs terminés : « un résultat qui arrive
après la décision ne la change jamais ».

**La phrase à amender, dite honnêtement.** Le README l'écrit à deux endroits,
dans « des myriades de modèles bon marché » comme dans le périmètre négatif :
« les candidats ne fusionnent jamais, un seul travail finit sur la branche de
l'item et les autres sont détruits ». Sous `concat`, c'est faux : N travaux
finissent sur la branche de l'item. Cette phrase était la **preuve** employée
pour les candidats, pas l'invariant lui-même — et une preuve qui ne s'applique
plus se remplace, elle ne se contourne pas en silence. Elle est donc à
reprendre ainsi, dans le même esprit que l'amendement qu'a déjà reçu le
fan-out :

> Les runs d'un nœud sont réduits à une issue unique avant que l'item
> n'avance. Sous une **sélection** (`first_pass`, `keep_n`), un seul travail
> finit sur la branche de l'item et les autres sont détruits. Sous une
> **agrégation** (`concat`), les travaux de tous les morceaux y finissent —
> mais seulement après que le noyau a **prouvé** qu'ils touchent des fichiers
> deux à deux disjoints, et à un seul point de décision. Ce n'est pas une
> jointure de chemins : l'item garde un seul état, une seule révision, une
> seule issue de nœud, et n'est jamais sur deux nœuds à la fois.

Qui lit « rien ne fusionne » comme l'invariant lui-même, et refuse
l'amendement, tient une conclusion cohérente : il faut alors **abandonner
`concat`** et ne garder que `unanimous`, qui ne fusionne aucun artefact — des
verdicts ne se recollent pas. La découpe survit sur les verdicts, et les ACT
restent une course. Ce repli est net, et il est la position de repli de ce
document.

**Ce que la review humaine voit.** Une chose et une seule. La réduction est
close avant que l'item n'atteigne un WAIT : ce que l'humain lit, c'est la
branche de l'item — un diff, unique, déjà recollé, ou rien du tout si la
preuve de disjonction a échoué. Jamais N résultats à comparer, jamais un
recollage à faire de tête. C'est déjà la règle du fan-out, et elle vaut ici
a fortiori : sous une sélection, un humain qui verrait N candidats ferait le
travail du juge ; sous une agrégation, il ferait celui de git.

## 5. Ce que le noyau doit changer, et ce qu'il ne change pas

### Ce qui change

| où | quoi |
| --- | --- |
| `graph.py` — `FANOUT_REDUCERS` | deux entrées de plus : `unanimous` sur JUDGE et CHECK, `concat` sur ACT. La table reste la seule autorité, et le refus reste à la publication. |
| `graph.py` — `_validate_fanout` | accepte `pieces` en exclusion de `variants` ; refuse `repeat` avec `pieces` ; applique `FANOUT_MAX_CANDIDATES` à `len(pieces)`. |
| `graph.py` — deux valideurs, à côté de `_validate_keep_n` | `_validate_unanimous` : `on` présent, et arête déclarée du nœud. `_validate_concat` : `paths` présent et non vide sur chaque morceau, et deux à deux disjoints. |
| `graph.py` — `fanout_variants` | énumère aussi les morceaux. C'est le seul endroit qui dit la largeur d'une tentative : `claim` et la barrière en héritent sans être touchés. |
| `blocks.py` — `VARIANT_TOKENS` | un jeton de plus, `paths`, pour qu'un prompt puisse nommer le périmètre fermé de son morceau. `label` sert déjà. |
| `kernel.py` — `_reduce` | deux branches de plus, sur le même modèle que les deux existantes. |
| `kernel.py` — `_unanimous` | monotone comme `_first_pass` : décide dès qu'un morceau s'écarte de `on`, révoque les frères par `_revoke_losers`. |
| `kernel.py` — `_concat` | attend tout le monde comme `_keep_n` ; tous en succès et la preuve de disjonction tenue → l'issue commune est routée ; sinon l'issue d'échec, sans rien fusionner. |
| `kernel.py` — `_ateliers` | le cas `concat` : fusionner les branches des morceaux dans l'ordre déclaré au lieu de promouvoir un gagnant, puis `discard` comme aujourd'hui. |
| `worktree.py` — une fonction à côté de `promote` | la preuve de disjonction (`git diff --name-only`, périmètres déclarés, intersections) et les fusions successives. Le refus se dit à voix haute et laisse la branche de l'item intacte, comme le fait déjà `promote`. |
| `tests/` | un test sans base sur la validation de `pieces` — dans la lignée de `fanout_config_test.py` —, et un test de la preuve de disjonction sur un dépôt jetable. |
| `README.md` | la phrase « les candidats ne fusionnent jamais… », amendée mot pour mot comme en section 4, à ses deux endroits — « des myriades de modèles bon marché » et le périmètre négatif. |

### Ce qui ne change pas

- **Le schéma.** `node_run.candidate` porte déjà le numéro d'un morceau ;
  aucune colonne, aucune table, aucune migration.
- **`claim` et la barrière.** La largeur d'une tentative se lit toujours dans
  le bundle, la tentative se ferme toujours quand elle est atteinte, le
  `fence` de l'item joue le même rôle.
- **`_route`, `_settle`, `apply_item`, `apply()`.** Une réduction rend une
  issue ; ce qui en découle est inchangé.
- **Les deux compteurs.** `MAX_ATTEMPTS` par passage, budget d'escalades qui
  ne se régénère jamais, `timed_out` qui escalade tout de suite.
- **`first_pass`, `keep_n`, le nœud arbitre, `finalists_from`, les bornes
  `FANOUT_KEEP_MIN`/`FANOUT_KEEP_MAX`.** La découpe est à côté, pas à la
  place.
- **`FANOUT_MAX_CANDIDATES = 8`**, et le fait qu'il soit vérifié à la
  publication.
- **WAIT.** Jeu de réductions vide, hier comme demain.
- **Le front et l'API.** Un morceau est un run de candidat : la page d'un
  item l'affiche déjà.
- **Les bundles d'`examples/`.** Aucun ne déclare `pieces` : ils se valident
  et s'exécutent à l'identique.

## 6. Ce que ce document ne fait pas

Il ne change aucun comportement. Aucun fichier de `src/`, `tests/`,
`front/`, `scripts/`, `profiles/` ou `examples/` n'est touché : la table
`FANOUT_REDUCERS` refuse toujours `unanimous` et `concat`, `_validate_fanout`
refuse toujours `pieces`, et l'amendement de la section 4 est écrit ici pour
être appliqué au README **par l'issue qui livrera `concat`** — pas avant, pour
que le README ne décrive jamais autre chose que ce que le code fait.
