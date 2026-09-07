"""Order filter projections and session-scoped selection; preserve source records."""

import json
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op

revision = "20260907_0007"
down_revision = "20260907_0006"
branch_labels = None
depends_on = None


def _number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and abs(result) < Decimal("1e30") else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _project(item):
    details = item.get("details") or {}
    return {
        "currency": str(item.get("currency") or "").strip(),
        "carrier": str(details.get("carrier") or "").strip(),
        "has_tracking": bool(str(details.get("tracking") or "").strip()),
        "has_commission": _number(item.get("commission_amount")) is not None,
        "sale_eur": _number(item.get("sale_amount_eur")),
        "commission_eur": _number(item.get("commission_amount_eur")),
        "payout_eur": _number(item.get("payout_amount_eur")),
        "purchase_eur": _number(item.get("purchase_cost_eur", item.get("purchase_cost"))),
        "profit_eur": _number(item.get("profit_amount_eur", item.get("profit_amount"))),
        "quantity": max(1, int(_number(item.get("quantity")) or 1)),
        "excluded": str(item.get("status") or "").casefold() in {"cancelled", "canceled"}
        or bool(details.get("excluded_from_totals")),
        "catalog_cost": str(item.get("purchase_cost_source") or "").startswith(
            "Listino pubblicato"),
    }


def upgrade():
    columns = [
        sa.Column("projection_updated_at", sa.DateTime(timezone=True)),
        sa.Column("currency", sa.String(16), nullable=False, server_default=""),
        sa.Column("carrier", sa.Text(), nullable=False, server_default=""),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        *[sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()) for name in (
            "has_tracking", "has_commission", "excluded", "catalog_cost",
        )],
        *[sa.Column(name, sa.Numeric(38, 8)) for name in (
            "sale_eur", "commission_eur", "payout_eur", "purchase_eur", "profit_eur",
        )],
    ]
    for column in columns:
        op.add_column("seller_order_lines", column)
    connection = op.get_bind()
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=connection)
    last = None
    while True:
        query = sa.select(lines.c.id, lines.c.canonical_json, lines.c.updated_at)
        if last is not None:
            query = query.where(lines.c.id > last)
        rows = connection.execute(query.order_by(lines.c.id).limit(100)).mappings().all()
        if not rows:
            break
        for row in rows:
            connection.execute(lines.update().where(lines.c.id == row["id"]).values(
                **_project(json.loads(row["canonical_json"])),
                projection_updated_at=row["updated_at"],
            ))
        last = rows[-1]["id"]
    op.create_table(
        "seller_order_selections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("seller_id", sa.Uuid(), sa.ForeignKey("seller_profiles.id"), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("filter_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "seller_id", "account_id", "environment", "filter_hash",
                            name="uq_order_selection_filter"),
    )
    op.create_table(
        "seller_order_selection_members",
        sa.Column("selection_id", sa.Uuid(),
                  sa.ForeignKey("seller_order_selections.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("line_id", sa.Uuid(), sa.ForeignKey("seller_order_lines.id", ondelete="CASCADE"),
                  primary_key=True),
    )


def downgrade():
    op.drop_table("seller_order_selection_members")
    op.drop_table("seller_order_selections")
    for name in ("projection_updated_at", "currency", "carrier", "quantity", "has_tracking",
                 "has_commission", "excluded", "catalog_cost", "sale_eur", "commission_eur",
                 "payout_eur", "purchase_eur", "profit_eur"):
        op.drop_column("seller_order_lines", name)
