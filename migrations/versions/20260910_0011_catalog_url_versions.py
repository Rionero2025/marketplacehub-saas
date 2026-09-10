"""Add durable URL catalog sources, immutable versions, and refresh jobs."""

import sqlalchemy as sa

revision = "20260910_0011"
down_revision = "20260910_0010"
branch_labels = None
depends_on = None

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
FORMATS = "'csv', 'txt', 'tsv', 'xls', 'xlsx', 'xml'"


def _sha256_check(column: str) -> str:
    return (
        f"length({column}) = 64 "
        f"AND {column} = lower({column}) "
        "AND length("
        "replace(replace(replace(replace(replace(replace(replace(replace("
        "replace(replace(replace(replace(replace(replace(replace(replace("
        f"{column}, '0', ''), '1', ''), '2', ''), '3', ''), "
        "'4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), "
        "'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')"
        ") = 0"
    )


def _version_table() -> sa.Table:
    metadata = sa.MetaData()
    sa.Table("organizations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table("seller_profiles", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    price_lists = sa.Table(
        "seller_price_lists",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("seller_id", sa.Uuid(), nullable=False),
    )
    return sa.Table(
        "seller_price_list_versions",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id", sa.Uuid(),
            sa.ForeignKey("organizations.id", name="fk_seller_price_list_versions_organization"),
            nullable=False,
        ),
        sa.Column(
            "seller_id", sa.Uuid(),
            sa.ForeignKey("seller_profiles.id", name="fk_seller_price_list_versions_seller"),
            nullable=False,
        ),
        sa.Column("price_list_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(200), nullable=False),
        sa.Column("file_format", sa.String(16), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_size", sa.Integer(), nullable=False),
        sa.Column("artifact_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("product_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "id", "organization_id", "seller_id",
            name="uq_seller_price_list_version_scope_id",
        ),
        sa.UniqueConstraint(
            "price_list_id", "version_number", name="uq_seller_price_list_version_number",
        ),
        sa.UniqueConstraint(
            "price_list_id", "artifact_sha256", name="uq_seller_price_list_version_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["price_list_id", "organization_id", "seller_id"],
            [price_lists.c.id, price_lists.c.organization_id, price_lists.c.seller_id],
            name="fk_seller_price_list_versions_price_list_scope",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "version_number > 0", name="ck_seller_price_list_version_positive",
        ),
        sa.CheckConstraint(
            f"file_format IN ({FORMATS})", name="ck_seller_price_list_version_format",
        ),
        sa.CheckConstraint(
            f"artifact_size > 0 AND artifact_size <= {MAX_ARTIFACT_BYTES}",
            name="ck_seller_price_list_version_artifact_size",
        ),
        sa.CheckConstraint(
            "length(artifact_bytes) = artifact_size",
            name="ck_seller_price_list_version_artifact_length",
        ),
        sa.CheckConstraint(
            "length(trim(original_filename)) > 0 AND length(trim(media_type)) > 0",
            name="ck_seller_price_list_version_metadata",
        ),
        sa.CheckConstraint(
            _sha256_check("artifact_sha256"),
            name="ck_seller_price_list_version_sha256_shape",
        ),
        sa.CheckConstraint(
            "product_count > 0", name="ck_seller_price_list_version_product_count",
        ),
    )


def _create_versions(connection) -> None:
    table = _version_table()
    table.create(connection)
    sa.Index(
        "ix_seller_price_list_versions_scope_list",
        table.c.organization_id,
        table.c.seller_id,
        table.c.price_list_id,
        table.c.version_number,
    ).create(connection)
    source = sa.Table("seller_price_lists", sa.MetaData(), autoload_with=connection)
    # Keep the potentially large artifact payload inside the database. The
    # version lives in a separate table, so reusing the globally unique list ID
    # as the initial version ID is deterministic and avoids dialect-specific
    # UUID generation in this historical migration.
    columns = (
        "id",
        "organization_id",
        "seller_id",
        "price_list_id",
        "version_number",
        "original_filename",
        "media_type",
        "file_format",
        "artifact_sha256",
        "artifact_size",
        "artifact_bytes",
        "product_count",
        "created_at",
    )
    connection.execute(table.insert().from_select(columns, sa.select(
        source.c.id,
        source.c.organization_id,
        source.c.seller_id,
        source.c.id,
        sa.literal(1),
        source.c.original_filename,
        source.c.media_type,
        source.c.file_format,
        source.c.artifact_sha256,
        source.c.artifact_size,
        source.c.artifact_bytes,
        source.c.product_count,
        source.c.created_at,
    )))


def _create_upload_version_bridge(connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("""
            CREATE FUNCTION mh_seed_upload_price_list_version()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                INSERT INTO seller_price_list_versions (
                    id, organization_id, seller_id, price_list_id, version_number,
                    original_filename, media_type, file_format, artifact_sha256,
                    artifact_size, artifact_bytes, product_count, created_at
                ) VALUES (
                    NEW.id, NEW.organization_id, NEW.seller_id, NEW.id, 1,
                    NEW.original_filename, NEW.media_type, NEW.file_format,
                    NEW.artifact_sha256, NEW.artifact_size, NEW.artifact_bytes,
                    NEW.product_count, NEW.created_at
                )
                ON CONFLICT (price_list_id, version_number) DO NOTHING;
                RETURN NEW;
            END;
            $$
        """))
        connection.execute(sa.text("""
            CREATE TRIGGER trg_seller_price_lists_seed_upload_version
            AFTER INSERT ON seller_price_lists
            FOR EACH ROW
            WHEN (NEW.source_type = 'upload' AND NEW.active_version_number = 1)
            EXECUTE FUNCTION mh_seed_upload_price_list_version()
        """))
        return
    if connection.dialect.name == "sqlite":
        connection.execute(sa.text("""
            CREATE TRIGGER trg_seller_price_lists_seed_upload_version
            AFTER INSERT ON seller_price_lists
            FOR EACH ROW
            WHEN NEW.source_type = 'upload' AND NEW.active_version_number = 1
            BEGIN
                INSERT OR IGNORE INTO seller_price_list_versions (
                    id, organization_id, seller_id, price_list_id, version_number,
                    original_filename, media_type, file_format, artifact_sha256,
                    artifact_size, artifact_bytes, product_count, created_at
                ) VALUES (
                    NEW.id, NEW.organization_id, NEW.seller_id, NEW.id, 1,
                    NEW.original_filename, NEW.media_type, NEW.file_format,
                    NEW.artifact_sha256, NEW.artifact_size, NEW.artifact_bytes,
                    NEW.product_count, NEW.created_at
                );
            END
        """))
        return
    raise RuntimeError(
        f"Unsupported database for catalog upload bridge: {connection.dialect.name}"
    )


def _drop_upload_version_bridge(connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text(
            "DROP TRIGGER IF EXISTS trg_seller_price_lists_seed_upload_version "
            "ON seller_price_lists"
        ))
        connection.execute(sa.text(
            "DROP FUNCTION IF EXISTS mh_seed_upload_price_list_version()"
        ))
        return
    if connection.dialect.name == "sqlite":
        connection.execute(sa.text(
            "DROP TRIGGER IF EXISTS trg_seller_price_lists_seed_upload_version"
        ))
        return
    raise RuntimeError(
        f"Unsupported database for catalog upload bridge: {connection.dialect.name}"
    )


def _create_jobs() -> None:
    from alembic import op

    op.create_table(
        "seller_price_list_refresh_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id", sa.Uuid(),
            sa.ForeignKey("organizations.id", name="fk_seller_price_list_jobs_organization"),
            nullable=False,
        ),
        sa.Column(
            "seller_id", sa.Uuid(),
            sa.ForeignKey("seller_profiles.id", name="fk_seller_price_list_jobs_seller"),
            nullable=False,
        ),
        sa.Column("price_list_id", sa.Uuid(), nullable=False),
        sa.Column(
            "requested_by", sa.Uuid(),
            sa.ForeignKey("auth_users.id", name="fk_seller_price_list_jobs_requested_by"),
            nullable=False,
        ),
        sa.Column("realm", sa.String(16), nullable=False),
        sa.Column("source_config_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("processed_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_bytes", sa.Integer()),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_code", sa.String(64)),
        sa.Column("result_version", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "id", "organization_id", "seller_id", name="uq_seller_price_list_job_scope_id",
        ),
        sa.ForeignKeyConstraint(
            ["price_list_id", "organization_id", "seller_id"],
            [
                "seller_price_lists.id",
                "seller_price_lists.organization_id",
                "seller_price_lists.seller_id",
            ],
            name="fk_seller_price_list_jobs_price_list_scope",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'error')",
            name="ck_seller_price_list_job_status",
        ),
        sa.CheckConstraint(
            "realm IN ('seller', 'agency', 'platform')",
            name="ck_seller_price_list_job_realm",
        ),
        sa.CheckConstraint(
            "source_config_revision > 0 AND processed_bytes >= 0 "
            "AND (total_bytes IS NULL OR "
            "(total_bytes >= 0 AND processed_bytes <= total_bytes))",
            name="ck_seller_price_list_job_progress",
        ),
        sa.CheckConstraint(
            "result_version IS NULL OR result_version > 0",
            name="ck_seller_price_list_job_result_version",
        ),
    )
    op.create_index(
        "ix_seller_price_list_jobs_scope_date",
        "seller_price_list_refresh_jobs",
        ["organization_id", "seller_id", "price_list_id", "created_at"],
    )
    active = sa.text("status IN ('queued', 'running')")
    op.create_index(
        "uq_seller_price_list_active_refresh",
        "seller_price_list_refresh_jobs",
        ["organization_id", "seller_id", "price_list_id"],
        unique=True,
        postgresql_where=active,
        sqlite_where=active,
    )


def upgrade() -> None:
    from alembic import op

    # Existing upload rows receive active version 1 during the table copy.
    # Alembic recreates automatically on SQLite; PostgreSQL uses native ALTER
    # statements so dependent product foreign keys remain valid during deploy.
    with op.batch_alter_table("seller_price_lists") as batch:
        batch.add_column(sa.Column(
            "source_type", sa.String(16), nullable=False, server_default="upload",
        ))
        batch.add_column(sa.Column("source_config_encrypted", sa.Text()))
        batch.add_column(sa.Column(
            "source_host", sa.Text(), nullable=False, server_default="",
        ))
        batch.add_column(sa.Column(
            "source_config_revision", sa.Integer(), nullable=False, server_default="1",
        ))
        batch.add_column(sa.Column(
            "active_version_number", sa.Integer(), nullable=False, server_default="1",
        ))
        batch.add_column(sa.Column("last_checked_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("last_success_at", sa.DateTime(timezone=True)))
        for column, type_ in (
            ("original_filename", sa.String(255)),
            ("media_type", sa.String(200)),
            ("file_format", sa.String(16)),
            ("artifact_sha256", sa.String(64)),
            ("artifact_size", sa.Integer()),
            ("artifact_bytes", sa.LargeBinary()),
        ):
            batch.alter_column(column, existing_type=type_, nullable=True)
        for name in (
            "ck_seller_price_list_format",
            "ck_seller_price_list_sha256_shape",
            "ck_seller_price_list_artifact_size",
            "ck_seller_price_list_artifact_length",
            "ck_seller_price_list_filename_not_blank",
            "ck_seller_price_list_media_type_not_blank",
            "ck_seller_price_list_product_count",
        ):
            batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(
            "ck_seller_price_list_filename_not_blank",
            "original_filename IS NULL OR length(trim(original_filename)) > 0",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_media_type_not_blank",
            "media_type IS NULL OR length(trim(media_type)) > 0",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_format",
            f"file_format IS NULL OR file_format IN ({FORMATS})",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_sha256_shape",
            f"artifact_sha256 IS NULL OR ({_sha256_check('artifact_sha256')})",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_artifact_size",
            "artifact_size IS NULL OR "
            f"(artifact_size > 0 AND artifact_size <= {MAX_ARTIFACT_BYTES})",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_artifact_length",
            "artifact_bytes IS NULL OR length(artifact_bytes) = artifact_size",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_product_count", "product_count >= 0",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_source_type", "source_type IN ('upload', 'url')",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_revisions",
            "source_config_revision > 0 AND active_version_number >= 0",
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
        batch.create_check_constraint(
            "ck_seller_price_list_source_config",
            "(source_type = 'upload' AND source_config_encrypted IS NULL "
            "AND length(source_host) = 0 AND active_version_number = 1) OR "
            "(source_type = 'url' AND source_config_encrypted IS NOT NULL "
            "AND length(trim(source_host)) > 0)",
        )
    connection = op.get_bind()
    _create_versions(connection)
    _create_upload_version_bridge(connection)

    op.drop_index(
        "ix_seller_price_list_products_scope_list_ean",
        table_name="seller_price_list_products",
    )
    op.drop_index(
        "ix_seller_price_list_products_scope_list_sku",
        table_name="seller_price_list_products",
    )
    with op.batch_alter_table("seller_price_list_products") as batch:
        batch.add_column(sa.Column(
            "version_number", sa.Integer(), nullable=False, server_default="1",
        ))
        batch.drop_constraint("uq_seller_price_list_product_source_row", type_="unique")
        batch.create_unique_constraint(
            "uq_seller_price_list_product_source_row",
            ["price_list_id", "version_number", "source_row"],
        )
        batch.create_check_constraint(
            "ck_seller_price_list_product_version_positive", "version_number > 0",
        )
        batch.create_foreign_key(
            "fk_seller_price_list_products_version",
            "seller_price_list_versions",
            ["price_list_id", "version_number"],
            ["price_list_id", "version_number"],
            ondelete="CASCADE",
        )
    op.create_index(
        "ix_seller_price_list_products_scope_list_ean",
        "seller_price_list_products",
        ["organization_id", "seller_id", "price_list_id", "version_number", "ean"],
    )
    op.create_index(
        "ix_seller_price_list_products_scope_list_sku",
        "seller_price_list_products",
        ["organization_id", "seller_id", "price_list_id", "version_number", "sku"],
    )
    _create_jobs()


def downgrade() -> None:
    from alembic import op

    op.drop_index(
        "uq_seller_price_list_active_refresh",
        table_name="seller_price_list_refresh_jobs",
    )
    op.drop_index(
        "ix_seller_price_list_jobs_scope_date",
        table_name="seller_price_list_refresh_jobs",
    )
    op.drop_table("seller_price_list_refresh_jobs")

    connection = op.get_bind()
    _drop_upload_version_bridge(connection)
    # 0010 has one product snapshot. Keep only the currently active version.
    connection.execute(sa.text(
        "DELETE FROM seller_price_list_products "
        "WHERE version_number <> ("
        "SELECT active_version_number FROM seller_price_lists "
        "WHERE seller_price_lists.id = seller_price_list_products.price_list_id)"
    ))
    op.drop_index(
        "ix_seller_price_list_products_scope_list_ean",
        table_name="seller_price_list_products",
    )
    op.drop_index(
        "ix_seller_price_list_products_scope_list_sku",
        table_name="seller_price_list_products",
    )
    with op.batch_alter_table("seller_price_list_products") as batch:
        batch.drop_constraint(
            "fk_seller_price_list_products_version", type_="foreignkey",
        )
        batch.drop_constraint("uq_seller_price_list_product_source_row", type_="unique")
        batch.drop_constraint("ck_seller_price_list_product_version_positive", type_="check")
        batch.create_unique_constraint(
            "uq_seller_price_list_product_source_row", ["price_list_id", "source_row"],
        )
        batch.drop_column("version_number")
    op.create_index(
        "ix_seller_price_list_products_scope_list_ean",
        "seller_price_list_products",
        ["organization_id", "seller_id", "price_list_id", "ean"],
    )
    op.create_index(
        "ix_seller_price_list_products_scope_list_sku",
        "seller_price_list_products",
        ["organization_id", "seller_id", "price_list_id", "sku"],
    )

    op.drop_index(
        "ix_seller_price_list_versions_scope_list",
        table_name="seller_price_list_versions",
    )
    op.drop_table("seller_price_list_versions")
    # Pending URL feeds have no 0010 representation; completed ones retain the
    # active artifact and normalized product rows.
    connection.execute(sa.text(
        "DELETE FROM seller_price_lists WHERE active_version_number = 0"
    ))

    with op.batch_alter_table("seller_price_lists") as batch:
        for name in (
            "ck_seller_price_list_format",
            "ck_seller_price_list_sha256_shape",
            "ck_seller_price_list_artifact_size",
            "ck_seller_price_list_artifact_length",
            "ck_seller_price_list_filename_not_blank",
            "ck_seller_price_list_media_type_not_blank",
            "ck_seller_price_list_product_count",
            "ck_seller_price_list_source_type",
            "ck_seller_price_list_revisions",
            "ck_seller_price_list_artifact_group",
            "ck_seller_price_list_source_config",
        ):
            batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(
            "ck_seller_price_list_filename_not_blank",
            "length(trim(original_filename)) > 0",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_media_type_not_blank",
            "length(trim(media_type)) > 0",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_format", f"file_format IN ({FORMATS})",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_sha256_shape", _sha256_check("artifact_sha256"),
        )
        batch.create_check_constraint(
            "ck_seller_price_list_artifact_size",
            f"artifact_size > 0 AND artifact_size <= {MAX_ARTIFACT_BYTES}",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_artifact_length",
            "length(artifact_bytes) = artifact_size",
        )
        batch.create_check_constraint(
            "ck_seller_price_list_product_count", "product_count > 0",
        )
        for column, type_ in (
            ("original_filename", sa.String(255)),
            ("media_type", sa.String(200)),
            ("file_format", sa.String(16)),
            ("artifact_sha256", sa.String(64)),
            ("artifact_size", sa.Integer()),
            ("artifact_bytes", sa.LargeBinary()),
        ):
            batch.alter_column(column, existing_type=type_, nullable=False)
        for column in (
            "last_success_at",
            "last_checked_at",
            "active_version_number",
            "source_config_revision",
            "source_host",
            "source_config_encrypted",
            "source_type",
        ):
            batch.drop_column(column)
