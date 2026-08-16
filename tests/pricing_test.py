"""Tarifs publics : parsing fermé, tokens disjoints et résolution du modèle.

Usage : uv run python tests/pricing_test.py
"""

import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import pricing  # noqa: E402


OPENAI = """## Pricing
### Text tokens
| Metric | Price | Unit |
| --- | ---: | --- |
| Input | $5 | 1M tokens |
| Cached input | $0.5 | 1M tokens |
| Output | $30 | 1M tokens |
- Cache writes are billed at 1.25x the uncached input token rate.
## Endpoints
"""

DEEPSEEK = """<table><tr><td>MODEL</td><td>deepseek-v4-flash</td>
<td>deepseek-v4-pro</td></tr>
<tr><td>1M INPUT TOKENS (CACHE HIT)</td><td>$0.0028</td><td>$0.003625</td></tr>
<tr><td>1M INPUT TOKENS (CACHE MISS)</td><td>$0.14</td><td>$0.435</td></tr>
<tr><td>1M OUTPUT TOKENS</td><td>$0.28</td><td>$0.87</td></tr></table>"""


def graph(fanout: bool = False) -> dict:
    config: dict = {"execution": {"kind": "agent"},
                    "agent": {"prompt": "travaille"}}
    if fanout:
        config["fanout"] = {
            "reduce": "first_pass",
            "variants": [{"agent": {"cli": "codex", "model": "gpt-5.6-luna"}}],
        }
    return {"nodes": {"work": {"block": "ACT", "config": config}}}


def main() -> None:
    sol = pricing.parse_openai("gpt-5.6-sol", OPENAI)
    assert (sol.input, sol.cache_read, sol.cache_write, sol.output) == (
        Decimal("5"), Decimal("0.5"), Decimal("6.25"), Decimal("30"))
    deepseek = pricing.parse_deepseek(DEEPSEEK)
    assert [price.model for price in deepseek] == [
        "deepseek-v4-flash", "deepseek-v4-pro"]
    assert deepseek[0].cache_read == Decimal("0.0028")
    print("1. pages officielles : quatre classes de prix lues sans tolérance ✓")

    codex = pricing.normalize("openai", {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 800_000,
        "cache_write_input_tokens": 100_000,
        "output_tokens": 20_000,
        "reasoning_output_tokens": 15_000,
    })
    assert codex == pricing.Tokens(100_000, 800_000, 100_000, 20_000)
    costs = pricing.components(codex, sol)
    assert costs["estimated_cost_usd"] == Decimal("2.125")

    opencode = pricing.normalize("deepseek", {
        "input_tokens": 100, "cache_read_tokens": 200,
        "cache_write_tokens": 300, "output_tokens": 400,
        "reasoning_tokens": 500,
    })
    assert opencode == pricing.Tokens(100, 200, 300, 900)
    print("2. Codex et OpenCode : cache et raisonnement jamais comptés deux fois ✓")

    structured = graph(fanout=True)
    assert pricing.run_model(
        structured, {"node": "work", "candidate": 0}, {}) == (
            "openai", "gpt-5.6-luna", "graph")
    reported = pricing.run_model(
        graph(), {"node": "work", "candidate": None},
        {"model": "opencode/deepseek-v4-flash-free"})
    assert reported == ("deepseek", "deepseek-v4-flash", "usage")
    # un run sans modèle déclaré ni rapporté reste non estimé — jamais deviné
    assert pricing.run_model(graph(), {"node": "work", "candidate": None},
                             {}) is None
    print("3. modèle : usage puis graph, jamais deviné ✓")
    print("\npricing : OK")


if __name__ == "__main__":
    main()
