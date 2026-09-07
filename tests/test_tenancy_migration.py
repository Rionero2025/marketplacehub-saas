from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from marketplace_hub_core.auth.models import AuthRealm
from marketplace_hub_core.auth.sql_repository import SqlAuthRepository
from marketplace_hub_core.settings import get_settings
from marketplace_hub_core.tenancy.constants import (
    PERMISSION_LABELS,
    PLATFORM_ID,
    ROLE_LABELS,
)
from marketplace_hub_core.tenancy.schema import (
    membership_permissions,
    membership_seller_access,
    memberships,
    organization_relationships,
    organizations,
    permissions,
    roles,
    seller_profiles,
)

NOW = datetime(2026, 9, 7, 15, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def legacy_database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'tenancy.sqlite3'}"
    monkeypatch.setenv("MH_DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "20260907_0002")
    engine = sa.create_engine(url)
    try:
        yield config, engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def seed_legacy(engine, *, users=(), tenants=(), members=(), sellers=(), owners=(), clients=()):
    metadata = sa.MetaData()
    tables = {
        "users": sa.Table(
            "app_users", metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("username", sa.Text, nullable=False, unique=True),
            sa.Column("display_name", sa.Text, nullable=False, default=""),
            sa.Column("password_hash", sa.Text, nullable=False, default="preserved-opaque-hash"),
            sa.Column("is_admin", sa.Integer, nullable=False, default=0),
            sa.Column("active", sa.Integer, nullable=False, default=1),
            sa.Column("seller_ids_json", sa.Text, nullable=True, default="null"),
            sa.Column("permissions_json", sa.Text, nullable=True, default="[]"),
        ),
        "tenants": sa.Table(
            "tenants", metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.Text, nullable=False, default="Workspace"),
            sa.Column("tenant_type", sa.Text, nullable=False),
            sa.Column("status", sa.Text, nullable=False, default="active"),
        ),
        "members": sa.Table(
            "tenant_memberships", metadata,
            sa.Column("user_id", sa.Integer, primary_key=True),
            sa.Column("tenant_id", sa.Integer, primary_key=True),
            sa.Column("role", sa.Text, nullable=False, default="operator"),
            sa.Column("active", sa.Integer, nullable=False, default=1),
        ),
        "sellers": sa.Table(
            "sellers", metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("legal_name", sa.Text, nullable=True, default=""),
            sa.Column("email", sa.Text, nullable=True, default=""),
            sa.Column("our_profit_pct", sa.Float, nullable=False, default=19.5),
            sa.Column("partner_profit_pct", sa.Float, nullable=False, default=80.5),
            sa.Column("active", sa.Integer, nullable=False, default=1),
        ),
        "owners": sa.Table(
            "tenant_sellers", metadata,
            sa.Column("tenant_id", sa.Integer, primary_key=True),
            sa.Column("seller_id", sa.Integer, primary_key=True),
            sa.Column("active", sa.Integer, nullable=False, default=1),
        ),
        "clients": sa.Table(
            "agency_clients", metadata,
            sa.Column("agency_tenant_id", sa.Integer, primary_key=True),
            sa.Column("client_tenant_id", sa.Integer, primary_key=True),
            sa.Column("active", sa.Integer, nullable=False, default=1),
        ),
    }
    metadata.create_all(engine)
    with engine.begin() as connection:
        for name, values in (
            ("users", users), ("tenants", tenants), ("members", members),
            ("sellers", sellers), ("owners", owners), ("clients", clients),
        ):
            for value in values:
                connection.execute(tables[name].insert().values(**value))
    return tables


def rows(engine, table):
    with engine.connect() as connection:
        return list(connection.execute(sa.select(table)).mappings())


def legacy_identity(user_id):
    return uuid5(NAMESPACE_URL, f"https://marketplacehub.internal/legacy/app_users/{user_id}")


def grants_for(engine, membership_id):
    return {
        row["permission_code"] for row in rows(engine, membership_permissions)
        if row["membership_id"] == membership_id
    }


def test_seller_owner_preserves_original_scope_identity_metadata_and_legacy_source(legacy_database):
    config, engine = legacy_database
    source = seed_legacy(
        engine,
        users=[{
            "id": 41, "username": "Rionero", "permissions_json": '["accounting"]',
            "seller_ids_json": "[7,8,777]",
        }],
        tenants=[
            {"id": 11, "name": "Rionero Srl", "tenant_type": "merchant"},
            {"id": 12, "name": "Other customer", "tenant_type": "merchant"},
        ],
        members=[{"user_id": 41, "tenant_id": 11, "role": "owner"}],
        sellers=[
            {"id": 7, "name": "RioneroShop", "legal_name": "Rionero Srl", "email": "a@b.test"},
            {"id": 8, "name": "OtherShop"},
        ],
        owners=[{"tenant_id": 11, "seller_id": 7}, {"tenant_id": 12, "seller_id": 8}],
    )
    before = {name: rows(engine, table) for name, table in source.items()}
    command.upgrade(config, "20260907_0004")
    migrated_org = next(row for row in rows(engine, organizations) if row["legacy_tenant_id"] == 11)
    profiles = {row["legacy_seller_id"]: row for row in rows(engine, seller_profiles)}
    assert profiles[7]["organization_id"] == migrated_org["id"]
    assert profiles[7]["name"] == "RioneroShop"
    assert profiles[7]["legal_name"] == "Rionero Srl"
    assert profiles[7]["email"] == "a@b.test"
    assert "our_profit_pct" not in profiles[7]
    member, = rows(engine, memberships)
    assert member["user_id"] == legacy_identity(41)
    assert member["organization_id"] == migrated_org["id"]
    assert member["role_code"] == "SELLER_OWNER"
    assert member["all_sellers"] is False
    assert grants_for(engine, member["id"]) == {"WORKSPACE_VIEW", "ACCOUNTING"}
    assert rows(engine, membership_seller_access) == [{
        "membership_id": member["id"], "seller_id": profiles[7]["id"],
    }]
    assert {name: rows(engine, table) for name, table in source.items()} == before
    assert all(row["role_code"] != "PLATFORM_ADMIN" for row in rows(engine, memberships))


def test_long_source_names_legal_names_and_emails_are_copied_in_full(legacy_database):
    config, engine = legacy_database
    tenant_name = "Agenzia " + "È" * 320
    seller_name = "Negozio " + "è" * 340
    legal_name = "Ragione sociale " + "ò" * 420
    email = "contatto." * 40 + "@example.test"
    seed_legacy(
        engine,
        tenants=[{"id": 1, "name": tenant_name, "tenant_type": "agency"}],
        sellers=[{
            "id": 7, "name": seller_name, "legal_name": legal_name, "email": email,
        }],
        owners=[{"tenant_id": 1, "seller_id": 7}],
    )
    command.upgrade(config, "20260907_0004")
    organization_rows = rows(engine, organizations)
    agency = next(row for row in organization_rows if row["legacy_tenant_id"] == 1)
    profile, = rows(engine, seller_profiles)
    synthetic_child = next(row for row in organization_rows
                           if row["id"] == profile["organization_id"])
    assert agency["name"] == tenant_name
    assert synthetic_child["name"] == seller_name
    assert profile["name"] == seller_name
    assert profile["legal_name"] == legal_name
    assert profile["email"] == email
    inspector = sa.inspect(engine)
    for table_name, field_names in (
        ("organizations", {"name"}),
        ("seller_profiles", {"name", "legal_name", "email"}),
    ):
        for column in inspector.get_columns(table_name):
            if column["name"] in field_names:
                assert isinstance(column["type"], sa.Text)


def test_agency_direct_sellers_become_children_and_delegation_keeps_ownership(legacy_database):
    config, engine = legacy_database
    seed_legacy(
        engine,
        users=[{
            "id": 1, "username": "agency_user", "seller_ids_json": "[11,12,13,14]",
            "permissions_json": '["marketplace_orders","support"]',
        }],
        tenants=[
            {"id": 1, "tenant_type": "agency"},
            {"id": 2, "tenant_type": "merchant"},
            {"id": 3, "tenant_type": "merchant"},
            {"id": 4, "tenant_type": "merchant"},
        ],
        members=[{"user_id": 1, "tenant_id": 1, "role": "manager"}],
        sellers=[{"id": item, "name": f"Store{item}"} for item in (11, 12, 13, 14)],
        owners=[{"tenant_id": i, "seller_id": i + 10} for i in range(1, 5)],
        clients=[
            {"agency_tenant_id": 1, "client_tenant_id": 2},
            {"agency_tenant_id": 1, "client_tenant_id": 3, "active": 0},
        ],
    )
    command.upgrade(config, "20260907_0004")
    orgs = {row["legacy_tenant_id"]: row for row in rows(engine, organizations)
            if row["legacy_tenant_id"] is not None}
    profiles = {row["legacy_seller_id"]: row for row in rows(engine, seller_profiles)}
    child = profiles[11]["organization_id"]
    assert child != orgs[1]["id"]
    assert next(row for row in rows(engine, organizations) if row["id"] == child)["kind"] == (
        "SELLER"
    )
    assert profiles[12]["organization_id"] == orgs[2]["id"]
    assert (orgs[1]["id"], child, True) in {
        (row["parent_id"], row["child_id"], row["active"])
        for row in rows(engine, organization_relationships)
    }
    assert (orgs[1]["id"], orgs[3]["id"], False) in {
        (row["parent_id"], row["child_id"], row["active"])
        for row in rows(engine, organization_relationships)
    }
    member, = rows(engine, memberships)
    assert member["role_code"] == "AGENCY_USER" and member["read_only"] is False
    assert grants_for(engine, member["id"]) == {
        "WORKSPACE_VIEW", "LOGISTICS", "CUSTOMER_SERVICE",
    }
    assert {row["seller_id"] for row in rows(engine, membership_seller_access)} == {
        profiles[11]["id"], profiles[12]["id"],
    }
    assert len(rows(engine, memberships)) == 1  # No implicit client tenant ownership.


@pytest.mark.parametrize("selection,all_sellers,expected", [
    (None, True, set()), ("", True, set()), ("null", True, set()),
    (" null ", True, set()), ("[]", False, set()), ("{}", False, set()),
    ('"all"', False, set()), ("invalid", False, set()),
    ('[7,"7",0,-1,"bad",8]', False, {7}),
])
def test_legacy_seller_selection_is_intersected_with_ownership(
    legacy_database, selection, all_sellers, expected,
):
    config, engine = legacy_database
    seed_legacy(
        engine,
        users=[{"id": 1, "username": "user", "seller_ids_json": selection}],
        tenants=[{"id": i, "tenant_type": "merchant"} for i in (1, 2)],
        members=[{"user_id": 1, "tenant_id": 1}],
        sellers=[{"id": i, "name": str(i)} for i in (7, 8)],
        owners=[{"tenant_id": 1, "seller_id": 7}, {"tenant_id": 2, "seller_id": 8}],
    )
    command.upgrade(config, "20260907_0004")
    member, = rows(engine, memberships)
    assert member["all_sellers"] is all_sellers
    profile_ids = {row["id"]: row["legacy_seller_id"] for row in rows(engine, seller_profiles)}
    assert {profile_ids[row["seller_id"]] for row in rows(engine, membership_seller_access)} == (
        expected
    )


def test_inactive_memberships_orgs_ownership_and_sellers_remain_inactive(legacy_database):
    config, engine = legacy_database
    seed_legacy(
        engine,
        users=[{"id": 1, "username": "viewer"}, {"id": 2, "username": "inactive", "active": 0}],
        tenants=[
            {"id": 1, "tenant_type": "merchant"},
            {"id": 2, "tenant_type": "merchant", "status": "suspended"},
        ],
        members=[
            {"user_id": 1, "tenant_id": 1, "role": "viewer", "active": 0},
            {"user_id": 2, "tenant_id": 2, "role": "viewer"},
        ],
        sellers=[
            {"id": 1, "name": "Ownership disabled"},
            {"id": 2, "name": "Seller disabled", "active": 0},
            {"id": 3, "name": "Tenant suspended"},
        ],
        owners=[
            {"tenant_id": 1, "seller_id": 1, "active": 0},
            {"tenant_id": 1, "seller_id": 2}, {"tenant_id": 2, "seller_id": 3},
        ],
    )
    command.upgrade(config, "20260907_0004")
    orgs = {row["legacy_tenant_id"]: row for row in rows(engine, organizations)}
    assert orgs[2]["active"] is False
    profiles = {row["legacy_seller_id"]: row for row in rows(engine, seller_profiles)}
    assert profiles[1]["active"] is False and profiles[2]["active"] is False
    member_rows = {row["user_id"]: row for row in rows(engine, memberships)}
    assert member_rows[legacy_identity(1)]["active"] is False
    assert member_rows[legacy_identity(2)]["active"] is True
    for member in member_rows.values():
        assert member["read_only"] is True
        assert grants_for(engine, member["id"]) == {"WORKSPACE_VIEW", "VIEW_ONLY"}
    assert SqlAuthRepository(engine).find_user_by_login("inactive").active is False


def test_clean_install_seeds_only_platform_and_explicit_platform_bootstrap(legacy_database):
    config, engine = legacy_database
    repository = SqlAuthRepository(engine)
    seller = repository.create_user(
        login="seller", display_name="Seller", password_hash="opaque",
        realms=[AuthRealm.SELLER], now=NOW,
    )
    platform = repository.create_user(
        login="bootstrap", display_name="Admin", password_hash="opaque",
        realms=[AuthRealm.PLATFORM], now=NOW,
    )
    command.upgrade(config, "20260907_0004")
    org, = rows(engine, organizations)
    assert org["id"] == PLATFORM_ID and org["kind"] == "PLATFORM"
    member, = rows(engine, memberships)
    assert member["user_id"] == platform.id and member["user_id"] != seller.id
    assert member["role_code"] == "PLATFORM_OWNER" and member["all_sellers"] is True
    assert grants_for(engine, member["id"]) == set(PERMISSION_LABELS) - {"VIEW_ONLY"}
    assert rows(engine, seller_profiles) == []
    assert {row["code"]: row["label"] for row in rows(engine, roles)} == ROLE_LABELS
    assert {row["code"]: row["label"] for row in rows(engine, permissions)} == PERMISSION_LABELS


def test_legacy_global_admin_does_not_invent_tenant_ownership(legacy_database):
    config, engine = legacy_database
    seed_legacy(
        engine,
        users=[{"id": 1, "username": "global", "is_admin": 1, "seller_ids_json": "[]"}],
        tenants=[{"id": 1, "tenant_type": "merchant"}],
        sellers=[{"id": 1, "name": "Existing"}], owners=[{"tenant_id": 1, "seller_id": 1}],
    )
    command.upgrade(config, "20260907_0004")
    member, = rows(engine, memberships)
    assert member["role_code"] == "PLATFORM_ADMIN"
    assert member["organization_id"] == PLATFORM_ID
    assert member["all_sellers"] is True
    assert grants_for(engine, member["id"]) == set(PERMISSION_LABELS) - {"VIEW_ONLY"}


def test_missing_or_ambiguous_ownership_is_skipped_and_logged_without_identifying_rows(
    legacy_database, caplog,
):
    config, engine = legacy_database
    seed_legacy(
        engine,
        tenants=[{"id": 1, "tenant_type": "merchant"}, {"id": 2, "tenant_type": "merchant"}],
        sellers=[{"id": i, "name": f"Private-{i}"} for i in (1, 2, 3)],
        owners=[
            {"tenant_id": 99, "seller_id": 2},
            {"tenant_id": 1, "seller_id": 3}, {"tenant_id": 2, "seller_id": 3},
        ],
    )
    command.upgrade(config, "20260907_0004")
    assert rows(engine, seller_profiles) == []
    assert not any("Private-" in record.getMessage() for record in caplog.records)


def test_downgrade_reupgrade_preserves_legacy_rows_and_reuses_stable_mappings(legacy_database):
    config, engine = legacy_database
    source = seed_legacy(
        engine,
        users=[{"id": 1, "username": "owner"}],
        tenants=[{"id": 1, "tenant_type": "merchant"}],
        members=[{"user_id": 1, "tenant_id": 1, "role": "owner"}],
        sellers=[{"id": 1, "name": "Store"}], owners=[{"tenant_id": 1, "seller_id": 1}],
    )
    before = {name: rows(engine, table) for name, table in source.items()}
    command.upgrade(config, "20260907_0004")
    identities = {
        table.name: {row["id"] for row in rows(engine, table)}
        for table in (organizations, memberships, seller_profiles)
    }
    command.downgrade(config, "20260907_0003")
    assert not set(("organizations", "memberships", "seller_profiles")) & set(
        sa.inspect(engine).get_table_names()
    )
    assert {name: rows(engine, table) for name, table in source.items()} == before
    command.upgrade(config, "20260907_0004")
    assert identities == {
        table.name: {row["id"] for row in rows(engine, table)}
        for table in (organizations, memberships, seller_profiles)
    }


@pytest.mark.parametrize("raw", (None, "[]", "{}", "invalid", '["unknown"]'))
def test_tenant_admin_does_not_gain_missing_legacy_permissions(legacy_database, raw):
    config, engine = legacy_database
    seed_legacy(
        engine, users=[{"id": 1, "username": "admin", "permissions_json": raw}],
        tenants=[{"id": 1, "tenant_type": "merchant"}],
        members=[{"user_id": 1, "tenant_id": 1, "role": "admin"}],
    )
    command.upgrade(config, "20260907_0004")
    member, = rows(engine, memberships)
    assert member["role_code"] == "SELLER_ADMIN"
    assert grants_for(engine, member["id"]) == {"WORKSPACE_VIEW"}


def test_all_supported_legacy_permission_groups_are_mapped(legacy_database):
    config, engine = legacy_database
    seed_legacy(
        engine,
        users=[{
            "id": 1, "username": "operator",
            "permissions_json": json.dumps([
                "accounting", "tracking", "marketplace_orders", "packlink", "cecotec_orders",
                "innpro_orders", "suppliers_lists", "work_lists", "product_creation",
                "marketplace_publication", "buybox", "support", "top_products", "seller_management",
            ]),
        }],
        tenants=[{"id": 1, "tenant_type": "merchant"}],
        members=[{"user_id": 1, "tenant_id": 1}],
    )
    command.upgrade(config, "20260907_0004")
    member, = rows(engine, memberships)
    assert grants_for(engine, member["id"]) == set(PERMISSION_LABELS) - {"VIEW_ONLY"}


def test_migration_schema_constraints_reject_role_or_profile_in_wrong_organization(legacy_database):
    config, engine = legacy_database
    seed_legacy(
        engine, users=[{"id": 1, "username": "owner"}],
        tenants=[{"id": 1, "tenant_type": "merchant"}],
        members=[{"user_id": 1, "tenant_id": 1, "role": "owner"}],
        sellers=[{"id": 1, "name": "Store"}], owners=[{"tenant_id": 1, "seller_id": 1}],
    )
    command.upgrade(config, "20260907_0004")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(memberships.update().values(role_code="PLATFORM_ADMIN"))
        connection.rollback()
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(seller_profiles.update().values(organization_id=PLATFORM_ID))
        connection.rollback()


def test_postgresql_legacy_read_restores_rls_scope_even_when_read_fails(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_0004", ROOT / "migrations/versions/20260907_0004_organizations.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    table = sa.Table("sellers", sa.MetaData(), sa.Column("id", sa.Integer()))
    monkeypatch.setattr(migration.sa, "Table", lambda *args, **kwargs: table)

    class Connection:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def __init__(self):
            self.calls = []

        def scalar(self, query):
            self.calls.append((str(query), None))
            return "original"

        def execute(self, query, values=None):
            self.calls.append((str(query), values))
            if isinstance(query, sa.sql.Select):
                raise RuntimeError("read failed")

    connection = Connection()
    with pytest.raises(RuntimeError, match="read failed"):
        migration._legacy_rows(connection, "sellers", {"sellers"})
    assert "set_config('marketplace_hub.rls_bypass', '1', true)" in connection.calls[1][0]
    assert connection.calls[-1] == (
        "SELECT set_config('marketplace_hub.rls_bypass', :previous, true)",
        {"previous": "original"},
    )
