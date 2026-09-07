"""Les noms que vulture croit morts et qui sont des rappels du protocole HTTP.

`http.server` appelle ces méthodes par leur nom : elles sont vivantes sans
qu'aucune ligne du dépôt ne les nomme. Ce fichier est lu par vulture avec
les sources, jamais importé.
"""

from graphatom.web import Handler

Handler.log_message
Handler.do_GET
Handler.do_POST
