from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from services.db import execute, row, rows
from services.shared_cache import cache_get_or_set, cache_invalidate

_SCHEMA_READY = False

# Commercial baseline agreed during the Marketplace Hub SaaS design. Limits that
# have not yet been commercially fixed remain None (= unlimited) on purpose.
STANDARD_FEATURES = frozenset({
    "dashboard",
    "seller_management",
    "suppliers_lists",
    "ai_provider",
    "work_lists",
    "product_creation",
    "marketplace_publication",
    "buybox",
    "marketplace_orders",
    "top_products",
    "cecotec_orders",
    "innpro_orders",
    "packlink",
    "tracking",
    "accounting",
    "support",
    "marketplace_deletion",
    "history",
    "catalog_sharing",
})

# None means unlimited/not commercially constrained yet.
DEFAULT_PLANS: dict[str, dict[str, Any]] = {
    "starter": {
        "name": "Starter",
        "tenant_type": "merchant",
        "public": True,
        "monthly_price_cents": 2900,
        "currency": "EUR",
        "features": sorted(STANDARD_FEATURES),
        "limits": {
            "max_users": None,
            "max_sellers": None,
            "max_marketplaces": 1,
            "max_suppliers": 1,
            "max_price_lists": None,
            "monthly_orders": None,
            "monthly_background_jobs": None,
        },
    },
    "growth": {
        "name": "Growth",
        "tenant_type": "merchant",
        "public": True,
        "monthly_price_cents": 3900,
        "currency": "EUR",
        "features": sorted(STANDARD_FEATURES),
        "limits": {
            "max_users": None,
            "max_sellers": None,
            "max_marketplaces": 1,
            "max_suppliers": 3,
            "max_price_lists": None,
            "monthly_orders": None,
            "monthly_background_jobs": None,
        },
    },
    "pro": {
        "name": "Pro",
        "tenant_type": "merchant",
        "public": True,
        "monthly_price_cents": 5900,
        "currency": "EUR",
        "features": sorted(STANDARD_FEATURES),
        "limits": {
            "max_users": None,
            "max_sellers": None,
            "max_marketplaces": 2,
            "max_suppliers": 3,
            "max_price_lists": None,
            "monthly_orders": None,
            "monthly_background_jobs": None,
        },
    },
    "unlimited": {
        "name": "Unlimited",
        "tenant_type": "merchant",
        "public": True,
        "monthly_price_cents": 49900,
        "currency": "EUR",
        "features": sorted(STANDARD_FEATURES),
        "limits": {
            "max_users": None,
            "max_sellers": None,
            "max_marketplaces": None,
            "max_suppliers": None,
            "max_price_lists": None,
            "monthly_orders": None,
            "monthly_background_jobs": None,
        },
    },
    # Internal Agency workspace: billing will be attached to the Agency product
    # later. It is intentionally not exposed in the public pricing catalogue.
    "agency": {
        "name": "Agency",
        "tenant_type": "agency",
        "public": False,
        "monthly_price_cents": 0,
        "currency": "EUR",
        "features": sorted(STANDARD_FEATURES | {"agency_console", "catalog_sharing_agency"}),
        "limits": {
            "max_users": None,
            "max_sellers": None,
            "max_marketplaces": None,
            "max_suppliers": None,
            "max_price_lists": None,
            "monthly_orders": None,
            "monthly_background_jobs": None,
        },
    },
    # Compatibility plan for tenants created before the subscription engine.
    # It prevents the v318 migration from silently removing existing capability.
    "legacy": {
        "name": "Legacy compatibility",
        "tenant_type": "any",
        "public": False,
        "monthly_price_cents": 0,
        "currency": "EUR",
        "features": sorted(STANDARD_FEATURES | {"agency_console", "catalog_sharing_agency"}),
        "limits": {
            "max_users": None,
            "max_sellers": None,
            "max_marketplaces": None,
            "max_suppliers": None,
            "max_price_lists": None,
            "monthly_orders": None,
            "monthly_background_jobs": None,
        },
    },
}

JOB_FEATURE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("orders.", "marketplace_orders"),
    ("accounting.", "accounting"),
    ("buybox.", "buybox"),
    ("packlink.", "packlink"),
    ("tracking.", "tracking"),
    ("catalog.", "work_lists"),
    ("product.", "product_creation"),
    ("support.", "support"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _loads(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
        return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _limit_value(value: Any) -> int | float | None:
    if value in (None, "", "null"):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return None
    return int(numeric) if numeric.is_integer() else numeric


def ensure_entitlement_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    from services.tenancy import ensure_tenancy_schema
    ensure_tenancy_schema()
    execute(
        """
        CREATE TABLE IF NOT EXISTS saas_plans (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            tenant_type TEXT NOT NULL DEFAULT 'merchant',
            public INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            monthly_price_cents INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'EUR',
            features_json TEXT NOT NULL DEFAULT '[]',
            limits_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_subscriptions (
            tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            plan_code TEXT NOT NULL REFERENCES saas_plans(code),
            status TEXT NOT NULL DEFAULT 'manual',
            current_period_start TEXT NOT NULL DEFAULT '',
            current_period_end TEXT NOT NULL DEFAULT '',
            cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
            external_customer_id TEXT NOT NULL DEFAULT '',
            external_subscription_id TEXT NOT NULL DEFAULT '',
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_entitlement_overrides (
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            entitlement_key TEXT NOT NULL,
            entitlement_kind TEXT NOT NULL CHECK(entitlement_kind IN ('feature','limit')),
            enabled INTEGER,
            limit_value REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id,entitlement_key,entitlement_kind)
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_usage_monthly (
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            period_ym TEXT NOT NULL,
            metric TEXT NOT NULL,
            used_value REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id,period_ym,metric)
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_tenant_usage_period ON tenant_usage_monthly(period_ym,metric,tenant_id)")
    execute("CREATE INDEX IF NOT EXISTS idx_tenant_subscriptions_plan ON tenant_subscriptions(plan_code,status)")

    stamp = now_iso()
    for code, spec in DEFAULT_PLANS.items():
        existing = row("SELECT code FROM saas_plans WHERE code=?", (code,))
        values = (
            str(spec["name"]),
            str(spec["tenant_type"]),
            1 if spec.get("public") else 0,
            int(spec.get("monthly_price_cents") or 0),
            str(spec.get("currency") or "EUR"),
            _json(spec.get("features") or []),
            _json(spec.get("limits") or {}),
            stamp,
        )
        if existing:
            # Keep commercial values centrally editable while ensuring new code
            # ships with the intended baseline. Custom plans (other codes) are untouched.
            execute(
                """UPDATE saas_plans SET name=?,tenant_type=?,public=?,active=1,
                   monthly_price_cents=?,currency=?,features_json=?,limits_json=?,updated_at=?
                   WHERE code=?""",
                values + (code,),
            )
        else:
            execute(
                """INSERT INTO saas_plans(code,name,tenant_type,public,active,monthly_price_cents,
                   currency,features_json,limits_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    code, str(spec["name"]), str(spec["tenant_type"]),
                    1 if spec.get("public") else 0, 1,
                    int(spec.get("monthly_price_cents") or 0), str(spec.get("currency") or "EUR"),
                    _json(spec.get("features") or []), _json(spec.get("limits") or {}), stamp, stamp,
                ),
            )

    # Existing tenants keep current capabilities. New tenants receive a real plan
    # in services.tenancy.create_tenant (starter for merchants, agency for agencies).
    for tenant in rows("SELECT id,tenant_type,plan_code FROM tenants ORDER BY id"):
        tenant_id = int(tenant["id"])
        configured = str(tenant.get("plan_code") or "").strip().lower()
        if not configured:
            configured = "agency" if str(tenant.get("tenant_type") or "") == "agency" else "legacy"
        if not row("SELECT code FROM saas_plans WHERE code=? AND active=1", (configured,)):
            configured = "legacy"
        if not row("SELECT tenant_id FROM tenant_subscriptions WHERE tenant_id=?", (tenant_id,)):
            execute(
                """INSERT INTO tenant_subscriptions(
                    tenant_id,plan_code,status,created_at,updated_at
                ) VALUES(?,?,?,?,?)""",
                (tenant_id, configured, "manual", stamp, stamp),
            )
        if str(tenant.get("plan_code") or "").strip().lower() != configured:
            execute("UPDATE tenants SET plan_code=?,updated_at=? WHERE id=?", (configured, stamp, tenant_id))
    _SCHEMA_READY = True


def plan_record(code: str) -> dict | None:
    ensure_entitlement_schema()
    found = row("SELECT * FROM saas_plans WHERE code=? AND active=1", (str(code or "").strip().lower(),))
    if not found:
        return None
    item = dict(found)
    item["features"] = sorted({str(x) for x in _loads(item.get("features_json"), []) if str(x)})
    raw_limits = _loads(item.get("limits_json"), {})
    item["limits"] = {str(k): _limit_value(v) for k, v in raw_limits.items()} if isinstance(raw_limits, dict) else {}
    item["public"] = bool(int(item.get("public") or 0))
    item["active"] = bool(int(item.get("active") or 0))
    return item


def list_plans(*, public_only: bool = False) -> list[dict]:
    ensure_entitlement_schema()
    sql = "SELECT code FROM saas_plans WHERE active=1"
    if public_only:
        sql += " AND public=1"
    sql += " ORDER BY monthly_price_cents,code"
    return [plan_record(item["code"]) or {} for item in rows(sql)]


def tenant_subscription(tenant_id: int) -> dict:
    ensure_entitlement_schema()
    tenant_id = int(tenant_id)
    found = row(
        """SELECT s.*,t.tenant_type,t.name tenant_name,t.plan_code tenant_plan_code
           FROM tenants t LEFT JOIN tenant_subscriptions s ON s.tenant_id=t.id
           WHERE t.id=?""",
        (tenant_id,),
    )
    if not found:
        return {}
    item = dict(found)
    code = str(item.get("plan_code") or item.get("tenant_plan_code") or "legacy").strip().lower() or "legacy"
    if not plan_record(code):
        code = "legacy"
    item["plan_code"] = code
    item["status"] = str(item.get("status") or "manual").lower()
    return item


def set_tenant_plan(
    tenant_id: int,
    plan_code: str,
    *,
    status: str = "manual",
    external_customer_id: str = "",
    external_subscription_id: str = "",
) -> dict:
    ensure_entitlement_schema()
    tenant_id = int(tenant_id)
    code = str(plan_code or "").strip().lower()
    plan = plan_record(code)
    if not plan:
        raise ValueError("Piano SaaS non valido o disattivato.")
    tenant = row("SELECT id,tenant_type FROM tenants WHERE id=?", (tenant_id,))
    if not tenant:
        raise ValueError("Tenant non trovato.")
    required_type = str(plan.get("tenant_type") or "any")
    tenant_type = str(tenant.get("tenant_type") or "merchant")
    if required_type not in {"any", tenant_type}:
        raise ValueError(f"Il piano {code} non è compatibile con tenant_type={tenant_type}.")
    status = str(status or "manual").strip().lower()
    allowed_statuses = {"manual", "trialing", "active", "past_due", "paused", "canceled"}
    if status not in allowed_statuses:
        raise ValueError("Stato abbonamento non valido.")
    stamp = now_iso()
    existing = row("SELECT tenant_id FROM tenant_subscriptions WHERE tenant_id=?", (tenant_id,))
    if existing:
        execute(
            """UPDATE tenant_subscriptions SET plan_code=?,status=?,external_customer_id=?,
               external_subscription_id=?,updated_at=? WHERE tenant_id=?""",
            (code, status, str(external_customer_id or ""), str(external_subscription_id or ""), stamp, tenant_id),
        )
    else:
        execute(
            """INSERT INTO tenant_subscriptions(tenant_id,plan_code,status,external_customer_id,
               external_subscription_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
            (tenant_id, code, status, str(external_customer_id or ""), str(external_subscription_id or ""), stamp, stamp),
        )
    execute("UPDATE tenants SET plan_code=?,updated_at=? WHERE id=?", (code, stamp, tenant_id))
    cache_invalidate("entitlements")
    return tenant_entitlements(tenant_id, use_cache=False)


def set_entitlement_override(
    tenant_id: int,
    key: str,
    *,
    kind: str,
    enabled: bool | None = None,
    limit_value: int | float | None = None,
) -> None:
    ensure_entitlement_schema()
    tenant_id = int(tenant_id)
    key = str(key or "").strip()
    kind = str(kind or "").strip().lower()
    if not key or kind not in {"feature", "limit"}:
        raise ValueError("Override entitlement non valido.")
    stamp = now_iso()
    execute(
        """INSERT INTO tenant_entitlement_overrides(
            tenant_id,entitlement_key,entitlement_kind,enabled,limit_value,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(tenant_id,entitlement_key,entitlement_kind)
        DO UPDATE SET enabled=excluded.enabled,limit_value=excluded.limit_value,updated_at=excluded.updated_at""",
        (
            tenant_id,
            key,
            kind,
            None if enabled is None else (1 if enabled else 0),
            None if limit_value is None else float(limit_value),
            stamp,
            stamp,
        ),
    )
    cache_invalidate("entitlements")


def clear_entitlement_override(tenant_id: int, key: str, kind: str) -> None:
    ensure_entitlement_schema()
    execute(
        "DELETE FROM tenant_entitlement_overrides WHERE tenant_id=? AND entitlement_key=? AND entitlement_kind=?",
        (int(tenant_id), str(key or "").strip(), str(kind or "").strip().lower()),
    )
    cache_invalidate("entitlements")


def _owned_seller_ids(tenant_id: int) -> list[int]:
    return [
        int(item["seller_id"])
        for item in rows(
            "SELECT seller_id FROM tenant_sellers WHERE tenant_id=? AND active=1 ORDER BY seller_id",
            (int(tenant_id),),
        )
    ]


def tenant_resource_usage(tenant_id: int) -> dict[str, int | float]:
    ensure_entitlement_schema()
    tenant_id = int(tenant_id)
    seller_ids = _owned_seller_ids(tenant_id)
    placeholders = ",".join("?" for _ in seller_ids)
    users = row(
        "SELECT COUNT(*) total FROM tenant_memberships WHERE tenant_id=? AND active=1",
        (tenant_id,),
    )
    result: dict[str, int | float] = {
        "users": int((users or {}).get("total") or 0),
        "sellers": len(seller_ids),
        "marketplaces": 0,
        "marketplace_accounts": 0,
        "suppliers": 0,
        "price_lists": 0,
    }
    if seller_ids:
        account = row(
            f"""SELECT COUNT(*) total,COUNT(DISTINCT lower(marketplace)) distinct_marketplaces
                FROM marketplace_accounts WHERE active=1 AND seller_id IN ({placeholders})""",
            tuple(seller_ids),
        ) or {}
        result["marketplace_accounts"] = int(account.get("total") or 0)
        result["marketplaces"] = int(account.get("distinct_marketplaces") or 0)
        supplier = row(
            f"SELECT COUNT(DISTINCT id) total FROM suppliers WHERE owner_seller_id IN ({placeholders})",
            tuple(seller_ids),
        ) or {}
        result["suppliers"] = int(supplier.get("total") or 0)
        price_lists = row(
            f"SELECT COUNT(DISTINCT id) total FROM price_lists WHERE owner_seller_id IN ({placeholders}) AND active=1",
            tuple(seller_ids),
        ) or {}
        result["price_lists"] = int(price_lists.get("total") or 0)
    # Monthly counters are event based and complement current resource counts.
    for item in rows(
        "SELECT metric,used_value FROM tenant_usage_monthly WHERE tenant_id=? AND period_ym=?",
        (tenant_id, current_period()),
    ):
        result[str(item["metric"])] = float(item.get("used_value") or 0)
    return result


def _effective_raw(tenant_id: int) -> dict[str, Any]:
    sub = tenant_subscription(tenant_id)
    if not sub:
        return {}
    plan = plan_record(sub.get("plan_code") or "legacy") or plan_record("legacy") or {}
    features = {str(x): True for x in plan.get("features") or []}
    limits = dict(plan.get("limits") or {})
    for item in rows(
        """SELECT entitlement_key,entitlement_kind,enabled,limit_value
           FROM tenant_entitlement_overrides WHERE tenant_id=?""",
        (int(tenant_id),),
    ):
        key = str(item.get("entitlement_key") or "")
        if item.get("entitlement_kind") == "feature" and item.get("enabled") is not None:
            features[key] = bool(int(item.get("enabled") or 0))
        elif item.get("entitlement_kind") == "limit":
            limits[key] = _limit_value(item.get("limit_value"))
    status = str(sub.get("status") or "manual").lower()
    active = status in {"manual", "trialing", "active"}
    return {
        "tenant_id": int(tenant_id),
        "plan_code": str(plan.get("code") or sub.get("plan_code") or "legacy"),
        "plan_name": str(plan.get("name") or ""),
        "status": status,
        "active": active,
        "monthly_price_cents": int(plan.get("monthly_price_cents") or 0),
        "currency": str(plan.get("currency") or "EUR"),
        "features": features,
        "limits": limits,
    }


def tenant_entitlements(tenant_id: int, *, use_cache: bool = True) -> dict[str, Any]:
    ensure_entitlement_schema()
    tenant_id = int(tenant_id)

    def load() -> dict[str, Any]:
        base = _effective_raw(tenant_id)
        if not base:
            return {}
        usage = tenant_resource_usage(tenant_id)
        remaining: dict[str, int | float | None] = {}
        metric_map = {
            "max_users": "users",
            "max_sellers": "sellers",
            "max_marketplaces": "marketplaces",
            "max_suppliers": "suppliers",
            "max_price_lists": "price_lists",
            "monthly_orders": "orders",
            "monthly_background_jobs": "background_jobs",
        }
        for limit_key, limit in (base.get("limits") or {}).items():
            if limit is None:
                remaining[limit_key] = None
                continue
            metric = metric_map.get(limit_key, limit_key)
            remaining[limit_key] = max(0, float(limit) - float(usage.get(metric) or 0))
        base["usage"] = usage
        base["remaining"] = remaining
        return base

    if not use_cache:
        return load()
    return cache_get_or_set("entitlements", f"tenant:{tenant_id}", load, ttl_seconds=20)


def feature_enabled(tenant_id: int, feature: str) -> bool:
    tenant_id = int(tenant_id)
    if tenant_id <= 0:
        return False
    ent = tenant_entitlements(tenant_id)
    if not ent or not ent.get("active"):
        return False
    return bool((ent.get("features") or {}).get(str(feature or "").strip(), False))


def require_tenant_feature(tenant_id: int, feature: str) -> None:
    if not feature_enabled(tenant_id, feature):
        ent = tenant_entitlements(tenant_id)
        plan = str(ent.get("plan_code") or "")
        raise PermissionError(f"Funzione '{feature}' non disponibile nel piano {plan or 'corrente'}.")


def limit_for(tenant_id: int, limit_key: str) -> int | float | None:
    ent = tenant_entitlements(int(tenant_id))
    return (ent.get("limits") or {}).get(str(limit_key or ""))


def assert_capacity(
    tenant_id: int,
    limit_key: str,
    current_value: int | float,
    *,
    increment: int | float = 1,
    label: str = "risorse",
) -> None:
    limit = limit_for(tenant_id, limit_key)
    if limit is None:
        return
    if float(current_value) + float(increment) > float(limit):
        raise PermissionError(
            f"Limite piano raggiunto per {label}: {current_value}/{int(limit) if float(limit).is_integer() else limit}."
        )


def assert_resource_capacity(tenant_id: int, limit_key: str, *, increment: int = 1) -> None:
    usage = tenant_resource_usage(int(tenant_id))
    mapping = {
        "max_users": ("users", "utenti"),
        "max_sellers": ("sellers", "Seller"),
        "max_marketplaces": ("marketplaces", "marketplace"),
        "max_suppliers": ("suppliers", "fornitori"),
        "max_price_lists": ("price_lists", "listini"),
    }
    metric, label = mapping.get(limit_key, (limit_key, limit_key))
    assert_capacity(tenant_id, limit_key, float(usage.get(metric) or 0), increment=increment, label=label)


def assert_marketplace_capacity(tenant_id: int, marketplace: str) -> None:
    """Enforce number of distinct marketplace integrations, not account count."""
    ensure_entitlement_schema()
    tenant_id = int(tenant_id)
    market = str(marketplace or "").strip().lower()
    if not market:
        raise ValueError("Marketplace non valido.")
    seller_ids = _owned_seller_ids(tenant_id)
    if not seller_ids:
        assert_capacity(tenant_id, "max_marketplaces", 0, increment=1, label="marketplace")
        return
    placeholders = ",".join("?" for _ in seller_ids)
    existing = row(
        f"SELECT 1 found FROM marketplace_accounts WHERE active=1 AND lower(marketplace)=? AND seller_id IN ({placeholders}) LIMIT 1",
        (market, *seller_ids),
    )
    if existing:
        return
    usage = tenant_resource_usage(tenant_id)
    assert_capacity(
        tenant_id,
        "max_marketplaces",
        float(usage.get("marketplaces") or 0),
        increment=1,
        label="marketplace",
    )


def record_usage(tenant_id: int, metric: str, amount: int | float = 1, *, period_ym: str = "") -> float:
    ensure_entitlement_schema()
    tenant_id = int(tenant_id)
    metric = str(metric or "").strip()
    if tenant_id <= 0 or not metric:
        return 0.0
    period = str(period_ym or current_period())
    stamp = now_iso()
    execute(
        """INSERT INTO tenant_usage_monthly(tenant_id,period_ym,metric,used_value,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(tenant_id,period_ym,metric)
           DO UPDATE SET used_value=tenant_usage_monthly.used_value+excluded.used_value,
                         updated_at=excluded.updated_at""",
        (tenant_id, period, metric, float(amount), stamp),
    )
    cache_invalidate("entitlements")
    found = row(
        "SELECT used_value FROM tenant_usage_monthly WHERE tenant_id=? AND period_ym=? AND metric=?",
        (tenant_id, period, metric),
    )
    return float((found or {}).get("used_value") or 0)


def job_feature(kind: str) -> str:
    text = str(kind or "").strip().lower()
    for prefix, feature in JOB_FEATURE_PREFIXES:
        if text.startswith(prefix):
            return feature
    return ""


def validate_plan_for_tenant_type(plan_code: str, tenant_type: str) -> str:
    ensure_entitlement_schema()
    code = str(plan_code or "").strip().lower()
    if not code:
        code = "agency" if str(tenant_type or "merchant").lower() == "agency" else "starter"
    plan = plan_record(code)
    if not plan:
        raise ValueError("Piano SaaS non valido.")
    expected = str(plan.get("tenant_type") or "any")
    actual = str(tenant_type or "merchant").strip().lower()
    if expected not in {"any", actual}:
        raise ValueError(f"Il piano {code} non è compatibile con tenant_type={actual}.")
    return code
