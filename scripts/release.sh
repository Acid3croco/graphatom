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
ATTENTES=8       # relectures du merge, une toutes les 15 s : deux minutes

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

# 2. le commit — titre de l'issue, ligne vide, `Closes #<num>`
if [ -n "$(git -C "$WT" status --porcelain)" ]; then
    TITRE=$(gh issue view "$NUM" --repo "$DEPOT" --json title -q .title) \
        || echoue "titre de l'issue #$NUM introuvable" 4
    lance git -C "$WT" add -A || echoue "git add" 4
    lance git -C "$WT" commit -qm "$TITRE" -m "Closes #$NUM" || echoue "git commit" 4
    dit "commit $(git -C "$WT" rev-parse --short HEAD) — $TITRE"
else
    dit "rien à committer — HEAD reste $(git -C "$WT" rev-parse --short HEAD)"
fi

# 3. le push
lance git -C "$WT" push -u origin "$BR" || echoue "push de $BR" 5

# 4. la PR : celle qui est ouverte, sinon une neuve. Un nom de branche se
#    réutilise d'un cycle à l'autre, et `gh pr view <branche>` rend alors
#    volontiers la PR mergée du cycle précédent — c'est `gh pr list`, filtré
#    sur la tête et sur l'état, qui ne rend que celle qui compte.
ouverte() {
    gh pr list --repo "$DEPOT" --head "$BR" --state open \
        --json number -q '.[0].number // empty' 2>/dev/null
}
PR=$(ouverte)
if [ -z "$PR" ]; then
    lance gh pr create --repo "$DEPOT" --head "$BR" --fill || echoue "création de la PR" 6
    PR=$(ouverte)
    if [ -z "$PR" ]; then echoue "PR créée mais introuvable sur $BR" 6; fi
fi
URL=$(gh pr view "$PR" --repo "$DEPOT" --json url -q .url)
dit "PR #$PR — $URL"

# 5. le merge, puis sa surveillance jusqu'au SHA. `gh pr view` n'a pas de
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

# 6. les trois clés que le frontend lit — rien de libre dedans
printf '{"pr_number": %s, "pr_url": "%s", "merge_sha": "%s"}\n' "$PR" "$URL" "$SHA" \
    > "$WS/release.json"
dit "PR #$PR mergée — commit de merge $SHA"
dit "script suffisant : les six pas sont passés sans intervention."
exit 0
