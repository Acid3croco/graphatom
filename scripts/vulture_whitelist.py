"""Les noms que vulture croit morts et qui sont des rappels du protocole HTTP.

`http.server` appelle ces méthodes par leur nom : elles sont vivantes sans
qu'aucune ligne du dépôt ne les nomme. Ce fichier est lu par vulture avec
les sources, jamais importé.
"""

from graphatom.web import Handler

Handler.log_message
Handler.do_GET
Handler.do_POST

# Le serveur factice des preuves du client GitHub sert PATCH et DELETE par
# les mêmes rappels du protocole — nommés par `http.server`, jamais par nous.
from github_outils import Serveur  # noqa: E402 — lu par vulture, jamais importé

Serveur.do_PATCH
Serveur.do_DELETE
