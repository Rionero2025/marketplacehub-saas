"""Preserve existing SaaS identities when moving to the new authentication tables."""

import logging
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "20260907_0003"
down_revision = "20260907_0002"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    links = op.create_table(
        "auth_legacy_user_links",
        sa.Column("legacy_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False, unique=True),
        sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="CASCADE"),
    )
    if "app_users" not in tables:
        logger.info("Legacy identity migration: no previous user table; clean installation.")
        return

    metadata = sa.MetaData()
    old_users = sa.Table("app_users", metadata, autoload_with=connection)
    users = sa.Table(
        "auth_users", metadata, sa.Column("id", sa.Uuid(), primary_key=True),
        autoload_with=connection,
    )
    realms = sa.Table(
        "auth_user_realms", metadata, sa.Column("user_id", sa.Uuid(), primary_key=True),
        autoload_with=connection,
    )
    required = {"id", "username", "password_hash", "active", "is_admin"}
    if not required.issubset(old_users.c.keys()):
        raise RuntimeError("Unsupported legacy identity schema; no accounts were migrated.")

    active_tenants = {}
    if "tenants" in tables:
        tenants = sa.Table("tenants", metadata, autoload_with=connection)
        active_tenants = {
            row["id"]: row["tenant_type"]
            for row in connection.execute(
                sa.select(tenants.c.id, tenants.c.tenant_type).where(tenants.c.status == "active")
            ).mappings()
        }
    held_tenants: dict[int, set[int]] = {}
    if "tenant_memberships" in tables:
        memberships = sa.Table("tenant_memberships", metadata, autoload_with=connection)
        for row in connection.execute(
            sa.select(memberships.c.user_id, memberships.c.tenant_id)
            .where(memberships.c.active == 1)
        ).mappings():
            if row["tenant_id"] in active_tenants:
                held_tenants.setdefault(row["user_id"], set()).add(row["tenant_id"])
    agency_clients: dict[int, set[int]] = {}
    if "agency_clients" in tables:
        clients = sa.Table("agency_clients", metadata, autoload_with=connection)
        for row in connection.execute(
            sa.select(clients.c.agency_tenant_id, clients.c.client_tenant_id)
            .where(clients.c.active == 1)
        ).mappings():
            agency, client = row["agency_tenant_id"], row["client_tenant_id"]
            if active_tenants.get(agency) == "agency" and active_tenants.get(client) == "merchant":
                agency_clients.setdefault(agency, set()).add(client)

    existing = {
        row["login"]: row["id"]
        for row in connection.execute(sa.select(users.c.id, users.c.login)).mappings()
    }
    seen = set()
    pending_users, pending_realms, pending_links = [], [], []
    now = datetime.now(UTC)
    for row in connection.execute(sa.select(old_users).order_by(old_users.c.id)).mappings():
        login = str(row["username"]).strip().casefold()
        identity = uuid5(NAMESPACE_URL, f"https://marketplacehub.internal/legacy/app_users/{row['id']}")
        if not login or len(login) > 254 or login in seen:
            raise RuntimeError(
                "Legacy login collision or invalid login; no accounts were migrated."
            )
        if login in existing and existing[login] != identity:
            raise RuntimeError(
                "Legacy login conflicts with an existing account; migration stopped."
            )
        seen.add(login)
        pending_links.append({"legacy_user_id": row["id"], "user_id": identity})
        if login in existing:
            # A downgrade/re-upgrade must retain credentials and permissions already updated.
            continue
        password_hash = str(row["password_hash"])
        if len(password_hash) > 255:
            raise RuntimeError("Unsupported legacy credential format; no accounts were migrated.")
        pending_users.append({
            "id": identity,
            "login": login,
            "display_name": str(row.get("display_name") or row["username"])[:160],
            "password_hash": password_hash,
            "active": row["active"] == 1,
            "created_at": now,
            "updated_at": now,
        })

        # Match the previous auth portal rules, including agency-managed Seller access.
        allowed_realms = set()
        if row["is_admin"] == 1:
            allowed_realms.add("platform")
            accessible = set(active_tenants)
        else:
            accessible = set(held_tenants.get(row["id"], set()))
            for tenant_id in tuple(accessible):
                accessible.update(agency_clients.get(tenant_id, set()))
        for tenant_id in accessible:
            tenant_type = active_tenants[tenant_id]
            if tenant_type == "merchant":
                allowed_realms.add("seller")
            elif tenant_type == "agency":
                allowed_realms.add("agency")
        pending_realms.extend(
            {"user_id": identity, "realm": realm} for realm in sorted(allowed_realms)
        )

    # All identity conflicts are checked before inserting anything.
    if pending_users:
        connection.execute(sa.insert(users), pending_users)
    if pending_realms:
        connection.execute(sa.insert(realms), pending_realms)
    if pending_links:
        connection.execute(sa.insert(links), pending_links)
    logger.info(
        "Legacy identity migration: %d users imported, %d existing identities retained, "
        "%d portal authorizations restored.",
        len(pending_users), len(pending_links) - len(pending_users), len(pending_realms),
    )


def downgrade() -> None:
    # Never delete user accounts or revert passwords after a successful login.
    op.drop_table("auth_legacy_user_links")
