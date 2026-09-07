from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)

from marketplace_hub_core.tenancy.schema import metadata

seller_commercial_settings = Table(
    "seller_commercial_settings",
    metadata,
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), primary_key=True),
    Column("our_profit_pct", Float(), nullable=False, default=0.0),
    Column("partner_profit_pct", Float(), nullable=False, default=100.0),
)

seller_marketplace_accounts = Table(
    "seller_marketplace_accounts",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), nullable=False, index=True),
    Column("organization_id", Uuid(), ForeignKey("organizations.id"), nullable=False, index=True),
    Column("marketplace", String(32), nullable=False),
    Column("account_name", Text(), nullable=False),
    Column("credentials_encrypted", Text(), nullable=False),
    Column("settings_json", Text(), nullable=False, default="{}"),
    Column("active", Boolean(), nullable=False, default=True),
    Column("legacy_account_id", BigInteger(), unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("seller_id", "marketplace", "account_name"),
)

SELLER_SETTINGS_TABLES = [seller_commercial_settings, seller_marketplace_accounts]
