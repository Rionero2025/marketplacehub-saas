from pathlib import Path

from api.main import app


def test_v320_onboarding_routes_exist():
    paths = {r.path for r in app.routes}
    assert {"/api/v1/onboarding/plans", "/api/v1/onboarding/signup", "/api/v1/onboarding/status", "/api/v1/onboarding/marketplaces"} <= paths


def test_v320_signup_is_feature_flagged_and_invite_ready():
    root = Path(__file__).resolve().parents[1]
    source = (root / "services" / "onboarding.py").read_text(encoding="utf-8")
    assert "MARKETPLACE_HUB_PUBLIC_SIGNUP" in source
    assert "MARKETPLACE_HUB_SIGNUP_INVITE_CODE" in source
    assert "start_trial" in source


def test_v320_marketplace_credentials_are_encrypted():
    root = Path(__file__).resolve().parents[1]
    source = (root / "services" / "onboarding.py").read_text(encoding="utf-8")
    assert "encrypt_dict(creds)" in source
    assert "KauflandClient" in source
    assert "validate_credentials" in source


def test_v320_signup_creates_real_multitenant_chain():
    root = Path(__file__).resolve().parents[1]
    source = (root / "services" / "onboarding.py").read_text(encoding="utf-8")
    for token in ("create_tenant", "attach_seller", "create_user", "add_membership", "start_trial"):
        assert token in source
