from api.dependencies import ApiUser
from api.main import app


def test_v315_routes_exist():
    paths = {route.path for route in app.routes}
    assert "/api/v1/tenants" in paths
    assert "/api/v1/tenants/{tenant_id}/activate" in paths
    assert "/api/v1/tenants/{tenant_id}/sellers" in paths


def test_platform_admin_does_not_bypass_active_tenant_seller_scope():
    admin = ApiUser(
        id=1,
        username="admin",
        display_name="Admin",
        is_admin=True,
        permissions=frozenset(),
        seller_ids=frozenset({3, 8}),
        expires_at=9999999999,
        tenant_ids=frozenset({1, 2}),
        active_tenant_id=1,
        active_tenant_name="Agency",
        active_tenant_type="agency",
        tenant_role="platform_admin",
    )
    assert admin.can("database")
    assert admin.can_access_tenant(2)
    assert admin.can_access_seller(3)
    assert not admin.can_access_seller(9)


def test_merchant_user_is_bound_to_active_tenant_scope():
    user = ApiUser(
        id=7,
        username="merchant",
        display_name="Merchant",
        is_admin=False,
        permissions=frozenset({"dashboard", "accounting"}),
        seller_ids=frozenset({11, 12}),
        expires_at=9999999999,
        tenant_ids=frozenset({5}),
        active_tenant_id=5,
        active_tenant_name="Cliente A",
        active_tenant_type="merchant",
        tenant_role="owner",
    )
    assert user.can_access_tenant(5)
    assert not user.can_access_tenant(6)
    assert user.can_access_seller(11)
    assert not user.can_access_seller(99)
