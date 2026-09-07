from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)

from marketplace_hub_core.tenancy.schema import metadata

order_lines = Table(
    "seller_order_lines", metadata,
    Column("id", Uuid(), primary_key=True),
    Column("organization_id", Uuid(), ForeignKey("organizations.id"), nullable=False),
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), nullable=False),
    # Keep order history even when a connector is removed; every public read still
    # requires the exact active Seller/account scope, so orphaned history is hidden.
    Column("account_id", Uuid(), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("external_line_id", Text(), nullable=False),
    Column("order_id", Text(), nullable=False),
    Column("marketplace", String(32), nullable=False),
    Column("order_created_at", DateTime(timezone=True)),
    Column("status", Text(), nullable=False, default=""),
    Column("storefront", Text(), nullable=False, default=""),
    Column("search_text", Text(), nullable=False, default=""),
    Column("canonical_json", Text(), nullable=False),
    Column("raw_json", Text(), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("projection_updated_at", DateTime(timezone=True)),
    Column("currency", String(16), nullable=False, server_default=""),
    Column("carrier", Text(), nullable=False, server_default=""),
    Column("has_tracking", Boolean(), nullable=False, server_default="false"),
    Column("has_commission", Boolean(), nullable=False, server_default="false"),
    Column("quantity", Integer(), nullable=False, server_default="1"),
    Column("excluded", Boolean(), nullable=False, server_default="false"),
    Column("catalog_cost", Boolean(), nullable=False, server_default="false"),
    *[Column(name, Numeric(38, 8)) for name in (
        "sale_eur", "commission_eur", "payout_eur", "purchase_eur", "profit_eur",
    )],
    UniqueConstraint("seller_id", "account_id", "environment", "order_id", "external_line_id",
                     name="uq_order_line_scope"),
)
Index("ix_order_lines_scope_date", order_lines.c.organization_id, order_lines.c.seller_id,
      order_lines.c.account_id, order_lines.c.environment, order_lines.c.order_created_at)

order_sync_jobs = Table(
    "seller_order_sync_jobs", metadata,
    Column("id", Uuid(), primary_key=True),
    Column("organization_id", Uuid(), ForeignKey("organizations.id"), nullable=False),
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), nullable=False),
    Column("account_id", Uuid(), nullable=False),
    Column("requested_by", Uuid(), ForeignKey("auth_users.id"), nullable=False),
    Column("realm", String(16), nullable=False),
    Column("marketplace", String(32), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("maximum", Integer),
    Column("include_details", Boolean(), nullable=False),
    Column("status", String(16), nullable=False),
    Column("processed", Integer, nullable=False, default=0),
    Column("total", Integer),
    Column("message", Text(), nullable=False, default=""),
    Column("error_code", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
)
Index("ix_order_jobs_scope_date", order_sync_jobs.c.organization_id, order_sync_jobs.c.seller_id,
      order_sync_jobs.c.account_id, order_sync_jobs.c.environment, order_sync_jobs.c.created_at)
active = order_sync_jobs.c.status.in_(["queued", "running"])
Index("uq_order_active_sync", order_sync_jobs.c.seller_id, order_sync_jobs.c.account_id,
      order_sync_jobs.c.environment, unique=True, postgresql_where=active, sqlite_where=active)

ORDER_TABLES = [order_lines, order_sync_jobs]

order_selections = Table(
    "seller_order_selections", metadata,
    Column("id", Uuid(), primary_key=True),
    Column("session_id", Uuid(), ForeignKey("auth_sessions.id", ondelete="CASCADE"),
           nullable=False),
    Column("organization_id", Uuid(), ForeignKey("organizations.id"), nullable=False),
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), nullable=False),
    Column("account_id", Uuid(), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("filter_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("session_id", "seller_id", "account_id", "environment", "filter_hash",
                     name="uq_order_selection_filter"),
)
order_selection_members = Table(
    "seller_order_selection_members", metadata,
    Column("selection_id", Uuid(), ForeignKey("seller_order_selections.id", ondelete="CASCADE"),
           primary_key=True),
    Column("line_id", Uuid(), ForeignKey("seller_order_lines.id", ondelete="CASCADE"),
           primary_key=True),
)
SELECTION_TABLES = [order_selections, order_selection_members]
