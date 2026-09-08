import json
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
import test_tenancy_migration
from alembic import command
from test_seller_settings_migration import read_rows, seed_settings

legacy_database = test_tenancy_migration.legacy_database


def test_tracking_audit_migration_is_reversible_and_preserves_legacy_duplicates(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260907_0007")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    now = datetime.now(UTC)
    shared = {
        "organization_id": account["organization_id"],
        "seller_id": account["seller_id"],
        "account_id": account["id"],
        "environment": "live",
        "external_line_id": "legacy-duplicate-unit",
        "marketplace": "kaufland",
        "order_created_at": now,
        "status": "sent",
        "storefront": "de",
        "raw_json": "{}",
        "updated_at": now,
    }
    with engine.begin() as connection:
        connection.execute(lines.insert(), [
            {
                **shared,
                "id": uuid4().hex,
                "order_id": order_id,
                "search_text": f"{order_id} legacy-duplicate-unit".casefold(),
                "canonical_json": json.dumps({
                    "order_id": order_id,
                    "external_line_id": "legacy-duplicate-unit",
                    "details": {},
                }),
            }
            for order_id in ("LEGACY-A", "LEGACY-B")
        ])
    existing = sa.inspect(engine).get_table_names()
    snapshots = {name: read_rows(engine, name) for name in existing if name != "alembic_version"}
    command.upgrade(config, "20260908_0008")
    assert "seller_order_tracking_events" in sa.inspect(engine).get_table_names()
    assert any(
        foreign_key["constrained_columns"] == ["line_id"]
        and foreign_key["referred_table"] == "seller_order_lines"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in sa.inspect(engine).get_foreign_keys(
            "seller_order_tracking_events"
        )
    )
    assert read_rows(engine, "seller_order_tracking_events") == []
    assert snapshots == {name: read_rows(engine, name) for name in snapshots}
    duplicates = [
        row for row in read_rows(engine, "seller_order_lines")
        if row["external_line_id"] == "legacy-duplicate-unit"
    ]
    assert {row["order_id"] for row in duplicates} == {"LEGACY-A", "LEGACY-B"}
    command.downgrade(config, "20260907_0007")
    assert "seller_order_tracking_events" not in sa.inspect(engine).get_table_names()
    assert snapshots == {name: read_rows(engine, name) for name in snapshots}
