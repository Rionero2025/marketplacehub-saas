from __future__ import annotations

import hmac
import json
import os
from typing import Any

from services.billing import billing_snapshot, start_trial
from services.db import execute, now_iso, row, rows
from services.entitlements import STANDARD_FEATURES, list_plans, assert_marketplace_capacity
from services.security import encrypt_dict
from services.tenant_db import platform_database_scope
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
            seller_id = execute(
                """INSERT INTO sellers(name,legal_name,email,our_profit_pct,partner_profit_pct,active,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (internal_seller_name, str(legal_name or "").strip(), email, 0.0, 100.0, 1, now_iso()),
            )
            attach_seller(tenant_id, seller_id)
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
    seller_rows = rows(
        """SELECT s.id,s.name FROM tenant_sellers ts JOIN sellers s ON s.id=ts.seller_id
           WHERE ts.tenant_id=? AND ts.active=1 AND s.active=1 ORDER BY s.id""",
        (tenant_id,),
    )
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
