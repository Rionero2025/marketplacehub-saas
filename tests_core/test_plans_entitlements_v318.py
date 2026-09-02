from pathlib import Path

import pytest

from api.main import app
from services.entitlements import DEFAULT_PLANS, STANDARD_FEATURES, assert_capacity, job_feature
from services.tenant_db import RLS_TABLES


def test_v318_commercial_baseline_is_encoded_without_inventing_unagreed_volume_limits():
    assert DEFAULT_PLANS["starter"]["monthly_price_cents"] == 2900
    assert DEFAULT_PLANS["starter"]["limits"]["max_marketplaces"] == 1
    assert DEFAULT_PLANS["starter"]["limits"]["max_suppliers"] == 1
    assert DEFAULT_PLANS["growth"]["monthly_price_cents"] == 3900
    assert DEFAULT_PLANS["growth"]["limits"]["max_marketplaces"] == 1
    assert DEFAULT_PLANS["growth"]["limits"]["max_suppliers"] == 3
    assert DEFAULT_PLANS["pro"]["monthly_price_cents"] == 5900
    assert DEFAULT_PLANS["pro"]["limits"]["max_marketplaces"] == 2
    assert DEFAULT_PLANS["pro"]["limits"]["max_suppliers"] == 3
    assert DEFAULT_PLANS["unlimited"]["monthly_price_cents"] == 49900
    assert DEFAULT_PLANS["starter"]["limits"]["monthly_orders"] is None
    assert "accounting" in STANDARD_FEATURES
    assert "marketplace_orders" in STANDARD_FEATURES


def test_v318_entitlement_routes_exist():
    paths = {route.path for route in app.routes}
    assert "/api/v1/plans" in paths
    assert "/api/v1/tenants/{tenant_id}/entitlements" in paths
    assert "/api/v1/tenants/{tenant_id}/plan" in paths
    assert "/api/v1/tenants/{tenant_id}/entitlements/{key}" in paths


def test_v318_jobs_map_to_plan_features():
    assert job_feature("orders.kaufland.sync") == "marketplace_orders"
    assert job_feature("accounting.costs.refresh") == "accounting"
    assert job_feature("buybox.kaufland.quick") == "buybox"
    assert job_feature("packlink.quotes.mass") == "packlink"
    assert job_feature("catalog.materialize") == "work_lists"


def test_capacity_helper_blocks_only_when_limit_is_exceeded(monkeypatch):
    import services.entitlements as ent

    monkeypatch.setattr(ent, "limit_for", lambda tenant_id, key: 2)
    ent.assert_capacity(7, "max_marketplaces", 1, increment=1, label="marketplace")
    with pytest.raises(PermissionError):
        ent.assert_capacity(7, "max_marketplaces", 2, increment=1, label="marketplace")


def test_v318_subscription_state_is_inside_postgresql_rls_boundary():
    assert "tenant_subscriptions" in RLS_TABLES
    assert "tenant_entitlement_overrides" in RLS_TABLES
    assert "tenant_usage_monthly" in RLS_TABLES
    root = Path(__file__).resolve().parents[1]
    dependency_source = (root / "api" / "dependencies.py").read_text(encoding="utf-8")
    assert "PLAN_ENTITLEMENT_REQUIRED" in dependency_source
    worker_source = (root / "services" / "background_jobs.py").read_text(encoding="utf-8")
    assert "require_tenant_feature" in worker_source
