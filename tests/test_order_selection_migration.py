import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import sqlalchemy as sa
import test_tenancy_migration
from alembic import command
from test_seller_settings_migration import read_rows, seed_settings

legacy_database = test_tenancy_migration.legacy_database


def test_migration_backfills_projections_preserves_source_and_reverses(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260907_0006")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    table = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    now = datetime.now(UTC)
    item = {"currency": "EUR", "quantity": 2, "commission_amount": "0",
            "sale_amount_eur": "123.45", "commission_amount_eur": "0",
            "payout_amount_eur": "123.45", "purchase_cost_eur": "100",
            "profit_amount_eur": "23.45", "status": "sent",
            "details": {"tracking": "TRACK", "carrier": "DHL"}}
    original = {
        "id": uuid4().hex, "seller_id": account["seller_id"],
        "organization_id": account["organization_id"], "account_id": account["id"],
        "environment": "live", "marketplace": "kaufland", "external_line_id": "unit",
        "order_id": "order", "status": "sent", "storefront": "de", "search_text": "order",
        "canonical_json": json.dumps(item), "raw_json": '{"private":"preserved"}',
        "updated_at": now, "order_created_at": now,
    }
    with engine.begin() as connection:
        connection.execute(table.insert().values(**original))
    tables = sa.inspect(engine).get_table_names()
    before = {name: read_rows(engine, name) for name in tables if name != "alembic_version"}
    command.upgrade(config, "20260907_0007")
    reflected = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        row = connection.execute(sa.select(reflected)).mappings().one()
    assert row["has_tracking"] is True and row["has_commission"] is True
    assert row["quantity"] == 2 and row["sale_eur"] == Decimal("123.45")
    assert row["commission_eur"] == 0 and row["profit_eur"] == Decimal("23.45")
    assert row["projection_updated_at"] == row["updated_at"]
    assert row["currency"] == "EUR" and row["carrier"] == "DHL"
    assert row["canonical_json"] == original["canonical_json"]
    assert row["raw_json"] == original["raw_json"]
    for name in before:
        if name != "seller_order_lines":
            assert read_rows(engine, name) == before[name]
    assert read_rows(engine, "seller_order_selections") == []
    command.upgrade(config, "20260907_0007")
    command.downgrade(config, "20260907_0006")
    assert "seller_order_selections" not in sa.inspect(engine).get_table_names()
    assert {name: read_rows(engine, name) for name in before} == before
    command.upgrade(config, "20260907_0007")
    assert read_rows(engine, "seller_order_lines")[0]["sale_eur"] == Decimal("123.45")
