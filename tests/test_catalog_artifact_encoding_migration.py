from __future__ import annotations

import ast
import gzip
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
    ROOT / "migrations" / "versions" / "20260911_0013_catalog_artifact_encoding.py"
)
spec = importlib.util.spec_from_file_location("catalog_artifact_encoding_migration", MIGRATION_PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_artifact_encoding_migration_has_no_application_runtime_imports():
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


def test_postgresql_rolling_bridge_copies_storage_metadata():
    class CaptureConnection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self):
            self.statements: list[str] = []

        def execute(self, statement):
            self.statements.append(" ".join(str(statement).split()))

    connection = CaptureConnection()
    migration._replace_upload_version_bridge(connection, include_storage=True)
    ddl = "\n".join(connection.statements)
    assert "CREATE OR REPLACE FUNCTION mh_seed_upload_price_list_version()" in ddl
    assert "artifact_encoding, artifact_stored_size" in ddl
    assert "COALESCE(NEW.artifact_encoding, 'identity')" in ddl
    assert "COALESCE(NEW.artifact_stored_size, NEW.artifact_size)" in ddl


def test_migration_preserves_legacy_identity_bridge_and_supports_previous_upload_writer(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0012")
    (
        _suppliers,
        previous_price_lists,
        _products,
        supplier_id,
        existing_list_id,
        _product_id,
    ) = test_catalogs_migration._insert_catalog(engine)

    command.upgrade(config, "20260911_0013")
    existing = next(
        row for row in read_rows(engine, "seller_price_lists")
        if str(row["id"]) == str(existing_list_id)
    )
    existing_version = next(
        row for row in read_rows(engine, "seller_price_list_versions")
        if str(row["price_list_id"]) == str(existing_list_id)
    )
    assert (existing["artifact_encoding"], existing["artifact_stored_size"]) == (None, None)
    assert (
        existing_version["artifact_encoding"], existing_version["artifact_stored_size"],
    ) == (None, None)

    organization_id, seller_id = test_catalogs_migration._catalog_scope(engine)
    list_id = uuid4().hex
    payload = b"ean,sku,cost\n8050000000099,ROLLING,12.34\n"
    now = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
    with engine.begin() as connection:
        # This Table was reflected before 0013, like a process from the prior
        # release that remains alive briefly during a rolling deployment.
        connection.execute(previous_price_lists.insert().values(
            id=list_id,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name="Upload dal processo precedente",
            provider="generic",
            feed_role="standard",
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
        row for row in read_rows(engine, "seller_price_lists") if str(row["id"]) == list_id
    )
    rolling_version = next(
        row for row in read_rows(engine, "seller_price_list_versions")
        if str(row["price_list_id"]) == list_id
    )
    assert rolling["artifact_encoding"] is None
    assert rolling["artifact_stored_size"] is None
    assert (
        rolling_version["artifact_encoding"], rolling_version["artifact_stored_size"],
    ) == ("identity", len(payload))


def test_migration_supports_previous_remote_version_writer(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0012")
    _, previous_price_lists, _, _supplier_id, list_id, _ = (
        test_catalogs_migration._insert_catalog(engine)
    )
    previous_versions = sa.Table(
        "seller_price_list_versions", sa.MetaData(), autoload_with=engine,
    )
    organization_id, seller_id = test_catalogs_migration._catalog_scope(engine)
    now = datetime(2026, 9, 11, 14, 30, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(previous_price_lists.update().where(
            previous_price_lists.c.id == list_id,
        ).values(
            source_type="url",
            source_config_encrypted="opaque",
            source_host="feed.example.test",
            updated_at=now,
        ))

    command.upgrade(config, "20260911_0013")
    previous = next(
        row for row in read_rows(engine, "seller_price_lists") if str(row["id"]) == str(list_id)
    )
    assert previous["active_version_number"] == 1
    assert (previous["artifact_encoding"], previous["artifact_stored_size"]) == (None, None)

    # A worker from the previous release can finish a refresh after the schema
    # migration. It cannot write the new storage columns, and the replacement
    # artifact deliberately has a different byte length from active version 1.
    payload = b"ean,sku,cost\n8050000000098,REMOTE-NEW-WITH-LONGER-SKU,18.00\n"
    assert len(payload) != previous["artifact_size"]
    with engine.begin() as connection:
        connection.execute(previous_versions.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            price_list_id=list_id,
            version_number=2,
            provider="generic",
            feed_role="standard",
            original_filename="remote.csv",
            media_type="text/csv",
            file_format="csv",
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_size=len(payload),
            artifact_bytes=payload,
            product_count=1,
            created_at=now,
        ))
        connection.execute(previous_price_lists.update().where(
            previous_price_lists.c.id == list_id,
        ).values(
            active_version_number=2,
            last_checked_at=now,
            last_success_at=now,
            original_filename="remote.csv",
            media_type="text/csv",
            file_format="csv",
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_size=len(payload),
            artifact_bytes=payload,
            product_count=1,
            updated_at=now,
        ))

    saved = next(
        row for row in read_rows(engine, "seller_price_lists") if str(row["id"]) == list_id
    )
    version = next(
        row for row in read_rows(engine, "seller_price_list_versions")
        if str(row["price_list_id"]) == str(list_id) and row["version_number"] == 2
    )
    assert (saved["artifact_encoding"], saved["artifact_stored_size"]) == (None, None)
    assert (version["artifact_encoding"], version["artifact_stored_size"]) == (None, None)


def _insert_compressed_iof(engine) -> str:
    _, _, _, supplier_id, _, _ = test_catalogs_migration._insert_catalog(engine)
    organization_id, seller_id = test_catalogs_migration._catalog_scope(engine)
    price_lists = sa.Table("seller_price_lists", sa.MetaData(), autoload_with=engine)
    raw = b"x" * (21 * 1024 * 1024)
    stored = gzip.compress(raw, mtime=0)
    list_id = uuid4().hex
    now = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(price_lists.insert().values(
            id=list_id,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name="InnPro FULL",
            provider="innpro",
            feed_role="full",
            source_type="upload",
            source_config_encrypted=None,
            source_host="",
            source_config_revision=1,
            active_version_number=1,
            last_checked_at=now,
            last_success_at=now,
            original_filename="stock-full.xml",
            media_type="application/xml",
            file_format="iof",
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
            artifact_size=len(raw),
            artifact_encoding="gzip",
            artifact_stored_size=len(stored),
            artifact_bytes=stored,
            product_count=1,
            created_at=now,
            updated_at=now,
        ))
    return list_id


def test_migration_accepts_bounded_compressed_iof_and_bridge_copies_it(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0013")
    list_id = _insert_compressed_iof(engine)

    saved = next(
        row for row in read_rows(engine, "seller_price_lists") if str(row["id"]) == list_id
    )
    version = next(
        row for row in read_rows(engine, "seller_price_list_versions")
        if str(row["price_list_id"]) == list_id
    )
    assert saved["artifact_size"] > migration.LEGACY_MAX_ARTIFACT_BYTES
    assert saved["artifact_stored_size"] == len(saved["artifact_bytes"])
    assert saved["artifact_stored_size"] < saved["artifact_size"]
    assert saved["file_format"] == "iof"
    assert version["artifact_encoding"] == "gzip"
    assert version["artifact_stored_size"] == saved["artifact_stored_size"]


def test_storage_constraints_reject_incoherent_identity_metadata(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0013")
    _, _, _, supplier_id, _, _ = test_catalogs_migration._insert_catalog(engine)
    organization_id, seller_id = test_catalogs_migration._catalog_scope(engine)
    price_lists = sa.Table("seller_price_lists", sa.MetaData(), autoload_with=engine)
    payload = b"short"
    now = datetime(2026, 9, 11, 16, 0, tzinfo=UTC)

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(price_lists.insert().values(
            id=uuid4().hex,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name="Metadati incoerenti",
            provider="generic",
            feed_role="standard",
            source_type="upload",
            source_config_encrypted=None,
            source_host="",
            source_config_revision=1,
            active_version_number=1,
            last_checked_at=now,
            last_success_at=now,
            original_filename="bad.csv",
            media_type="text/csv",
            file_format="csv",
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_size=len(payload) + 1,
            artifact_encoding="identity",
            artifact_stored_size=len(payload),
            artifact_bytes=payload,
            product_count=1,
            created_at=now,
            updated_at=now,
        ))


def test_downgrade_restores_0012_for_identity_artifacts(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0012")
    test_catalogs_migration._insert_catalog(engine)
    command.upgrade(config, "20260911_0013")
    command.downgrade(config, "20260911_0012")

    inspector = sa.inspect(engine)
    assert "artifact_encoding" not in {
        item["name"] for item in inspector.get_columns("seller_price_lists")
    }
    assert "artifact_stored_size" not in {
        item["name"] for item in inspector.get_columns("seller_price_list_versions")
    }
    with engine.connect() as connection:
        trigger = connection.scalar(sa.text(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_seller_price_lists_seed_upload_version'"
        ))
    assert trigger is not None


def test_downgrade_refuses_lossy_compressed_artifacts(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260911_0013")
    _insert_compressed_iof(engine)

    with pytest.raises(RuntimeError, match="compressed, oversized, or IOF"):
        command.downgrade(config, "20260911_0012")
