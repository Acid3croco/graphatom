#!/usr/bin/env bash
#
# Le test de l'image du front : elle builde, et elle tient dans son budget.
#
# Ce que ce script vérifie n'a pas besoin de base ni de serveur — c'est un
# `docker build` et une soustraction. Le reste du front se vérifie dans un
# navigateur, sur les pages rendues, et c'est le travail du test frontend.
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

echo "1. build de l'image $IMAGE depuis $ROOT/front"
docker build -t "$IMAGE" "$ROOT/front"

echo "2. contrôle de taille"
size=$(docker image inspect "$IMAGE" --format '{{.Size}}')
printf '   %s o pour un budget de %s o\n' "$size" "$BUDGET"
if [ "$size" -ge "$BUDGET" ]; then
    echo "ÉCHEC : l'image dépasse son budget" >&2
    exit 1
fi

echo
echo "front : OK — l'image builde et tient dans son budget"
