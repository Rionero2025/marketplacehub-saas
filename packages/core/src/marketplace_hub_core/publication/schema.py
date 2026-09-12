from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Table, Text, Uuid

from marketplace_hub_core.auth.schema import metadata

jobs = Table(
    "seller_publication_jobs",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("organization_id", Uuid(), ForeignKey("organizations.id"), nullable=False),
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), nullable=False),
    Column("account_id", Uuid(), nullable=False),
    Column("requested_by", Uuid(), nullable=False),
    Column("realm", String(20), nullable=False),
    Column("view_id", Uuid(), nullable=False),
    Column("view_name", String(200), nullable=False),
    Column("account_name", String(200), nullable=False),
    Column("credentials_digest", String(64), nullable=False),
    Column("marketplace", String(30), nullable=False),
    Column("rules_json", Text(), nullable=False),
    Column("execution_token", Uuid(), nullable=True),
    Column("status", String(30), nullable=False),
    Column("total", Integer(), nullable=False),
    Column("filtered_total", Integer(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
items = Table(
    "seller_publication_items",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "job_id",
        Uuid(),
        ForeignKey("seller_publication_jobs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("position", Integer(), nullable=False),
    Column("public_json", Text(), nullable=False),
    Column("payload_json", Text(), nullable=False),
    Column("status", String(30), nullable=False),
    Column("result_code", String(100), nullable=False),
)
Index("ix_publication_scope", jobs.c.organization_id, jobs.c.seller_id, jobs.c.created_at)
Index("ix_publication_items", items.c.job_id, items.c.position)
active = jobs.c.status.in_(["queued", "running"])
Index(
    "uq_publication_active_account",
    jobs.c.account_id,
    unique=True,
    sqlite_where=active,
    postgresql_where=active,
)
