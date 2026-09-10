from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
    false,
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
    Column("payment_projection_updated_at", DateTime(timezone=True)),
    Column("currency", String(16), nullable=False, server_default=""),
    Column("carrier", Text(), nullable=False, server_default=""),
    Column("has_tracking", Boolean(), nullable=False, server_default="false"),
    Column("has_commission", Boolean(), nullable=False, server_default="false"),
    Column("quantity", Integer(), nullable=False, server_default="1"),
    Column("excluded", Boolean(), nullable=False, server_default="false"),
    Column("catalog_cost", Boolean(), nullable=False, server_default="false"),
    Column("payment_due_at", DateTime(timezone=True)),
    Column("payment_available", Boolean(), nullable=False, server_default=false()),
    Column("payment_date_final", Boolean(), nullable=False, server_default=false()),
    Column("payment_ticket_open", Boolean(), nullable=False, server_default=false()),
    Column("payment_ticket_delay_days", Numeric(18, 2), nullable=False, server_default="0"),
    # Migration-only rollback state for the six provider event fields repaired
    # from archived raw payloads. New rows keep this NULL.
    Column("payment_event_backup_json", Text()),
    *[Column(name, Numeric(38, 8)) for name in (
        "sale_eur", "commission_eur", "payout_eur", "purchase_eur", "profit_eur",
    )],
    UniqueConstraint("seller_id", "account_id", "environment", "order_id", "external_line_id",
                     name="uq_order_line_scope"),
    CheckConstraint(
        "marketplace <> 'kaufland' OR "
        "(payment_projection_updated_at IS NOT NULL "
        "AND payment_projection_updated_at = updated_at)",
        name="ck_order_lines_kaufland_payment_projection_fresh",
    ),
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

payment_tickets = Table(
    "seller_order_payment_tickets", metadata,
    Column("id", Uuid(), primary_key=True),
    Column("organization_id", Uuid(), ForeignKey("organizations.id"), nullable=False),
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), nullable=False),
    Column("account_id", Uuid(), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("external_ticket_id", Text(), nullable=False),
    Column("order_unit_ids_json", Text(), nullable=False, server_default="[]"),
    Column("marketplace_created_at", DateTime(timezone=True)),
    Column("marketplace_updated_at", DateTime(timezone=True)),
    Column("status", String(64), nullable=False, server_default=""),
    Column("synced_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("seller_id", "account_id", "environment", "external_ticket_id",
                     name="uq_order_payment_ticket_scope"),
)
Index("ix_order_payment_tickets_scope_status", payment_tickets.c.organization_id,
      payment_tickets.c.seller_id, payment_tickets.c.account_id,
      payment_tickets.c.environment, payment_tickets.c.status)

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
    # Sparse selection: members are exceptions to this default.
    Column("default_selected", Boolean(), nullable=False, server_default=false()),
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

order_tracking_events = Table(
    "seller_order_tracking_events", metadata,
    Column("id", Uuid(), primary_key=True),
    Column("organization_id", Uuid(), ForeignKey("organizations.id"), nullable=False),
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), nullable=False),
    Column("account_id", Uuid(), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("line_id", Uuid(), ForeignKey("seller_order_lines.id", ondelete="CASCADE"),
           nullable=False),
    Column("actor_id", Uuid(), ForeignKey("auth_users.id"), nullable=False),
    Column("source", String(32), nullable=False),
    Column("previous_carrier", Text(), nullable=False, server_default=""),
    Column("previous_tracking", Text(), nullable=False, server_default=""),
    Column("carrier", Text(), nullable=False, server_default=""),
    Column("tracking", Text(), nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_order_tracking_events_scope_date", order_tracking_events.c.organization_id,
      order_tracking_events.c.seller_id, order_tracking_events.c.account_id,
      order_tracking_events.c.environment, order_tracking_events.c.created_at)
