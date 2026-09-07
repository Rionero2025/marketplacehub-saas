from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_public_portal_choice_exposes_seller_and_agency_but_not_platform() -> None:
    source = (ROOT / "apps/app-web/app/page.tsx").read_text(encoding="utf-8")
    assert 'href="/login/seller"' in source
    assert 'href="/login/agency"' in source
    assert "system-admin" not in source


def test_each_portal_has_a_protected_destination() -> None:
    expected = {
        "seller/page.tsx": 'requireRealm("seller", "/login/seller")',
        "agency/page.tsx": 'requireRealm("agency", "/login/agency")',
        "system-admin/page.tsx": 'requireRealm("platform", "/system-admin/login")',
    }
    app = ROOT / "apps/app-web/app"
    for relative_path, contract in expected.items():
        assert contract in (app / relative_path).read_text(encoding="utf-8")


def test_platform_login_is_marked_noindex() -> None:
    source = (ROOT / "apps/app-web/app/system-admin/login/layout.tsx").read_text(
        encoding="utf-8"
    )
    assert "index: false" in source
    assert "follow: false" in source
