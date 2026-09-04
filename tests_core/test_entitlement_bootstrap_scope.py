"""Regression: a cold worker must not backfill another tenant under RLS."""
import sqlite3
import pytest
from services import entitlements, tenancy, tenant_db


@pytest.fixture
def database(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE tenants (id INTEGER PRIMARY KEY, tenant_type TEXT, plan_code TEXT, updated_at TEXT)")
    con.executemany("INSERT INTO tenants VALUES (?, 'merchant', 'starter', '')", [(1,), (22,)])
    monkeypatch.setattr(entitlements, "_SCHEMA_READY", False)
    monkeypatch.setattr(tenancy, "ensure_tenancy_schema", lambda: None)
    monkeypatch.setattr(tenant_db, "database_engine", lambda: "postgresql")
    writes = []

    def execute(sql, params=()):
        if "INSERT INTO tenant_subscriptions" in sql:
            owner = params[0]
            if tenant_db.database_engine() == "postgresql" and not tenant_db.platform_bypass_enabled():
                assert owner == tenant_db.current_tenant_id(), "cross-tenant subscription write"
            writes.append(owner)
        return con.execute(sql, params).lastrowid

    def rows(sql, params=()):
        items = [dict(item) for item in con.execute(sql, params).fetchall()]
        if "FROM tenant_subscriptions" in sql and tenant_db.database_engine() == "postgresql" and not tenant_db.platform_bypass_enabled():
            items = [item for item in items if item["tenant_id"] == tenant_db.current_tenant_id()]
        return items

    monkeypatch.setattr(entitlements, "execute", execute)
    monkeypatch.setattr(entitlements, "rows", rows)
    monkeypatch.setattr(entitlements, "row", lambda sql, params=(): next(iter(rows(sql, params)), None))
    yield con, writes
    con.close()


@pytest.mark.parametrize("existing", [False, True])
def test_cold_worker_only_backfills_its_own_subscription(database, existing):
    con, writes = database
    # First run the authorized platform migration, then simulate a fresh process.
    with tenant_db.platform_database_scope():
        entitlements.ensure_entitlement_schema()
    if not existing:
        con.execute("DELETE FROM tenant_subscriptions WHERE tenant_id=22")
    writes.clear()
    entitlements._SCHEMA_READY = False
    with tenant_db.tenant_database_scope(22):
        entitlements.ensure_entitlement_schema()
        assert not tenant_db.platform_bypass_enabled()
    assert writes == ([] if existing else [22])
    assert con.execute("SELECT count(*) FROM tenant_subscriptions").fetchone()[0] == 2
    assert tenant_db.current_tenant_id() == 0


def test_unscoped_postgresql_does_not_backfill_any_tenant(database):
    _, writes = database
    with tenant_db.tenant_database_scope(0):
        entitlements.ensure_entitlement_schema()
    assert writes == []


def test_platform_migration_backfills_all_tenants(database):
    _, writes = database
    with tenant_db.platform_database_scope():
        entitlements.ensure_entitlement_schema()
    assert writes == [1, 22]


def test_sqlite_keeps_legacy_migration(database, monkeypatch):
    _, writes = database
    monkeypatch.setattr(tenant_db, "database_engine", lambda: "sqlite")
    entitlements.ensure_entitlement_schema()
    assert writes == [1, 22]
