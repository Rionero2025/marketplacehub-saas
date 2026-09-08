"""Add append-only audit events for Seller order tracking changes."""

import sqlalchemy as sa
from alembic import op

revision = "20260908_0008"
down_revision = "20260907_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "seller_order_tracking_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"),
                  nullable=False),
        sa.Column("seller_id", sa.Uuid(), sa.ForeignKey("seller_profiles.id"), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("line_id", sa.Uuid(), sa.ForeignKey(
            "seller_order_lines.id", ondelete="CASCADE",
        ), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("auth_users.id"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("previous_carrier", sa.Text(), nullable=False, server_default=""),
        sa.Column("previous_tracking", sa.Text(), nullable=False, server_default=""),
        sa.Column("carrier", sa.Text(), nullable=False, server_default=""),
        sa.Column("tracking", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_order_tracking_events_scope_date", "seller_order_tracking_events",
        ["organization_id", "seller_id", "account_id", "environment", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_order_tracking_events_scope_date", table_name="seller_order_tracking_events")
    op.drop_table("seller_order_tracking_events")
