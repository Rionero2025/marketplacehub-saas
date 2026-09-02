from api.dependencies import ApiUser
from api.main import app


def test_api_routes_expose_core_boundaries():
    paths = {route.path for route in app.routes}
    expected = {
        "/health",
        "/ready",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        "/api/v1/sellers",
        "/api/v1/sellers/{seller_id}/orders",
        "/api/v1/sellers/{seller_id}/accounting/status",
        "/api/v1/sellers/{seller_id}/buybox",
        "/api/v1/sellers/{seller_id}/catalogs/{price_list_id}/products",
        "/api/v1/jobs/{job_id}",
    }
    assert expected <= paths


def test_api_user_enforces_permissions_and_seller_scope():
    user = ApiUser(
        id=7,
        username="operatore",
        display_name="Operatore",
        is_admin=False,
        permissions=frozenset({"marketplace_orders", "accounting"}),
        seller_ids=frozenset({3, 8}),
        expires_at=9999999999,
    )
    assert user.can("accounting")
    assert not user.can("database")
    assert user.can_access_seller(3)
    assert user.can_access_seller(8)
    assert not user.can_access_seller(9)


def test_admin_is_unrestricted():
    admin = ApiUser(
        id=1,
        username="admin",
        display_name="Admin",
        is_admin=True,
        permissions=frozenset(),
        seller_ids=None,
        expires_at=9999999999,
    )
    assert admin.can("database")
    assert admin.can_access_seller(999999)
