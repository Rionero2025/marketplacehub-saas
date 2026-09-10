from __future__ import annotations

import ast
import hashlib
import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
import test_tenancy_migration
from alembic import command
from sqlalchemy.dialects import postgresql
from test_seller_settings_migration import read_rows, seed_settings

legacy_database = test_tenancy_migration.legacy_database
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260910_0010_catalogs.py"
)
spec = importlib.util.spec_from_file_location("catalogs_migration", MIGRATION_PATH)
catalogs_migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalogs_migration)

CATALOG_TABLE_NAMES = {
    "seller_suppliers",
    "seller_price_lists",
    "seller_price_list_products",
}


def _catalog_scope(engine):
    profile = read_rows(engine, "seller_profiles")[0]
    return profile["organization_id"], profile["id"]


def _insert_catalog(engine, *, payload: bytes = b"ean,sku,cost\n123,ABC,10.00\n"):
    organization_id, seller_id = _catalog_scope(engine)
    metadata = sa.MetaData()
    suppliers = sa.Table("seller_suppliers", metadata, autoload_with=engine)
    price_lists = sa.Table("seller_price_lists", metadata, autoload_with=engine)
    products = sa.Table("seller_price_list_products", metadata, autoload_with=engine)
    supplier_id = uuid4().hex
    price_list_id = uuid4().hex
    product_id = uuid4().hex
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(suppliers.insert().values(
            id=supplier_id,
            organization_id=organization_id,
            seller_id=seller_id,
            name="Fornitore prova",
            notes="Listino di collaudo",
            created_at=now,
            updated_at=now,
        ))
        connection.execute(price_lists.insert().values(
            id=price_list_id,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name="Listino settembre",
            original_filename="catalogo.csv",
            media_type="text/csv",
            file_format="csv",
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_size=len(payload),
            artifact_bytes=payload,
            product_count=1,
            created_at=now,
            updated_at=now,
        ))
        connection.execute(products.insert().values(
            id=product_id,
            organization_id=organization_id,
            seller_id=seller_id,
            price_list_id=price_list_id,
            source_row=2,
            ean="123",
            sku="ABC",
            name="Prodotto prova",
            cost=Decimal("10.00000000"),
            shipping_cost=Decimal("2.00000000"),
            total_cost=Decimal("12.00000000"),
            quantity=Decimal("4.00000000"),
            canonical_json='{"ean":"123","sku":"ABC"}',
            created_at=now,
        ))
    return suppliers, price_lists, products, supplier_id, price_list_id, product_id


def test_catalog_migration_has_no_application_runtime_imports():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
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


def test_catalog_migration_adds_empty_tables_and_roundtrips_without_source_mutation(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0009")
    existing = sa.inspect(engine).get_table_names()
    snapshots = {
        name: read_rows(engine, name)
        for name in existing
        if name != "alembic_version"
    }

    command.upgrade(config, "20260910_0010")
    assert CATALOG_TABLE_NAMES <= set(sa.inspect(engine).get_table_names())
    assert all(read_rows(engine, name) == [] for name in CATALOG_TABLE_NAMES)
    assert snapshots == {name: read_rows(engine, name) for name in snapshots}

    command.upgrade(config, "20260910_0010")
    command.downgrade(config, "20260910_0009")
    assert CATALOG_TABLE_NAMES.isdisjoint(sa.inspect(engine).get_table_names())
    assert snapshots == {name: read_rows(engine, name) for name in snapshots}


def test_catalog_migration_persists_artifact_and_normalized_numeric_rows(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0010")
    payload = b"ean;sku;cost\r\n8050000000001;SKU-1;19,90\r\n"
    _insert_catalog(engine, payload=payload)

    saved_list = read_rows(engine, "seller_price_lists")[0]
    saved_product = read_rows(engine, "seller_price_list_products")[0]
    assert saved_list["artifact_bytes"] == payload
    assert saved_list["artifact_size"] == len(payload)
    assert saved_list["artifact_sha256"] == hashlib.sha256(payload).hexdigest()
    assert saved_list["product_count"] == 1
    assert saved_product["source_row"] == 2
    assert saved_product["cost"] == Decimal("10.00000000")
    assert saved_product["shipping_cost"] == Decimal("2.00000000")
    assert saved_product["total_cost"] == Decimal("12.00000000")
    assert saved_product["quantity"] == Decimal("4.00000000")


def test_catalog_migration_enforces_names_artifact_shape_and_source_row_identity(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0010")
    suppliers, price_lists, products, supplier_id, price_list_id, _ = _insert_catalog(engine)
    organization_id, seller_id = _catalog_scope(engine)
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(suppliers.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            name="Fornitore prova",
            notes="duplicato",
            created_at=now,
            updated_at=now,
        ))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(suppliers.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            name="   ",
            notes="",
            created_at=now,
            updated_at=now,
        ))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(price_lists.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name="Artefatto incoerente",
            original_filename="catalogo.csv",
            media_type="text/csv",
            file_format="csv",
            artifact_sha256="0" * 64,
            artifact_size=999,
            artifact_bytes=b"x",
            product_count=0,
            created_at=now,
            updated_at=now,
        ))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(price_lists.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name="Hash non esadecimale",
            original_filename="catalogo.csv",
            media_type="text/csv",
            file_format="csv",
            artifact_sha256="z" * 64,
            artifact_size=1,
            artifact_bytes=b"x",
            product_count=1,
            created_at=now,
            updated_at=now,
        ))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(products.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            price_list_id=price_list_id,
            source_row=2,
            ean="different",
            sku="different",
            name="Riga duplicata",
            canonical_json="{}",
            created_at=now,
        ))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(products.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            price_list_id=price_list_id,
            source_row=0,
            canonical_json="{}",
            created_at=now,
        ))


def test_catalog_foreign_keys_cascade_supplier_list_artifact_and_product_rows(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0010")
    suppliers, _, _, supplier_id, _, _ = _insert_catalog(engine)

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    with engine.begin() as connection:
        connection.execute(suppliers.delete().where(suppliers.c.id == supplier_id))
    assert read_rows(engine, "seller_price_list_products") == []
    assert read_rows(engine, "seller_price_lists") == []
    assert read_rows(engine, "seller_suppliers") == []


def test_catalog_schema_exposes_scoped_indexes_and_delete_contracts_on_sqlite(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0010")
    inspector = sa.inspect(engine)

    # SQLite's Inspector can retain an empty index cache while Alembic reuses
    # the same engine across non-transactional DDL. Query the durable catalog
    # directly, then use Inspector for the foreign-key contract below.
    with engine.connect() as connection:
        index_names = connection.scalars(sa.text(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'seller_price_list_products' "
            "AND name LIKE 'ix_%'"
        )).all()
        product_indexes = {
            name: [row[2] for row in connection.exec_driver_sql(
                f'PRAGMA index_info("{name}")'
            )]
            for name in index_names
        }
    assert product_indexes == {
        "ix_seller_price_list_products_scope_list_ean": [
            "organization_id", "seller_id", "price_list_id", "ean",
        ],
        "ix_seller_price_list_products_scope_list_sku": [
            "organization_id", "seller_id", "price_list_id", "sku",
        ],
    }
    list_foreign_keys = inspector.get_foreign_keys("seller_price_lists")
    assert any(
        foreign_key["constrained_columns"] == ["supplier_id", "organization_id", "seller_id"]
        and foreign_key["referred_table"] == "seller_suppliers"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in list_foreign_keys
    )
    product_foreign_keys = inspector.get_foreign_keys("seller_price_list_products")
    assert any(
        foreign_key["constrained_columns"] == [
            "price_list_id", "organization_id", "seller_id",
        ]
        and foreign_key["referred_table"] == "seller_price_lists"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in product_foreign_keys
    )


def test_catalog_postgresql_ddl_keeps_binary_artifact_and_named_constraints():
    price_list_ddl = str(sa.schema.CreateTable(
        catalogs_migration.seller_price_lists,
    ).compile(dialect=postgresql.dialect()))
    product_ddl = str(sa.schema.CreateTable(
        catalogs_migration.seller_price_list_products,
    ).compile(dialect=postgresql.dialect()))

    assert "artifact_bytes BYTEA NOT NULL" in price_list_ddl
    assert "artifact_size INTEGER NOT NULL" in price_list_ddl
    assert "CONSTRAINT ck_seller_price_list_artifact_size CHECK" in price_list_ddl
    assert "CONSTRAINT uq_seller_price_list_scope_name UNIQUE" in price_list_ddl
    assert "FOREIGN KEY(supplier_id, organization_id, seller_id)" in price_list_ddl
    assert "ON DELETE CASCADE" in price_list_ddl
    assert "NUMERIC(38, 8)" in product_ddl
    assert "CONSTRAINT uq_seller_price_list_product_source_row UNIQUE" in product_ddl
    assert "FOREIGN KEY(price_list_id, organization_id, seller_id)" in product_ddl
    assert "ON DELETE CASCADE" in product_ddl

    index_columns = {
        index.name: tuple(column.name for column in index.columns)
        for index in catalogs_migration.seller_price_list_products.indexes
    }
    assert index_columns == {
        "ix_seller_price_list_products_scope_list_ean": (
            "organization_id", "seller_id", "price_list_id", "ean",
        ),
        "ix_seller_price_list_products_scope_list_sku": (
            "organization_id", "seller_id", "price_list_id", "sku",
        ),
    }
