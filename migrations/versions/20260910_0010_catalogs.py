"""Create tenant-scoped supplier catalogs with durable source artifacts."""

import sqlalchemy as sa

revision = "20260910_0010"
down_revision = "20260910_0009"
branch_labels = None
depends_on = None


metadata = sa.MetaData()

SHA256_HEX_CHECK = (
    "length(artifact_sha256) = 64 "
    "AND artifact_sha256 = lower(artifact_sha256) "
    "AND length("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "artifact_sha256, '0', ''), '1', ''), '2', ''), '3', ''), "
    "'4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), "
    "'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')"
    ") = 0"
)

# Foreign-key targets are owned by the tenancy migrations and are deliberately
# excluded from CATALOG_TABLES so this revision never creates or drops them.
sa.Table("organizations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
sa.Table("seller_profiles", metadata, sa.Column("id", sa.Uuid(), primary_key=True))


seller_suppliers = sa.Table(
    "seller_suppliers",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "organization_id",
        sa.Uuid(),
        sa.ForeignKey("organizations.id", name="fk_seller_suppliers_organization"),
        nullable=False,
    ),
    sa.Column(
        "seller_id",
        sa.Uuid(),
        sa.ForeignKey("seller_profiles.id", name="fk_seller_suppliers_seller"),
        nullable=False,
    ),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("notes", sa.Text(), nullable=False, server_default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "id",
        "organization_id",
        "seller_id",
        name="uq_seller_supplier_scope_id",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "seller_id",
        "name",
        name="uq_seller_supplier_scope_name",
    ),
    sa.CheckConstraint(
        "length(trim(name)) > 0",
        name="ck_seller_supplier_name_not_blank",
    ),
)
sa.Index(
    "ix_seller_suppliers_scope_name",
    seller_suppliers.c.organization_id,
    seller_suppliers.c.seller_id,
    seller_suppliers.c.name,
)


seller_price_lists = sa.Table(
    "seller_price_lists",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "organization_id",
        sa.Uuid(),
        sa.ForeignKey("organizations.id", name="fk_seller_price_lists_organization"),
        nullable=False,
    ),
    sa.Column(
        "seller_id",
        sa.Uuid(),
        sa.ForeignKey("seller_profiles.id", name="fk_seller_price_lists_seller"),
        nullable=False,
    ),
    sa.Column(
        "supplier_id",
        sa.Uuid(),
        nullable=False,
    ),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("original_filename", sa.String(255), nullable=False),
    sa.Column("media_type", sa.String(200), nullable=False),
    sa.Column("file_format", sa.String(16), nullable=False),
    sa.Column("artifact_sha256", sa.String(64), nullable=False),
    sa.Column("artifact_size", sa.Integer(), nullable=False),
    sa.Column("artifact_bytes", sa.LargeBinary(), nullable=False),
    sa.Column("product_count", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "id",
        "organization_id",
        "seller_id",
        name="uq_seller_price_list_scope_id",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "seller_id",
        "name",
        name="uq_seller_price_list_scope_name",
    ),
    sa.ForeignKeyConstraint(
        ["supplier_id", "organization_id", "seller_id"],
        [
            "seller_suppliers.id",
            "seller_suppliers.organization_id",
            "seller_suppliers.seller_id",
        ],
        name="fk_seller_price_lists_supplier_scope",
        ondelete="CASCADE",
    ),
    sa.CheckConstraint(
        "length(trim(name)) > 0",
        name="ck_seller_price_list_name_not_blank",
    ),
    sa.CheckConstraint(
        "length(trim(original_filename)) > 0",
        name="ck_seller_price_list_filename_not_blank",
    ),
    sa.CheckConstraint(
        "length(trim(media_type)) > 0",
        name="ck_seller_price_list_media_type_not_blank",
    ),
    sa.CheckConstraint(
        "file_format IN ('csv', 'txt', 'tsv', 'xls', 'xlsx', 'xml')",
        name="ck_seller_price_list_format",
    ),
    sa.CheckConstraint(
        SHA256_HEX_CHECK,
        name="ck_seller_price_list_sha256_shape",
    ),
    sa.CheckConstraint(
        f"artifact_size > 0 AND artifact_size <= {20 * 1024 * 1024}",
        name="ck_seller_price_list_artifact_size",
    ),
    sa.CheckConstraint(
        "length(artifact_bytes) = artifact_size",
        name="ck_seller_price_list_artifact_length",
    ),
    sa.CheckConstraint(
        "product_count > 0",
        name="ck_seller_price_list_product_count",
    ),
)
sa.Index(
    "ix_seller_price_lists_scope_supplier",
    seller_price_lists.c.organization_id,
    seller_price_lists.c.seller_id,
    seller_price_lists.c.supplier_id,
)


seller_price_list_products = sa.Table(
    "seller_price_list_products",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "organization_id",
        sa.Uuid(),
        sa.ForeignKey("organizations.id", name="fk_seller_price_list_products_organization"),
        nullable=False,
    ),
    sa.Column(
        "seller_id",
        sa.Uuid(),
        sa.ForeignKey("seller_profiles.id", name="fk_seller_price_list_products_seller"),
        nullable=False,
    ),
    sa.Column(
        "price_list_id",
        sa.Uuid(),
        nullable=False,
    ),
    sa.Column("source_row", sa.Integer(), nullable=False),
    sa.Column("ean", sa.Text(), nullable=False, server_default=""),
    sa.Column("sku", sa.Text(), nullable=False, server_default=""),
    sa.Column("name", sa.Text(), nullable=False, server_default=""),
    sa.Column("cost", sa.Numeric(38, 8), nullable=False, server_default="0"),
    sa.Column("shipping_cost", sa.Numeric(38, 8), nullable=False, server_default="0"),
    sa.Column("total_cost", sa.Numeric(38, 8), nullable=False, server_default="0"),
    sa.Column("quantity", sa.Numeric(38, 8), nullable=False, server_default="0"),
    sa.Column("canonical_json", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "price_list_id",
        "source_row",
        name="uq_seller_price_list_product_source_row",
    ),
    sa.ForeignKeyConstraint(
        ["price_list_id", "organization_id", "seller_id"],
        [
            "seller_price_lists.id",
            "seller_price_lists.organization_id",
            "seller_price_lists.seller_id",
        ],
        name="fk_seller_price_list_products_price_list_scope",
        ondelete="CASCADE",
    ),
    sa.CheckConstraint(
        "source_row > 0",
        name="ck_seller_price_list_product_source_row_positive",
    ),
)
sa.Index(
    "ix_seller_price_list_products_scope_list_ean",
    seller_price_list_products.c.organization_id,
    seller_price_list_products.c.seller_id,
    seller_price_list_products.c.price_list_id,
    seller_price_list_products.c.ean,
)
sa.Index(
    "ix_seller_price_list_products_scope_list_sku",
    seller_price_list_products.c.organization_id,
    seller_price_list_products.c.seller_id,
    seller_price_list_products.c.price_list_id,
    seller_price_list_products.c.sku,
)


CATALOG_TABLES = (
    seller_suppliers,
    seller_price_lists,
    seller_price_list_products,
)


def upgrade() -> None:
    from alembic import op

    connection = op.get_bind()
    for table in CATALOG_TABLES:
        table.create(connection)


def downgrade() -> None:
    from alembic import op

    connection = op.get_bind()
    for table in reversed(CATALOG_TABLES):
        table.drop(connection)
