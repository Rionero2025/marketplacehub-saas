from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from api import dependencies
from api.routers import billing, plans, tenants
from services import entitlements, tenancy, tenant_db


def session_record(role="owner", active=11):
    return {"id": 1, "username": "synthetic-owner", "is_admin": False,
            "permissions": ["accounting"], "tenant_ids": [11, 22],
            "active_tenant_id": active, "seller_ids": [110],
            "legacy_seller_ids": [110, 220], "tenant_role": role}


@pytest.mark.parametrize("role,expected", [("owner", 200), ("admin", 200),
    ("manager", 200), ("operator", 200), ("viewer", 403), ("", 403), ("unknown", 403)])
def test_membership_caps_writes_despite_global_menu_permissions(monkeypatch, role, expected):
    monkeypatch.setattr(dependencies, "session_user", lambda token: session_record(role))
    monkeypatch.setattr(entitlements, "feature_enabled", lambda *args: True)
    app = FastAPI()
    writes = []

    @app.get("/records")
    def read(user=Depends(dependencies.require_permission("accounting"))):
        return []

    @app.post("/records")
    def write(user=Depends(dependencies.require_permission("accounting"))):
        writes.append(user.id)
        return {}

    with TestClient(app) as client:
        assert client.get("/records").status_code == 200
        assert client.post("/records").status_code == expected
    assert writes == ([1] if expected == 200 else [])


def test_target_tenant_scope_is_authorized_and_restored(monkeypatch):
    monkeypatch.setattr(dependencies, "session_user", lambda token: session_record())
    def events(tenant_id, **kwargs):
        assert tenant_db.current_tenant_id() == tenant_id
        assert not tenant_db.platform_bypass_enabled()
        if tenant_id == 11:
            raise RuntimeError("synthetic failure")
        return [{"tenant_id": tenant_id}]
    monkeypatch.setattr(billing, "billing_events", events)
    app = FastAPI()
    app.include_router(billing.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/tenants/22/billing/events").json() == [{"tenant_id": 22}]
        assert client.get("/tenants/33/billing/events").status_code == 404
        assert client.get("/tenants/11/billing/events").status_code == 500
        assert client.get("/tenants/22/billing/events").status_code == 200
    assert tenant_db.current_tenant_id() == 0
    assert not tenant_db.platform_bypass_enabled()


def test_target_tenant_keeps_legacy_seller_restriction(monkeypatch):
    monkeypatch.setattr(dependencies, "session_user", lambda token: session_record())
    monkeypatch.setattr(tenants, "tenant_seller_ids", lambda tid: [220, 221])
    app = FastAPI()
    app.include_router(tenants.router)
    with TestClient(app) as client:
        assert client.get("/tenants/22/sellers").json()["seller_ids"] == [220]


@pytest.mark.parametrize("admin", [False, True])
def test_billing_write_requires_platform_admin_and_target_scope(monkeypatch, admin):
    monkeypatch.setattr(dependencies, "session_user", lambda token: {**session_record(), "is_admin": admin})
    calls = []
    def start(tenant_id, plan_code, **kwargs):
        assert tenant_db.current_tenant_id() == tenant_id == 22
        assert not tenant_db.platform_bypass_enabled()
        calls.append(tenant_id)
        return {"tenant_id": tenant_id, "plan_code": plan_code, "status": "trialing"}
    monkeypatch.setattr(billing, "start_trial", start)
    app = FastAPI()
    app.include_router(billing.router)
    with TestClient(app) as client:
        response = client.post("/tenants/22/billing/trial", json={"plan_code": "starter"})
        assert response.status_code == (200 if admin else 403)
    assert calls == ([22] if admin else [])
    assert tenant_db.current_tenant_id() == 0


def test_plan_read_uses_explicit_target_scope(monkeypatch):
    monkeypatch.setattr(dependencies, "session_user", lambda token: session_record())
    def entitlements(tenant_id):
        assert tenant_db.current_tenant_id() == tenant_id == 22
        return {"tenant_id": tenant_id, "plan_code": "starter", "status": "active", "active": True}
    monkeypatch.setattr(plans, "tenant_entitlements", entitlements)
    app = FastAPI()
    app.include_router(plans.router)
    with TestClient(app) as client:
        assert client.get("/tenants/22/entitlements").json()["tenant_id"] == 22


@pytest.mark.parametrize("role", ["viewer", "operator", "owner"])
def test_agency_client_inherits_membership_role(monkeypatch, role):
    monkeypatch.setattr(tenancy, "ensure_tenancy_schema", lambda: None)
    def rows(sql, params):
        if "FROM tenant_memberships" in sql:
            return [{"id": 11, "tenant_type": "agency", "role": role}]
        assert params == (11,)
        return [{"id": 22, "tenant_type": "merchant", "agency_tenant_id": 11}]
    monkeypatch.setattr(tenancy, "rows", rows)
    contexts = tenancy.accessible_tenants_for_user(1)
    assert {item["id"] for item in contexts} == {11, 22}
    assert next(item for item in contexts if item["id"] == 22)["role"] == role


def test_direct_viewer_membership_is_not_elevated_by_agency(monkeypatch):
    monkeypatch.setattr(tenancy, "ensure_tenancy_schema", lambda: None)
    monkeypatch.setattr(tenancy, "rows", lambda sql, params: (
        [{"id": 11, "tenant_type": "agency", "role": "owner"},
         {"id": 22, "tenant_type": "merchant", "role": "viewer"}]
        if "FROM tenant_memberships" in sql else
        [{"id": 22, "tenant_type": "merchant", "agency_tenant_id": 11}]
    ))
    assert tenancy.tenant_context_for_user(1, 22)["role"] == "viewer"
