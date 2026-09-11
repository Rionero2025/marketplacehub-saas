import sqlalchemy as sa
import test_catalogs_migration
import test_tenancy_migration
from alembic import command
from test_seller_settings_migration import read_rows, seed_settings

legacy_database = test_tenancy_migration.legacy_database


def test_measurements_upgrade_and_downgrade_preserve_existing_catalog(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0013")
    test_catalogs_migration._insert_catalog(engine)
    before = read_rows(engine, "seller_price_list_products")
    artifacts = read_rows(engine, "seller_price_lists")
    command.upgrade(config, "20260911_0014")
    fields = {"weight_kg", "length_cm", "width_cm", "height_cm"}
    columns = {c["name"] for c in sa.inspect(engine).get_columns("seller_price_list_products")}
    assert fields <= columns
    after = read_rows(engine, "seller_price_list_products")
    assert [{k: v for k, v in row.items() if k not in fields} for row in after] == before
    assert all(row[field] is None for row in after for field in fields)
    assert read_rows(engine, "seller_price_lists") == artifacts
    command.downgrade(config, "20260911_0013")
    assert read_rows(engine, "seller_price_list_products") == before
    assert read_rows(engine, "seller_price_lists") == artifacts
