#!/usr/bin/env bash
#
# Ferme la boucle git d'un item du rail : commit, push, PR, merge surveillé.
#
# Tout le nominal d'une release tient ici — déterministe, sans modèle. Le
# nœud `release` du graph ne fait que lancer ce script : sortie 0, c'est
# fini. Chaque pas a son code de retour, pour que l'agent qui reprend la
# main sache lequel a lâché sans relire un journal :
#
#   2  environnement incomplet (workspace, clone de référence, sujet)
#   3  worktree absent, ou pas sur la branche de l'item
#   4  commit refusé (ou titre d'issue introuvable)
#   5  push refusé
#   6  PR ni retrouvée ni créée
#   7  merge refusé
#   8  merge lancé, jamais observé
#   9  rapprochement d'origin/main impossible sans jugement
#  10  fetch en échec, ou origin/main illisible — main serait lu périmé
#  11  porte de construction en échec sur le contenu absorbé
#
# Les codes 2 à 8 gardent leur sens : le pas de rapprochement est arrivé
# après eux, il prend donc les deux codes libres qui suivent, pas la place
# qu'il occupe dans la course.
#
# Le compte rendu s'écrit pas à pas dans `release.md` du workspace ; les
# trois clés que le web lit vont dans `release.json`. Le script n'écrit
# jamais `outcome.json` : l'issue du nœud appartient à l'agent qui le
# lance — lui seul sait s'il a dû réparer quelque chose.
#
# Usage : scripts/release.sh — tout vient des variables GRAPHATOM_*.

set -u

WS="${GRAPHATOM_WORKSPACE:-}"
REPO="${GRAPHATOM_REPO_DIR:-}"
SUJET="${GRAPHATOM_SUBJECT_KEY:-}"
MD=""            # release.md, connu dès que le workspace l'est
TITRE=""         # le titre de l'issue, lu au plus tard : le commit et la PR le portent
ATTENTES=8       # relectures du merge, une toutes les 15 s : deux minutes
ABSORBE=""       # les fichiers que le rapprochement a changés — vide, aucune porte

dit() {  # une ligne de journal, à l'écran et dans le compte rendu
    printf '%s\n' "$*"
    if [ -n "$MD" ]; then printf '%s\n' "$*" >> "$MD"; fi
}

lance() {  # exécute, journalise la commande et sa sortie, rend son code
    dit "\$ $*"
    SORTIE=$("$@" 2>&1)
    CODE=$?
    if [ -n "$SORTIE" ]; then dit "$SORTIE"; fi
    return $CODE
}

echoue() {  # dernier mot du script : le pas qui a lâché, puis son code
    dit "ÉCHEC — pas « $1 » → code $2"
    exit "$2"
}

lis_titre() {  # le titre de l'issue dans TITRE, une seule fois
    if [ -z "$TITRE" ]; then
        TITRE=$(gh issue view "$NUM" --repo "$DEPOT" --json title -q .title)
    fi
    [ -n "$TITRE" ]
}

# 0. l'environnement du bloc : sans lui, rien n'est identifiable
if [ ! -d "$WS" ] || [ -z "$REPO" ]; then
    echo "GRAPHATOM_WORKSPACE (un répertoire) et GRAPHATOM_REPO_DIR sont requis" >&2
    exit 2
fi
MD="$WS/release.md"
NUM=$(printf '%s' "$SUJET" | sed -n 's/.*#\([0-9][0-9]*\)$/\1/p')
DEPOT=$(printf '%s' "$SUJET" | sed -n 's|^gh:\([^#]*\)#[0-9][0-9]*$|\1|p')
WT="$REPO/.worktrees/rail-$(basename "$WS")"
BR="rail/issue-$NUM"

dit ""
dit "## release de ${SUJET:-sujet inconnu} — $(date -Is)"
if [ -z "$NUM" ] || [ -z "$DEPOT" ]; then
    echoue "sujet ${SUJET:-vide} : ni dépôt ni numéro d'issue à en tirer" 2
fi

# 1. le worktree de l'item, et lui seul : le clone de référence n'est pas l'atelier
if [ ! -d "$WT" ]; then echoue "worktree $WT absent" 3; fi
TETE=$(git -C "$WT" rev-parse --abbrev-ref HEAD 2>&1)
if [ "$TETE" != "$BR" ]; then echoue "worktree $WT sur $TETE au lieu de $BR" 3; fi
cd "$WT" || echoue "worktree $WT inaccessible" 3  # `gh` lit le dépôt sous ses pieds

# 2. le rapprochement d'`origin/main`, dans le worktree de l'item. Un merge,
#    et rien qui réécrive l'histoire : la branche est publique dès son
#    premier push. Le cas fréquent — main n'a pas bougé, ou a bougé ailleurs
#    — se résout tout seul ; le conflit textuel, lui, demande un jugement, et
#    le script le rend à l'agent plutôt que de le trancher.
lance git -C "$WT" fetch origin \
    || echoue "fetch d'origin : main serait lu périmé, la release s'arrête ici" 10
AMONT=$(git -C "$WT" rev-parse --short origin/main 2>&1) \
    || echoue "origin/main introuvable après le fetch : $AMONT" 10
AVANT=$(git -C "$WT" rev-parse HEAD)
if git -C "$WT" merge-base --is-ancestor origin/main HEAD; then
    dit "rapprochement : déjà à jour avec origin/main ($AMONT) — rien d'absorbé"
elif lance git -C "$WT" merge origin/main --no-edit; then
    FUSION=$(git -C "$WT" rev-parse --short HEAD)
    ABSORBE=$(git -C "$WT" diff --name-only "$AVANT" HEAD)
    dit "rapprochement : merge automatique d'origin/main ($AMONT) — commit $FUSION"
else
    CONFLITS=$(git -C "$WT" diff --name-only --diff-filter=U)
    if [ -n "$CONFLITS" ]; then
        dit "fichiers en conflit avec origin/main ($AMONT) :"
        dit "$CONFLITS"
        lance git -C "$WT" merge --abort || dit "merge --abort refusé — worktree à relire"
    else
        # git a refusé d'entrée : rien à annuler, et la liste qu'il vient
        # d'écrire est celle des fichiers qu'il n'a pas voulu écraser
        dit "merge refusé d'entrée : du travail non commité sur des fichiers bougés"
        dit "par origin/main ($AMONT) — la liste est juste au-dessus"
    fi
    echoue "rapprochement d'origin/main ($AMONT) impossible sans jugement" 9
fi

# 2 bis. la porte de construction, sur le contenu que le rapprochement vient
#    d'absorber. Un merge sans le moindre marqueur de conflit peut casser la
#    construction : deux branches se compilaient chacune de son côté, leur
#    somme non — git a raison sur le texte et tort sur le sens. Le
#    compilateur tranche cette classe d'erreurs en secondes ; on ne rejoue
#    donc pas les tests ici, on compile. Rien d'absorbé, rien à vérifier :
#    le contenu a déjà été testé tel quel par les nœuds de test, et ce
#    chemin-là — le plus fréquent — reste aussi rapide qu'avant.
if [ -n "$ABSORBE" ]; then
    dit "contenu absorbé — $(printf '%s\n' "$ABSORBE" | wc -l) fichier(s) à construire"
    # le bytecode part dans un répertoire jetable : une porte ne salit pas le
    # worktree qu'elle vérifie, et le commit qui suit n'a rien de neuf à voir
    PYC=$(mktemp -d)
    export PYTHONPYCACHEPREFIX="$PYC"
    lance python3 -m compileall -q "$WT/src"
    PORTE=$?
    unset PYTHONPYCACHEPREFIX
    rm -rf "$PYC"
    [ "$PORTE" -eq 0 ] || echoue "porte compilation python sur le contenu absorbé" 11
    dit "porte compilation python : passée"
    # le front ne se construit que si l'absorption l'a touché : c'est la
    # porte chère, et un merge qui n'apporte que du Python ne la mérite pas
    if printf '%s\n' "$ABSORBE" | grep -q '^front/'; then
        lance npm --prefix "$WT/front" run build \
            || echoue "porte construction front sur le contenu absorbé" 11
        dit "porte construction front : passée"
    fi
fi

# 3. le commit — titre de l'issue, ligne vide, `Closes #<num>`. Un agent qui
#    commite au fil de l'eau laisse un worktree propre : il n'y a alors rien
#    à committer, et ce n'est pas une erreur — c'est le cas nominal.
if [ -n "$(git -C "$WT" status --porcelain)" ]; then
    lis_titre || echoue "titre de l'issue #$NUM introuvable" 4
    lance git -C "$WT" add -A || echoue "git add" 4
    lance git -C "$WT" commit -qm "$TITRE" -m "Closes #$NUM" || echoue "git commit" 4
    dit "commit $(git -C "$WT" rev-parse --short HEAD) — $TITRE"
else
    dit "rien à committer — HEAD reste $(git -C "$WT" rev-parse --short HEAD)"
fi

# 4. le push
lance git -C "$WT" push -u origin "$BR" || echoue "push de $BR" 5

# 5. la PR : celle qui est ouverte, sinon une neuve. Un nom de branche se
#    réutilise d'un cycle à l'autre, et `gh pr view <branche>` rend alors
#    volontiers la PR mergée du cycle précédent — c'est `gh pr list`, filtré
#    sur la tête et sur l'état, qui ne rend que celle qui compte.
#    Le corps est écrit, jamais déduit des commits : depuis que l'agent
#    d'implémentation commite au fil de l'eau, aucun de ses messages ne porte
#    `Closes #<num>`, et c'est cette ligne-là qui ferme l'issue au merge.
ouverte() {
    gh pr list --repo "$DEPOT" --head "$BR" --state open \
        --json number -q '.[0].number // empty' 2>/dev/null
}
PR=$(ouverte)
if [ -z "$PR" ]; then
    lis_titre || echoue "titre de l'issue #$NUM introuvable" 6
    lance gh pr create --repo "$DEPOT" --head "$BR" --title "$TITRE" \
        --body "Closes #$NUM" || echoue "création de la PR" 6
    PR=$(ouverte)
    if [ -z "$PR" ]; then echoue "PR créée mais introuvable sur $BR" 6; fi
fi
URL=$(gh pr view "$PR" --repo "$DEPOT" --json url -q .url)
dit "PR #$PR — $URL"

# 6. le merge, puis sa surveillance jusqu'au SHA. `gh pr view` n'a pas de
#    champ `merged` : c'est `state` qui dit MERGED, et `mergeCommit` le SHA.
lance gh pr merge "$PR" --repo "$DEPOT" --merge || echoue "merge de la PR #$PR" 7
SHA=""
essai=0
while [ "$essai" -lt "$ATTENTES" ]; do
    VU=$(gh pr view "$PR" --repo "$DEPOT" --json state,mergeCommit \
         -q '.state + " " + (.mergeCommit.oid // "")' 2>/dev/null)
    case "$VU" in
        "MERGED "?*) SHA=${VU#MERGED } ;;
    esac
    if [ -n "$SHA" ]; then break; fi
    sleep 15
    essai=$((essai + 1))
done
if [ -z "$SHA" ]; then echoue "merge de la PR #$PR jamais observé en deux minutes" 8; fi

# 7. les trois clés que le frontend lit — rien de libre dedans
printf '{"pr_number": %s, "pr_url": "%s", "merge_sha": "%s"}\n' "$PR" "$URL" "$SHA" \
    > "$WS/release.json"
dit "PR #$PR mergée — commit de merge $SHA"
dit "script suffisant : les sept pas sont passés sans intervention."
exit 0
