"""Import Seller profit settings and Kaufland accounts without changing legacy data."""

import logging
import math
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "20260907_0005"
down_revision = "20260907_0004"
branch_labels = None
depends_on = None
logger = logging.getLogger("alembic.runtime.migration")


def _schema():
    metadata = sa.MetaData()
    sa.Table("seller_profiles", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table("organizations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    commercial = sa.Table(
        "seller_commercial_settings",
        metadata,
        sa.Column("seller_id", sa.Uuid(), sa.ForeignKey("seller_profiles.id"), primary_key=True),
        sa.Column("our_profit_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("partner_profit_pct", sa.Float(), nullable=False, server_default="100"),
    )
    accounts = sa.Table(
        "seller_marketplace_accounts",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "seller_id", sa.Uuid(), sa.ForeignKey("seller_profiles.id"), nullable=False, index=True
        ),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("marketplace", sa.String(32), nullable=False),
        sa.Column("account_name", sa.Text(), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("legacy_account_id", sa.BigInteger(), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("seller_id", "marketplace", "account_name"),
    )
    return commercial, accounts


def _split(our, partner):
    # Frozen original services/profit_sharing.py read rules (not creation defaults).
    def clamp(value, fallback):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = fallback
        if not math.isfinite(number):
            number = fallback
        return round(min(100.0, max(0.0, number)), 4)

    our, partner = clamp(our, 0.0), clamp(partner, 100.0)
    if abs(our + partner - 100.0) > 0.01:
        partner = round(100.0 - our, 4)
    return our, partner


def _read_legacy(connection, name, existing):
    if name not in existing:
        return []
    table = sa.Table(name, sa.MetaData(), autoload_with=connection)
    if connection.dialect.name != "postgresql":
        return list(connection.execute(sa.select(table)).mappings())
    # Authorized one-time migration reads the old RLS tables with the existing
    # platform migration scope; restore it in the same transaction even on error.
    previous = connection.scalar(
        sa.text("SELECT current_setting('marketplace_hub.rls_bypass', true)")
    )
    connection.execute(sa.text("SELECT set_config('marketplace_hub.rls_bypass', '1', true)"))
    try:
        return list(connection.execute(sa.select(table)).mappings())
    finally:
        connection.execute(
            sa.text("SELECT set_config('marketplace_hub.rls_bypass', :previous, true)"),
            {"previous": previous or ""},
        )


def _created_at(value, fallback):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return fallback


def upgrade():
    connection = op.get_bind()
    existing = set(sa.inspect(connection).get_table_names())
    commercial, accounts = _schema()
    commercial.create(connection)
    accounts.create(connection)
    profiles = sa.Table(
        "seller_profiles",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid()),
        autoload_with=connection,
    )
    profile_rows = list(connection.execute(sa.select(profiles)).mappings())
    by_legacy = {
        p["legacy_seller_id"]: p for p in profile_rows if p["legacy_seller_id"] is not None
    }
    legacy_sellers = {r["id"]: r for r in _read_legacy(connection, "sellers", existing)}
    for profile in profile_rows:
        source = legacy_sellers.get(profile["legacy_seller_id"], {})
        our, partner = _split(source.get("our_profit_pct"), source.get("partner_profit_pct"))
        connection.execute(
            commercial.insert().values(
                seller_id=profile["id"],
                our_profit_pct=our,
                partner_profit_pct=partner,
            )
        )
    now = datetime.now(UTC)
    imported = skipped = deferred = 0
    names = set()
    for source in _read_legacy(connection, "marketplace_accounts", existing):
        if str(source.get("marketplace") or "").casefold().strip() != "kaufland":
            deferred += 1
            continue
        profile = by_legacy.get(source.get("seller_id"))
        if profile is None:
            skipped += 1
            continue
        name = str(source.get("account_name") or "")
        identity = (profile["id"], name)
        if identity in names:
            # Ambiguous legacy duplicates must not disappear silently.
            raise RuntimeError(
                "Duplicate Kaufland account names in legacy data; migration aborted."
            )
        names.add(identity)
        connection.execute(
            accounts.insert().values(
                id=uuid5(
                    NAMESPACE_URL,
                    f"https://marketplacehub.internal/legacy/marketplace_accounts/{source['id']}",
                ),
                seller_id=profile["id"],
                organization_id=profile["organization_id"],
                marketplace="kaufland",
                account_name=name,
                credentials_encrypted=str(source.get("credentials_encrypted") or ""),
                settings_json=str(source.get("settings_json") or "{}"),
                active=str(source.get("active", 1)).lower() not in {"0", "false", "none", ""},
                legacy_account_id=source["id"],
                created_at=_created_at(source.get("created_at"), now),
                updated_at=now,
            )
        )
        imported += 1
    logger.info(
        "B10.1 import: %s Seller settings, %s Kaufland accounts; "
        "%s unassigned skipped, %s other-channel accounts deferred",
        len(profile_rows),
        imported,
        skipped,
        deferred,
    )


def downgrade():
    commercial, accounts = _schema()
    accounts.drop(op.get_bind())
    commercial.drop(op.get_bind())
