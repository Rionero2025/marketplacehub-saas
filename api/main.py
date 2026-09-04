from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import API_VERSION
from api.routers import accounting, auth, billing, buybox, catalogs, dashboard, health, jobs, onboarding, orders, plans, sellers, tenants
from api.session_store import ensure_api_session_schema
from services.db import init_db
from services.performance_indexes import ensure_performance_indexes
from services.tenancy import ensure_tenancy_schema
from services.entitlements import ensure_entitlement_schema
from services.billing import ensure_billing_schema
from services.onboarding import ensure_onboarding_schema
from services.tenant_db import ensure_tenant_database_isolation, platform_database_scope


def _cors_origins() -> list[str]:
    raw = str(os.getenv("MARKETPLACE_HUB_CORS_ORIGINS") or "").strip()
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Startup/migrations are platform maintenance and intentionally bypass RLS.
    with platform_database_scope():
        init_db()
        ensure_tenancy_schema()
        ensure_entitlement_schema()
        ensure_billing_schema()
        ensure_onboarding_schema()
        ensure_api_session_schema()
        try:
            ensure_performance_indexes()
        except Exception:
            pass
        ensure_tenant_database_isolation()
    yield


docs_enabled = str(os.getenv("MARKETPLACE_HUB_API_DOCS", "1")).strip().lower() not in {
    "0", "false", "no", "off"
}
app = FastAPI(
    title="Marketplace Hub API",
    version=API_VERSION,
    description="API SaaS-ready sopra il Marketplace Hub Performance Core.",
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    lifespan=lifespan,
)

origins = _cors_origins()
if origins:
    wildcard = "*" in origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if wildcard else origins,
        allow_credentials=not wildcard,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )

app.include_router(health.router)
for router in (
    auth.router,
    onboarding.router,
    tenants.router,
    plans.router,
    billing.router,
    sellers.router,
    dashboard.router,
    orders.router,
    accounting.router,
    buybox.router,
    catalogs.router,
    jobs.router,
):
    app.include_router(router, prefix="/api/v1")


@app.get("/")
def root() -> dict:
    return {
        "service": "Marketplace Hub API",
        "api_version": API_VERSION,
        "health": "/health",
        "docs": "/docs" if docs_enabled else "disabled",
    }
