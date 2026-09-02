from __future__ import annotations

import calendar
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from services.db import execute, row, rows
from services.shared_cache import cache_invalidate

_SCHEMA_READY = False
BILLING_STATUSES = {
    "manual",
    "trialing",
    "active",
    "past_due",
    "suspended",
    "paused",      # legacy alias, normalized to suspended
    "canceled",
}
BILLING_INTERVALS = {"monthly", "annual", "manual"}
BILLING_PROVIDERS = {"manual", "stripe"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat(timespec="seconds")


def _iso(value: datetime | str | None) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return ""
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _loads(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _normalize_status(value: str) -> str:
    status = str(value or "manual").strip().lower()
    if status == "paused":
        return "suspended"
    if status == "cancelled":
        return "canceled"
    return status if status in BILLING_STATUSES else "manual"


def _normalize_interval(value: str) -> str:
    interval = str(value or "monthly").strip().lower()
    return interval if interval in BILLING_INTERVALS else "monthly"


def _normalize_provider(value: str) -> str:
    provider = str(value or "manual").strip().lower()
    return provider if provider in BILLING_PROVIDERS else "manual"


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + int(months)
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def period_end_for(start: datetime, interval: str) -> datetime:
    interval = _normalize_interval(interval)
    if interval == "annual":
        return _add_months(start, 12)
    if interval == "manual":
        return start
    return _add_months(start, 1)


def _has_column(table: str, column: str) -> bool:
    try:
        row(f"SELECT {column} FROM {table} LIMIT 1")
        return True
    except Exception:
        return False


def _ensure_column(table: str, column: str, ddl: str) -> None:
    if not _has_column(table, column):
        execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_billing_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    from services.entitlements import ensure_entitlement_schema
    ensure_entitlement_schema()

    additions = {
        "provider": "TEXT NOT NULL DEFAULT 'manual'",
        "billing_interval": "TEXT NOT NULL DEFAULT 'monthly'",
        "trial_start": "TEXT NOT NULL DEFAULT ''",
        "trial_end": "TEXT NOT NULL DEFAULT ''",
        "grace_period_end": "TEXT NOT NULL DEFAULT ''",
        "cancel_requested_at": "TEXT NOT NULL DEFAULT ''",
        "canceled_at": "TEXT NOT NULL DEFAULT ''",
        "suspended_at": "TEXT NOT NULL DEFAULT ''",
        "ended_at": "TEXT NOT NULL DEFAULT ''",
        "next_plan_code": "TEXT NOT NULL DEFAULT ''",
        "next_plan_effective_at": "TEXT NOT NULL DEFAULT ''",
        "last_payment_at": "TEXT NOT NULL DEFAULT ''",
        "last_payment_status": "TEXT NOT NULL DEFAULT ''",
        "last_payment_reference": "TEXT NOT NULL DEFAULT ''",
        "last_payment_amount_cents": "INTEGER NOT NULL DEFAULT 0",
        "manual_until": "TEXT NOT NULL DEFAULT ''",
        "status_reason": "TEXT NOT NULL DEFAULT ''",
    }
    for column, ddl in additions.items():
        _ensure_column("tenant_subscriptions", column, ddl)

    execute(
        """
        CREATE TABLE IF NOT EXISTS billing_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            old_status TEXT NOT NULL DEFAULT '',
            new_status TEXT NOT NULL DEFAULT '',
            old_plan_code TEXT NOT NULL DEFAULT '',
            new_plan_code TEXT NOT NULL DEFAULT '',
            amount_cents INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'EUR',
            provider TEXT NOT NULL DEFAULT 'manual',
            external_event_id TEXT,
            reference TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    execute("CREATE INDEX IF NOT EXISTS idx_billing_events_tenant_created ON billing_events(tenant_id,created_at)")
    execute("CREATE INDEX IF NOT EXISTS idx_billing_events_external ON billing_events(external_event_id)")
    _SCHEMA_READY = True


def subscription_record(tenant_id: int) -> dict[str, Any]:
    ensure_billing_schema()
    found = row(
        """SELECT s.*,t.name tenant_name,t.tenant_type,t.plan_code tenant_plan_code
           FROM tenants t LEFT JOIN tenant_subscriptions s ON s.tenant_id=t.id
           WHERE t.id=?""",
        (int(tenant_id),),
    )
    if not found:
        return {}
    item = dict(found)
    item["status"] = _normalize_status(item.get("status") or "manual")
    item["provider"] = _normalize_provider(item.get("provider") or "manual")
    item["billing_interval"] = _normalize_interval(item.get("billing_interval") or "monthly")
    item["cancel_at_period_end"] = bool(int(item.get("cancel_at_period_end") or 0))
    item["settings"] = _loads(item.get("settings_json"))
    return item


def subscription_access_active(subscription: dict[str, Any], *, at: datetime | None = None) -> bool:
    if not subscription:
        return False
    at = at or now_utc()
    status = _normalize_status(str(subscription.get("status") or "manual"))
    if status in {"suspended", "canceled"}:
        return False
    if status == "trialing":
        end = _dt(subscription.get("trial_end"))
        return bool(end is None or at < end)
    if status == "past_due":
        grace = _dt(subscription.get("grace_period_end"))
        return bool(grace is not None and at < grace)
    if status == "manual":
        manual_until = _dt(subscription.get("manual_until"))
        return bool(manual_until is None or at < manual_until)
    if status == "active":
        end = _dt(subscription.get("current_period_end"))
        return bool(end is None or at < end)
    return False


def _event(
    tenant_id: int,
    event_type: str,
    *,
    old: dict[str, Any] | None = None,
    new: dict[str, Any] | None = None,
    amount_cents: int = 0,
    currency: str = "EUR",
    provider: str = "manual",
    external_event_id: str | None = None,
    reference: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    ensure_billing_schema()
    if external_event_id:
        duplicate = row("SELECT id FROM billing_events WHERE external_event_id=? LIMIT 1", (external_event_id,))
        if duplicate:
            return
    execute(
        """INSERT INTO billing_events(
            tenant_id,event_type,old_status,new_status,old_plan_code,new_plan_code,
            amount_cents,currency,provider,external_event_id,reference,metadata_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(tenant_id), str(event_type), str((old or {}).get("status") or ""),
            str((new or {}).get("status") or ""), str((old or {}).get("plan_code") or ""),
            str((new or {}).get("plan_code") or ""), int(amount_cents or 0),
            str(currency or "EUR").upper(), _normalize_provider(provider), external_event_id,
            str(reference or ""), _json(metadata), now_iso(),
        ),
    )


def billing_events(tenant_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    ensure_billing_schema()
    return [
        {**dict(item), "metadata": _loads(item.get("metadata_json"))}
        for item in rows(
            "SELECT * FROM billing_events WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
            (int(tenant_id), max(1, min(int(limit), 500))),
        )
    ]


def _validate_plan(tenant_id: int, plan_code: str) -> str:
    from services.entitlements import validate_plan_for_tenant_type
    tenant = row("SELECT tenant_type FROM tenants WHERE id=?", (int(tenant_id),))
    if not tenant:
        raise ValueError("Tenant non trovato.")
    return validate_plan_for_tenant_type(plan_code, str(tenant.get("tenant_type") or "merchant"))


def _update_subscription(tenant_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    if not fields:
        return subscription_record(tenant_id)
    allowed = {
        "plan_code", "status", "provider", "billing_interval", "current_period_start", "current_period_end",
        "trial_start", "trial_end", "grace_period_end", "cancel_at_period_end", "cancel_requested_at",
        "canceled_at", "suspended_at", "ended_at", "next_plan_code", "next_plan_effective_at",
        "last_payment_at", "last_payment_status", "last_payment_reference", "last_payment_amount_cents",
        "manual_until", "status_reason", "external_customer_id", "external_subscription_id", "settings_json",
    }
    clean = {k: v for k, v in fields.items() if k in allowed}
    clean["updated_at"] = now_iso()
    assignments = ",".join(f"{key}=?" for key in clean)
    execute(
        f"UPDATE tenant_subscriptions SET {assignments} WHERE tenant_id=?",
        tuple(clean.values()) + (int(tenant_id),),
    )
    if "plan_code" in clean:
        execute("UPDATE tenants SET plan_code=?,updated_at=? WHERE id=?", (clean["plan_code"], now_iso(), int(tenant_id)))
    cache_invalidate("entitlements")
    return subscription_record(tenant_id)


def start_trial(tenant_id: int, plan_code: str, *, days: int = 14, at: datetime | None = None) -> dict[str, Any]:
    ensure_billing_schema()
    days = max(1, min(int(days), 90))
    at = at or now_utc()
    code = _validate_plan(tenant_id, plan_code)
    old = subscription_record(tenant_id)
    end = at + timedelta(days=days)
    new = _update_subscription(
        tenant_id,
        {
            "plan_code": code, "status": "trialing", "provider": "manual", "billing_interval": "monthly",
            "trial_start": _iso(at), "trial_end": _iso(end), "current_period_start": _iso(at),
            "current_period_end": _iso(end), "grace_period_end": "", "cancel_at_period_end": 0,
            "cancel_requested_at": "", "canceled_at": "", "suspended_at": "", "ended_at": "",
            "next_plan_code": "", "next_plan_effective_at": "", "status_reason": "trial",
        },
    )
    _event(tenant_id, "trial_started", old=old, new=new, provider="manual", metadata={"days": days})
    return new


def activate_subscription(
    tenant_id: int,
    plan_code: str,
    *,
    billing_interval: str = "monthly",
    provider: str = "manual",
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    external_customer_id: str = "",
    external_subscription_id: str = "",
    reference: str = "",
) -> dict[str, Any]:
    ensure_billing_schema()
    code = _validate_plan(tenant_id, plan_code)
    interval = _normalize_interval(billing_interval)
    provider = _normalize_provider(provider)
    start = period_start or now_utc()
    end = period_end if period_end is not None else (None if interval == "manual" else period_end_for(start, interval))
    old = subscription_record(tenant_id)
    new = _update_subscription(
        tenant_id,
        {
            "plan_code": code, "status": "active", "provider": provider, "billing_interval": interval,
            "current_period_start": _iso(start), "current_period_end": _iso(end), "trial_start": "", "trial_end": "",
            "grace_period_end": "", "cancel_at_period_end": 0, "cancel_requested_at": "", "canceled_at": "",
            "suspended_at": "", "ended_at": "", "next_plan_code": "", "next_plan_effective_at": "",
            "status_reason": "activated", "external_customer_id": str(external_customer_id or ""),
            "external_subscription_id": str(external_subscription_id or ""),
        },
    )
    _event(tenant_id, "subscription_activated", old=old, new=new, provider=provider, reference=reference)
    return new


def _external_event_seen(external_event_id: str | None) -> bool:
    event_id = str(external_event_id or "").strip()
    if not event_id:
        return False
    return bool(row("SELECT id FROM billing_events WHERE external_event_id=? LIMIT 1", (event_id,)))


def record_payment_success(
    tenant_id: int,
    *,
    amount_cents: int = 0,
    currency: str = "EUR",
    reference: str = "",
    external_event_id: str | None = None,
    paid_at: datetime | None = None,
    period_end: datetime | None = None,
) -> dict[str, Any]:
    ensure_billing_schema()
    if _external_event_seen(external_event_id):
        return subscription_record(tenant_id)
    old = subscription_record(tenant_id)
    if not old:
        raise ValueError("Abbonamento non trovato.")
    paid = paid_at or now_utc()
    interval = _normalize_interval(str(old.get("billing_interval") or "monthly"))
    current_end = _dt(old.get("current_period_end"))
    start = current_end if current_end and current_end > paid else paid
    end = period_end if period_end is not None else (None if interval == "manual" else period_end_for(start, interval))
    plan_code = str(old.get("plan_code") or "legacy")
    next_plan = str(old.get("next_plan_code") or "").strip().lower()
    effective = _dt(old.get("next_plan_effective_at"))
    if next_plan and (effective is None or effective <= start):
        plan_code = _validate_plan(tenant_id, next_plan)
    new = _update_subscription(
        tenant_id,
        {
            "plan_code": plan_code, "status": "active", "current_period_start": _iso(start),
            "current_period_end": _iso(end), "grace_period_end": "", "suspended_at": "", "ended_at": "",
            "last_payment_at": _iso(paid), "last_payment_status": "succeeded",
            "last_payment_reference": str(reference or ""), "last_payment_amount_cents": int(amount_cents or 0),
            "next_plan_code": "", "next_plan_effective_at": "", "status_reason": "payment_succeeded",
        },
    )
    _event(
        tenant_id, "payment_succeeded", old=old, new=new, amount_cents=int(amount_cents or 0),
        currency=currency, provider=str(old.get("provider") or "manual"), external_event_id=external_event_id,
        reference=reference,
    )
    return new


def record_payment_failed(
    tenant_id: int,
    *,
    grace_days: int | None = None,
    reference: str = "",
    external_event_id: str | None = None,
    failed_at: datetime | None = None,
) -> dict[str, Any]:
    ensure_billing_schema()
    if _external_event_seen(external_event_id):
        return subscription_record(tenant_id)
    old = subscription_record(tenant_id)
    if not old:
        raise ValueError("Abbonamento non trovato.")
    failed = failed_at or now_utc()
    if grace_days is None:
        grace_days = max(0, int(os.getenv("MARKETPLACE_HUB_BILLING_GRACE_DAYS", "7")))
    grace = failed + timedelta(days=max(0, int(grace_days)))
    new = _update_subscription(
        tenant_id,
        {
            "status": "past_due", "grace_period_end": _iso(grace), "last_payment_at": _iso(failed),
            "last_payment_status": "failed", "last_payment_reference": str(reference or ""),
            "status_reason": "payment_failed",
        },
    )
    _event(
        tenant_id, "payment_failed", old=old, new=new, provider=str(old.get("provider") or "manual"),
        external_event_id=external_event_id, reference=reference, metadata={"grace_days": int(grace_days)},
    )
    return new


def suspend_subscription(tenant_id: int, *, reason: str = "manual") -> dict[str, Any]:
    ensure_billing_schema()
    old = subscription_record(tenant_id)
    new = _update_subscription(
        tenant_id,
        {"status": "suspended", "suspended_at": now_iso(), "status_reason": str(reason or "manual")},
    )
    _event(tenant_id, "subscription_suspended", old=old, new=new, provider=str(old.get("provider") or "manual"), metadata={"reason": reason})
    return new


def resume_subscription(tenant_id: int, *, reason: str = "manual") -> dict[str, Any]:
    ensure_billing_schema()
    old = subscription_record(tenant_id)
    if not old:
        raise ValueError("Abbonamento non trovato.")
    now = now_utc()
    end = _dt(old.get("current_period_end"))
    fields: dict[str, Any] = {
        "status": "active", "suspended_at": "", "grace_period_end": "", "status_reason": str(reason or "manual"),
    }
    if end is None or end <= now:
        fields["current_period_start"] = _iso(now)
        fields["current_period_end"] = _iso(period_end_for(now, str(old.get("billing_interval") or "monthly")))
    new = _update_subscription(tenant_id, fields)
    _event(tenant_id, "subscription_resumed", old=old, new=new, provider=str(old.get("provider") or "manual"), metadata={"reason": reason})
    return new


def cancel_subscription(tenant_id: int, *, at_period_end: bool = True, reason: str = "") -> dict[str, Any]:
    ensure_billing_schema()
    old = subscription_record(tenant_id)
    if not old:
        raise ValueError("Abbonamento non trovato.")
    now = now_utc()
    end = _dt(old.get("current_period_end"))
    if at_period_end and end and end > now:
        new = _update_subscription(
            tenant_id,
            {"cancel_at_period_end": 1, "cancel_requested_at": _iso(now), "status_reason": str(reason or "cancel_at_period_end")},
        )
        event_type = "cancellation_scheduled"
    else:
        new = _update_subscription(
            tenant_id,
            {
                "status": "canceled", "cancel_at_period_end": 0, "cancel_requested_at": _iso(now),
                "canceled_at": _iso(now), "ended_at": _iso(now), "status_reason": str(reason or "canceled"),
            },
        )
        event_type = "subscription_canceled"
    _event(tenant_id, event_type, old=old, new=new, provider=str(old.get("provider") or "manual"), metadata={"reason": reason})
    return new


def schedule_plan_change(
    tenant_id: int,
    plan_code: str,
    *,
    immediate: bool = False,
    effective_at: datetime | None = None,
) -> dict[str, Any]:
    ensure_billing_schema()
    code = _validate_plan(tenant_id, plan_code)
    old = subscription_record(tenant_id)
    if not old:
        raise ValueError("Abbonamento non trovato.")
    if immediate:
        new = _update_subscription(tenant_id, {"plan_code": code, "next_plan_code": "", "next_plan_effective_at": ""})
        event = "plan_changed"
    else:
        when = effective_at or _dt(old.get("current_period_end"))
        if when is None:
            raise ValueError("Manca la data di efficacia del cambio piano.")
        new = _update_subscription(tenant_id, {"next_plan_code": code, "next_plan_effective_at": _iso(when)})
        event = "plan_change_scheduled"
    _event(tenant_id, event, old=old, new=new, provider=str(old.get("provider") or "manual"))
    return new


def refresh_subscription_state(tenant_id: int, *, at: datetime | None = None) -> dict[str, Any]:
    ensure_billing_schema()
    at = at or now_utc()
    old = subscription_record(tenant_id)
    if not old:
        return {}
    status = _normalize_status(str(old.get("status") or "manual"))
    fields: dict[str, Any] = {}
    event = ""

    if status == "trialing":
        trial_end = _dt(old.get("trial_end"))
        if trial_end and at >= trial_end:
            fields = {"status": "suspended", "suspended_at": _iso(at), "status_reason": "trial_expired"}
            event = "trial_expired"
    elif status == "past_due":
        grace = _dt(old.get("grace_period_end"))
        if grace and at >= grace:
            fields = {"status": "suspended", "suspended_at": _iso(at), "status_reason": "grace_period_expired"}
            event = "grace_period_expired"
    elif status == "manual":
        manual_until = _dt(old.get("manual_until"))
        if manual_until and at >= manual_until:
            fields = {"status": "suspended", "suspended_at": _iso(at), "status_reason": "manual_period_expired"}
            event = "manual_period_expired"
    elif status == "active":
        period_end = _dt(old.get("current_period_end"))
        if period_end and at >= period_end:
            if bool(old.get("cancel_at_period_end")):
                fields = {
                    "status": "canceled", "cancel_at_period_end": 0, "canceled_at": _iso(at),
                    "ended_at": _iso(at), "status_reason": "canceled_at_period_end",
                }
                event = "subscription_canceled"
            else:
                grace_days = max(0, int(os.getenv("MARKETPLACE_HUB_BILLING_GRACE_DAYS", "7")))
                fields = {
                    "status": "past_due", "grace_period_end": _iso(at + timedelta(days=grace_days)),
                    "status_reason": "renewal_due",
                }
                event = "renewal_due"

    if fields:
        new = _update_subscription(tenant_id, fields)
        _event(tenant_id, event, old=old, new=new, provider=str(old.get("provider") or "manual"))
        return new
    return old


def refresh_all_subscriptions() -> dict[str, int]:
    ensure_billing_schema()
    total = 0
    changed = 0
    for item in rows("SELECT tenant_id,status FROM tenant_subscriptions ORDER BY tenant_id"):
        total += 1
        before = _normalize_status(str(item.get("status") or "manual"))
        after = refresh_subscription_state(int(item["tenant_id"]))
        if after and _normalize_status(str(after.get("status") or "manual")) != before:
            changed += 1
    return {"total": total, "changed": changed}


def billing_snapshot(tenant_id: int, *, refresh: bool = True) -> dict[str, Any]:
    ensure_billing_schema()
    sub = refresh_subscription_state(tenant_id) if refresh else subscription_record(tenant_id)
    if not sub:
        return {}
    from services.entitlements import plan_record
    plan = plan_record(str(sub.get("plan_code") or "legacy")) or {}
    return {
        **sub,
        "access_active": subscription_access_active(sub),
        "plan_name": str(plan.get("name") or ""),
        "monthly_price_cents": int(plan.get("monthly_price_cents") or 0),
        "currency": str(plan.get("currency") or "EUR"),
    }
