"""Restore legacy organization, membership and Seller ownership boundaries."""

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "20260907_0004"
down_revision = "20260907_0003"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")
PLATFORM_ID = uuid5(NAMESPACE_URL, "https://marketplacehub.internal/platform")
# Frozen here: replaying this migration must not depend on future application code.
ROLE_LABELS = {
    "PLATFORM_OWNER": "Proprietario piattaforma",
    "PLATFORM_ADMIN": "Amministratore piattaforma",
    "PLATFORM_SUPPORT": "Assistenza piattaforma",
    "AGENCY_OWNER": "Proprietario agenzia",
    "AGENCY_ADMIN": "Amministratore agenzia",
    "AGENCY_USER": "Collaboratore agenzia",
    "SELLER_OWNER": "Proprietario Seller",
    "SELLER_ADMIN": "Amministratore Seller",
    "SELLER_USER": "Collaboratore Seller",
}
PERMISSION_LABELS = {
    "ACCOUNTING": "Contabilità", "LOGISTICS": "Logistica", "CATALOG": "Catalogo",
    "CUSTOMER_SERVICE": "Assistenza clienti", "MARKETING": "Marketing",
    "VIEW_ONLY": "Sola lettura", "WORKSPACE_VIEW": "Consulta il negozio",
    "WORKSPACE_MANAGE": "Modifica anagrafica negozio",
}
LEGACY_PERMISSIONS = {
    "accounting": "ACCOUNTING", "shipping": "LOGISTICS", "tracking": "LOGISTICS",
    "marketplace_orders": "LOGISTICS", "packlink": "LOGISTICS",
    "cecotec_orders": "LOGISTICS", "innpro_orders": "LOGISTICS",
    "suppliers_lists": "CATALOG", "work_lists": "CATALOG", "product_creation": "CATALOG",
    "marketplace_publication": "CATALOG", "buybox": "CATALOG",
    "support": "CUSTOMER_SERVICE", "top_products": "MARKETING",
    "seller_management": "WORKSPACE_MANAGE",
}
TABLE_NAMES = (
    "organizations", "organization_relationships", "roles", "permissions", "memberships",
    "membership_permissions", "seller_profiles", "membership_seller_access",
    "workspace_selections",
)


def _schema():
    metadata = sa.MetaData()
    # Foreign-key targets already exist, and are deliberately excluded from create/drop.
    sa.Table("auth_users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table("auth_sessions", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        "organizations", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("legacy_tenant_id", sa.BigInteger(), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "kind"),
        sa.CheckConstraint(
            "kind IN ('PLATFORM', 'AGENCY', 'SELLER')", name="ck_organization_kind"
        ),
    )
    sa.Table(
        "organization_relationships", metadata,
        sa.Column("parent_id", sa.Uuid(), primary_key=True),
        sa.Column("child_id", sa.Uuid(), primary_key=True),
        sa.Column("parent_kind", sa.String(20), nullable=False),
        sa.Column("child_kind", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id", "parent_kind"], ["organizations.id", "organizations.kind"]
        ),
        sa.ForeignKeyConstraint(
            ["child_id", "child_kind"], ["organizations.id", "organizations.kind"]
        ),
        sa.CheckConstraint("parent_id <> child_id", name="ck_relationship_no_self"),
        sa.CheckConstraint(
            "(parent_kind = 'PLATFORM' AND child_kind IN ('AGENCY', 'SELLER')) OR "
            "(parent_kind = 'AGENCY' AND child_kind = 'SELLER')",
            name="ck_relationship_direction",
        ),
    )
    sa.Table(
        "roles", metadata,
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("organization_kind", sa.String(20), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.UniqueConstraint("code", "organization_kind"),
    )
    sa.Table(
        "permissions", metadata,
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("label", sa.String(80), nullable=False),
    )
    sa.Table(
        "memberships", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("auth_users.id"),
                  nullable=False, index=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("organization_kind", sa.String(20), nullable=False),
        sa.Column("role_code", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("all_sellers", sa.Boolean(), nullable=False, default=False),
        sa.Column("read_only", sa.Boolean(), nullable=False, default=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "organization_id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "organization_kind"], ["organizations.id", "organizations.kind"]
        ),
        sa.ForeignKeyConstraint(
            ["role_code", "organization_kind"], ["roles.code", "roles.organization_kind"]
        ),
    )
    sa.Table(
        "membership_permissions", metadata,
        sa.Column("membership_id", sa.Uuid(), sa.ForeignKey("memberships.id"), primary_key=True),
        sa.Column("permission_code", sa.String(32), sa.ForeignKey("permissions.code"),
                  primary_key=True),
    )
    sa.Table(
        "seller_profiles", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("organization_kind", sa.String(20), nullable=False, default="SELLER"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=False, default=""),
        sa.Column("email", sa.Text(), nullable=False, default=""),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("legacy_seller_id", sa.BigInteger(), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "organization_kind"], ["organizations.id", "organizations.kind"]
        ),
        sa.CheckConstraint("organization_kind = 'SELLER'", name="ck_profile_seller_organization"),
    )
    sa.Table(
        "membership_seller_access", metadata,
        sa.Column("membership_id", sa.Uuid(), sa.ForeignKey("memberships.id"), primary_key=True),
        sa.Column("seller_id", sa.Uuid(), sa.ForeignKey("seller_profiles.id"), primary_key=True),
    )
    sa.Table(
        "workspace_selections", metadata,
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("seller_id", sa.Uuid(), sa.ForeignKey("seller_profiles.id"), nullable=False),
    )
    return {name: metadata.tables[name] for name in TABLE_NAMES}


def _identity(path):
    return uuid5(NAMESPACE_URL, f"https://marketplacehub.internal/{path}")


def _selected_sellers(user):
    if user.get("is_admin") == 1:
        return None
    raw = user.get("seller_ids_json")
    if raw in (None, "", "null"):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if parsed is None:
        return None
    if not isinstance(parsed, list):
        return set()
    result = set()
    for value in parsed:
        try:
            seller_id = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if seller_id > 0:
            result.add(seller_id)
    return result


def _permissions(user, read_only):
    result = {"WORKSPACE_VIEW"}
    if user.get("is_admin") == 1:
        result.update(set(PERMISSION_LABELS) - {"VIEW_ONLY"})
    else:
        try:
            parsed = json.loads(user.get("permissions_json") or "[]")
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            result.update(
                LEGACY_PERMISSIONS[str(code)] for code in parsed
                if str(code) in LEGACY_PERMISSIONS
            )
    if read_only:
        result.add("VIEW_ONLY")
    return result


def _legacy_rows(connection, name, existing):
    if name not in existing:
        return []
    metadata = sa.MetaData()
    extra = [sa.Column("user_id", sa.Uuid())] if name == "auth_legacy_user_links" else []
    table = sa.Table(name, metadata, *extra, autoload_with=connection)
    if name != "sellers" or connection.dialect.name != "postgresql":
        return list(connection.execute(sa.select(table)).mappings())
    # The existing business tables have RLS. Only this read uses the old platform
    # scope, transaction-locally; no legacy business row is inserted or updated.
    previous = connection.scalar(
        sa.text("SELECT current_setting('marketplace_hub.rls_bypass', true)")
    )
    connection.execute(
        sa.text("SELECT set_config('marketplace_hub.rls_bypass', '1', true)")
    )
    try:
        return list(connection.execute(sa.select(table)).mappings())
    finally:
        connection.execute(
            sa.text("SELECT set_config('marketplace_hub.rls_bypass', :previous, true)"),
            {"previous": previous or ""},
        )


def upgrade() -> None:
    connection = op.get_bind()
    existing = set(sa.inspect(connection).get_table_names())
    schema = _schema()
    for table in schema.values():
        table.create(connection)
    connection.execute(schema["roles"].insert(), [
        {"code": code, "organization_kind": code.split("_")[0], "label": label}
        for code, label in ROLE_LABELS.items()
    ])
    connection.execute(schema["permissions"].insert(), [
        {"code": code, "label": label} for code, label in PERMISSION_LABELS.items()
    ])
    now = datetime.now(UTC)
    organizations = [{
        "id": PLATFORM_ID, "name": "Marketplace Hub", "kind": "PLATFORM", "active": True,
        "legacy_tenant_id": None, "created_at": now,
    }]
    tenants = {}
    skipped_tenants = 0
    for tenant in _legacy_rows(connection, "tenants", existing):
        kind = {"merchant": "SELLER", "agency": "AGENCY"}.get(tenant["tenant_type"])
        if kind is None:
            skipped_tenants += 1
            continue
        organization = {
            "id": _identity(f"legacy/tenants/{tenant['id']}"),
            "name": str(tenant.get("name") or f"Workspace {tenant['id']}"),
            "kind": kind, "active": tenant["status"] == "active",
            "legacy_tenant_id": tenant["id"], "created_at": now,
        }
        tenants[tenant["id"]] = organization
        organizations.append(organization)

    relationships = {}

    def link(parent, child, active):
        relationships[(parent["id"], child["id"])] = {
            "parent_id": parent["id"], "child_id": child["id"],
            "parent_kind": parent["kind"], "child_kind": child["kind"], "active": active,
        }

    for client in _legacy_rows(connection, "agency_clients", existing):
        parent = tenants.get(client["agency_tenant_id"])
        child = tenants.get(client["client_tenant_id"])
        if parent and child and parent["kind"] == "AGENCY" and child["kind"] == "SELLER":
            link(parent, child, client["active"] == 1)

    ownership = defaultdict(list)
    for item in _legacy_rows(connection, "tenant_sellers", existing):
        ownership[item["seller_id"]].append(item)
    profiles = {}
    skipped_sellers = 0
    for seller in _legacy_rows(connection, "sellers", existing):
        owners = ownership[seller["id"]]
        if len(owners) != 1 or owners[0]["tenant_id"] not in tenants:
            skipped_sellers += 1
            continue  # Never adopt an orphan, unknown or ambiguously owned Seller.
        owner = owners[0]
        organization = tenants[owner["tenant_id"]]
        if organization["kind"] == "AGENCY":
            child = {
                "id": _identity(f"legacy/agency/{owner['tenant_id']}/seller/{seller['id']}"),
                "name": str(seller["name"]), "kind": "SELLER",
                "active": organization["active"], "legacy_tenant_id": None, "created_at": now,
            }
            organizations.append(child)
            link(organization, child, owner["active"] == 1)
            organization = child
        profiles[seller["id"]] = {
            "id": _identity(f"legacy/sellers/{seller['id']}"),
            "organization_id": organization["id"], "organization_kind": "SELLER",
            "name": str(seller["name"]),
            "legal_name": str(seller.get("legal_name") or ""),
            "email": str(seller.get("email") or ""),
            "active": seller.get("active", 1) == 1 and owner["active"] == 1,
            "legacy_seller_id": seller["id"], "created_at": now, "updated_at": now,
        }
    for organization in organizations[1:]:
        link(organizations[0], organization, True)
    connection.execute(schema["organizations"].insert(), organizations)
    if relationships:
        connection.execute(schema["organization_relationships"].insert(),
                           list(relationships.values()))
    if profiles:
        connection.execute(schema["seller_profiles"].insert(), list(profiles.values()))

    users = {u["id"]: u for u in _legacy_rows(connection, "app_users", existing)}
    user_links = {
        item["legacy_user_id"]: item["user_id"]
        for item in _legacy_rows(connection, "auth_legacy_user_links", existing)
    }
    members, grants, selections = [], [], []

    def add_member(user_id, organization, role, active, source):
        identity = _identity(f"memberships/{user_id}/{organization['id']}")
        read_only = source.get("is_admin") != 1 and role not in {
            "owner", "admin", "manager", "operator", "platform_owner",
        }
        role_suffix = {"owner": "OWNER", "admin": "ADMIN", "platform_owner": "OWNER"}.get(
            role, "USER"
        )
        chosen = _selected_sellers(source)
        member = {
            "id": identity, "user_id": user_id, "organization_id": organization["id"],
            "organization_kind": organization["kind"],
            "role_code": f"{organization['kind']}_{role_suffix}",
            "active": active, "all_sellers": chosen is None, "read_only": read_only,
            "created_at": now,
        }
        members.append(member)
        grants.extend({"membership_id": identity, "permission_code": code}
                      for code in sorted(_permissions(source, read_only)))
        if chosen is not None:
            permitted_orgs = {organization["id"]}
            permitted_orgs.update(
                relation["child_id"] for relation in relationships.values()
                if relation["parent_id"] == organization["id"] and relation["active"]
            )
            selections.extend({"membership_id": identity, "seller_id": profiles[seller_id]["id"]}
                              for seller_id in sorted(chosen) if seller_id in profiles
                              and profiles[seller_id]["organization_id"] in permitted_orgs)

    skipped_memberships = 0
    for member in _legacy_rows(connection, "tenant_memberships", existing):
        user_id = user_links.get(member["user_id"])
        organization = tenants.get(member["tenant_id"])
        source = users.get(member["user_id"])
        if not user_id or not organization or source is None:
            skipped_memberships += 1
            continue
        add_member(user_id, organization, str(member["role"]), member["active"] == 1, source)
    for old_id, user_id in user_links.items():
        source = users.get(old_id)
        if source and source.get("is_admin") == 1:
            add_member(user_id, organizations[0], "admin", source.get("active") == 1, source)

    # Only explicit Platform bootstrap identities get a clean-install membership.
    # A Seller/Agency login realm alone is never a tenant ownership assignment.
    auth_metadata = sa.MetaData()
    auth_users = sa.Table("auth_users", auth_metadata,
                         sa.Column("id", sa.Uuid(), primary_key=True), autoload_with=connection)
    realms = sa.Table("auth_user_realms", auth_metadata,
                      sa.Column("user_id", sa.Uuid(), primary_key=True), autoload_with=connection)
    legacy_identities = set(user_links.values())
    for user in connection.execute(
        sa.select(auth_users.c.id, auth_users.c.active).join(
            realms, realms.c.user_id == auth_users.c.id
        ).where(realms.c.realm == "platform")
    ).mappings():
        if user["id"] not in legacy_identities:
            add_member(user["id"], organizations[0], "platform_owner", user["active"],
                       {"is_admin": 1})
    if members:
        connection.execute(schema["memberships"].insert(), members)
    if grants:
        connection.execute(schema["membership_permissions"].insert(), grants)
    if selections:
        connection.execute(schema["membership_seller_access"].insert(), selections)
    logger.info(
        "Organization migration: %d organizations, %d relationships, %d Seller profiles, "
        "%d memberships, %d permission grants and %d explicit Seller scopes imported; "
        "%d unsupported tenants, %d unowned/ambiguous Sellers and %d unlinked memberships skipped.",
        len(organizations), len(relationships), len(profiles), len(members), len(grants),
        len(selections), skipped_tenants, skipped_sellers, skipped_memberships,
    )


def downgrade() -> None:
    for name in reversed(TABLE_NAMES):
        op.drop_table(name)
