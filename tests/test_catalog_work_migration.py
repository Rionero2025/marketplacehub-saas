import sqlalchemy as sa
import test_tenancy_migration
from alembic import command
from test_seller_settings_migration import read_rows, seed_settings

legacy_database = test_tenancy_migration.legacy_database


def test_work_view_migration_preserves_existing_data(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0014")
    tables = set(sa.inspect(engine).get_table_names()) - {"alembic_version"}
    before = {name: read_rows(engine, name) for name in tables}
    command.upgrade(config, "20260911_0015")
    new = {"seller_catalog_views", "seller_catalog_view_rows"}
    assert new <= set(sa.inspect(engine).get_table_names())
    assert {name: read_rows(engine, name) for name in tables} == before
    command.downgrade(config, "20260911_0014")
    assert new.isdisjoint(sa.inspect(engine).get_table_names())
    assert {name: read_rows(engine, name) for name in tables} == before
