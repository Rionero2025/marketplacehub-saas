"""Seller work views, independently persisted from supplier feeds."""

import sqlalchemy as sa
from alembic import op

revision = "20260911_0015"
down_revision = "20260911_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "seller_catalog_views",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("seller_id", sa.Uuid(), sa.ForeignKey("seller_profiles.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("recipe_json", sa.Text(), nullable=False),
        sa.Column("accounts_json", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_catalog_views_scope", "seller_catalog_views", ["organization_id", "seller_id"]
    )
    op.create_table(
        "seller_catalog_view_rows",
        sa.Column(
            "view_id",
            sa.Uuid(),
            sa.ForeignKey("seller_catalog_views.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_catalog_view_rows_position", "seller_catalog_view_rows", ["view_id", "position"]
    )


def downgrade():
    op.drop_table("seller_catalog_view_rows")
    op.drop_table("seller_catalog_views")
