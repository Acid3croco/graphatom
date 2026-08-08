#!/usr/bin/env bash
#
# Le test de l'image du front : elle builde, elle tient dans son budget, et
# la page d'un item jugé sépare bien les deux bouts de l'haltère.
#
# Les deux premières étapes n'ont besoin de rien — un `docker build` et une
# soustraction. La troisième rend une vraie page : elle lance l'image contre
# `tests/front_stub_api.py`, une API de doublure qui sert un item passé par
# un nœud arbitre, et lit le DOM qui en sort. Ce qui s'y prouve est le prix
# du jugement affiché à part de celui des candidats — un total unique
# mélangerait ce qu'on paie pour produire et ce qu'on paie pour choisir, et
# la question qui compte n'aurait plus de réponse.
#
# Le budget est une contrainte de déploiement, pas une coquetterie : le
# front voyage à chaque redéploiement, et une image Next.js qui embarque
# ses sources et sa toolchain triple de taille sans que rien ne le dise.
# `output: 'standalone'` et le Dockerfile multi-étage tiennent ce budget ;
# ce script est ce qui le remarque le jour où l'un des deux lâche.
#
# Usage : bash tests/front_test.sh
#
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${GRAPHATOM_FRONT_IMAGE:-graphatom-front}
BUDGET=${GRAPHATOM_FRONT_BUDGET:-400000000}  # 400 Mo
API_PORT=${GRAPHATOM_FRONT_TEST_API_PORT:-8853}
WEB_PORT=${GRAPHATOM_FRONT_TEST_WEB_PORT:-3853}

echo "1. build de l'image $IMAGE depuis $ROOT/front"
docker build -t "$IMAGE" "$ROOT/front"

echo "2. contrôle de taille"
size=$(docker image inspect "$IMAGE" --format '{{.Size}}')
printf '   %s o pour un budget de %s o\n' "$size" "$BUDGET"
if [ "$size" -ge "$BUDGET" ]; then
    echo "ÉCHEC : l'image dépasse son budget" >&2
    exit 1
fi

# La doublure et le conteneur meurent avec le script, quoi qu'il arrive :
# un port resté pris ferait échouer la prochaine exécution sans rien dire.
STUB=""
CONTENEUR=""
nettoyer() {
    [ -n "$CONTENEUR" ] && docker rm -f "$CONTENEUR" >/dev/null 2>&1
    [ -n "$STUB" ] && kill "$STUB" >/dev/null 2>&1
    return 0
}
trap nettoyer EXIT

echo "3. le prix du jugement, à part de celui des candidats"
uv run --project "$ROOT" python "$ROOT/tests/front_stub_api.py" "$API_PORT" &
STUB=$!

# `--network host` : le serveur Next rend la page côté conteneur, c'est donc
# lui qui appelle la doublure — sur la boucle locale de l'hôte.
CONTENEUR=$(docker run -d --network host \
    -e "GRAPHATOM_API_URL=http://127.0.0.1:$API_PORT" \
    -e "PORT=$WEB_PORT" "$IMAGE")

page=""
for _ in $(seq 40); do
    sleep 1
    page=$(curl -sf "http://127.0.0.1:$WEB_PORT/item/1" 2>/dev/null) && break
    page=""
done
if [ -z "$page" ]; then
    echo "ÉCHEC : la page de l'item 1 ne répond pas" >&2
    docker logs "$CONTENEUR" >&2
    exit 1
fi

# Le front groupe les milliers avec une espace insécable et React coupe ses
# textes par des commentaires : on compare donc sur un rendu sans espaces ni
# marqueurs, où chaque usage redevient une suite contiguë.
nu=$(printf '%s' "$page" | sed 's/<!-- -->//g; s/\xc2\xa0//g; s/\xe2\x80\xaf//g; s/[[:space:]]//g')

# Les trois parts, chacune avec ses propres chiffres. Que les trois lignes
# se lisent séparément est tout l'objet : le total ne dit pas si le juge
# coûte plus cher que ce qu'il départage, ces deux-là le disent.
JUGEMENT='700000in·9000out·2.5000$'
CANDIDATS='300000in·21000out·6.2500$'
TOTAL='1000000in·30000out·8.7500$'
for attendu in 'data-testid="cout-jugement"' \
               "jugement$JUGEMENT" "candidats$CANDIDATS" "$TOTAL"; do
    case $nu in
        *"$attendu"*) ;;
        *) echo "ÉCHEC : « $attendu » absent du DOM rendu" >&2
           printf '%s\n' "$nu" | grep -o 'cout-jugement.\{0,200\}' >&2 || true
           exit 1 ;;
    esac
done
printf '   jugement %s, candidats %s, total %s — trois lignes, trois prix ✓\n' \
    "$JUGEMENT" "$CANDIDATS" "$TOTAL"

echo
echo "front : OK — l'image builde, tient dans son budget, et sépare les deux coûts"
