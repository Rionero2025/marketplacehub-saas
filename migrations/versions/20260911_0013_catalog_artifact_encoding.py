"""Store large catalog sources with bounded artifact encoding metadata."""

import sqlalchemy as sa

revision = "20260911_0013"
down_revision = "20260911_0012"
branch_labels = None
depends_on = None

LEGACY_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_RAW_ARTIFACT_BYTES = 200 * 1024 * 1024
MAX_STORED_ARTIFACT_BYTES = 64 * 1024 * 1024
LEGACY_FORMATS = "'csv', 'txt', 'tsv', 'xls', 'xlsx', 'xml'"
FORMATS = f"{LEGACY_FORMATS}, 'iof'"


def _replace_upload_version_bridge(connection, *, include_storage: bool) -> None:
    storage_columns = ", artifact_encoding, artifact_stored_size" if include_storage else ""
    storage_values = (
        ", COALESCE(NEW.artifact_encoding, 'identity'), "
        "COALESCE(NEW.artifact_stored_size, NEW.artifact_size)"
        if include_storage else ""
    )
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text(f"""
            CREATE OR REPLACE FUNCTION mh_seed_upload_price_list_version()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                INSERT INTO seller_price_list_versions (
                    id, organization_id, seller_id, price_list_id, version_number,
                    provider, feed_role, original_filename, media_type, file_format,
                    artifact_sha256, artifact_size{storage_columns}, artifact_bytes,
                    product_count, created_at
                ) VALUES (
                    NEW.id, NEW.organization_id, NEW.seller_id, NEW.id, 1,
                    NEW.provider, NEW.feed_role, NEW.original_filename, NEW.media_type,
                    NEW.file_format, NEW.artifact_sha256, NEW.artifact_size
                    {storage_values}, NEW.artifact_bytes, NEW.product_count, NEW.created_at
                )
                ON CONFLICT (price_list_id, version_number) DO NOTHING;
                RETURN NEW;
            END;
            $$
        """))
        return
    if connection.dialect.name == "sqlite":
        connection.execute(sa.text(
            "DROP TRIGGER IF EXISTS trg_seller_price_lists_seed_upload_version"
        ))
        connection.execute(sa.text(f"""
            CREATE TRIGGER trg_seller_price_lists_seed_upload_version
            AFTER INSERT ON seller_price_lists
            FOR EACH ROW
            WHEN NEW.source_type = 'upload' AND NEW.active_version_number = 1
            BEGIN
                INSERT OR IGNORE INTO seller_price_list_versions (
                    id, organization_id, seller_id, price_list_id, version_number,
                    provider, feed_role, original_filename, media_type, file_format,
                    artifact_sha256, artifact_size{storage_columns}, artifact_bytes,
                    product_count, created_at
                ) VALUES (
                    NEW.id, NEW.organization_id, NEW.seller_id, NEW.id, 1,
                    NEW.provider, NEW.feed_role, NEW.original_filename, NEW.media_type,
                    NEW.file_format, NEW.artifact_sha256, NEW.artifact_size
                    {storage_values}, NEW.artifact_bytes, NEW.product_count, NEW.created_at
                );
            END
        """))
        return
    raise RuntimeError(
        f"Unsupported database for catalog upload bridge: {connection.dialect.name}"
    )


def _drop_sqlite_bridge(connection) -> None:
    if connection.dialect.name == "sqlite":
        connection.execute(sa.text(
            "DROP TRIGGER IF EXISTS trg_seller_price_lists_seed_upload_version"
        ))


def _add_list_storage_constraints(batch) -> None:
    batch.create_check_constraint(
        "ck_seller_price_list_format",
        f"file_format IS NULL OR file_format IN ({FORMATS})",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_artifact_size",
        "artifact_size IS NULL OR "
        f"(artifact_size > 0 AND artifact_size <= {MAX_RAW_ARTIFACT_BYTES})",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_artifact_stored_size",
        "artifact_stored_size IS NULL OR "
        f"(artifact_stored_size > 0 AND artifact_stored_size <= {MAX_STORED_ARTIFACT_BYTES})",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_artifact_encoding",
        "artifact_encoding IS NULL OR artifact_encoding IN ('identity', 'gzip')",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_artifact_length",
        "artifact_bytes IS NULL OR "
        "length(artifact_bytes) = coalesce(artifact_stored_size, artifact_size)",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_artifact_storage",
        "(artifact_encoding IS NULL AND artifact_stored_size IS NULL) OR "
        "(artifact_encoding IS NOT NULL AND artifact_stored_size IS NOT NULL AND "
        "((artifact_encoding = 'identity' AND artifact_stored_size = artifact_size) OR "
        "artifact_encoding = 'gzip'))",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_artifact_group",
        "(artifact_bytes IS NULL AND artifact_sha256 IS NULL AND artifact_size IS NULL "
        "AND artifact_encoding IS NULL AND artifact_stored_size IS NULL "
        "AND original_filename IS NULL AND media_type IS NULL AND file_format IS NULL "
        "AND product_count = 0 AND active_version_number = 0) OR "
        "(artifact_bytes IS NOT NULL AND artifact_sha256 IS NOT NULL "
        "AND artifact_size IS NOT NULL AND original_filename IS NOT NULL "
        "AND ((artifact_encoding IS NULL AND artifact_stored_size IS NULL) OR "
        "(artifact_encoding IS NOT NULL AND artifact_stored_size IS NOT NULL)) "
        "AND media_type IS NOT NULL AND file_format IS NOT NULL "
        "AND product_count > 0 AND active_version_number > 0)",
    )


def _add_version_storage_constraints(batch) -> None:
    batch.create_check_constraint(
        "ck_seller_price_list_version_format",
        f"file_format IN ({FORMATS})",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_version_artifact_size",
        f"artifact_size > 0 AND artifact_size <= {MAX_RAW_ARTIFACT_BYTES}",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_version_artifact_stored_size",
        "artifact_stored_size IS NULL OR "
        f"(artifact_stored_size > 0 AND artifact_stored_size <= {MAX_STORED_ARTIFACT_BYTES})",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_version_artifact_encoding",
        "artifact_encoding IS NULL OR artifact_encoding IN ('identity', 'gzip')",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_version_artifact_length",
        "length(artifact_bytes) = coalesce(artifact_stored_size, artifact_size)",
    )
    batch.create_check_constraint(
        "ck_seller_price_list_version_artifact_storage",
        "(artifact_encoding IS NULL AND artifact_stored_size IS NULL) OR "
        "(artifact_encoding IS NOT NULL AND artifact_stored_size IS NOT NULL AND "
        "((artifact_encoding = 'identity' AND artifact_stored_size = artifact_size) OR "
        "artifact_encoding = 'gzip'))",
    )


def upgrade() -> None:
    from alembic import op

    connection = op.get_bind()
    # SQLite drops table triggers during batch table copies. Removing this one
    # explicitly also prevents it from writing into a half-migrated version table.
    _drop_sqlite_bridge(connection)

    with op.batch_alter_table("seller_price_lists") as batch:
        batch.add_column(sa.Column("artifact_encoding", sa.String(16)))
        batch.add_column(sa.Column("artifact_stored_size", sa.Integer()))
    with op.batch_alter_table("seller_price_list_versions") as batch:
        batch.add_column(sa.Column("artifact_encoding", sa.String(16)))
        batch.add_column(sa.Column("artifact_stored_size", sa.Integer()))

    with op.batch_alter_table("seller_price_lists") as batch:
        for name in (
            "ck_seller_price_list_format",
            "ck_seller_price_list_artifact_group",
            "ck_seller_price_list_artifact_length",
            "ck_seller_price_list_artifact_size",
        ):
            batch.drop_constraint(name, type_="check")
        _add_list_storage_constraints(batch)

    with op.batch_alter_table("seller_price_list_versions") as batch:
        for name in (
            "ck_seller_price_list_version_format",
            "ck_seller_price_list_version_artifact_length",
            "ck_seller_price_list_version_artifact_size",
        ):
            batch.drop_constraint(name, type_="check")
        _add_version_storage_constraints(batch)

    _replace_upload_version_bridge(connection, include_storage=True)


def _assert_downgrade_safe(connection) -> None:
    checks = (
        "SELECT count(*) FROM seller_price_lists "
        "WHERE artifact_bytes IS NOT NULL AND ("
        "coalesce(artifact_encoding, 'identity') <> 'identity' OR "
        f"artifact_size > {LEGACY_MAX_ARTIFACT_BYTES} OR "
        "length(artifact_bytes) <> artifact_size)",
        "SELECT count(*) FROM seller_price_list_versions WHERE "
        "coalesce(artifact_encoding, 'identity') <> 'identity' OR "
        f"artifact_size > {LEGACY_MAX_ARTIFACT_BYTES} OR "
        "length(artifact_bytes) <> artifact_size",
        "SELECT count(*) FROM seller_price_lists WHERE file_format = 'iof'",
        "SELECT count(*) FROM seller_price_list_versions WHERE file_format = 'iof'",
    )
    if any(int(connection.scalar(sa.text(statement)) or 0) for statement in checks):
        raise RuntimeError(
            "Cannot downgrade catalog storage while compressed, oversized, or IOF artifacts "
            "exist."
        )


def downgrade() -> None:
    from alembic import op

    connection = op.get_bind()
    _assert_downgrade_safe(connection)
    _drop_sqlite_bridge(connection)
    if connection.dialect.name == "postgresql":
        # Replace the rolling bridge while both storage columns are still visible.
        _replace_upload_version_bridge(connection, include_storage=False)

    with op.batch_alter_table("seller_price_list_versions") as batch:
        for name in (
            "ck_seller_price_list_version_format",
            "ck_seller_price_list_version_artifact_storage",
            "ck_seller_price_list_version_artifact_encoding",
            "ck_seller_price_list_version_artifact_stored_size",
            "ck_seller_price_list_version_artifact_length",
            "ck_seller_price_list_version_artifact_size",
        ):
            batch.drop_constraint(name, type_="check")
        batch.drop_column("artifact_stored_size")
        batch.drop_column("artifact_encoding")
        batch.create_check_constraint(
            "ck_seller_price_list_version_format",
            f"file_format IN ({LEGACY_FORMATS})",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_version_artifact_size",
            f"artifact_size > 0 AND artifact_size <= {LEGACY_MAX_ARTIFACT_BYTES}",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_version_artifact_length",
            "length(artifact_bytes) = artifact_size",
        )

    with op.batch_alter_table("seller_price_lists") as batch:
        for name in (
            "ck_seller_price_list_format",
            "ck_seller_price_list_artifact_group",
            "ck_seller_price_list_artifact_storage",
            "ck_seller_price_list_artifact_encoding",
            "ck_seller_price_list_artifact_stored_size",
            "ck_seller_price_list_artifact_length",
            "ck_seller_price_list_artifact_size",
        ):
            batch.drop_constraint(name, type_="check")
        batch.drop_column("artifact_stored_size")
        batch.drop_column("artifact_encoding")
        batch.create_check_constraint(
            "ck_seller_price_list_format",
            f"file_format IS NULL OR file_format IN ({LEGACY_FORMATS})",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_artifact_size",
            "artifact_size IS NULL OR "
            f"(artifact_size > 0 AND artifact_size <= {LEGACY_MAX_ARTIFACT_BYTES})",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_artifact_length",
            "artifact_bytes IS NULL OR length(artifact_bytes) = artifact_size",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_artifact_group",
            "(artifact_bytes IS NULL AND artifact_sha256 IS NULL AND artifact_size IS NULL "
            "AND original_filename IS NULL AND media_type IS NULL AND file_format IS NULL "
            "AND product_count = 0 AND active_version_number = 0) OR "
            "(artifact_bytes IS NOT NULL AND artifact_sha256 IS NOT NULL "
            "AND artifact_size IS NOT NULL AND original_filename IS NOT NULL "
            "AND media_type IS NOT NULL AND file_format IS NOT NULL "
            "AND product_count > 0 AND active_version_number > 0)",
        )

    # SQLite's list table copy removed the trigger; rebuild the 0012 bridge.
    if connection.dialect.name == "sqlite":
        _replace_upload_version_bridge(connection, include_storage=False)
