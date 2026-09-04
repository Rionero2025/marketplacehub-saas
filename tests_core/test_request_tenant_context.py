"""Exercise FastAPI's real dependency/thread boundary without a live database."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.dependencies as dependencies
from api.routers import sellers
from services import background_jobs, entitlements, tenant_db


@pytest.fixture
def app(monkeypatch):
    def session(token):
        if token not in {"tenant-11", "tenant-22"}:
            return None
        tenant_id = int(token.split("-")[1])
        return {
            "id": tenant_id, "username": token, "active_tenant_id": tenant_id,
            "tenant_ids": [tenant_id], "seller_ids": [tenant_id * 10],
            "permissions": ["dashboard"], "is_admin": False,
        }

    def feature_enabled(tenant_id, feature):
        assert tenant_db.current_tenant_id() == tenant_id
        assert not tenant_db.platform_bypass_enabled()
        return True

    def seller_rows(sql, params):
        # Model the RLS predicate for the real sellers endpoint.
        tenant_id = tenant_db.current_tenant_id()
        if tenant_id and tenant_id * 10 in params:
            return [{"id": tenant_id * 10, "name": f"Seller {tenant_id}", "active": 1}]
        return []

    monkeypatch.setattr(dependencies, "session_user", session)
    monkeypatch.setattr(entitlements, "feature_enabled", feature_enabled)
    monkeypatch.setattr(sellers, "rows", seller_rows)
    application = FastAPI()
    application.include_router(sellers.router, prefix="/api/v1")

    @application.get("/context")
    async def context(user: dependencies.CurrentUser):
        await asyncio.sleep(0)
        return {"user": user.active_tenant_id, "database": tenant_db.current_tenant_id()}

    @application.get("/failure")
    def failure(user: dependencies.CurrentUser):
        assert tenant_db.current_tenant_id() == user.active_tenant_id
        raise RuntimeError("simulated endpoint failure")

    return application


def test_sellers_and_permission_dependency_receive_request_tenant(app):
    with TestClient(app) as client:
        response = client.get("/api/v1/sellers", headers={"Authorization": "Bearer tenant-11"})
    assert response.status_code == 200
    assert response.json() == [{"id": 110, "name": "Seller 11", "legal_name": "", "active": True}]
    assert tenant_db.current_tenant_id() == 0


def test_concurrent_requests_and_failures_do_not_leak_tenant(app):
    async def exercise():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async def fetch(tenant_id):
                r = await client.get("/context", headers={"Authorization": f"Bearer tenant-{tenant_id}"})
                assert r.status_code == 200
                assert r.json() == {"user": tenant_id, "database": tenant_id}
                assert tenant_db.current_tenant_id() == 0
            await asyncio.gather(*(fetch(t) for t in [11, 22] * 5))
            r = await client.get("/failure", headers={"Authorization": "Bearer tenant-11"})
            assert r.status_code == 500
            assert tenant_db.current_tenant_id() == 0
            assert not tenant_db.platform_bypass_enabled()
            await fetch(22)
            r = await client.get("/context")
            assert r.status_code == 401
            assert tenant_db.current_tenant_id() == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("allowed", [True, False])
def test_worker_checks_plan_inside_tenant_scope(monkeypatch, allowed):
    called = []
    monkeypatch.setattr(tenant_db, "assert_seller_in_tenant", lambda seller, tenant: None)

    def require_feature(tenant_id, feature):
        assert tenant_db.current_tenant_id() == tenant_id == 11
        assert not tenant_db.platform_bypass_enabled()
        if not allowed:
            raise PermissionError("subscription suspended")

    def handler(job):
        assert tenant_db.current_tenant_id() == 11
        called.append(job["id"])
        return {"saved": 3}

    monkeypatch.setattr(entitlements, "require_tenant_feature", require_feature)
    monkeypatch.setattr(background_jobs, "_run_orders_kaufland", handler)
    job = {"id": "local-test", "kind": "orders.kaufland.sync", "tenant_id": "11", "seller_id": 110}
    if allowed:
        assert background_jobs.execute_claimed_job(job) == {"saved": 3}
        assert called == ["local-test"]
    else:
        with pytest.raises(PermissionError, match="suspended"):
            background_jobs.execute_claimed_job(job)
        assert not called
    assert tenant_db.current_tenant_id() == 0
