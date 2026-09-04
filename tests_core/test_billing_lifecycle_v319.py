from datetime import datetime, timedelta, timezone
from pathlib import Path

from api.main import app
from services.billing import period_end_for, subscription_access_active
from services.tenant_db import RLS_TABLES


def test_v319_billing_routes_exist():
    paths = set(app.openapi()["paths"])
    expected = {
        "/api/v1/tenants/{tenant_id}/billing",
        "/api/v1/tenants/{tenant_id}/billing/events",
        "/api/v1/tenants/{tenant_id}/billing/trial",
        "/api/v1/tenants/{tenant_id}/billing/activate",
        "/api/v1/tenants/{tenant_id}/billing/payment-success",
        "/api/v1/tenants/{tenant_id}/billing/payment-failed",
        "/api/v1/tenants/{tenant_id}/billing/suspend",
        "/api/v1/tenants/{tenant_id}/billing/resume",
        "/api/v1/tenants/{tenant_id}/billing/cancel",
        "/api/v1/tenants/{tenant_id}/billing/plan-change",
        "/api/v1/tenants/{tenant_id}/billing/refresh",
    }
    assert expected <= paths


def test_v319_past_due_keeps_access_only_inside_grace_period():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    active = {"status": "past_due", "grace_period_end": (now + timedelta(days=3)).isoformat()}
    expired = {"status": "past_due", "grace_period_end": (now - timedelta(seconds=1)).isoformat()}
    assert subscription_access_active(active, at=now)
    assert not subscription_access_active(expired, at=now)


def test_v319_trial_and_canceled_access_rules():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    assert subscription_access_active({"status":"trialing","trial_end":(now+timedelta(days=1)).isoformat()}, at=now)
    assert not subscription_access_active({"status":"trialing","trial_end":(now-timedelta(seconds=1)).isoformat()}, at=now)
    assert not subscription_access_active({"status":"suspended"}, at=now)
    assert not subscription_access_active({"status":"canceled"}, at=now)


def test_v319_monthly_period_handles_end_of_month():
    start = datetime(2026, 1, 31, 10, 0, tzinfo=timezone.utc)
    assert period_end_for(start, "monthly").date().isoformat() == "2026-02-28"
    leap = datetime(2028, 1, 31, 10, 0, tzinfo=timezone.utc)
    assert period_end_for(leap, "monthly").date().isoformat() == "2028-02-29"


def test_v319_billing_events_are_rls_protected_and_stripe_is_optional():
    assert "billing_events" in RLS_TABLES
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "stripe" not in requirements
    source = (root / "services" / "billing.py").read_text(encoding="utf-8")
    assert 'BILLING_PROVIDERS = {"manual", "stripe"}' in source
