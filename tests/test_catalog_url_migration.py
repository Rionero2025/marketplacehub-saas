from __future__ import annotations

import ast
import hashlib
import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa
import test_catalogs_migration
import test_tenancy_migration
from alembic import command
from test_seller_settings_migration import read_rows, seed_settings

legacy_database = test_tenancy_migration.legacy_database
ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "migrations" / "versions" / "20260910_0011_catalog_url_versions.py"
)
spec = importlib.util.spec_from_file_location("catalog_url_migration", MIGRATION_PATH)
catalog_url_migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog_url_migration)


def test_catalog_url_migration_has_no_application_runtime_imports():
    tree = ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))
    modules = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ]
    modules.extend(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(module.startswith("marketplace_hub") for module in modules)


def test_catalog_url_migration_versions_existing_upload_and_roundtrips(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0010")
    payload = b"ean;sku;cost\n8050000000001;SKU-1;19,90\n"
    test_catalogs_migration._insert_catalog(engine, payload=payload)
    before_supplier = read_rows(engine, "seller_suppliers")
    before_list = read_rows(engine, "seller_price_lists")
    before_products = read_rows(engine, "seller_price_list_products")

    command.upgrade(config, "20260910_0011")
    inspector = sa.inspect(engine)
    assert {
        "seller_price_list_versions", "seller_price_list_refresh_jobs",
    } <= set(inspector.get_table_names())
    columns = {column["name"]: column for column in inspector.get_columns(
        "seller_price_lists"
    )}
    assert columns["source_config_encrypted"]["nullable"] is True
    assert columns["artifact_bytes"]["nullable"] is True
    assert columns["product_count"]["nullable"] is False
    assert str(columns["active_version_number"]["default"]).strip("()'\"") == "1"

    saved_list = read_rows(engine, "seller_price_lists")[0]
    version = read_rows(engine, "seller_price_list_versions")[0]
    product = read_rows(engine, "seller_price_list_products")[0]
    assert saved_list["source_type"] == "upload"
    assert saved_list["source_config_encrypted"] is None
    assert saved_list["source_host"] == ""
    assert saved_list["source_config_revision"] == 1
    assert saved_list["active_version_number"] == 1
    assert saved_list["artifact_bytes"] == payload
    assert version["price_list_id"] == saved_list["id"]
    assert version["version_number"] == 1
    assert version["artifact_bytes"] == payload
    assert product["version_number"] == 1

    command.upgrade(config, "20260910_0011")
    command.downgrade(config, "20260910_0010")
    assert read_rows(engine, "seller_suppliers") == before_supplier
    assert read_rows(engine, "seller_price_lists") == before_list
    assert read_rows(engine, "seller_price_list_products") == before_products
    assert not any(
        foreign_key["referred_table"] == "seller_price_list_versions"
        for foreign_key in sa.inspect(engine).get_foreign_keys(
            "seller_price_list_products"
        )
    )
    with engine.connect() as connection:
        assert connection.scalar(sa.text(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_seller_price_lists_seed_upload_version'"
        )) == 0


def test_initial_version_backfill_copies_artifacts_inside_the_database(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0010")
    payload = b"ean;sku;cost\n8050000000001;SKU-1;19,90\n"
    test_catalogs_migration._insert_catalog(engine, payload=payload)
    statements: list[str] = []

    def capture_insert(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("insert into seller_price_list_versions"):
            statements.append(normalized)

    sa.event.listen(engine, "before_cursor_execute", capture_insert)
    try:
        with engine.begin() as connection:
            catalog_url_migration._create_versions(connection)
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture_insert)

    assert len(statements) == 1
    assert " select " in statements[0]
    assert " from seller_price_lists" in statements[0]
    version = read_rows(engine, "seller_price_list_versions")[0]
    assert version["artifact_bytes"] == payload


def test_previous_release_upload_write_remains_valid_after_upgrade(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0010")
    (
        _suppliers,
        legacy_price_lists,
        legacy_products,
        supplier_id,
        _existing_list_id,
        _existing_product_id,
    ) = test_catalogs_migration._insert_catalog(engine)
    organization_id, seller_id = test_catalogs_migration._catalog_scope(engine)

    command.upgrade(config, "20260910_0011")
    price_list_id = uuid4().hex
    product_id = uuid4().hex
    payload = b"ean,sku,cost\n8050000000099,LEGACY-99,12.34\n"
    now = datetime(2026, 9, 10, 12, 30, tzinfo=UTC)
    with engine.begin() as connection:
        # These Table objects were reflected before 0011, exactly like the
        # previous web process that remains live while Render runs preDeploy.
        connection.execute(legacy_price_lists.insert().values(
            id=price_list_id,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name="Listino dal processo precedente",
            original_filename="legacy.csv",
            media_type="text/csv",
            file_format="csv",
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_size=len(payload),
            artifact_bytes=payload,
            product_count=1,
            created_at=now,
            updated_at=now,
        ))
        connection.execute(legacy_products.insert().values(
            id=product_id,
            organization_id=organization_id,
            seller_id=seller_id,
            price_list_id=price_list_id,
            source_row=2,
            ean="8050000000099",
            sku="LEGACY-99",
            name="Prodotto legacy",
            cost=Decimal("12.34000000"),
            shipping_cost=Decimal("0"),
            total_cost=Decimal("12.34000000"),
            quantity=Decimal("1"),
            canonical_json='{"ean":"8050000000099","sku":"LEGACY-99"}',
            created_at=now,
        ))

    saved_list = next(
        row for row in read_rows(engine, "seller_price_lists")
        if str(row["id"]) == price_list_id
    )
    saved_version = next(
        row for row in read_rows(engine, "seller_price_list_versions")
        if str(row["price_list_id"]) == price_list_id
    )
    saved_product = next(
        row for row in read_rows(engine, "seller_price_list_products")
        if str(row["id"]) == product_id
    )
    assert saved_list["source_type"] == "upload"
    assert saved_list["active_version_number"] == 1
    assert str(saved_version["id"]) == price_list_id
    assert saved_version["artifact_bytes"] == payload
    assert saved_product["version_number"] == 1


def test_postgresql_upload_bridge_ddl_is_created_and_reversible():
    class CaptureConnection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self):
            self.statements: list[str] = []

        def execute(self, statement):
            self.statements.append(" ".join(str(statement).split()))

    connection = CaptureConnection()
    catalog_url_migration._create_upload_version_bridge(connection)
    catalog_url_migration._drop_upload_version_bridge(connection)

    ddl = "\n".join(connection.statements)
    assert "CREATE FUNCTION mh_seed_upload_price_list_version()" in ddl
    assert "AFTER INSERT ON seller_price_lists" in ddl
    assert "ON CONFLICT (price_list_id, version_number) DO NOTHING" in ddl
    assert "DROP TRIGGER IF EXISTS trg_seller_price_lists_seed_upload_version" in ddl
    assert "DROP FUNCTION IF EXISTS mh_seed_upload_price_list_version()" in ddl


def test_catalog_url_migration_creates_scoped_cascades_and_one_active_job_index(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0011")
    # Alembic performs SQLite batch DDL through a separate connection. Inspect
    # the durable schema through a fresh engine so SQLAlchemy cannot reuse its
    # pre-migration PRAGMA cache.
    inspection_engine = sa.create_engine(str(engine.url))
    inspector = sa.inspect(inspection_engine)
    version_foreign_keys = inspector.get_foreign_keys("seller_price_list_versions")
    product_foreign_keys = inspector.get_foreign_keys("seller_price_list_products")
    job_foreign_keys = inspector.get_foreign_keys("seller_price_list_refresh_jobs")
    assert any(
        foreign_key["constrained_columns"] == [
            "price_list_id", "organization_id", "seller_id",
        ]
        and foreign_key["referred_table"] == "seller_price_lists"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in version_foreign_keys
    )
    assert any(
        foreign_key["constrained_columns"] == ["price_list_id", "version_number"]
        and foreign_key["referred_table"] == "seller_price_list_versions"
        and foreign_key["referred_columns"] == ["price_list_id", "version_number"]
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in product_foreign_keys
    )
    assert any(
        foreign_key["constrained_columns"] == [
            "price_list_id", "organization_id", "seller_id",
        ]
        and foreign_key["referred_table"] == "seller_price_lists"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in job_foreign_keys
    )
    with inspection_engine.connect() as connection:
        indexes = {
            row[1]: row[4]
            for row in connection.exec_driver_sql(
                "PRAGMA index_list('seller_price_list_refresh_jobs')"
            )
        }
        definition = connection.scalar(sa.text(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='uq_seller_price_list_active_refresh'"
        ))
    assert indexes["uq_seller_price_list_active_refresh"] == 1
    assert "WHERE status IN ('queued', 'running')" in definition
    inspection_engine.dispose()
