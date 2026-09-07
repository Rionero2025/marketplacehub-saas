from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
import test_tenancy_migration
from alembic import command
from test_tenancy_migration import seed_legacy

legacy_database = test_tenancy_migration.legacy_database

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "seller_settings_migration",
    ROOT / "migrations/versions/20260907_0005_seller_settings.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def seed_settings(engine):
    sources = seed_legacy(
        engine,
        users=[{"id": 1, "username": "config-test"}],
        tenants=[{"id": 10, "tenant_type": "merchant", "name": "Test seller"}],
        members=[{"user_id": 1, "tenant_id": 10, "role": "owner"}],
        sellers=[{"id": 7, "name": "Owned", "our_profit_pct": 40, "partner_profit_pct": 40}],
        owners=[{"tenant_id": 10, "seller_id": 7}],
    )
    accounts = sa.Table(
        "marketplace_accounts",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("seller_id", sa.Integer),
        sa.Column("marketplace", sa.Text),
        sa.Column("account_name", sa.Text),
        sa.Column("credentials_encrypted", sa.Text),
        sa.Column("settings_json", sa.Text, default="{}"),
        sa.Column("active", sa.Integer, default=1),
        sa.Column("created_at", sa.Text, default="2026-08-01T12:00:00Z"),
    )
    accounts.create(engine)
    with engine.begin() as conn:
        conn.execute(
            accounts.insert(),
            [
                {
                    "id": 21,
                    "seller_id": 7,
                    "marketplace": "kaufland",
                    "account_name": "Owned account",
                    "credentials_encrypted": "opaque-encrypted-value",
                    "settings_json": '{"storefront":"it"}',
                    "active": 0,
                },
                {
                    "id": 22,
                    "seller_id": 77,
                    "marketplace": "kaufland",
                    "account_name": "Unassigned account",
                    "credentials_encrypted": "not-owned",
                    "settings_json": "{}",
                    "active": 1,
                },
                {
                    "id": 23,
                    "seller_id": 7,
                    "marketplace": "worten",
                    "account_name": "Later channel",
                    "credentials_encrypted": "deferred",
                    "settings_json": "{}",
                    "active": 1,
                },
            ],
        )
    return sources, accounts


def read_rows(engine, name):
    table = sa.Table(name, sa.MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(sa.select(table)).mappings()]


def test_import_preserves_credentials_ownership_inactive_and_legacy_sources(legacy_database):
    config, engine = legacy_database
    sources, _ = seed_settings(engine)
    before = {name: read_rows(engine, table.name) for name, table in sources.items()}
    original_accounts = read_rows(engine, "marketplace_accounts")
    command.upgrade(config, "20260907_0005")
    settings = read_rows(engine, "seller_commercial_settings")
    profiles = read_rows(engine, "seller_profiles")
    accounts = read_rows(engine, "seller_marketplace_accounts")
    assert len(settings) == len(accounts) == 1
    assert settings[0]["our_profit_pct"] == 40
    assert settings[0]["partner_profit_pct"] == 60
    assert accounts[0]["seller_id"] == profiles[0]["id"] == settings[0]["seller_id"]
    assert accounts[0]["organization_id"] == profiles[0]["organization_id"]
    assert accounts[0]["credentials_encrypted"] == "opaque-encrypted-value"
    assert accounts[0]["settings_json"] == '{"storefront":"it"}'
    assert accounts[0]["legacy_account_id"] == 21
    assert not accounts[0]["active"]
    assert original_accounts == read_rows(engine, "marketplace_accounts")
    assert before == {name: read_rows(engine, table.name) for name, table in sources.items()}
    command.upgrade(config, "20260907_0005")
    assert accounts == read_rows(engine, "seller_marketplace_accounts")


def test_downgrade_and_reupgrade_leave_legacy_untouched(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    before = read_rows(engine, "marketplace_accounts")
    command.upgrade(config, "20260907_0005")
    account_id = read_rows(engine, "seller_marketplace_accounts")[0]["id"]
    command.downgrade(config, "20260907_0004")
    assert "seller_commercial_settings" not in sa.inspect(engine).get_table_names()
    assert "seller_marketplace_accounts" not in sa.inspect(engine).get_table_names()
    assert before == read_rows(engine, "marketplace_accounts")
    command.upgrade(config, "20260907_0005")
    assert read_rows(engine, "seller_marketplace_accounts")[0]["id"] == account_id


def test_fresh_install_has_no_invented_sellers_or_accounts(legacy_database):
    config, engine = legacy_database
    command.upgrade(config, "20260907_0005")
    assert read_rows(engine, "seller_commercial_settings") == []
    assert read_rows(engine, "seller_marketplace_accounts") == []


@pytest.mark.parametrize(
    "our,partner,expected",
    [
        (40, 40, (40, 60)),
        (None, None, (0, 100)),
        (150, -10, (100, 0)),
        (float("nan"), float("inf"), (0, 100)),
        (19.123456, 80.876544, (19.1235, 80.8765)),
        (40, 60.005, (40, 60.005)),
        ("bad", "bad", (0, 100)),
    ],
)
def test_migration_uses_original_read_normalization(our, partner, expected):
    assert migration._split(our, partner) == expected


@pytest.mark.parametrize("fail", [False, True])
def test_legacy_rls_scope_restored_on_read_or_exception(monkeypatch, fail):
    table = sa.Table("sellers", sa.MetaData(), sa.Column("id", sa.Integer))
    monkeypatch.setattr(migration.sa, "Table", lambda *args, **kwargs: table)
    changes = []

    class Result:
        def mappings(self):
            return [{"id": 7}]

    class Connection:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def scalar(self, _statement):
            return "previous-scope"

        def execute(self, statement, params=None):
            if isinstance(statement, sa.sql.Select):
                if fail:
                    raise RuntimeError("synthetic read failure")
                return Result()
            changes.append((str(statement), params))

    if fail:
        with pytest.raises(RuntimeError, match="synthetic"):
            migration._read_legacy(Connection(), "sellers", {"sellers"})
    else:
        assert migration._read_legacy(Connection(), "sellers", {"sellers"}) == [{"id": 7}]
    assert len(changes) == 2
    assert changes[-1][1] == {"previous": "previous-scope"}
