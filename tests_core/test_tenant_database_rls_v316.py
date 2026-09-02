from services.tenant_db import (
    current_tenant_id, platform_bypass_enabled, platform_database_scope,
    tenant_database_scope, _policy_expression, RLS_TABLES,
)
from api.dependencies import ApiUser


def test_tenant_context_is_scoped_and_resets():
    before = current_tenant_id()
    with tenant_database_scope(77):
        assert current_tenant_id() == 77
        assert not platform_bypass_enabled()
    assert current_tenant_id() == before


def test_platform_bypass_is_explicit_and_scoped():
    assert not platform_bypass_enabled()
    with platform_database_scope():
        assert platform_bypass_enabled()
    assert not platform_bypass_enabled()


def test_rls_expression_requires_matching_tenant_or_explicit_bypass():
    expr = _policy_expression()
    assert "tenant_id" in expr
    assert "marketplace_hub.tenant_id" in expr
    assert "marketplace_hub.rls_bypass" in expr


def test_shared_catalog_tables_are_not_naively_forced_into_rls():
    assert "price_lists" not in RLS_TABLES
    assert "suppliers" not in RLS_TABLES
    assert "marketplace_accounts" in RLS_TABLES
    assert "accounting_order_lines" in RLS_TABLES


def test_api_admin_still_cannot_cross_active_seller_scope():
    user = ApiUser(
        id=1, username="admin", display_name="Admin", is_admin=True,
        permissions=frozenset(), seller_ids=frozenset({3}), expires_at=9999999999,
        tenant_ids=frozenset({10, 11}), active_tenant_id=10,
        active_tenant_name="Tenant A", active_tenant_type="agency", tenant_role="platform_admin",
    )
    assert user.can_access_tenant(11)
    assert user.can_access_seller(3)
    assert not user.can_access_seller(4)


def test_seller_transfer_helper_is_available():
    from services.tenant_db import reassign_seller_tenant_rows
    assert callable(reassign_seller_tenant_rows)
