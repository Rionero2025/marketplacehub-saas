from __future__ import annotations

import ast
import hashlib
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
import test_catalogs_migration
import test_tenancy_migration
from alembic import command
from test_seller_settings_migration import read_rows, seed_settings

legacy_database = test_tenancy_migration.legacy_database
ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "migrations" / "versions" / "20260911_0012_catalog_feed_identity.py"
)
spec = importlib.util.spec_from_file_location("catalog_feed_identity_migration", MIGRATION_PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_feed_identity_migration_has_no_application_runtime_imports():
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


def test_postgresql_rolling_bridge_copies_feed_identity():
    class CaptureConnection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self):
            self.statements: list[str] = []

        def execute(self, statement):
            self.statements.append(" ".join(str(statement).split()))

    connection = CaptureConnection()
    migration._replace_upload_version_bridge(connection, include_identity=True)
    ddl = "\n".join(connection.statements)
    assert "CREATE OR REPLACE FUNCTION mh_seed_upload_price_list_version()" in ddl
    assert "provider, feed_role" in ddl
    assert "NEW.provider, NEW.feed_role" in ddl


def test_migration_backfills_existing_rows_and_keeps_old_upload_writer_compatible(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260910_0011")
    (
        _suppliers,
        legacy_price_lists,
        _products,
        supplier_id,
        existing_list_id,
        _product_id,
    ) = test_catalogs_migration._insert_catalog(engine)

    command.upgrade(config, "20260911_0012")
    existing = next(
        row for row in read_rows(engine, "seller_price_lists")
        if str(row["id"]) == str(existing_list_id)
    )
    existing_version = next(
        row for row in read_rows(engine, "seller_price_list_versions")
        if str(row["price_list_id"]) == str(existing_list_id)
    )
    assert (existing["provider"], existing["feed_role"]) == ("generic", "standard")
    assert (existing_version["provider"], existing_version["feed_role"]) == (
        "generic", "standard"
    )

    organization_id, seller_id = test_catalogs_migration._catalog_scope(engine)
    price_list_id = uuid4().hex
    payload = b"ean,sku,cost\n8050000000099,ROLLING,12.34\n"
    now = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
    with engine.begin() as connection:
        # Reflected before 0012, like the previous process during a rolling deploy.
        connection.execute(legacy_price_lists.insert().values(
            id=price_list_id,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name="Upload dal processo precedente",
            source_type="upload",
            source_config_encrypted=None,
            source_host="",
            source_config_revision=1,
            active_version_number=1,
            last_checked_at=now,
            last_success_at=now,
            original_filename="rolling.csv",
            media_type="text/csv",
            file_format="csv",
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_size=len(payload),
            artifact_bytes=payload,
            product_count=1,
            created_at=now,
            updated_at=now,
        ))

    rolling = next(
        row for row in read_rows(engine, "seller_price_lists")
        if str(row["id"]) == price_list_id
    )
    rolling_version = next(
        row for row in read_rows(engine, "seller_price_list_versions")
        if str(row["price_list_id"]) == price_list_id
    )
    assert (rolling["provider"], rolling["feed_role"]) == ("generic", "standard")
    assert (rolling_version["provider"], rolling_version["feed_role"]) == (
        "generic", "standard"
    )


@pytest.mark.parametrize(
    ("provider", "feed_role"),
    [
        ("generic", "full"),
        ("generic", "light"),
        ("innpro", "standard"),
        ("unknown", "standard"),
    ],
)
def test_migration_database_constraints_reject_incoherent_list_identity(
    legacy_database, provider, feed_role,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0012")
    _, _, _, supplier_id, _, _ = test_catalogs_migration._insert_catalog(engine)
    organization_id, seller_id = test_catalogs_migration._catalog_scope(engine)
    price_lists = sa.Table("seller_price_lists", sa.MetaData(), autoload_with=engine)
    now = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(price_lists.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name=f"Incoerente {provider} {feed_role}",
            provider=provider,
            feed_role=feed_role,
            source_type="url",
            source_config_encrypted="opaque",
            source_host="feed.example.test",
            source_config_revision=1,
            active_version_number=0,
            product_count=0,
            created_at=now,
            updated_at=now,
        ))


def test_migration_version_constraint_rejects_incoherent_identity(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0012")
    _, _, _, _, price_list_id, _ = test_catalogs_migration._insert_catalog(engine)
    organization_id, seller_id = test_catalogs_migration._catalog_scope(engine)
    versions = sa.Table("seller_price_list_versions", sa.MetaData(), autoload_with=engine)
    payload = b"ean,sku,cost\n8050000000077,V2,7\n"
    now = datetime(2026, 9, 11, 11, 0, tzinfo=UTC)

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(versions.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            price_list_id=price_list_id,
            version_number=2,
            provider="innpro",
            feed_role="standard",
            original_filename="invalid.xml",
            media_type="application/xml",
            file_format="xml",
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_size=len(payload),
            artifact_bytes=payload,
            product_count=1,
            created_at=now,
        ))


def test_migration_downgrade_restores_0011_columns_and_upload_bridge(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0012")
    command.downgrade(config, "20260910_0011")

    inspector = sa.inspect(engine)
    assert "provider" not in {
        item["name"] for item in inspector.get_columns("seller_price_lists")
    }
    assert "feed_role" not in {
        item["name"] for item in inspector.get_columns("seller_price_list_versions")
    }
    with engine.connect() as connection:
        trigger = connection.scalar(sa.text(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_seller_price_lists_seed_upload_version'"
        ))
    assert trigger is not None
