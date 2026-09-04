from pathlib import Path

from api.main import app
from services.catalog_sharing import SCOPES, PERMISSIONS


def test_catalog_sharing_scopes_are_saas_native():
    assert SCOPES == {"tenant", "agency", "platform"}
    assert PERMISSIONS == {"use", "manage"}


def test_v317_catalog_sharing_routes_exist():
    paths = set(app.openapi()["paths"])
    assert "/api/v1/sellers/{seller_id}/catalogs/{price_list_id}/sharing" in paths
    assert "/api/v1/sellers/{seller_id}/catalogs/suppliers/{supplier_id}/sharing" in paths


def test_accessible_lists_uses_tenant_catalog_model():
    root = Path(__file__).resolve().parents[1]
    source = (root / "services" / "db.py").read_text(encoding="utf-8")
    assert "accessible_price_lists" in source
    sharing = (root / "services" / "catalog_sharing.py").read_text(encoding="utf-8")
    assert "catalog_tenant_access" in sharing
    assert "agency_clients" in sharing
    assert "sharing_scope='platform'" in sharing


def test_postgresql_catalog_rls_is_not_single_owner_only():
    root = Path(__file__).resolve().parents[1]
    sharing = (root / "services" / "catalog_sharing.py").read_text(encoding="utf-8")
    assert "ALTER TABLE price_lists FORCE ROW LEVEL SECURITY" in sharing
    assert "mh_catalog_read" in sharing
    assert "catalog_tenant_access" in sharing
    assert "agency_clients" in sharing
    assert "mh_supplier_read" in sharing
