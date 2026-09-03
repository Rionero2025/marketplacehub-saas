from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from services.db import connect, execute, now_iso, row, rows
from services.tenancy import (
    accessible_tenants_for_user,
    default_tenant_id,
    effective_seller_ids,
    ensure_tenancy_schema,
    tenant_context_for_user,
)
from services.user_access import get_user, permissions_from_record, seller_ids_from_record
from services.database_config import database_engine
from services.tenant_db import platform_database_scope

_SCHEMA_READY = False


def _now() -> int:
    return int(time.time())


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _ttl_seconds(remember: bool) -> int:
    if remember:
        days = max(1, int(os.getenv("MARKETPLACE_HUB_API_REMEMBER_DAYS", "30")))
        return days * 24 * 60 * 60
    hours = max(1, int(os.getenv("MARKETPLACE_HUB_API_SESSION_HOURS", "12")))
    return hours * 60 * 60


@dataclass(frozen=True, slots=True)
class ApiSession:
    token: str
    user_id: int
    expires_at: int
    active_tenant_id: int = 0


def ensure_api_session_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    ensure_tenancy_schema()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at_epoch INTEGER NOT NULL,
                expires_at_epoch INTEGER NOT NULL,
                revoked_at_epoch INTEGER NOT NULL DEFAULT 0,
                last_seen_at_epoch INTEGER NOT NULL DEFAULT 0,
                active_tenant_id INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_api_sessions_user
            ON api_sessions(user_id,expires_at_epoch);
            CREATE INDEX IF NOT EXISTS idx_api_sessions_expiry
            ON api_sessions(expires_at_epoch,revoked_at_epoch);
            """
        )
    # Upgrade v314 sessions without invalidating them.
    try:
        row("SELECT active_tenant_id FROM api_sessions LIMIT 1")
    except Exception:
        with connect() as con:
            con.execute("ALTER TABLE api_sessions ADD COLUMN active_tenant_id INTEGER NOT NULL DEFAULT 0")
    _SCHEMA_READY = True


def cleanup_sessions() -> None:
    ensure_api_session_schema()
    cutoff = _now()
    with connect() as con:
        con.execute(
            "DELETE FROM api_sessions WHERE expires_at_epoch<? OR revoked_at_epoch>0",
            (cutoff,),
        )


def issue_session(user_id: int, *, remember: bool = False) -> ApiSession:
    ensure_api_session_schema()
    user_id = int(user_id)
    if user_id <= 0:
        raise ValueError("user_id non valido")
    record = get_user(user_id)
    if not record:
        raise ValueError("Utente non trovato")
    global_admin = bool(int(record.get("is_admin") or 0))
    active_tenant_id = default_tenant_id(user_id, global_admin=global_admin)
    if active_tenant_id <= 0:
        raise ValueError("L'utente non è associato ad alcuna azienda/tenant.")
    token = secrets.token_urlsafe(48)
    created = _now()
    expires = created + _ttl_seconds(bool(remember))
    with connect() as con:
        con.execute(
            """INSERT INTO api_sessions(
                token_hash,user_id,created_at_epoch,expires_at_epoch,last_seen_at_epoch,active_tenant_id
            ) VALUES(?,?,?,?,?,?)""",
            (_token_hash(token), user_id, created, expires, created, active_tenant_id),
        )
    try:
        cleanup_sessions()
    except Exception:
        pass
    return ApiSession(token=token, user_id=user_id, expires_at=expires, active_tenant_id=active_tenant_id)


def revoke_session(token: str) -> bool:
    ensure_api_session_schema()
    token = str(token or "").strip()
    if not token:
        return False
    with connect() as con:
        cur = con.execute(
            "UPDATE api_sessions SET revoked_at_epoch=? WHERE token_hash=? AND revoked_at_epoch=0",
            (_now(), _token_hash(token)),
        )
        return bool(int(getattr(cur, "rowcount", 0) or 0))


def switch_session_tenant(token: str, tenant_id: int) -> dict | None:
    ensure_api_session_schema()
    token = str(token or "").strip()
    if not token:
        return None
    now = _now()
    session = row(
        """SELECT user_id FROM api_sessions
           WHERE token_hash=? AND revoked_at_epoch=0 AND expires_at_epoch>?""",
        (_token_hash(token), now),
    )
    if not session:
        return None
    record = get_user(int(session.get("user_id") or 0))
    if not record:
        return None
    global_admin = bool(int(record.get("is_admin") or 0))
    context = tenant_context_for_user(int(record["id"]), int(tenant_id), global_admin=global_admin)
    if not context:
        return None
    with connect() as con:
        con.execute(
            "UPDATE api_sessions SET active_tenant_id=?,last_seen_at_epoch=? WHERE token_hash=?",
            (int(tenant_id), now, _token_hash(token)),
        )
    return context




def _repair_owner_seller_scope(user_id: int, tenant_id: int, context: dict) -> bool:
    """Repair an incomplete merchant onboarding without widening non-owner access.

    v323 introduced PostgreSQL tenant RLS while the legacy Seller ownership map
    still lives in ``tenant_sellers``.  A signup interrupted during that bridge
    can leave the new Owner with a valid tenant/session but no effective Seller.

    For a direct tenant Owner only, reconstruct ``tenant_sellers`` from the
    authoritative PostgreSQL ``sellers.tenant_id`` marker when necessary and
    restore the Owner's legacy ``seller_ids_json`` restriction.  The repair is
    deliberately a no-op for operators/managers and when no tenant-owned Seller
    exists, so it can never grant cross-tenant access.
    """
    if int(user_id or 0) <= 0 or int(tenant_id or 0) <= 0:
        return False
    if str((context or {}).get("role") or "").strip().lower() != "owner":
        return False

    try:
        with platform_database_scope():
            owned = rows(
                "SELECT seller_id FROM tenant_sellers WHERE tenant_id=? AND active=1 ORDER BY seller_id",
                (int(tenant_id),),
            )

            # PostgreSQL RLS adds sellers.tenant_id.  If the compatibility map
            # was not written during signup, rebuild it only from rows already
            # stamped with this exact tenant id.
            if not owned and database_engine() == "postgresql":
                candidates = rows(
                    "SELECT id FROM sellers WHERE tenant_id=? AND active=1 ORDER BY id",
                    (int(tenant_id),),
                )
                for item in candidates:
                    seller_id = int(item.get("id") or 0)
                    if seller_id <= 0:
                        continue
                    execute(
                        """INSERT INTO tenant_sellers(tenant_id,seller_id,active,created_at)
                           VALUES(?,?,1,?)
                           ON CONFLICT(seller_id) DO UPDATE SET tenant_id=excluded.tenant_id,active=1""",
                        (int(tenant_id), seller_id, now_iso()),
                    )
                owned = rows(
                    "SELECT seller_id FROM tenant_sellers WHERE tenant_id=? AND active=1 ORDER BY seller_id",
                    (int(tenant_id),),
                )

            seller_ids = sorted(
                {int(item.get("seller_id") or 0) for item in owned if int(item.get("seller_id") or 0) > 0}
            )
            if not seller_ids:
                return False

            import json
            execute(
                "UPDATE app_users SET seller_ids_json=?,updated_at=? WHERE id=?",
                (json.dumps(seller_ids, separators=(",", ":")), now_iso(), int(user_id)),
            )
            return True
    except Exception:
        # Session resolution must never fail just because a best-effort repair
        # could not be completed. The normal empty-scope behavior remains safe.
        return False


def session_user(token: str) -> dict[str, Any] | None:
    ensure_api_session_schema()
    token = str(token or "").strip()
    if not token:
        return None
    now = _now()
    session = row(
        """SELECT user_id,expires_at_epoch,last_seen_at_epoch,active_tenant_id
           FROM api_sessions
           WHERE token_hash=? AND revoked_at_epoch=0 AND expires_at_epoch>?""",
        (_token_hash(token), now),
    )
    if not session:
        return None
    record = get_user(int(session.get("user_id") or 0))
    if not record or int(record.get("active") or 0) != 1:
        try:
            revoke_session(token)
        except Exception:
            pass
        return None

    user_id = int(record.get("id") or 0)
    global_admin = bool(int(record.get("is_admin") or 0))
    tenants = accessible_tenants_for_user(user_id, global_admin=global_admin)
    tenant_ids = [int(item["id"]) for item in tenants]
    active_tenant_id = int(session.get("active_tenant_id") or 0)
    context = tenant_context_for_user(user_id, active_tenant_id, global_admin=global_admin)
    if not context:
        active_tenant_id = default_tenant_id(user_id, global_admin=global_admin)
        context = tenant_context_for_user(user_id, active_tenant_id, global_admin=global_admin)
        if active_tenant_id > 0:
            try:
                with connect() as con:
                    con.execute(
                        "UPDATE api_sessions SET active_tenant_id=? WHERE token_hash=?",
                        (active_tenant_id, _token_hash(token)),
                    )
            except Exception:
                pass
    if not context:
        return None

    legacy_scope = seller_ids_from_record(record)
    seller_ids = effective_seller_ids(
        user_id,
        active_tenant_id,
        global_admin=global_admin,
        legacy_seller_scope=legacy_scope,
    )

    # Self-heal only a direct Owner whose freshly created workspace has become
    # seller-less because the v323 RLS/legacy ownership bridge was incomplete.
    if not global_admin and not seller_ids and _repair_owner_seller_scope(user_id, active_tenant_id, context):
        record = get_user(user_id) or record
        legacy_scope = seller_ids_from_record(record)
        seller_ids = effective_seller_ids(
            user_id,
            active_tenant_id,
            global_admin=False,
            legacy_seller_scope=legacy_scope,
        )

    last_seen = int(session.get("last_seen_at_epoch") or 0)
    if now - last_seen >= 60:
        try:
            with connect() as con:
                con.execute(
                    "UPDATE api_sessions SET last_seen_at_epoch=? WHERE token_hash=?",
                    (now, _token_hash(token)),
                )
        except Exception:
            pass
    return {
        "id": user_id,
        "username": str(record.get("username") or ""),
        "display_name": str(record.get("display_name") or ""),
        "is_admin": global_admin,
        "permissions": permissions_from_record(record),
        "seller_ids": seller_ids,
        "expires_at": int(session.get("expires_at_epoch") or 0),
        "tenant_ids": tenant_ids,
        "active_tenant_id": active_tenant_id,
        "active_tenant_name": str(context.get("name") or ""),
        "active_tenant_type": str(context.get("tenant_type") or ""),
        "tenant_role": str(context.get("role") or ""),
    }
