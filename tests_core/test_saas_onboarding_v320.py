from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import sqlite3

import pytest

from api.main import app
from services import billing, db, entitlements, onboarding, shared_cache, tenancy, user_access


def test_v320_onboarding_routes_exist():
    paths = set(app.openapi()["paths"])
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


@pytest.fixture
def signup_database(monkeypatch):
    """Run real signup services against private SQLite, without external storage.

    Seed the legacy tables/columns used by signup and capacity checks. User,
    tenancy, plan and billing schemas use their production initializers. This checks
    persistence and relationships, not PostgreSQL RLS or deployment migrations.
    """
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("""CREATE TABLE sellers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE, legal_name TEXT DEFAULT '', email TEXT DEFAULT '',
        our_profit_pct REAL NOT NULL DEFAULT 0,
        partner_profit_pct REAL NOT NULL DEFAULT 100,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    )""")
    con.executescript("""
        CREATE TABLE marketplace_accounts (
            id INTEGER PRIMARY KEY, seller_id INTEGER REFERENCES sellers(id),
            marketplace TEXT, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE suppliers (
            id INTEGER PRIMARY KEY, owner_seller_id INTEGER REFERENCES sellers(id)
        );
        CREATE TABLE price_lists (
            id INTEGER PRIMARY KEY, owner_seller_id INTEGER REFERENCES sellers(id),
            active INTEGER NOT NULL DEFAULT 1
        );
    """)

    @contextmanager
    def connect():
        with con:
            yield con

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setenv("MARKETPLACE_HUB_DB_ENGINE", "sqlite")
    monkeypatch.setenv("MARKETPLACE_HUB_PUBLIC_SIGNUP", "1")
    monkeypatch.setenv("MARKETPLACE_HUB_SIGNUP_INVITE_CODE", "")
    monkeypatch.setattr(shared_cache, "_BACKEND", shared_cache.LocalSharedCache())
    for module in (billing, entitlements, onboarding, tenancy, user_access):
        monkeypatch.setattr(module, "_SCHEMA_READY", False)
    try:
        yield con
    finally:
        con.close()


def test_v320_signup_creates_real_multitenant_chain(signup_database):
    tenant_ids, seller_ids, user_ids = set(), set(), set()
    # Two signups expose links accidentally assigned to the legacy/first tenant.
    for suffix in ("alpha", "beta"):
        result = onboarding.register_merchant(
            company_name=f"Company {suffix}", username=f"owner-{suffix}",
            password="Synthetic-test-password-123", email=f"{suffix}@example.test",
            seller_name=f"Store {suffix}", plan_code="starter", trial_days=14,
        )
        tenant_id = result["tenant"]["id"]
        seller_id = result["seller"]["id"]
        user_id = result["user_id"]
        tenant_ids.add(tenant_id)
        seller_ids.add(seller_id)
        user_ids.add(user_id)

        tenant = db.row("SELECT * FROM tenants WHERE id=?", (tenant_id,))
        assert tenant["name"] == f"Company {suffix}"
        assert tenant["tenant_type"] == "merchant"
        assert tenant["plan_code"] == "starter"
        assert result["seller"]["name"] == f"Store {suffix}"
        assert db.rows(
            "SELECT tenant_id,seller_id,active FROM tenant_sellers WHERE seller_id=?",
            (seller_id,),
        ) == [{"tenant_id": tenant_id, "seller_id": seller_id, "active": 1}]
        assert db.rows(
            "SELECT tenant_id,role,active FROM tenant_memberships WHERE user_id=?",
            (user_id,),
        ) == [{"tenant_id": tenant_id, "role": "owner", "active": 1}]

        user = db.row("SELECT * FROM app_users WHERE id=?", (user_id,))
        assert user["username"] == f"owner-{suffix}"
        assert user["email"] == f"{suffix}@example.test"
        assert user["active"] == 1 and user["is_admin"] == 0
        assert json.loads(user["seller_ids_json"]) == [seller_id]
        assert user_access.verify_password("Synthetic-test-password-123", user["password_hash"])

        subscription = db.row("SELECT * FROM tenant_subscriptions WHERE tenant_id=?", (tenant_id,))
        assert subscription["status"] == result["billing"]["status"] == "trialing"
        assert subscription["plan_code"] == "starter"
        assert datetime.fromisoformat(subscription["trial_end"]) - datetime.fromisoformat(
            subscription["trial_start"]
        ) == timedelta(days=14)
        assert db.rows(
            "SELECT tenant_id,user_id,seller_id FROM onboarding_events WHERE tenant_id=? AND event_type=?",
            (tenant_id, "signup_completed"),
        ) == [{"tenant_id": tenant_id, "user_id": user_id, "seller_id": seller_id}]

    assert len(tenant_ids) == len(seller_ids) == len(user_ids) == 2
