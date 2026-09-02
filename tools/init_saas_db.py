from __future__ import annotations

"""Initialize the persistent SaaS staging schema before each API deploy.

This script is intentionally idempotent. It performs platform-level maintenance
outside tenant RLS, then exits. Individual domain modules still keep their own
lazy schema guards for backward compatibility.
"""

from api.session_store import ensure_api_session_schema
from services.background_jobs import ensure_job_schema
from services.billing import ensure_billing_schema
from services.catalog_sharing import ensure_catalog_sharing_schema
from services.db import init_db
from services.entitlements import ensure_entitlement_schema
from services.onboarding import ensure_onboarding_schema
from services.performance_indexes import ensure_performance_indexes
from services.saved_view_storage import ensure_saved_view_storage_schema
from services.lists import ensure_price_list_storage_schema
from services.tenant_db import ensure_tenant_database_isolation, platform_database_scope
from services.tenancy import ensure_tenancy_schema


def initialize() -> None:
    with platform_database_scope():
        init_db()
        ensure_tenancy_schema()
        ensure_entitlement_schema()
        ensure_billing_schema()
        ensure_onboarding_schema()
        ensure_api_session_schema()
        ensure_job_schema()
        ensure_catalog_sharing_schema()
        ensure_price_list_storage_schema()
        ensure_saved_view_storage_schema()
        try:
            ensure_performance_indexes()
        except Exception:
            # Index creation must never make a staging deploy unrecoverable.
            pass
        ensure_tenant_database_isolation()


if __name__ == "__main__":
    initialize()
    print("Marketplace Hub SaaS staging schema: OK")
