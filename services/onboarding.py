from __future__ import annotations

import hmac
import json
import os
from typing import Any

from services.billing import billing_snapshot, start_trial
from services.db import execute, now_iso, row, rows
from services.entitlements import STANDARD_FEATURES, list_plans, assert_marketplace_capacity
from services.security import encrypt_dict
from services.tenant_db import platform_database_scope, tenant_database_scope
from services.tenancy import add_membership, attach_seller, create_tenant, tenant_record
from services.user_access import ALL_MENU_KEYS, create_user, delete_user, find_user

_SCHEMA_READY = False


def public_signup_enabled() -> bool:
    return str(os.getenv("MARKETPLACE_HUB_PUBLIC_SIGNUP", "0")).strip().lower() in {"1","true","yes","on"}


def _default_trial_days() -> int:
    try:
        return max(1, min(int(os.getenv("MARKETPLACE_HUB_DEFAULT_TRIAL_DAYS", "14")), 90))
    except ValueError:
        return 14


def _check_invite(value: str) -> None:
    expected = str(os.getenv("MARKETPLACE_HUB_SIGNUP_INVITE_CODE") or "").strip()
    if expected and not hmac.compare_digest(expected, str(value or "").strip()):
        raise ValueError("Codice invito non valido.")


def ensure_onboarding_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    execute(
        """
        CREATE TABLE IF NOT EXISTS onboarding_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES app_users(id) ON DELETE SET NULL,
            seller_id INTEGER REFERENCES sellers(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_onboarding_events_tenant ON onboarding_events(tenant_id,id)")
    _SCHEMA_READY = True


def public_plan_codes() -> set[str]:
    return {
        str(item.get("code") or "")
        for item in list_plans(public_only=True)
        if str(item.get("tenant_type") or "merchant") == "merchant"
    }


def _unique_seller_name(requested: str, tenant_id: int) -> str:
    base = str(requested or "").strip()
    if not base:
        raise ValueError("Inserisci il nome del Seller.")
    if not row("SELECT id FROM sellers WHERE name=?", (base,)):
        return base
    candidate = f"{base} · {tenant_id}"
    suffix = 2
    while row("SELECT id FROM sellers WHERE name=?", (candidate,)):
        candidate = f"{base} · {tenant_id}-{suffix}"
        suffix += 1
    return candidate


def _owner_permissions(plan_code: str) -> list[str]:
    plan = next((p for p in list_plans(public_only=False) if str(p.get("code")) == plan_code), None)
    features = set(plan.get("features") or STANDARD_FEATURES) if plan else set(STANDARD_FEATURES)
    # Keep platform maintenance areas out of self-service merchant accounts.
    forbidden = {"database", "backup_transfer"}
    return [key for key in ALL_MENU_KEYS if key in features and key not in forbidden]



def _seller_rows_for_tenant(tenant_id: int) -> list[dict[str, Any]]:
    """Read the tenant's active Sellers under the PostgreSQL tenant context.

    The SaaS v323 RLS policies hide ``sellers`` unless the connection carries the
    correct tenant id.  Onboarding/status used to join ``tenant_sellers`` to
    ``sellers`` without that context, which made an already-created Seller look
    missing in the web UI.
    """
    tenant_id = int(tenant_id)
    if tenant_id <= 0:
        return []
    with tenant_database_scope(tenant_id):
        return rows(
            """SELECT s.id,s.name FROM tenant_sellers ts JOIN sellers s ON s.id=ts.seller_id
               WHERE ts.tenant_id=? AND ts.active=1 AND s.active=1 ORDER BY s.id""",
            (tenant_id,),
        )


def repair_tenant_seller_links(tenant_id: int) -> list[dict[str, Any]]:
    """Repair only links proven by this tenant's completed signup event.

    Existing workspaces created while the PostgreSQL/RLS bridge was being fixed
    can contain the Seller row and the Owner user but miss the compatibility row
    in ``tenant_sellers``.  The ``signup_completed`` event is the safest source
    for reconstructing that exact tenant/user/Seller relation; no name matching
    and no cross-tenant guessing is used.
    """
    tenant_id = int(tenant_id)
    if tenant_id <= 0:
        return []

    current = _seller_rows_for_tenant(tenant_id)
    if current:
        return current

    try:
        with platform_database_scope():
            events = rows(
                """SELECT id,user_id,seller_id FROM onboarding_events
                   WHERE tenant_id=? AND event_type='signup_completed' AND seller_id IS NOT NULL
                   ORDER BY id DESC""",
                (tenant_id,),
            )

            for event in events:
                seller_id = int(event.get("seller_id") or 0)
                user_id = int(event.get("user_id") or 0)
                if seller_id <= 0:
                    continue

                # The Seller must itself be stamped with this exact tenant by
                # the v323 PostgreSQL trigger.  This prevents accidental relinks.
                seller = row(
                    "SELECT id FROM sellers WHERE id=? AND tenant_id=? AND active=1",
                    (seller_id, tenant_id),
                )
                if not seller:
                    continue

                mapping = row(
                    "SELECT tenant_id FROM tenant_sellers WHERE seller_id=?",
                    (seller_id,),
                )
                if mapping:
                    if int(mapping.get("tenant_id") or 0) != tenant_id:
                        continue
                    execute(
                        "UPDATE tenant_sellers SET active=1 WHERE tenant_id=? AND seller_id=?",
                        (tenant_id, seller_id),
                    )
                else:
                    execute(
                        """INSERT INTO tenant_sellers(tenant_id,seller_id,active,created_at)
                           VALUES(?,?,1,?)""",
                        (tenant_id, seller_id, now_iso()),
                    )

                # Signup already writes this scope, but restore it if the old
                # interrupted flow left the Owner record incomplete.
                if user_id > 0:
                    user = row("SELECT seller_ids_json FROM app_users WHERE id=?", (user_id,)) or {}
                    try:
                        saved = json.loads(str(user.get("seller_ids_json") or "[]"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        saved = []
                    seller_ids = sorted({
                        *[int(value) for value in saved if str(value).isdigit() and int(value) > 0],
                        seller_id,
                    })
                    execute(
                        "UPDATE app_users SET seller_ids_json=?,updated_at=? WHERE id=?",
                        (json.dumps(seller_ids, separators=(",", ":")), now_iso(), user_id),
                    )

    except Exception:
        # Status pages must stay available even if an old/incomplete record
        # cannot be repaired automatically.
        pass

    return _seller_rows_for_tenant(tenant_id)

def register_merchant(
    *,
    company_name: str,
    username: str,
    password: str,
    display_name: str = "",
    email: str = "",
    seller_name: str = "",
    legal_name: str = "",
    plan_code: str = "starter",
    trial_days: int | None = None,
    invite_code: str = "",
) -> dict[str, Any]:
    if not public_signup_enabled():
        raise PermissionError("Registrazione pubblica non abilitata.")
    _check_invite(invite_code)
    company_name = str(company_name or "").strip()
    username = str(username or "").strip()
    email = str(email or "").strip().lower()
    seller_name = str(seller_name or company_name).strip()
    plan_code = str(plan_code or "starter").strip().lower()
    if not company_name:
        raise ValueError("Inserisci il nome dell'azienda.")
    if not username:
        raise ValueError("Inserisci lo username.")
    if plan_code not in public_plan_codes():
        raise ValueError("Piano SaaS non disponibile per la registrazione.")
    if find_user(username):
        raise ValueError("Username già utilizzato.")
    if email and row("SELECT id FROM app_users WHERE lower(COALESCE(email,''))=?", (email,)):
        raise ValueError("E-mail già utilizzata.")

    tenant_id = user_id = seller_id = 0
    with platform_database_scope():
        ensure_onboarding_schema()
        try:
            tenant_id = create_tenant(company_name, tenant_type="merchant", plan_code=plan_code)
            internal_seller_name = _unique_seller_name(seller_name, tenant_id)
            # PostgreSQL staging/prod protects sellers with tenant-aware RLS.
            # The legacy INSERT intentionally omits tenant_id, so bind the new
            # tenant while inserting: the database trigger can then populate
            # sellers.tenant_id correctly instead of rejecting the row.
            with tenant_database_scope(tenant_id):
                seller_id = execute(
                    """INSERT INTO sellers(name,legal_name,email,our_profit_pct,partner_profit_pct,active,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (internal_seller_name, str(legal_name or "").strip(), email, 0.0, 100.0, 1, now_iso()),
                )
            attach_seller(tenant_id, seller_id)
            # Verify the compatibility relation used by the web workspace.
            # The Seller row is already tenant-stamped by the PostgreSQL trigger.
            linked = row(
                "SELECT 1 ok FROM tenant_sellers WHERE tenant_id=? AND seller_id=? AND active=1",
                (tenant_id, seller_id),
            )
            if not linked:
                execute(
                    """INSERT INTO tenant_sellers(tenant_id,seller_id,active,created_at)
                       VALUES(?,?,1,?)""",
                    (tenant_id, seller_id, now_iso()),
                )
            user_id = create_user(
                username,
                password,
                display_name=str(display_name or company_name).strip(),
                email=email,
                permissions=_owner_permissions(plan_code),
                seller_ids=[seller_id],
                is_admin=False,
                active=True,
            )
            add_membership(tenant_id, user_id, role="owner", active=True)
            days = _default_trial_days() if trial_days is None else max(1, min(int(trial_days), 90))
            subscription = start_trial(tenant_id, plan_code, days=days)
            execute(
                """INSERT INTO onboarding_events(tenant_id,user_id,seller_id,event_type,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (tenant_id, user_id, seller_id, "signup_completed", json.dumps({"plan_code": plan_code, "trial_days": days}, separators=(",", ":")), now_iso()),
            )
            return {
                "tenant": tenant_record(tenant_id) or {},
                "user_id": user_id,
                "seller": row("SELECT id,name,legal_name,email,active FROM sellers WHERE id=?", (seller_id,)) or {},
                "billing": billing_snapshot(tenant_id),
            }
        except Exception:
            # Every legacy helper commits independently; compensate in reverse order.
            if user_id:
                try: delete_user(user_id)
                except Exception: pass
            if seller_id:
                try: execute("DELETE FROM sellers WHERE id=?", (seller_id,))
                except Exception: pass
            if tenant_id:
                try: execute("DELETE FROM tenants WHERE id=?", (tenant_id,))
                except Exception: pass
            raise


def connect_marketplace(
    *,
    tenant_id: int,
    seller_id: int,
    marketplace: str,
    account_name: str,
    credentials: dict[str, Any],
    validate: bool = True,
) -> dict[str, Any]:
    tenant_id, seller_id = int(tenant_id), int(seller_id)
    market = str(marketplace or "").strip().lower()
    if market not in {"kaufland", "worten"}:
        raise ValueError("Marketplace onboarding supportato: Kaufland o Worten.")
    repair_tenant_seller_links(tenant_id)
    owned = row("SELECT 1 ok FROM tenant_sellers WHERE tenant_id=? AND seller_id=? AND active=1", (tenant_id, seller_id))
    if not owned:
        raise ValueError("Seller non appartenente al tenant attivo.")
    assert_marketplace_capacity(tenant_id, market)
    account_name = str(account_name or "").strip() or f"{market.title()} principale"
    creds = {str(k): v for k, v in dict(credentials or {}).items()}
    check: dict[str, Any] = {"ok": True, "message": "Credenziali salvate senza verifica remota."}
    if market == "kaufland":
        if not str(creds.get("client_key") or "").strip() or not str(creds.get("secret_key") or "").strip():
            raise ValueError("Kaufland richiede Client Key e Secret Key.")
        if validate:
            from services.kaufland import KauflandClient
            playground = bool(creds.get("playground", False))
            KauflandClient(str(creds["client_key"]).strip(), str(creds["secret_key"]).strip(), playground).ping()
            check = {"ok": True, "message": "Connessione Kaufland verificata."}
    else:
        from services.worten import DEFAULT_API_URL
        api_key = str(creds.get("api_key") or "").strip()
        shop_id = str(creds.get("shop_id") or "").strip()
        api_url = str(creds.get("api_url") or DEFAULT_API_URL).strip()
        if not api_key or not shop_id:
            raise ValueError("Worten richiede API Key e Shop ID.")
        creds.update({"api_key": api_key, "shop_id": shop_id, "api_url": api_url, "country": "pt"})
        if validate:
            from services.worten import validate_credentials
            check = validate_credentials(api_key, shop_id, api_url)
            if not check.get("ok"):
                raise ValueError(str(check.get("message") or "Credenziali Worten non valide."))

    existing = row(
        "SELECT id FROM marketplace_accounts WHERE seller_id=? AND marketplace=? AND account_name=?",
        (seller_id, market, account_name),
    )
    if existing:
        account_id = int(existing["id"])
        execute(
            "UPDATE marketplace_accounts SET credentials_encrypted=?,active=1 WHERE id=?",
            (encrypt_dict(creds), account_id),
        )
    else:
        account_id = execute(
            """INSERT INTO marketplace_accounts(seller_id,marketplace,account_name,credentials_encrypted,created_at)
               VALUES(?,?,?,?,?)""",
            (seller_id, market, account_name, encrypt_dict(creds), now_iso()),
        )
    execute(
        """INSERT INTO onboarding_events(tenant_id,user_id,seller_id,event_type,metadata_json,created_at)
           VALUES(?,?,?,?,?,?)""",
        (tenant_id, None, seller_id, "marketplace_connected", json.dumps({"marketplace": market, "account_id": account_id}, separators=(",", ":")), now_iso()),
    )
    return {"account_id": account_id, "marketplace": market, "account_name": account_name, "validation": check}


def onboarding_status(tenant_id: int) -> dict[str, Any]:
    tenant_id = int(tenant_id)
    seller_rows = repair_tenant_seller_links(tenant_id)
    seller_ids = [int(s["id"]) for s in seller_rows]
    accounts = []
    if seller_ids:
        ph = ",".join("?" for _ in seller_ids)
        accounts = rows(
            f"SELECT id,seller_id,marketplace,account_name,active FROM marketplace_accounts WHERE seller_id IN ({ph}) AND active=1 ORDER BY id",
            tuple(seller_ids),
        )
    billing = billing_snapshot(tenant_id)
    completed = ["account", "company"]
    if seller_rows: completed.append("seller")
    if billing and str(billing.get("status") or "") in {"trialing","active","past_due"}: completed.append("plan")
    if accounts: completed.append("marketplace")
    next_step = "done" if accounts else ("connect_marketplace" if seller_rows else "create_seller")
    return {"tenant_id": tenant_id, "completed_steps": completed, "next_step": next_step, "sellers": seller_rows, "marketplace_accounts": accounts, "billing": billing}
