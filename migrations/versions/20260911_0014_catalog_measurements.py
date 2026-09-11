"""Queryable physical measurements; backfill uses a separate bounded command."""
import sqlalchemy as sa

revision = "20260911_0014"
down_revision = "20260911_0013"
branch_labels = None
depends_on = None
FIELDS = ("weight_kg", "length_cm", "width_cm", "height_cm")


def upgrade():
    from alembic import op
    for field in FIELDS:
        op.add_column("seller_price_list_products", sa.Column(field, sa.Numeric(18, 6)))


def downgrade():
    from alembic import op
    with op.batch_alter_table("seller_price_list_products") as batch:
        for field in reversed(FIELDS):
            batch.drop_column(field)
