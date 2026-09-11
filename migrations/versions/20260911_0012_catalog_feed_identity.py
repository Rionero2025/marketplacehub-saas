"""Classify catalog feeds by provider and semantic role."""

import sqlalchemy as sa

revision = "20260911_0012"
down_revision = "20260910_0011"
branch_labels = None
depends_on = None


def _replace_upload_version_bridge(connection, *, include_identity: bool) -> None:
    identity_columns = ", provider, feed_role" if include_identity else ""
    identity_values = ", NEW.provider, NEW.feed_role" if include_identity else ""
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text(f"""
            CREATE OR REPLACE FUNCTION mh_seed_upload_price_list_version()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                INSERT INTO seller_price_list_versions (
                    id, organization_id, seller_id, price_list_id, version_number
                    {identity_columns}, original_filename, media_type, file_format,
                    artifact_sha256, artifact_size, artifact_bytes, product_count, created_at
                ) VALUES (
                    NEW.id, NEW.organization_id, NEW.seller_id, NEW.id, 1
                    {identity_values}, NEW.original_filename, NEW.media_type,
                    NEW.file_format, NEW.artifact_sha256, NEW.artifact_size,
                    NEW.artifact_bytes, NEW.product_count, NEW.created_at
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
                    id, organization_id, seller_id, price_list_id, version_number
                    {identity_columns}, original_filename, media_type, file_format,
                    artifact_sha256, artifact_size, artifact_bytes, product_count, created_at
                ) VALUES (
                    NEW.id, NEW.organization_id, NEW.seller_id, NEW.id, 1
                    {identity_values}, NEW.original_filename, NEW.media_type,
                    NEW.file_format, NEW.artifact_sha256, NEW.artifact_size,
                    NEW.artifact_bytes, NEW.product_count, NEW.created_at
                );
            END
        """))
        return
    raise RuntimeError(
        f"Unsupported database for catalog upload bridge: {connection.dialect.name}"
    )


def _add_identity_constraints(batch, *, version: bool) -> None:
    prefix = "ck_seller_price_list_version" if version else "ck_seller_price_list"
    batch.create_check_constraint(
        f"{prefix}_provider", "provider IN ('generic', 'innpro')",
    )
    batch.create_check_constraint(
        f"{prefix}_feed_role", "feed_role IN ('standard', 'full', 'light')",
    )
    batch.create_check_constraint(
        f"{prefix}_feed_identity",
        "(provider = 'generic' AND feed_role = 'standard') OR "
        "(provider = 'innpro' AND feed_role IN ('full', 'light'))",
    )


def upgrade() -> None:
    from alembic import op

    with op.batch_alter_table("seller_price_lists") as batch:
        batch.add_column(sa.Column(
            "provider", sa.String(32), nullable=False, server_default="generic",
        ))
        batch.add_column(sa.Column(
            "feed_role", sa.String(16), nullable=False, server_default="standard",
        ))
        _add_identity_constraints(batch, version=False)

    with op.batch_alter_table("seller_price_list_versions") as batch:
        batch.add_column(sa.Column(
            "provider", sa.String(32), nullable=False, server_default="generic",
        ))
        batch.add_column(sa.Column(
            "feed_role", sa.String(16), nullable=False, server_default="standard",
        ))
        _add_identity_constraints(batch, version=True)

    _replace_upload_version_bridge(op.get_bind(), include_identity=True)


def downgrade() -> None:
    from alembic import op

    connection = op.get_bind()
    # Switch the rolling-deploy bridge before either identity column disappears.
    if connection.dialect.name == "sqlite":
        connection.execute(sa.text(
            "DROP TRIGGER IF EXISTS trg_seller_price_lists_seed_upload_version"
        ))
    else:
        _replace_upload_version_bridge(connection, include_identity=False)

    with op.batch_alter_table("seller_price_list_versions") as batch:
        for name in (
            "ck_seller_price_list_version_feed_identity",
            "ck_seller_price_list_version_feed_role",
            "ck_seller_price_list_version_provider",
        ):
            batch.drop_constraint(name, type_="check")
        batch.drop_column("feed_role")
        batch.drop_column("provider")

    with op.batch_alter_table("seller_price_lists") as batch:
        for name in (
            "ck_seller_price_list_feed_identity",
            "ck_seller_price_list_feed_role",
            "ck_seller_price_list_provider",
        ):
            batch.drop_constraint(name, type_="check")
        batch.drop_column("feed_role")
        batch.drop_column("provider")

    # SQLite batch DDL recreates the table and drops its trigger.
    _replace_upload_version_bridge(connection, include_identity=False)
