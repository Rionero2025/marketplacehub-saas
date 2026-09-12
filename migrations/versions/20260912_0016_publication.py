"""Durable reviewed publication jobs and offer receipts."""

import sqlalchemy as sa
from alembic import op

revision = "20260912_0016"
down_revision = "20260911_0015"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "seller_publication_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("seller_id", sa.Uuid(), sa.ForeignKey("seller_profiles.id"), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("realm", sa.String(20), nullable=False),
        sa.Column("view_id", sa.Uuid(), nullable=False),
        sa.Column("view_name", sa.String(200), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=False),
        sa.Column("credentials_digest", sa.String(64), nullable=False),
        sa.Column("marketplace", sa.String(30), nullable=False),
        sa.Column("rules_json", sa.Text(), nullable=False),
        sa.Column("execution_token", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("filtered_total", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "seller_publication_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("seller_publication_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("public_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("result_code", sa.String(100), nullable=False),
    )
    op.create_index(
        "ix_publication_scope",
        "seller_publication_jobs",
        ["organization_id", "seller_id", "created_at"],
    )
    op.create_index("ix_publication_items", "seller_publication_items", ["job_id", "position"])
    condition = sa.text("status IN ('queued', 'running')")
    op.create_index(
        "uq_publication_active_account",
        "seller_publication_jobs",
        ["account_id"],
        unique=True,
        sqlite_where=condition,
        postgresql_where=condition,
    )


def downgrade():
    op.drop_table("seller_publication_items")
    op.drop_table("seller_publication_jobs")
