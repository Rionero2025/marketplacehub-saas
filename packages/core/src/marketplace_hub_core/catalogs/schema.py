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

from marketplace_hub_core.tenancy.schema import metadata

MAX_CATALOG_ARTIFACT_BYTES = 20 * 1024 * 1024

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
    UniqueConstraint(
        "id", "organization_id", "seller_id", name="uq_seller_supplier_scope_id",
    ),
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
    Column("original_filename", String(255), nullable=False),
    Column("media_type", String(200), nullable=False),
    Column("file_format", String(16), nullable=False),
    Column("artifact_sha256", String(64), nullable=False),
    Column("artifact_size", Integer(), nullable=False),
    Column("artifact_bytes", LargeBinary(), nullable=False),
    Column("product_count", Integer(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "id", "organization_id", "seller_id", name="uq_seller_price_list_scope_id",
    ),
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
    CheckConstraint(
        "file_format IN ('csv', 'txt', 'tsv', 'xls', 'xlsx', 'xml')",
        name="ck_seller_price_list_format",
    ),
    CheckConstraint(
        f"artifact_size > 0 AND artifact_size <= {MAX_CATALOG_ARTIFACT_BYTES}",
        name="ck_seller_price_list_artifact_size",
    ),
    CheckConstraint(
        "length(artifact_bytes) = artifact_size",
        name="ck_seller_price_list_artifact_length",
    ),
    CheckConstraint(
        "length(trim(name)) > 0", name="ck_seller_price_list_name_not_blank",
    ),
    CheckConstraint(
        "length(trim(original_filename)) > 0",
        name="ck_seller_price_list_filename_not_blank",
    ),
    CheckConstraint(
        "length(trim(media_type)) > 0", name="ck_seller_price_list_media_type_not_blank",
    ),
    CheckConstraint(
        SHA256_HEX_CHECK,
        name="ck_seller_price_list_sha256_shape",
    ),
    CheckConstraint(
        "product_count > 0", name="ck_seller_price_list_product_count",
    ),
)
Index(
    "ix_seller_price_lists_scope_supplier",
    seller_price_lists.c.organization_id,
    seller_price_lists.c.seller_id,
    seller_price_lists.c.supplier_id,
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
    Column("source_row", Integer(), nullable=False),
    Column("ean", Text(), nullable=False, server_default=""),
    Column("sku", Text(), nullable=False, server_default=""),
    Column("name", Text(), nullable=False, server_default=""),
    Column("cost", Numeric(38, 8), nullable=False, server_default="0"),
    Column("shipping_cost", Numeric(38, 8), nullable=False, server_default="0"),
    Column("total_cost", Numeric(38, 8), nullable=False, server_default="0"),
    Column("quantity", Numeric(38, 8), nullable=False, server_default="0"),
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
    UniqueConstraint(
        "price_list_id", "source_row", name="uq_seller_price_list_product_source_row",
    ),
    CheckConstraint(
        "source_row > 0", name="ck_seller_price_list_product_source_row_positive",
    ),
)
Index(
    "ix_seller_price_list_products_scope_list_ean",
    seller_price_list_products.c.organization_id,
    seller_price_list_products.c.seller_id,
    seller_price_list_products.c.price_list_id,
    seller_price_list_products.c.ean,
)
Index(
    "ix_seller_price_list_products_scope_list_sku",
    seller_price_list_products.c.organization_id,
    seller_price_list_products.c.seller_id,
    seller_price_list_products.c.price_list_id,
    seller_price_list_products.c.sku,
)


CATALOG_TABLES = [seller_suppliers, seller_price_lists, seller_price_list_products]
