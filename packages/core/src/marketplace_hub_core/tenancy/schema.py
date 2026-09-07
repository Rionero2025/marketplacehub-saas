from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)

from marketplace_hub_core.auth.schema import metadata

# Reuse auth metadata so all foreign keys resolve in isolated repository tests.
organizations = Table(
    "organizations",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("name", Text(), nullable=False),
    Column("kind", String(20), nullable=False),
    Column("active", Boolean(), nullable=False),
    Column("legacy_tenant_id", BigInteger(), unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("id", "kind"),
    CheckConstraint("kind IN ('PLATFORM', 'AGENCY', 'SELLER')", name="ck_organization_kind"),
)
organization_relationships = Table(
    "organization_relationships",
    metadata,
    Column("parent_id", Uuid(), primary_key=True),
    Column("child_id", Uuid(), primary_key=True),
    Column("parent_kind", String(20), nullable=False),
    Column("child_kind", String(20), nullable=False),
    Column("active", Boolean(), nullable=False),
    ForeignKeyConstraint(["parent_id", "parent_kind"], ["organizations.id", "organizations.kind"]),
    ForeignKeyConstraint(["child_id", "child_kind"], ["organizations.id", "organizations.kind"]),
    CheckConstraint("parent_id <> child_id", name="ck_relationship_no_self"),
    CheckConstraint(
        "(parent_kind = 'PLATFORM' AND child_kind IN ('AGENCY', 'SELLER')) OR "
        "(parent_kind = 'AGENCY' AND child_kind = 'SELLER')",
        name="ck_relationship_direction",
    ),
)
roles = Table(
    "roles",
    metadata,
    Column("code", String(32), primary_key=True),
    Column("organization_kind", String(20), nullable=False),
    Column("label", String(80), nullable=False),
    UniqueConstraint("code", "organization_kind"),
)
permissions = Table(
    "permissions",
    metadata,
    Column("code", String(32), primary_key=True),
    Column("label", String(80), nullable=False),
)
memberships = Table(
    "memberships",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("user_id", Uuid(), ForeignKey("auth_users.id"), nullable=False, index=True),
    Column("organization_id", Uuid(), nullable=False, index=True),
    Column("organization_kind", String(20), nullable=False),
    Column("role_code", String(32), nullable=False),
    Column("active", Boolean(), nullable=False),
    Column("all_sellers", Boolean(), nullable=False, default=False),
    Column("read_only", Boolean(), nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("user_id", "organization_id"),
    ForeignKeyConstraint(
        ["organization_id", "organization_kind"], ["organizations.id", "organizations.kind"]
    ),
    ForeignKeyConstraint(
        ["role_code", "organization_kind"], ["roles.code", "roles.organization_kind"]
    ),
)
membership_permissions = Table(
    "membership_permissions",
    metadata,
    Column("membership_id", Uuid(), ForeignKey("memberships.id"), primary_key=True),
    Column("permission_code", String(32), ForeignKey("permissions.code"), primary_key=True),
)
seller_profiles = Table(
    "seller_profiles",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("organization_id", Uuid(), nullable=False, index=True),
    Column("organization_kind", String(20), nullable=False, default="SELLER"),
    Column("name", Text(), nullable=False),
    Column("legal_name", Text(), nullable=False, default=""),
    Column("email", Text(), nullable=False, default=""),
    Column("active", Boolean(), nullable=False),
    Column("legacy_seller_id", BigInteger(), unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["organization_id", "organization_kind"], ["organizations.id", "organizations.kind"]
    ),
    CheckConstraint("organization_kind = 'SELLER'", name="ck_profile_seller_organization"),
)
membership_seller_access = Table(
    "membership_seller_access",
    metadata,
    Column("membership_id", Uuid(), ForeignKey("memberships.id"), primary_key=True),
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), primary_key=True),
)
workspace_selections = Table(
    "workspace_selections",
    metadata,
    Column(
        "session_id", Uuid(), ForeignKey("auth_sessions.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("seller_id", Uuid(), ForeignKey("seller_profiles.id"), nullable=False),
)

TENANCY_TABLES = [
    organizations,
    organization_relationships,
    roles,
    permissions,
    memberships,
    membership_permissions,
    seller_profiles,
    membership_seller_access,
    workspace_selections,
]


def migration_metadata() -> MetaData:
    """Kept for tools that need a full, resolvable schema graph."""
    return metadata
