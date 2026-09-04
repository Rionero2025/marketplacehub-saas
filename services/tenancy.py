from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterable

from services.db import execute, row, rows
from services.shared_cache import cache_get_or_set
from services.user_access import ensure_user_schema

_SCHEMA_READY = False

TENANT_TYPES = {"merchant", "agency"}
MEMBERSHIP_ROLES = {"owner", "admin", "manager", "operator", "viewer"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text[:80] or "tenant"


def ensure_tenancy_schema() -> None:
    """Create the SaaS tenancy model without rewriting legacy business tables.

    v315 makes ``tenant_sellers`` the authoritative ownership boundary. Existing
    business tables keep their seller_id, so all legacy logic continues to work;
    the API resolves tenant -> sellers before any business query is allowed.

    On the first migration only, current users/sellers are placed in one legacy
    Agency tenant. This preserves the current Agency installation while new SaaS
    merchant tenants can be created separately.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    ensure_user_schema()
    execute(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            tenant_type TEXT NOT NULL DEFAULT 'merchant',
            status TEXT NOT NULL DEFAULT 'active',
            plan_code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_memberships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'operator',
            active INTEGER NOT NULL DEFAULT 1,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(tenant_id,user_id)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_sellers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(seller_id)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS agency_clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agency_tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            client_tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(agency_tenant_id,client_tenant_id)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS tenancy_meta (
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT NOT NULL DEFAULT ''
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_tenant_memberships_user ON tenant_memberships(user_id,active,tenant_id)")
    execute("CREATE INDEX IF NOT EXISTS idx_tenant_sellers_tenant ON tenant_sellers(tenant_id,active,seller_id)")
    execute("CREATE INDEX IF NOT EXISTS idx_agency_clients_agency ON agency_clients(agency_tenant_id,active,client_tenant_id)")

    migrated = row("SELECT meta_value FROM tenancy_meta WHERE meta_key='legacy_v315_bootstrap'")
    if not migrated:
        stamp = now_iso()
        legacy = row("SELECT id FROM tenants WHERE slug='marketplace-hub-agency'")
        if legacy:
            agency_id = int(legacy["id"])
        else:
            agency_id = execute(
                """INSERT INTO tenants(name,slug,tenant_type,status,plan_code,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("Marketplace Hub Agency", "marketplace-hub-agency", "agency", "active", "agency", stamp, stamp),
            )
        # Existing installation = Agency baseline. Only rows that existed at the
        # moment of migration are adopted; future SaaS users are never auto-added.
        for user in rows("SELECT id,is_admin FROM app_users ORDER BY id"):
            execute(
                """INSERT INTO tenant_memberships(tenant_id,user_id,role,active,settings_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id,user_id) DO NOTHING""",
                (agency_id, int(user["id"]), "owner" if int(user.get("is_admin") or 0) else "operator", 1, "{}", stamp, stamp),
            )
        for seller in rows(
            """SELECT s.id FROM sellers s
               LEFT JOIN tenant_sellers ts ON ts.seller_id=s.id
               WHERE ts.seller_id IS NULL ORDER BY s.id"""
        ):
            execute(
                """INSERT INTO tenant_sellers(tenant_id,seller_id,active,created_at)
                   VALUES(?,?,?,?) ON CONFLICT(seller_id) DO NOTHING""",
                (agency_id, int(seller["id"]), 1, stamp),
            )
        execute(
            "INSERT INTO tenancy_meta(meta_key,meta_value) VALUES('legacy_v315_bootstrap',?)",
            (json.dumps({"agency_tenant_id": agency_id, "at": stamp}, separators=(",", ":")),),
        )
    _SCHEMA_READY = True


def tenant_record(tenant_id: int) -> dict | None:
    ensure_tenancy_schema()
    return row(
        "SELECT id,name,slug,tenant_type,status,plan_code,created_at,updated_at FROM tenants WHERE id=?",
        (int(tenant_id),),
    )


def list_all_tenants() -> list[dict]:
    ensure_tenancy_schema()
    return rows(
        "SELECT id,name,slug,tenant_type,status,plan_code,created_at,updated_at FROM tenants ORDER BY lower(name),id"
    )


def create_tenant(
    name: str,
    *,
    slug: str = "",
    tenant_type: str = "merchant",
    plan_code: str = "starter",
    owner_user_id: int | None = None,
) -> int:
    ensure_tenancy_schema()
    name = str(name or "").strip()
    if not name:
        raise ValueError("Inserisci il nome dell'azienda/tenant.")
    tenant_type = str(tenant_type or "merchant").strip().lower()
    if tenant_type not in TENANT_TYPES:
        raise ValueError("tenant_type non valido")
    # v318: every new SaaS tenant starts with an explicit commercial plan.
    # Legacy tenants were migrated separately and remain compatibility-safe.
    from services.entitlements import validate_plan_for_tenant_type
    plan_code = validate_plan_for_tenant_type(plan_code, tenant_type)
    base_slug = _slug(slug or name)
    candidate = base_slug
    suffix = 2
    while row("SELECT id FROM tenants WHERE slug=?", (candidate,)):
        candidate = f"{base_slug[:72]}-{suffix}"
        suffix += 1
    stamp = now_iso()
    tenant_id = execute(
        """INSERT INTO tenants(name,slug,tenant_type,status,plan_code,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?)""",
        (name, candidate, tenant_type, "active", str(plan_code or ""), stamp, stamp),
    )
    from services.entitlements import set_tenant_plan
    set_tenant_plan(tenant_id, plan_code, status="manual")
    if owner_user_id:
        add_membership(tenant_id, int(owner_user_id), role="owner")
    return tenant_id


def add_membership(tenant_id: int, user_id: int, *, role: str = "operator", active: bool = True) -> None:
    ensure_tenancy_schema()
    tenant_id, user_id = int(tenant_id), int(user_id)
    if not tenant_record(tenant_id):
        raise ValueError("Tenant non trovato.")
    if not row("SELECT id FROM app_users WHERE id=?", (user_id,)):
        raise ValueError("Utente non trovato.")
    role = str(role or "operator").lower()
    if role not in MEMBERSHIP_ROLES:
        raise ValueError("Ruolo tenant non valido.")
    stamp = now_iso()
    existing = row("SELECT id,active FROM tenant_memberships WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
    if active and (not existing or not int(existing.get("active") or 0)):
        from services.entitlements import assert_resource_capacity
        assert_resource_capacity(tenant_id, "max_users", increment=1)
    if existing:
        execute(
            "UPDATE tenant_memberships SET role=?,active=?,updated_at=? WHERE tenant_id=? AND user_id=?",
            (role, 1 if active else 0, stamp, tenant_id, user_id),
        )
    else:
        execute(
            """INSERT INTO tenant_memberships(tenant_id,user_id,role,active,settings_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (tenant_id, user_id, role, 1 if active else 0, "{}", stamp, stamp),
        )


def attach_seller(tenant_id: int, seller_id: int, *, transfer: bool = False) -> None:
    ensure_tenancy_schema()
    tenant_id, seller_id = int(tenant_id), int(seller_id)
    if not tenant_record(tenant_id):
        raise ValueError("Tenant non trovato.")
    from services.tenant_db import platform_database_scope
    with platform_database_scope():
        seller_exists = row("SELECT id FROM sellers WHERE id=?", (seller_id,))
    if not seller_exists:
        raise ValueError("Seller non trovato.")
    current = row("SELECT tenant_id FROM tenant_sellers WHERE seller_id=?", (seller_id,))
    if not current or int(current.get("tenant_id") or 0) != tenant_id:
        from services.entitlements import assert_resource_capacity
        assert_resource_capacity(tenant_id, "max_sellers", increment=1)
    if current and int(current["tenant_id"]) != tenant_id:
        if not transfer:
            raise ValueError("Il Seller appartiene già a un altro tenant.")
        execute("DELETE FROM tenant_sellers WHERE seller_id=?", (seller_id,))
    if not current or int(current.get("tenant_id") or 0) != tenant_id:
        execute(
            "INSERT INTO tenant_sellers(tenant_id,seller_id,active,created_at) VALUES(?,?,?,?)",
            (tenant_id, seller_id, 1, now_iso()),
        )
    else:
        execute("UPDATE tenant_sellers SET active=1 WHERE seller_id=?", (seller_id,))
    # v316: if this is an explicit transfer, move the copied tenant_id markers in
    # all RLS-protected operational rows atomically from the application view.
    if transfer:
        try:
            from services.tenant_db import reassign_seller_tenant_rows
            reassign_seller_tenant_rows(seller_id, tenant_id)
        except Exception:
            raise


def link_agency_client(agency_tenant_id: int, client_tenant_id: int, *, active: bool = True) -> None:
    ensure_tenancy_schema()
    agency = tenant_record(int(agency_tenant_id))
    client = tenant_record(int(client_tenant_id))
    if not agency or str(agency.get("tenant_type")) != "agency":
        raise ValueError("Il tenant Agency non è valido.")
    if not client or str(client.get("tenant_type")) != "merchant":
        raise ValueError("Il tenant cliente deve essere di tipo merchant.")
    if int(agency_tenant_id) == int(client_tenant_id):
        raise ValueError("Agency e cliente non possono coincidere.")
    existing = row(
        "SELECT id FROM agency_clients WHERE agency_tenant_id=? AND client_tenant_id=?",
        (int(agency_tenant_id), int(client_tenant_id)),
    )
    if existing:
        execute(
            "UPDATE agency_clients SET active=? WHERE agency_tenant_id=? AND client_tenant_id=?",
            (1 if active else 0, int(agency_tenant_id), int(client_tenant_id)),
        )
    else:
        execute(
            "INSERT INTO agency_clients(agency_tenant_id,client_tenant_id,active,created_at) VALUES(?,?,?,?)",
            (int(agency_tenant_id), int(client_tenant_id), 1 if active else 0, now_iso()),
        )


def tenant_seller_ids(tenant_id: int) -> list[int]:
    ensure_tenancy_schema()
    return [
        int(item["seller_id"])
        for item in rows(
            "SELECT seller_id FROM tenant_sellers WHERE tenant_id=? AND active=1 ORDER BY seller_id",
            (int(tenant_id),),
        )
    ]


def accessible_tenants_for_user(user_id: int, *, global_admin: bool = False) -> list[dict]:
    ensure_tenancy_schema()
    user_id = int(user_id)
    if global_admin:
        result = list_all_tenants()
        for item in result:
            item["access_mode"] = "global_admin"
            item["role"] = "platform_admin"
        return result

    direct = rows(
        """SELECT t.id,t.name,t.slug,t.tenant_type,t.status,t.plan_code,m.role
           FROM tenant_memberships m JOIN tenants t ON t.id=m.tenant_id
           WHERE m.user_id=? AND m.active=1 AND t.status='active'
           ORDER BY lower(t.name),t.id""",
        (user_id,),
    )
    by_id: dict[int, dict] = {}
    agency_ids: list[int] = []
    agency_roles: dict[int, str] = {}
    for item in direct:
        tid = int(item["id"])
        item["access_mode"] = "direct"
        by_id[tid] = item
        if str(item.get("tenant_type")) == "agency":
            agency_ids.append(tid)
            agency_roles[tid] = str(item.get("role") or "viewer")

    if agency_ids:
        placeholders = ",".join("?" for _ in agency_ids)
        clients = rows(
            f"""SELECT t.id,t.name,t.slug,t.tenant_type,t.status,t.plan_code,ac.agency_tenant_id
                FROM agency_clients ac JOIN tenants t ON t.id=ac.client_tenant_id
                WHERE ac.agency_tenant_id IN ({placeholders}) AND ac.active=1 AND t.status='active'
                ORDER BY lower(t.name),t.id""",
            tuple(agency_ids),
        )
        role_rank = {"viewer": 0, "operator": 1, "manager": 2, "admin": 3, "owner": 4}
        for item in clients:
            tid = int(item["id"])
            role = agency_roles.get(int(item["agency_tenant_id"]), "viewer")
            if tid in by_id:
                # A direct membership is authoritative. Multiple authorized
                # agencies may contribute access; retain the highest such role.
                if by_id[tid].get("access_mode") == "agency" and role_rank.get(role, 0) > role_rank.get(by_id[tid].get("role"), 0):
                    by_id[tid]["role"] = role
                continue
            item["access_mode"] = "agency"
            item["role"] = role
            by_id[tid] = item
    return list(by_id.values())


def can_user_access_tenant(user_id: int, tenant_id: int, *, global_admin: bool = False) -> bool:
    return int(tenant_id) in {
        int(item["id"]) for item in accessible_tenants_for_user(user_id, global_admin=global_admin)
    }


def default_tenant_id(user_id: int, *, global_admin: bool = False) -> int:
    items = accessible_tenants_for_user(user_id, global_admin=global_admin)
    if not items:
        return 0
    # Prefer a directly-owned Agency context for the current installation.
    items.sort(
        key=lambda item: (
            0 if item.get("access_mode") == "direct" and item.get("tenant_type") == "agency" else 1,
            0 if item.get("access_mode") == "direct" else 1,
            str(item.get("name") or "").lower(),
        )
    )
    return int(items[0]["id"])


def tenant_context_for_user(user_id: int, tenant_id: int, *, global_admin: bool = False) -> dict | None:
    for item in accessible_tenants_for_user(user_id, global_admin=global_admin):
        if int(item["id"]) == int(tenant_id):
            return item
    return None


def effective_seller_ids(
    user_id: int,
    tenant_id: int,
    *,
    global_admin: bool = False,
    legacy_seller_scope: Iterable[int] | None = None,
) -> list[int]:
    """Return seller IDs allowed in the active tenant.

    The old per-user seller selection remains an additional restriction. It can
    never expand the tenant boundary, only narrow it.
    """
    if not can_user_access_tenant(user_id, tenant_id, global_admin=global_admin):
        return []
    owned = set(tenant_seller_ids(tenant_id))
    if legacy_seller_scope is None:
        return sorted(owned)
    allowed = {int(value) for value in legacy_seller_scope if int(value) > 0}
    return sorted(owned & allowed)
