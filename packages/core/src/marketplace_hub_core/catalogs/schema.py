from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)

from marketplace_hub_core.catalogs.artifacts import (
    MAX_REMOTE_CATALOG_SOURCE_BYTES,
    MAX_STORED_CATALOG_ARTIFACT_BYTES,
)
from marketplace_hub_core.tenancy.schema import metadata

# Uploads and the generic in-memory parser retain their original 20 MiB limit.
# The durable catalog model also accepts larger remote feeds when their stored
# representation fits the independently bounded compressed-artifact limit.
MAX_CATALOG_ARTIFACT_BYTES = 20 * 1024 * 1024


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


seller_suppliers = Table(
    "seller_suppliers",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "organization_id", Uuid(),
        ForeignKey("organizations.id", name="fk_seller_suppliers_organization"),
        nullable=False,
    ),
    Column(
        "seller_id", Uuid(),
        ForeignKey("seller_profiles.id", name="fk_seller_suppliers_seller"),
        nullable=False,
    ),
    Column("name", Text(), nullable=False),
    Column("notes", Text(), nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("id", "organization_id", "seller_id", name="uq_seller_supplier_scope_id"),
    UniqueConstraint(
        "organization_id", "seller_id", "name", name="uq_seller_supplier_scope_name",
    ),
    CheckConstraint("length(trim(name)) > 0", name="ck_seller_supplier_name_not_blank"),
)
Index(
    "ix_seller_suppliers_scope_name",
    seller_suppliers.c.organization_id,
    seller_suppliers.c.seller_id,
    seller_suppliers.c.name,
)


seller_price_lists = Table(
    "seller_price_lists",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "organization_id", Uuid(),
        ForeignKey("organizations.id", name="fk_seller_price_lists_organization"),
        nullable=False,
    ),
    Column(
        "seller_id", Uuid(),
        ForeignKey("seller_profiles.id", name="fk_seller_price_lists_seller"),
        nullable=False,
    ),
    Column("supplier_id", Uuid(), nullable=False),
    Column("name", Text(), nullable=False),
    Column("provider", String(32), nullable=False, server_default="generic"),
    Column("feed_role", String(16), nullable=False, server_default="standard"),
    Column("source_type", String(16), nullable=False, server_default="upload"),
    # Opaque Fernet token. Public repository reads never select this column.
    Column("source_config_encrypted", Text()),
    Column("source_host", Text(), nullable=False, server_default=""),
    Column("source_config_revision", Integer(), nullable=False, server_default="1"),
    Column("active_version_number", Integer(), nullable=False, server_default="1"),
    Column("last_checked_at", DateTime(timezone=True)),
    Column("last_success_at", DateTime(timezone=True)),
    # URL feeds exist before their first successful download, so this active
    # artifact group is nullable until active_version_number becomes positive.
    Column("original_filename", String(255)),
    Column("media_type", String(200)),
    Column("file_format", String(16)),
    Column("artifact_sha256", String(64)),
    Column("artifact_size", Integer()),
    Column("artifact_encoding", String(16)),
    Column("artifact_stored_size", Integer()),
    Column("artifact_bytes", LargeBinary()),
    Column("product_count", Integer(), nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("id", "organization_id", "seller_id", name="uq_seller_price_list_scope_id"),
    UniqueConstraint(
        "organization_id", "seller_id", "name", name="uq_seller_price_list_scope_name",
    ),
    ForeignKeyConstraint(
        ["supplier_id", "organization_id", "seller_id"],
        [
            "seller_suppliers.id",
            "seller_suppliers.organization_id",
            "seller_suppliers.seller_id",
        ],
        name="fk_seller_price_lists_supplier_scope",
        ondelete="CASCADE",
    ),
    CheckConstraint("length(trim(name)) > 0", name="ck_seller_price_list_name_not_blank"),
    CheckConstraint(
        "original_filename IS NULL OR length(trim(original_filename)) > 0",
        name="ck_seller_price_list_filename_not_blank",
    ),
    CheckConstraint(
        "media_type IS NULL OR length(trim(media_type)) > 0",
        name="ck_seller_price_list_media_type_not_blank",
    ),
    CheckConstraint(
        "file_format IS NULL OR "
        "file_format IN ('csv', 'txt', 'tsv', 'xls', 'xlsx', 'xml', 'iof')",
        name="ck_seller_price_list_format",
    ),
    CheckConstraint(
        f"artifact_sha256 IS NULL OR ({_sha256_check('artifact_sha256')})",
        name="ck_seller_price_list_sha256_shape",
    ),
    CheckConstraint(
        "artifact_size IS NULL OR "
        f"(artifact_size > 0 AND artifact_size <= {MAX_REMOTE_CATALOG_SOURCE_BYTES})",
        name="ck_seller_price_list_artifact_size",
    ),
    CheckConstraint(
        "artifact_stored_size IS NULL OR "
        f"(artifact_stored_size > 0 AND "
        f"artifact_stored_size <= {MAX_STORED_CATALOG_ARTIFACT_BYTES})",
        name="ck_seller_price_list_artifact_stored_size",
    ),
    CheckConstraint(
        "artifact_encoding IS NULL OR artifact_encoding IN ('identity', 'gzip')",
        name="ck_seller_price_list_artifact_encoding",
    ),
    CheckConstraint(
        "artifact_bytes IS NULL OR "
        "length(artifact_bytes) = coalesce(artifact_stored_size, artifact_size)",
        name="ck_seller_price_list_artifact_length",
    ),
    CheckConstraint(
        "(artifact_encoding IS NULL AND artifact_stored_size IS NULL) OR "
        "(artifact_encoding IS NOT NULL AND artifact_stored_size IS NOT NULL AND "
        "((artifact_encoding = 'identity' AND artifact_stored_size = artifact_size) OR "
        "artifact_encoding = 'gzip'))",
        name="ck_seller_price_list_artifact_storage",
    ),
    CheckConstraint("product_count >= 0", name="ck_seller_price_list_product_count"),
    CheckConstraint(
        "provider IN ('generic', 'innpro')", name="ck_seller_price_list_provider",
    ),
    CheckConstraint(
        "feed_role IN ('standard', 'full', 'light')",
        name="ck_seller_price_list_feed_role",
    ),
    CheckConstraint(
        "(provider = 'generic' AND feed_role = 'standard') OR "
        "(provider = 'innpro' AND feed_role IN ('full', 'light'))",
        name="ck_seller_price_list_feed_identity",
    ),
    CheckConstraint("source_type IN ('upload', 'url')", name="ck_seller_price_list_source_type"),
    CheckConstraint(
        "source_config_revision > 0 AND active_version_number >= 0",
        name="ck_seller_price_list_revisions",
    ),
    CheckConstraint(
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
        name="ck_seller_price_list_artifact_group",
    ),
    CheckConstraint(
        "(source_type = 'upload' AND source_config_encrypted IS NULL "
        "AND length(source_host) = 0 AND active_version_number = 1) OR "
        "(source_type = 'url' AND source_config_encrypted IS NOT NULL "
        "AND length(trim(source_host)) > 0)",
        name="ck_seller_price_list_source_config",
    ),
)
Index(
    "ix_seller_price_lists_scope_supplier",
    seller_price_lists.c.organization_id,
    seller_price_lists.c.seller_id,
    seller_price_lists.c.supplier_id,
)


seller_price_list_versions = Table(
    "seller_price_list_versions",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "organization_id", Uuid(),
        ForeignKey("organizations.id", name="fk_seller_price_list_versions_organization"),
        nullable=False,
    ),
    Column(
        "seller_id", Uuid(),
        ForeignKey("seller_profiles.id", name="fk_seller_price_list_versions_seller"),
        nullable=False,
    ),
    Column("price_list_id", Uuid(), nullable=False),
    Column("version_number", Integer(), nullable=False),
    Column("provider", String(32), nullable=False, server_default="generic"),
    Column("feed_role", String(16), nullable=False, server_default="standard"),
    Column("original_filename", String(255), nullable=False),
    Column("media_type", String(200), nullable=False),
    Column("file_format", String(16), nullable=False),
    Column("artifact_sha256", String(64), nullable=False),
    Column("artifact_size", Integer(), nullable=False),
    # Nullable only for rows written briefly by the previous release during a
    # rolling deploy; NULL/NULL has the precise legacy meaning ``identity``.
    Column("artifact_encoding", String(16)),
    Column("artifact_stored_size", Integer()),
    Column("artifact_bytes", LargeBinary(), nullable=False),
    Column("product_count", Integer(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "id", "organization_id", "seller_id", name="uq_seller_price_list_version_scope_id",
    ),
    UniqueConstraint(
        "price_list_id", "version_number", name="uq_seller_price_list_version_number",
    ),
    UniqueConstraint(
        "price_list_id", "artifact_sha256", name="uq_seller_price_list_version_sha256",
    ),
    ForeignKeyConstraint(
        ["price_list_id", "organization_id", "seller_id"],
        [
            "seller_price_lists.id",
            "seller_price_lists.organization_id",
            "seller_price_lists.seller_id",
        ],
        name="fk_seller_price_list_versions_price_list_scope",
        ondelete="CASCADE",
    ),
    CheckConstraint("version_number > 0", name="ck_seller_price_list_version_positive"),
    CheckConstraint(
        "provider IN ('generic', 'innpro')",
        name="ck_seller_price_list_version_provider",
    ),
    CheckConstraint(
        "feed_role IN ('standard', 'full', 'light')",
        name="ck_seller_price_list_version_feed_role",
    ),
    CheckConstraint(
        "(provider = 'generic' AND feed_role = 'standard') OR "
        "(provider = 'innpro' AND feed_role IN ('full', 'light'))",
        name="ck_seller_price_list_version_feed_identity",
    ),
    CheckConstraint(
        "file_format IN ('csv', 'txt', 'tsv', 'xls', 'xlsx', 'xml', 'iof')",
        name="ck_seller_price_list_version_format",
    ),
    CheckConstraint(
        f"artifact_size > 0 AND artifact_size <= {MAX_REMOTE_CATALOG_SOURCE_BYTES}",
        name="ck_seller_price_list_version_artifact_size",
    ),
    CheckConstraint(
        "artifact_stored_size IS NULL OR "
        f"(artifact_stored_size > 0 AND "
        f"artifact_stored_size <= {MAX_STORED_CATALOG_ARTIFACT_BYTES})",
        name="ck_seller_price_list_version_artifact_stored_size",
    ),
    CheckConstraint(
        "artifact_encoding IS NULL OR artifact_encoding IN ('identity', 'gzip')",
        name="ck_seller_price_list_version_artifact_encoding",
    ),
    CheckConstraint(
        "length(artifact_bytes) = coalesce(artifact_stored_size, artifact_size)",
        name="ck_seller_price_list_version_artifact_length",
    ),
    CheckConstraint(
        "(artifact_encoding IS NULL AND artifact_stored_size IS NULL) OR "
        "(artifact_encoding IS NOT NULL AND artifact_stored_size IS NOT NULL AND "
        "((artifact_encoding = 'identity' AND artifact_stored_size = artifact_size) OR "
        "artifact_encoding = 'gzip'))",
        name="ck_seller_price_list_version_artifact_storage",
    ),
    CheckConstraint(
        "length(trim(original_filename)) > 0 AND length(trim(media_type)) > 0",
        name="ck_seller_price_list_version_metadata",
    ),
    CheckConstraint(
        _sha256_check("artifact_sha256"),
        name="ck_seller_price_list_version_sha256_shape",
    ),
    CheckConstraint("product_count > 0", name="ck_seller_price_list_version_product_count"),
)
Index(
    "ix_seller_price_list_versions_scope_list",
    seller_price_list_versions.c.organization_id,
    seller_price_list_versions.c.seller_id,
    seller_price_list_versions.c.price_list_id,
    seller_price_list_versions.c.version_number,
)


seller_price_list_products = Table(
    "seller_price_list_products",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "organization_id", Uuid(),
        ForeignKey("organizations.id", name="fk_seller_price_list_products_organization"),
        nullable=False,
    ),
    Column(
        "seller_id", Uuid(),
        ForeignKey("seller_profiles.id", name="fk_seller_price_list_products_seller"),
        nullable=False,
    ),
    Column("price_list_id", Uuid(), nullable=False),
    Column("version_number", Integer(), nullable=False, server_default="1"),
    Column("source_row", Integer(), nullable=False),
    Column("ean", Text(), nullable=False, server_default=""),
    Column("sku", Text(), nullable=False, server_default=""),
    Column("name", Text(), nullable=False, server_default=""),
    Column("cost", Numeric(38, 8), nullable=False, server_default="0"),
    Column("shipping_cost", Numeric(38, 8), nullable=False, server_default="0"),
    Column("total_cost", Numeric(38, 8), nullable=False, server_default="0"),
    Column("quantity", Numeric(38, 8), nullable=False, server_default="0"),
    Column("weight_kg", Numeric(18, 6)),
    Column("length_cm", Numeric(18, 6)),
    Column("width_cm", Numeric(18, 6)),
    Column("height_cm", Numeric(18, 6)),
    Column("canonical_json", Text(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["price_list_id", "organization_id", "seller_id"],
        [
            "seller_price_lists.id",
            "seller_price_lists.organization_id",
            "seller_price_lists.seller_id",
        ],
        name="fk_seller_price_list_products_price_list_scope",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["price_list_id", "version_number"],
        [
            "seller_price_list_versions.price_list_id",
            "seller_price_list_versions.version_number",
        ],
        name="fk_seller_price_list_products_version",
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "price_list_id", "version_number", "source_row",
        name="uq_seller_price_list_product_source_row",
    ),
    CheckConstraint(
        "source_row > 0", name="ck_seller_price_list_product_source_row_positive",
    ),
    CheckConstraint(
        "version_number > 0", name="ck_seller_price_list_product_version_positive",
    ),
)
Index(
    "ix_seller_price_list_products_scope_list_ean",
    seller_price_list_products.c.organization_id,
    seller_price_list_products.c.seller_id,
    seller_price_list_products.c.price_list_id,
    seller_price_list_products.c.version_number,
    seller_price_list_products.c.ean,
)
Index(
    "ix_seller_price_list_products_scope_list_sku",
    seller_price_list_products.c.organization_id,
    seller_price_list_products.c.seller_id,
    seller_price_list_products.c.price_list_id,
    seller_price_list_products.c.version_number,
    seller_price_list_products.c.sku,
)


seller_price_list_refresh_jobs = Table(
    "seller_price_list_refresh_jobs",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "organization_id", Uuid(),
        ForeignKey("organizations.id", name="fk_seller_price_list_jobs_organization"),
        nullable=False,
    ),
    Column(
        "seller_id", Uuid(),
        ForeignKey("seller_profiles.id", name="fk_seller_price_list_jobs_seller"),
        nullable=False,
    ),
    Column("price_list_id", Uuid(), nullable=False),
    Column(
        "requested_by", Uuid(),
        ForeignKey("auth_users.id", name="fk_seller_price_list_jobs_requested_by"),
        nullable=False,
    ),
    Column("realm", String(16), nullable=False),
    Column("source_config_revision", Integer(), nullable=False),
    Column("status", String(16), nullable=False),
    Column("processed_bytes", Integer(), nullable=False, server_default="0"),
    Column("total_bytes", Integer()),
    Column("message", Text(), nullable=False, server_default=""),
    Column("error_code", String(64)),
    Column("result_version", Integer()),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint(
        "id", "organization_id", "seller_id", name="uq_seller_price_list_job_scope_id",
    ),
    ForeignKeyConstraint(
        ["price_list_id", "organization_id", "seller_id"],
        [
            "seller_price_lists.id",
            "seller_price_lists.organization_id",
            "seller_price_lists.seller_id",
        ],
        name="fk_seller_price_list_jobs_price_list_scope",
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "status IN ('queued', 'running', 'done', 'error')",
        name="ck_seller_price_list_job_status",
    ),
    CheckConstraint(
        "realm IN ('seller', 'agency', 'platform')",
        name="ck_seller_price_list_job_realm",
    ),
    CheckConstraint(
        "source_config_revision > 0 AND processed_bytes >= 0 "
        "AND (total_bytes IS NULL OR (total_bytes >= 0 AND processed_bytes <= total_bytes))",
        name="ck_seller_price_list_job_progress",
    ),
    CheckConstraint(
        "result_version IS NULL OR result_version > 0",
        name="ck_seller_price_list_job_result_version",
    ),
)
Index(
    "ix_seller_price_list_jobs_scope_date",
    seller_price_list_refresh_jobs.c.organization_id,
    seller_price_list_refresh_jobs.c.seller_id,
    seller_price_list_refresh_jobs.c.price_list_id,
    seller_price_list_refresh_jobs.c.created_at,
)
active_refresh = seller_price_list_refresh_jobs.c.status.in_(["queued", "running"])
Index(
    "uq_seller_price_list_active_refresh",
    seller_price_list_refresh_jobs.c.organization_id,
    seller_price_list_refresh_jobs.c.seller_id,
    seller_price_list_refresh_jobs.c.price_list_id,
    unique=True,
    postgresql_where=active_refresh,
    sqlite_where=active_refresh,
)


CATALOG_TABLES = [
    seller_suppliers,
    seller_price_lists,
    seller_price_list_versions,
    seller_price_list_products,
    seller_price_list_refresh_jobs,
]
