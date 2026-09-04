from __future__ import annotations

import hmac
import json
import os
from typing import Any

from services.billing import billing_snapshot, start_trial
from services.db import execute, now_iso, row, rows
from services.entitlements import STANDARD_FEATURES, assert_marketplace_capacity, list_plans
from services.security import encrypt_dict
from services.tenant_db import platform_database_scope, tenant_database_scope
from services.tenancy import add_membership, create_tenant, tenant_record
from services.user_access import ALL_MENU_KEYS, create_user, delete_user, find_user

_SCHEMA_READY = False


def public_signup_enabled() -> bool:
    return str(os.getenv("MARKETPLACE_HUB_PUBLIC_SIGNUP", "0")).strip().lower() in {"1", "true", "yes", "on"}


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
        if str(item.get("tenant_type") or "merchant") in {"merchant", "any"}
    }


def _unique_seller_name(requested: str, tenant_id: int) -> str:
    base = str(requested or "").strip()
    if not base:
        base = f"Workspace {int(tenant_id)}"
    try:
        with platform_database_scope():
            if not row("SELECT id FROM sellers WHERE name=?", (base,)):
                return base
            candidate = f"{base} · {tenant_id}"
            suffix = 2
            while row("SELECT id FROM sellers WHERE name=?", (candidate,)):
                candidate = f"{base} · {tenant_id}-{suffix}"
                suffix += 1
            return candidate
    except Exception:
        return f"{base} · {tenant_id}"


def _owner_permissions(plan_code: str) -> list[str]:
    plan = next((p for p in list_plans(public_only=False) if str(p.get("code")) == plan_code), None)
    features = set(plan.get("features") or STANDARD_FEATURES) if plan else set(STANDARD_FEATURES)
    forbidden = {"database", "backup_transfer"}
    return [key for key in ALL_MENU_KEYS if key in features and key not in forbidden]


def _direct_sellers_for_tenant(tenant_id: int) -> list[dict[str, Any]]:
    tenant_id = int(tenant_id)
    if tenant_id <= 0:
        return []
    try:
        with tenant_database_scope(tenant_id):
            return rows(
                """SELECT id,name,legal_name,email,active FROM sellers
                   WHERE tenant_id=? AND active=1 ORDER BY id""",
                (tenant_id,),
            )
    except Exception:
        try:
            with platform_database_scope():
                return rows(
                    """SELECT s.id,s.name,s.legal_name,s.email,s.active
                       FROM tenant_sellers ts JOIN sellers s ON s.id=ts.seller_id
                       WHERE ts.tenant_id=? AND ts.active=1 AND s.active=1 ORDER BY s.id""",
                    (tenant_id,),
                )
        except Exception:
            return []


def _event_seller_for_tenant(tenant_id: int) -> dict[str, Any] | None:
    """Recover a Seller referenced by this tenant's own completed signup event."""
    try:
        with platform_database_scope():
            event = row(
                """SELECT seller_id FROM onboarding_events
                   WHERE tenant_id=? AND event_type='signup_completed' AND seller_id IS NOT NULL
                   ORDER BY id DESC LIMIT 1""",
                (int(tenant_id),),
            )
            seller_id = int((event or {}).get("seller_id") or 0)
            if seller_id <= 0:
                return None
            item = row(
                "SELECT id,name,legal_name,email,active,tenant_id FROM sellers WHERE id=?",
                (seller_id,),
            )
            if not item or int(item.get("active") or 0) != 1:
                return None
            stamped = int(item.get("tenant_id") or 0)
            if stamped not in (0, int(tenant_id)):
                return None
            if stamped == 0:
                execute("UPDATE sellers SET tenant_id=? WHERE id=?", (int(tenant_id), seller_id))
            return {
                "id": seller_id,
                "name": str(item.get("name") or ""),
                "legal_name": str(item.get("legal_name") or ""),
                "email": str(item.get("email") or ""),
                "active": 1,
            }
    except Exception:
        return None


def _ensure_tenant_seller_link(tenant_id: int, seller_id: int) -> None:
    tenant_id, seller_id = int(tenant_id), int(seller_id)
    with platform_database_scope():
        existing = row("SELECT tenant_id FROM tenant_sellers WHERE seller_id=?", (seller_id,))
        if existing:
            mapped = int(existing.get("tenant_id") or 0)
            if mapped not in (0, tenant_id):
                raise ValueError("Il negozio interno risulta associato a un altro workspace.")
            execute(
                "UPDATE tenant_sellers SET tenant_id=?,active=1 WHERE seller_id=?",
                (tenant_id, seller_id),
            )
        else:
            execute(
                """INSERT INTO tenant_sellers(tenant_id,seller_id,active,created_at)
                   VALUES(?,?,1,?)""",
                (tenant_id, seller_id, now_iso()),
            )


def _grant_user_seller_scope(user_id: int, seller_id: int) -> None:
    user_id, seller_id = int(user_id), int(seller_id)
    if user_id <= 0 or seller_id <= 0:
        return
    with platform_database_scope():
        record = row("SELECT seller_ids_json FROM app_users WHERE id=?", (user_id,)) or {}
        try:
            current = json.loads(str(record.get("seller_ids_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            current = []
        values: set[int] = set()
        if isinstance(current, list):
            for value in current:
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    values.add(parsed)
        values.add(seller_id)
        execute(
            "UPDATE app_users SET seller_ids_json=?,updated_at=? WHERE id=?",
            (json.dumps(sorted(values), separators=(",", ":")), now_iso(), user_id),
        )


def ensure_internal_seller(tenant_id: int, *, user_id: int = 0) -> dict[str, Any]:
    """Return/create the Marketplace Hub internal Seller for a workspace.

    This is NOT the Kaufland/Worten shop. It is the internal container used by
    Marketplace Hub to scope catalogues, orders, accounting and marketplace
    accounts. The real marketplace account is created only after API validation.
    """
    tenant_id = int(tenant_id)
    if tenant_id <= 0:
        raise ValueError("Tenant non valido.")

    sellers = _direct_sellers_for_tenant(tenant_id)
    seller = sellers[0] if sellers else _event_seller_for_tenant(tenant_id)

    if not seller:
        tenant = tenant_record(tenant_id) or {}
        internal_name = _unique_seller_name(str(tenant.get("name") or f"Workspace {tenant_id}"), tenant_id)
        with tenant_database_scope(tenant_id):
            seller_id = execute(
                """INSERT INTO sellers(name,legal_name,email,our_profit_pct,partner_profit_pct,active,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    internal_name,
                    str(tenant.get("name") or ""),
                    "",
                    0.0,
                    100.0,
                    1,
                    now_iso(),
                ),
            )
            seller = row(
                "SELECT id,name,legal_name,email,active FROM sellers WHERE id=?",
                (seller_id,),
            ) or {"id": seller_id, "name": internal_name, "legal_name": str(tenant.get("name") or ""), "email": "", "active": 1}

    seller_id = int(seller.get("id") or 0)
    if seller_id <= 0:
        raise ValueError("Impossibile inizializzare il negozio interno Marketplace Hub.")
    _ensure_tenant_seller_link(tenant_id, seller_id)
    _grant_user_seller_scope(user_id, seller_id)
    return seller


def repair_tenant_seller_links(tenant_id: int, *, user_id: int = 0) -> list[dict[str, Any]]:
    """Compatibility helper retained for older v323 call sites."""
    ensure_internal_seller(tenant_id, user_id=user_id)
    sellers = _direct_sellers_for_tenant(tenant_id)
    for item in sellers:
        seller_id = int(item.get("id") or 0)
        if seller_id > 0:
            _ensure_tenant_seller_link(tenant_id, seller_id)
            _grant_user_seller_scope(user_id, seller_id)
    return sellers


def register_merchant(
    *,
    company_name: str,
    username: str,
    password: str,
    display_name: str = "",
    email: str = "",
    seller_name: str = "",
    legal_name: str = "",
    plan_code: str = "enterprise",
    tenant_type: str = "merchant",
    trial_days: int | None = None,
    invite_code: str = "",
) -> dict[str, Any]:
    if not public_signup_enabled():
        raise PermissionError("Registrazione pubblica non abilitata.")
    _check_invite(invite_code)
    company_name = str(company_name or "").strip()
    username = str(username or "").strip()
    email = str(email or "").strip().lower()
    # seller_name is the INTERNAL Marketplace Hub store/workspace label.
    seller_name = str(seller_name or display_name or company_name).strip()
    plan_code = str(plan_code or "enterprise").strip().lower()
    if tenant_type not in {"merchant", "agency"}:
        raise ValueError("Tipo workspace non valido")
    if not company_name:
        raise ValueError("Inserisci il nome dell'azienda.")
    if not username:
        raise ValueError("Inserisci lo username.")
    if plan_code not in public_plan_codes():
        raise ValueError("Piano SaaS non disponibile per la registrazione.")
    requested_plan_code = plan_code
    plan_code = "enterprise"
    if find_user(username):
        raise ValueError("Username già utilizzato.")
    if email and row("SELECT id FROM app_users WHERE lower(COALESCE(email,''))=?", (email,)):
        raise ValueError("E-mail già utilizzata.")

    tenant_id = user_id = seller_id = 0
    with platform_database_scope():
        ensure_onboarding_schema()
        try:
            tenant_id = create_tenant(company_name, tenant_type=tenant_type, plan_code=plan_code)
            internal_seller_name = _unique_seller_name(seller_name, tenant_id)
            with tenant_database_scope(tenant_id):
                seller_id = execute(
                    """INSERT INTO sellers(name,legal_name,email,our_profit_pct,partner_profit_pct,active,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (internal_seller_name, str(legal_name or company_name).strip(), email, 0.0, 100.0, 1, now_iso()),
                )
            _ensure_tenant_seller_link(tenant_id, seller_id)
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
            _grant_user_seller_scope(user_id, seller_id)
            days = _default_trial_days() if trial_days is None else max(1, min(int(trial_days), 90))
            start_trial(tenant_id, plan_code, days=days)
            execute(
                """INSERT INTO onboarding_events(tenant_id,user_id,seller_id,event_type,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (tenant_id, user_id, seller_id, "signup_completed", json.dumps({"plan_code": plan_code, "requested_plan_code": requested_plan_code, "trial_days": days}, separators=(",", ":")), now_iso()),
            )
            with tenant_database_scope(tenant_id):
                seller = row("SELECT id,name,legal_name,email,active FROM sellers WHERE id=?", (seller_id,)) or {}
            return {
                "tenant": tenant_record(tenant_id) or {},
                "user_id": user_id,
                "seller": seller,
                "billing": billing_snapshot(tenant_id),
            }
        except Exception:
            if user_id:
                try:
                    delete_user(user_id)
                except Exception:
                    pass
            if seller_id:
                try:
                    with tenant_database_scope(tenant_id):
                        execute("DELETE FROM sellers WHERE id=?", (seller_id,))
                except Exception:
                    pass
            if tenant_id:
                try:
                    execute("DELETE FROM tenants WHERE id=?", (tenant_id,))
                except Exception:
                    pass
            raise


def _kaufland_account_alias(profile: dict[str, Any]) -> str:
    detected = str(profile.get("display_name") or "").strip()
    if detected:
        return detected
    storefronts = [str(value or "").strip().upper() for value in profile.get("storefronts") or [] if str(value or "").strip()]
    if storefronts:
        return f"Kaufland · {', '.join(storefronts)}"
    return "Kaufland"


def connect_marketplace(
    *,
    tenant_id: int,
    seller_id: int = 0,
    marketplace: str,
    account_name: str = "",
    credentials: dict[str, Any],
    validate: bool = True,
    user_id: int = 0,
) -> dict[str, Any]:
    tenant_id, seller_id = int(tenant_id), int(seller_id or 0)
    market = str(marketplace or "").strip().lower()
    if market not in {"kaufland", "worten"}:
        raise ValueError("Marketplace onboarding supportato: Kaufland o Worten.")

    if seller_id > 0:
        with tenant_database_scope(tenant_id):
            internal_seller = row(
                "SELECT id,name,legal_name,email,active FROM sellers WHERE id=? AND tenant_id=? AND active=1",
                (seller_id, tenant_id),
            )
        if not internal_seller:
            raise ValueError("Il negozio interno selezionato non appartiene al workspace attivo.")
        _ensure_tenant_seller_link(tenant_id, seller_id)
        _grant_user_seller_scope(user_id, seller_id)
    else:
        internal_seller = ensure_internal_seller(tenant_id, user_id=user_id)
        seller_id = int(internal_seller["id"])

    assert_marketplace_capacity(tenant_id, market)
    requested_name = str(account_name or "").strip()
    creds = {str(k): v for k, v in dict(credentials or {}).items()}
    check: dict[str, Any] = {"ok": True, "message": "Credenziali salvate senza verifica remota."}

    if market == "kaufland":
        client_key = str(creds.get("client_key") or "").strip()
        secret_key = str(creds.get("secret_key") or "").strip()
        if not client_key or not secret_key:
            raise ValueError("Kaufland richiede Client Key e Secret Key.")
        profile: dict[str, Any] = {"display_name": "", "storefronts": [], "locales": []}
        if validate:
            from services.kaufland import KauflandClient, KauflandError

            try:
                client = KauflandClient(client_key, secret_key, bool(creds.get("playground", False)))
                profile = client.connection_profile()
            except KauflandError as exc:
                raise ValueError(f"Connessione Kaufland non riuscita: {exc}") from exc
            check = {
                "ok": True,
                "message": "Connessione Kaufland verificata.",
                "shop_name": str(profile.get("display_name") or ""),
                "storefronts": list(profile.get("storefronts") or []),
                "locales": list(profile.get("locales") or []),
            }
        account_name = requested_name or _kaufland_account_alias(profile)
        creds["detected_storefronts"] = list(profile.get("storefronts") or [])
        creds["detected_locales"] = list(profile.get("locales") or [])
    else:
        from services.worten import DEFAULT_API_URL, validate_credentials

        api_key = str(creds.get("api_key") or "").strip()
        shop_id = str(creds.get("shop_id") or "").strip()
        api_url = str(creds.get("api_url") or DEFAULT_API_URL).strip()
        if not api_key or not shop_id:
            raise ValueError("Worten richiede API Key e Shop ID.")
        creds.update({"api_key": api_key, "shop_id": shop_id, "api_url": api_url, "country": "pt"})
        if validate:
            try:
                check = validate_credentials(api_key, shop_id, api_url)
            except Exception as exc:
                raise ValueError(f"Connessione Worten non riuscita: {exc}") from exc
            if not check.get("ok"):
                raise ValueError(str(check.get("message") or "Credenziali Worten non valide."))
        detected = str(check.get("shop_name") or check.get("account_name") or "").strip() if isinstance(check, dict) else ""
        account_name = requested_name or detected or f"Worten · {shop_id}"

    with tenant_database_scope(tenant_id):
        existing = None
        if requested_name:
            existing = row(
                "SELECT id,account_name FROM marketplace_accounts WHERE seller_id=? AND marketplace=? AND account_name=?",
                (seller_id, market, account_name),
            )
        else:
            existing = row(
                "SELECT id,account_name FROM marketplace_accounts WHERE seller_id=? AND marketplace=? AND active=1 ORDER BY id LIMIT 1",
                (seller_id, market),
            )
        if existing:
            account_id = int(existing["id"])
            execute(
                "UPDATE marketplace_accounts SET account_name=?,credentials_encrypted=?,active=1 WHERE id=?",
                (account_name, encrypt_dict(creds), account_id),
            )
        else:
            account_id = execute(
                """INSERT INTO marketplace_accounts(seller_id,marketplace,account_name,credentials_encrypted,created_at)
                   VALUES(?,?,?,?,?)""",
                (seller_id, market, account_name, encrypt_dict(creds), now_iso()),
            )

    with platform_database_scope():
        execute(
            """INSERT INTO onboarding_events(tenant_id,user_id,seller_id,event_type,metadata_json,created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                tenant_id,
                int(user_id or 0) or None,
                seller_id,
                "marketplace_connected",
                json.dumps({"marketplace": market, "account_id": account_id}, separators=(",", ":")),
                now_iso(),
            ),
        )

    return {
        "account_id": account_id,
        "marketplace": market,
        "account_name": account_name,
        "validation": check,
    }


def onboarding_status(tenant_id: int, *, user_id: int = 0) -> dict[str, Any]:
    tenant_id = int(tenant_id)
    # Self-heal/create the INTERNAL Marketplace Hub Seller. No marketplace
    # account is created here and no external API is called.
    ensure_internal_seller(tenant_id, user_id=user_id)
    seller_rows = repair_tenant_seller_links(tenant_id, user_id=user_id)
    seller_ids = [int(s["id"]) for s in seller_rows]
    accounts: list[dict[str, Any]] = []
    if seller_ids:
        placeholders = ",".join("?" for _ in seller_ids)
        with tenant_database_scope(tenant_id):
            accounts = rows(
                f"SELECT id,seller_id,marketplace,account_name,active FROM marketplace_accounts WHERE seller_id IN ({placeholders}) AND active=1 ORDER BY id",
                tuple(seller_ids),
            )
    billing = billing_snapshot(tenant_id)
    completed = ["account", "company"]
    if seller_rows:
        completed.append("seller")
    if billing and str(billing.get("status") or "") in {"trialing", "active", "past_due"}:
        completed.append("plan")
    if accounts:
        completed.append("marketplace")
    next_step = "done" if accounts else "connect_marketplace"
    return {
        "tenant_id": tenant_id,
        "completed_steps": completed,
        "next_step": next_step,
        "sellers": seller_rows,
        "marketplace_accounts": accounts,
        "billing": billing,
    }
