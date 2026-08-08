portes du candidat — atelier /home/jack/projects/graphatom/.claude/worktrees/imperative-crunching-conway/.worktrees/fix-portes
$ uv run python -c import graphatom.blocks, graphatom.channel, graphatom.cli, graphatom.db, graphatom.github_sync, graphatom.graph, graphatom.heartbeat, graphatom.kernel, graphatom.scheduler, graphatom.web, graphatom.worktree
porte « import » passée en 0 s
$ uv run python tests/validate_test.py
1. exemples validés : code-task.json, gauntlet.json, github-pilot.json, supervision.json ✓
2. cible parasite refusée : on_kernel.notify_to → nœud non déclaré nowhere ✓
   clés du noyau toujours obligatoires ✓
3. _layers() couvre exactement les nœuds déclarés ✓
4. file sans arête réflexive refusée : deploy : file sans arête sur lui-même ✓
   boucle longue par la file refusée : cycle hors escalade via verify_deploy → deploy ✓

validation : OK — une cible on_kernel fantôme ne se publie plus
porte « tests/validate_test.py » passée en 1 s
$ uv run python tests/fanout_config_test.py
1. fan-out bien formé sur `implement` publié : révision 21ab4c8d7d09… ✓
2. fan-out sur un WAIT refusé : clarify : un nœud WAIT ne peut pas être en fan-out ✓
3. table bloc → réductions : ['first_pass', 'keep_n'] livrées, keep_n sur ACT seul, WAIT au jeu vide ✓
   réduction 'vote' refusée : implement : réduction 'vote' refusée sur un bloc ACT — permises : ['first_pass', 'keep_n'] ✓
   réduction 'best_by' refusée : implement : réduction 'best_by' refusée sur un bloc ACT — permises : ['first_pass', 'keep_n'] ✓
   keep_n sur un CHECK refusée : test_backend : réduction 'keep_n' refusée sur un bloc CHECK — permises : ['first_pass'] ✓
   keep_n sur un JUDGE refusée : scope : réduction 'keep_n' refusée sur un bloc JUDGE — permises : ['first_pass'] ✓
4. au-delà de la limite dure (8) refusé : implement : 2 variantes × 5 = 10 candidats, au-delà de la limite dure FANOUT_MAX_CANDIDATES = 8 ✓
   8 candidats pile passent ✓
5. variants absent → implement : fanout sans variants, ou variants vide ✓
5. variants vide → implement : fanout sans variants, ou variants vide ✓
5. variants pas une liste → implement : fanout sans variants, ou variants vide ✓
5. variante pas un objet → implement : variante qui n'est pas un objet : 'minimal' ✓
5. repeat non entier → implement : fanout.repeat doit être un entier ≥ 1, vu '2' ✓
5. repeat booléen → implement : fanout.repeat doit être un entier ≥ 1, vu True ✓
5. repeat nul → implement : fanout.repeat doit être un entier ≥ 1, vu 0 ✓
5. repeat négatif → implement : fanout.repeat doit être un entier ≥ 1, vu -1 ✓
5. reduce absent → implement : fanout sans reduce ✓
5. fanout pas un objet → implement : fanout n'est pas un objet ✓
   repeat absent vaut une fois ✓
6. les bundles d'examples/ se valident et se publient comme avant ✓
7. keep_n avec n = 2..3 passe ✓
   keep_n, n absent → implement : réduction keep_n sans n — la borne est 2 ≤ n ≤ 3 ✓
   keep_n, n = 1 → implement : keep_n.n = 1 hors de la borne dure 2 ≤ n ≤ 3 — au-delà, le juge lit trop de diffs ; en deçà, il n'a rien à départager ✓
   keep_n, n = 4 → implement : keep_n.n = 4 hors de la borne dure 2 ≤ n ≤ 3 — au-delà, le juge lit trop de diffs ; en deçà, il n'a rien à départager ✓
   keep_n, n = 0 → implement : keep_n.n = 0 hors de la borne dure 2 ≤ n ≤ 3 — au-delà, le juge lit trop de diffs ; en deçà, il n'a rien à départager ✓
   keep_n, n négatif → implement : keep_n.n = -2 hors de la borne dure 2 ≤ n ≤ 3 — au-delà, le juge lit trop de diffs ; en deçà, il n'a rien à départager ✓
   keep_n, n non entier → implement : keep_n.n doit être un entier, vu '2' — la borne est 2 ≤ n ≤ 3 ✓
   keep_n, n booléen → implement : keep_n.n doit être un entier, vu True — la borne est 2 ≤ n ≤ 3 ✓
8. l'exemple livré porte son arbitre : JUDGE, source `implement`, issues ['sole', 'chosen', 'none'] ✓
   arbitre sur un bloc ACT → judge : finalists_from demande un bloc JUDGE, vu ACT ✓
   source non déclarée → judge.finalists_from → nœud non déclaré fantome ✓
   source qui ne réduit pas par keep_n → judge.finalists_from → implement ne réduit pas par keep_n : rien à départager ✓
   arbitre sans arête `none` → judge : nœud arbitre sans arête ['none'] — les trois issues ['sole', 'chosen', 'none'] sont fermées ✓
   arbitre sans arête `sole` → judge : nœud arbitre sans arête ['sole'] — les trois issues ['sole', 'chosen', 'none'] sont fermées ✓

fan-out : OK — la déclaration existe, et une config fautive ne se publie pas
porte « tests/fanout_config_test.py » passée en 0 s
$ uv run python tests/answer_test.py
#87 ← q9-receipt
1. première ligne valide + prose : réponse prise, reçu posté ✓
#87 ← reply-1001
2. option inconnue : les options valides dites, question ouverte ✓
3. commande pour une autre question : aucun bruit croisé ✓
#87 ← reply-1001
4. `/answer plop merger` : une réponse, la forme attendue ✓
#87 ← reply-1001
#87 ← reply-1001
5. trois tours de sync, un seul reproche ✓
#87 ← reply-1001
#87 ← q9-receipt
6. autorisation, fenêtre d'armement, première réponse : inchangés ✓

/answer : OK — la prose passe, le raté parle, le reste se tait
porte « tests/answer_test.py » passée en 0 s
$ uv run python tests/api_test.py
1. /api/items : id, titre, état, status, issue, PR ✓
2. autre canal, item actif : aucun lien inventé, status actif ✓
3. /api/item/14 : les sept clés, et l'inconnu rend None ✓
4. graph redessinable, runs chiffrés, journal daté ✓
   fanout projeté : 4 candidats, la surcharge de cmd résolue ✓
5. files : nom + href, et criteria.md servi tel quel ✓
6. /api/questions : le jeton sort du HTML, les options avec ✓
7. /api/heartbeat, et le payload sérialisable en ISO 8601 ✓
7b. /api/load : 4 runs en vol pour un plafond de 8 (8 par item) ✓
8. /api/graphs : nom, révision, date, items qui la portent ✓
9. /api/graph/<rév> : le bundle entier, l'inconnue rend None ✓

api : OK — les pages se lisent en JSON, sans dépendance ni écriture
porte « tests/api_test.py » passée en 0 s
$ uv run python tests/checklist_test.py
1. test_frontend → validate → review / implement, cycle borné ✓
#60 ← q5
#60 ← q5
#60 ← q5
2. sans validate.md : la question reste ce qu'elle était ✓
#60 ← q5
3. la question embarque les critères cochés et leurs preuves ✓
#60 ← q5
4. citation bornée à 40 lignes, le reste compté ✓

validate : OK — les critères sont cochés, puis lus par l'humain
porte « tests/checklist_test.py » passée en 0 s
$ uv run python tests/criteria_test.py
1. scope → clarify : `go` / `reformuler`, retour borné par `escalade` ✓
2. le prompt de scope : l'exception, le corps intouché, la spec ✓
#87 ← q9
#87 ← q9
3. sans criteria.md : aucun commentaire, question inchangée ✓
#87 ← code-task-g1-criteria-8b897d964e12
4. les critères de `scope` publiés sur l'issue, clé graph+génération ✓
5. deux ticks, un seul commentaire de critères ✓
#87 ← q9
6. la question de `clarify` porte la lecture du rail ✓
#87 ← code-task-g1-criteria-01fafe85dff7

critères : OK — le rail dit sa lecture avant d'écrire une ligne
porte « tests/criteria_test.py » passée en 0 s
$ uv run python tests/depends_test.py
1. `Depends-on: #N` lu, task list et prose ignorées ✓
#1 ← code-task-depends-invalid
#1 ← code-task-depends-invalid
#1 ← code-task-depends-invalid
#1 ← code-task-depends-invalid
#1 ← code-task-depends-invalid
2. dépendance invalide : ignorée, dite une fois ✓
#41 ← code-task-blocked
#42 admis → item 1
#41 ← rail:blocked
3. dépendance ouverte : pas d'item, `rail:blocked`, un commentaire ✓
#41 admis → item 2
#41 ⌫ rail:blocked
4. dépendance fermée : admission au tick suivant, label retiré ✓

dépendances : OK — l'admission attend, puis part toute seule
porte « tests/depends_test.py » passée en 1 s
$ uv run python tests/split_deps_test.py
1. seule la ligne `Depends-on: #<mère>` est réécrite ✓
#20 ← reparent-10
#20 ← Depends-on: #13 (était #10)
#21 ← reparent-10
#21 ← Depends-on: #13 (était #10)
#11 admis → item 1
#12 ← code-task-blocked
#13 ← code-task-blocked
#20 ← code-task-blocked
#21 ← code-task-blocked
#10 fermée — découpée en #11, #12, #13
2. les deux dépendants portent la dernière fille, chacun avec son commentaire ✓
#11 admis → item 2
3. la mère ne ferme qu'après, et aucun dépendant n'est admis ✓
#30 fermée — découpée en #31
4. mère sans dépendant : une fermeture, aucune écriture de plus ✓
#42 ← reparent-40
#42 ← Depends-on: #41 (était #40)
5. réécriture impossible : mère ouverte, issue non réécrite nommée ✓

découpe : OK — les dépendances suivent la dernière fille, et la mère ferme en dernier
porte « tests/split_deps_test.py » passée en 0 s
$ uv run python tests/heartbeat_test.py
1. un UPSERT par batteur, une lecture d'une ligne ✓
2. vivant à 3 s, à l'arrêt à 2 min, à l'arrêt sans battement ✓
3. /api/heartbeat : les deux battements, horodatage, âge, périmé ✓
4. en-tête : rail vivant, ou bandeau — les états sont figés ✓
#7 ← rail:stalled
#8 ← rail:blocked
#7 ⌫ rail:stalled
5. `rail:stalled` posé à côté de l'état, retiré au retour du battement ✓

battement : OK — le silence de chaque processus se voit
porte « tests/heartbeat_test.py » passée en 0 s
$ uv run python tests/links_test.py
1. sujet gh → https://github.com/Acid3croco/graphatom/issues/27 ✓
2. sujet quelconque : texte brut, échappé ✓
3. release.json → https://github.com/Acid3croco/graphatom/pull/31 ✓
4. /items : #27 → GitHub, titre → /item/14, autre canal → vide ✓
5. question : le titre de l'item à côté de son numéro ✓

liens : OK — l'issue, la PR et le titre sont lisibles depuis le frontend
porte « tests/links_test.py » passée en 0 s
$ uv run python tests/live_test.py
1. meta marqueur + conteneur #live + script, refresh en noscript ✓
2. version de l'item, ou la plus haute des items listés ✓
3. marqueur stable à données égales, changé par la version ou l'arrêt ✓
4. onglet caché et focus respectés, conteneur remplacé, aucun canal ✓

rafraîchissement : OK — la page se suit sans se recharger
porte « tests/live_test.py » passée en 0 s
$ uv run python tests/orphans_test.py
1. agent fauché au timeout après 2.0s, autopsie -15 ✓
2. groupe 4017100 entièrement révoqué ✓
3. traces agent-travail-1-1.log et prompt-travail-1-1.md écrites ✓
4. agent nominal appliqué, usage fusionné : {'outcome': 'ok', 'summary': 'fait', 'usage': {'input_tokens': 12, 'output_tokens': 3456}} ✓
4 bis. agent sans usage.json : résultat inchangé ✓
5. autopsie du crash : sortie 42, queue « Execution error » ✓
6. worker tué, agent orphelin toujours vivant : groupe 4017556 ✓
orphelin révoqué : groupe 4017556 (ps -o pgid= -p $$ > pgid.txt; sleep 300 & sleep 300)
7. orphelin révoqué par le faucheur, trace effacée ✓
8. trace d'une autre tentative : ni suivie ni effacée ✓
9. identité périmée (naissance, boot) : le faucheur ne tue personne ✓
10. trace illisible ou amputée : le faucheur passe son chemin ✓

orphelins : OK — ni le bail ni la mort du worker ne laissent d'agent
porte « tests/orphans_test.py » passée en 3 s
$ uv run python tests/timeout_marge_test.py
1. 6 paires de l'issue — [(120, 60), (180, 120), (600, 540), (900, 840), (1020, 960), (1800, 1740)] — la dérivation les calcule toutes ✓
2. un nœud sans `lease_s` garde le défaut de 570 s ✓
3. un `timeout_s` explicite est honoré, quel que soit le bail ✓
4. la marge vit une seule fois, nommée, à côté de LEASE_SECONDS — aucune soustraction littérale `- 60` ailleurs ✓
5. code-task.json : 13 nœuds à bail, aucun `timeout_s`, couperet dérivé à l'identique ✓
5. gauntlet.json : 6 nœuds à bail, aucun `timeout_s`, couperet dérivé à l'identique ✓

timeout_marge : OK — le couperet descend du bail, une seule marge, les douze valeurs de l'issue inchangées
porte « tests/timeout_marge_test.py » passée en 0 s
$ uv run python tests/timeout_test.py
1. 1 appel sortant, borné par TIMEOUT_S = 30.0 s ✓
2. timeout à 2.0 s : appel rendu en 2.0 s — timed out ✓
3. tick rendu en 30.1 s, jamais gelé (< 60 s) ✓
4. incident réseau nominal, aucun traceback : github injoignable : timed out — on réessaie ✓

timeout : OK — un serveur muet coûte un tick, plus l'éternité
porte « tests/timeout_test.py » passée en 32 s
PORTES OK — le candidat peut rendre son issue de succès
