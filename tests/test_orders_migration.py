from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import test_tenancy_migration
from alembic import command
from marketplace_hub_core.orders.repository import SqlOrdersRepository
from test_seller_settings_migration import read_rows, seed_settings

legacy_database = test_tenancy_migration.legacy_database


def test_orders_migration_adds_cache_and_jobs_without_mutating_existing_data(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260907_0005")
    existing = sa.inspect(engine).get_table_names()
    snapshots = {name: read_rows(engine, name) for name in existing if name != "alembic_version"}
    command.upgrade(config, "20260907_0006")
    assert read_rows(engine, "seller_order_lines") == []
    assert read_rows(engine, "seller_order_sync_jobs") == []
    assert snapshots == {name: read_rows(engine, name) for name in snapshots}
    command.upgrade(config, "20260907_0006")
    command.downgrade(config, "20260907_0005")
    assert "seller_order_lines" not in sa.inspect(engine).get_table_names()
    assert "seller_order_sync_jobs" not in sa.inspect(engine).get_table_names()
    assert snapshots == {name: read_rows(engine, name) for name in snapshots}


def test_migration_active_job_scope_restart_and_environment_isolation(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260907_0006")
    accounts = read_rows(engine, "seller_marketplace_accounts")
    user = read_rows(engine, "auth_users")[0]
    tables = sa.MetaData()
    jobs = sa.Table("seller_order_sync_jobs", tables, autoload_with=engine)
    now = datetime.now(UTC)
    values = {
        "id": uuid4().hex, "seller_id": accounts[0]["seller_id"],
        "organization_id": accounts[0]["organization_id"], "account_id": accounts[0]["id"],
        "requested_by": user["id"], "realm": "seller", "marketplace": "kaufland",
        "environment": "live", "maximum": 1000, "include_details": True,
        "status": "queued", "processed": 0, "message": "Queued", "created_at": now,
        "updated_at": now,
    }
    # Reflection represents SQLite UUID columns as text; seed using their stored form.
    values = {key: value.hex if isinstance(value, UUID) else value for key, value in values.items()}
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(**values))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(jobs.insert().values(**{**values, "id": uuid4().hex}))
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(**{**values, "id": uuid4().hex,
                                                   "environment": "playground"}))
        connection.execute(jobs.update().where(jobs.c.id == values["id"]).values(status="done"))
        connection.execute(jobs.insert().values(**{**values, "id": uuid4().hex}))
    assert len(read_rows(engine, "seller_order_sync_jobs")) == 3


def test_migrated_cache_matches_repository_upsert_key(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260907_0007")
    saved_account = read_rows(engine, "seller_marketplace_accounts")[0]
    job = {
        "seller_id": UUID(saved_account["seller_id"]),
        "organization_id": UUID(saved_account["organization_id"]),
        "account_id": UUID(saved_account["id"]),
        "environment": "live", "marketplace": "worten",
    }
    repository = SqlOrdersRepository(engine)
    repository.upsert_batch(job, [
        {"external_line_id": "1", "order_id": "W1", "product_name": "Primo"},
        {"external_line_id": "1", "order_id": "W2", "product_name": "Secondo"},
    ])
    repository.upsert_batch(job, [
        {"external_line_id": "1", "order_id": "W1", "product_name": "Primo aggiornato"},
    ])
    rows = read_rows(engine, "seller_order_lines")
    assert len(rows) == 2
    assert {row["order_id"] for row in rows} == {"W1", "W2"}
