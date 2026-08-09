"""Le relevé épinglé chiffre un run en base et sort dans l'API.

Usage : uv run python tests/pricing_db_test.py
"""

import datetime as dt
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphatom import db, pricing, web  # noqa: E402


INSTANCE = os.environ.get(
    "GRAPHATOM_AGENT_DSN",
    "postgresql://graphatom:graphatom@127.0.0.1:54321/postgres",
)
NAME = f"graphatom_test_pricing_{os.getpid()}"


def main() -> None:
    with psycopg.connect(INSTANCE, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(NAME)))
    db.DSN = psycopg.conninfo.make_conninfo(INSTANCE, dbname=NAME)
    try:
        changes = db.init_db()
        assert any("model_price" in change for change in changes), changes
        assert any("run_cost" in change for change in changes), changes
        print("1. migration : relevés et coûts de runs créés ✓")

        now = dt.datetime.now(dt.timezone.utc)
        bundle = {
            "name": "pricing-test", "entry": "work", "on_kernel": {},
            "nodes": {"work": {"block": "ACT", "config": {"agent": {
                "cli": "codex", "model": "gpt-5.6-sol"}}}},
        }
        usage = {"input_tokens": 1_000_000, "cached_input_tokens": 800_000,
                 "cache_write_input_tokens": 100_000, "output_tokens": 20_000,
                 "reasoning_output_tokens": 15_000}
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO graph_revision (id, name, bundle) VALUES ('rev', 'pricing-test', %s)",
                (json.dumps(bundle),),
            )
            subject = conn.execute(
                "INSERT INTO subject (graph, subject_key) VALUES ('pricing-test', 'test:1') "
                "RETURNING id"
            ).fetchone()["id"]
            item = conn.execute(
                "INSERT INTO work_item (subject_id, generation, revision, state, "
                "escalations, wall_deadline, terminal_at) "
                "VALUES (%s, 1, 'rev', 'work', 1, %s, %s) RETURNING id",
                (subject, now, now),
            ).fetchone()["id"]
            run = conn.execute(
                "INSERT INTO node_run (item_id, node, attempt, status, fence, "
                "expected_version, lease_expires_at, finished_at, outcome, result) "
                "VALUES (%s, 'work', 1, 'applied', 1, 0, %s, %s, 'done', %s) "
                "RETURNING id",
                (item, now, now, json.dumps({"outcome": "done", "usage": usage})),
            ).fetchone()["id"]
            old_fetch = pricing.fetch_prices
            openai = [
                pricing.Price("openai", model, "official", Decimal("5"),
                              Decimal("0.5"), Decimal("6.25"), Decimal("30"))
                for model in pricing.OPENAI_MODELS
            ]
            deepseek = [
                pricing.Price("deepseek", model, "official", Decimal("0.14"),
                              Decimal("0.0028"), Decimal("0.14"), Decimal("0.28"))
                for model in ("deepseek-v4-flash", "deepseek-v4-pro")
            ]
            pricing.fetch_prices = lambda: openai + deepseek
            try:
                assert pricing.refresh(conn) == 5
                assert pricing.refresh(conn) == 0, "pas de seconde relève le même jour"
            finally:
                pricing.fetch_prices = old_fetch
            price = conn.execute(
                "SELECT id FROM model_price WHERE provider = 'openai' "
                "AND model = 'gpt-5.6-sol'"
            ).fetchone()["id"]

            assert pricing.estimate_missing(conn) == (1, 0)
            saved = conn.execute(
                "SELECT * FROM run_cost WHERE run_id = %s", (run,)
            ).fetchone()
            assert saved["price_id"] == price
            assert float(saved["estimated_cost_usd"]) == 2.125, saved

            row = web._api_items(conn)[0]
            assert row["total_cost_usd"] == 0
            assert row["reported_cost_usd"] is None
            assert row["estimated_cost_usd"] == 2.125
            detail = web._api_item(conn, item)
            estimate = detail["runs"][0]["cost_estimate"]
            assert estimate["model"] == "gpt-5.6-sol"
            assert estimate["pricing_basis"] == "base_standard"
            assert estimate["cache_write_cost_usd"] == 0.625
        print("2. run : quatre parts, total et relevé sortent dans l'API ✓")
        print("\npricing db : OK")
    finally:
        with psycopg.connect(INSTANCE, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                          .format(sql.Identifier(NAME)))


if __name__ == "__main__":
    main()
