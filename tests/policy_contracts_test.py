"""Les constantes du noyau qui sont des contrats, prouvées en chiffres.

Un réglage se change ; un contrat se documente et se tient. Le README et
les docstrings du noyau en nomment cinq, et chaque preuve les lit en
valeurs littérales — jamais par le symbole qu'elle vérifie :

  1. un nœud sans `lease_s` reçoit un bail de 30 secondes à la réservation
  2. le couperet d'un agent descend du bail moins 60 secondes ; sans bail,
     570 secondes — les deux en chiffres, pas en symboles
  3. trois tentatives par passage : les deux premières pannes relancent sur
     place, la troisième escalade — sur un vrai routage
  4. la clé d'admission est « GRAPHATO » en ASCII, dans la plage signée d'un
     verrou consultatif PostgreSQL

Usage : uv run python tests/policy_contracts_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from outils import (bundle_epreuve, etat, item_neuf, provision_postgres,  # noqa: E402
                    route)

os.environ["GRAPHATOM_DSN"] = provision_postgres("graphatom-contrats")

from graphatom import db, graph, kernel, policy  # noqa: E402
from graphatom import workspace as bureau  # noqa: E402

TRAVAIL = {"travail": {"block": "ACT", "edges": {"ok": "fini"}}}


def bail_par_defaut(conn, revision: str) -> None:
    item_id = item_neuf(conn, revision, "bail")
    avant = policy.now()
    run = kernel.claim(conn, item_id)
    duree = (run["lease_expires_at"] - avant).total_seconds()
    assert 29 <= duree <= 31, duree
    print("1. sans lease_s, la réservation pose un bail de 30 secondes ✓")


def couperet() -> None:
    assert policy.agent_timeout_s({"lease_s": 100}) == 40
    assert policy.agent_timeout_s({}) == 570
    assert policy.agent_timeout_s({"agent": {"timeout_s": 12}}) == 12
    assert policy.agent_timeout_s({"execution": {"timeout_s": 7}, "lease_s": 100}) == 7
    print("2. couperet : bail − 60 s, sinon 570 s ; l'exécution puis l'agent priment ✓")


def trois_tentatives(conn, revision: str) -> None:
    item_id = item_neuf(conn, revision, "tentatives")
    assert route(conn, item_id, "crashed", attempt=1) == "travail"
    assert route(conn, item_id, "crashed", attempt=2) == "travail"
    assert route(conn, item_id, "crashed", attempt=3) == "escalate"
    assert etat(conn, item_id)["escalations"] == 1, "un WAIT d'escalade décompte à chaque entrée"
    print("3. deux pannes relancent sur place, la troisième escalade ✓")


def cle_admission() -> None:
    assert policy.ADMISSION_LOCK == int.from_bytes(b"GRAPHATO", "big")
    assert 0 < policy.ADMISSION_LOCK < 2 ** 63, "dans la plage signée d'un bigint"
    print("4. la clé d'admission est « GRAPHATO » en ASCII, dans la plage signée ✓")


def main() -> None:
    bureau.DATA_DIR = Path(tempfile.mkdtemp(prefix="graphatom-contrats-"))
    db.init_db(drop=True)
    with db.connect() as conn:
        revision = graph.publish(conn, bundle_epreuve("contrats", TRAVAIL, escalations=2))
        bail_par_defaut(conn, revision)
        couperet()
        trois_tentatives(conn, revision)
        cle_admission()
    print("\ncontrats du noyau : OK — cinq chiffres tenus en toutes lettres")


if __name__ == "__main__":
    main()
