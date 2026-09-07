"""Le test de la politique : d'où descend le couperet d'un agent.

Sans base ni processus — `policy` est une feuille, et ses règles se prouvent
sur des configs de nœud :

  1. `execution.timeout_s` déclaré : l'exception est honorée telle quelle
  2. `agent.timeout_s` déclaré, sans `execution` : honoré aussi
  3. sans couperet déclaré, il descend du bail — `lease_s` moins la marge du
     faucheur, pour que le process soit coupé AVANT que le bail n'expire
  4. sans bail non plus, le défaut historique — 570 s, la valeur documentée,
     pas seulement le symbole importé — ou celui que l'appelant donne
  5. l'horloge du noyau est en UTC, jamais naïve, et elle avance : chaque
     lecture tombe entre deux lectures indépendantes de l'horloge système

Usage : uv run python tests/policy_test.py
"""

import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import policy  # noqa: E402
from graphatom.policy import AGENT_LEASE_MARGIN_S  # noqa: E402

DEFAUT_DOCUMENTE_S = 570  # le budget historique d'une tentative d'agent


def execution_declaree() -> None:
    config = {"execution": {"timeout_s": 12}, "agent": {"timeout_s": 99},
              "lease_s": 600}
    assert policy.agent_timeout_s(config) == 12.0
    print("1. execution.timeout_s prime sur tout le reste ✓")


def agent_declare() -> None:
    config = {"execution": {"kind": "agent"}, "agent": {"timeout_s": "45"},
              "lease_s": 600}
    couperet = policy.agent_timeout_s(config)
    assert couperet == 45.0 and isinstance(couperet, float), couperet
    print("2. agent.timeout_s honoré, converti en secondes flottantes ✓")


def descend_du_bail() -> None:
    couperet = policy.agent_timeout_s({"lease_s": 600})
    assert couperet == 600 - AGENT_LEASE_MARGIN_S, couperet
    assert couperet < 600, "le process doit être coupé avant le bail"
    print(f"3. sans couperet déclaré : bail − marge = {couperet:.0f}s ✓")


def defaut() -> None:
    assert policy.AGENT_TIMEOUT_S == DEFAUT_DOCUMENTE_S, policy.AGENT_TIMEOUT_S
    assert policy.agent_timeout_s({}) == DEFAUT_DOCUMENTE_S
    assert policy.agent_timeout_s({"agent": {}}, default=7.0) == 7.0
    print(f"4. sans bail : le défaut documenté, {DEFAUT_DOCUMENTE_S}s, ou celui donné ✓")


def horloge_utc() -> None:
    avant = dt.datetime.now(dt.timezone.utc)
    instant = policy.now()
    apres = dt.datetime.now(dt.timezone.utc)
    assert instant.tzinfo is not None, "horloge naïve"
    assert instant.utcoffset() == dt.timedelta(0), instant.utcoffset()
    assert avant <= instant <= apres, (avant, instant, apres)
    time.sleep(0.002)
    assert policy.now() > instant, "l'horloge est figée"
    print("5. now() en UTC, bornée par deux lectures système, et elle avance ✓")


def main() -> None:
    execution_declaree()
    agent_declare()
    descend_du_bail()
    defaut()
    horloge_utc()
    print("\npolicy : OK")


if __name__ == "__main__":
    main()
