from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from marketplace_hub_core.auth.models import AuthRealm
from marketplace_hub_core.auth.passwords import hash_password, verify_password
from marketplace_hub_core.auth.rate_limit import MemoryLoginRateLimiter
from marketplace_hub_core.auth.schema import auth_sessions, auth_users
from marketplace_hub_core.auth.service import (
    AuthService,
    InvalidCredentialsError,
    hash_session_token,
)
from marketplace_hub_core.auth.sql_repository import SqlAuthRepository
from marketplace_hub_core.settings import get_settings

NOW = datetime(2026, 9, 7, 15, 0, tzinfo=UTC)
OLD_PASSWORD = "oldpass8"


def old_hash(password: str = OLD_PASSWORD) -> str:
    # Independently reproduce services/user_access.py on the previous main branch.
    salt = bytes(range(18))
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 390_000)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"pbkdf2_sha256$390000${salt_text}${digest_text}"


OLD_HASH = old_hash()


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    url = f"sqlite:///{tmp_path / 'legacy-auth.sqlite3'}"
    monkeypatch.setenv("MH_DATABASE_URL", url)
    get_settings.cache_clear()
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "20260907_0002")
    engine = sa.create_engine(url)
    try:
        yield config, engine, SqlAuthRepository(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()


def seed_legacy(engine, *, users=None, tenants=None, memberships=None, clients=None):
    metadata = sa.MetaData()
    tables = {
        "users": sa.Table(
            "app_users",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("username", sa.Text, nullable=False, unique=True),
            sa.Column("display_name", sa.Text, nullable=False, default=""),
            sa.Column("password_hash", sa.Text, nullable=False, default=OLD_HASH),
            sa.Column("is_admin", sa.Integer, nullable=False, default=0),
            sa.Column("active", sa.Integer, nullable=False, default=1),
            sa.Column("created_at", sa.Text, nullable=False, default=NOW.isoformat()),
            sa.Column("updated_at", sa.Text, nullable=False, default=NOW.isoformat()),
        ),
        "tenants": sa.Table(
            "tenants",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("tenant_type", sa.Text, nullable=False),
            sa.Column("status", sa.Text, nullable=False, default="active"),
        ),
        "memberships": sa.Table(
            "tenant_memberships",
            metadata,
            sa.Column("user_id", sa.Integer, primary_key=True),
            sa.Column("tenant_id", sa.Integer, primary_key=True),
            sa.Column("role", sa.Text, nullable=False, default="operator"),
            sa.Column("active", sa.Integer, nullable=False, default=1),
        ),
        "clients": sa.Table(
            "agency_clients",
            metadata,
            sa.Column("agency_tenant_id", sa.Integer, primary_key=True),
            sa.Column("client_tenant_id", sa.Integer, primary_key=True),
            sa.Column("active", sa.Integer, nullable=False, default=1),
        ),
    }
    metadata.create_all(engine)
    with engine.begin() as connection:
        for name, rows in (
            ("users", users),
            ("tenants", tenants),
            ("memberships", memberships),
            ("clients", clients),
        ):
            for row in rows or []:
                connection.execute(tables[name].insert().values(**row))
    return tables


def seed_seller(engine, *, password_hash=OLD_HASH):
    return seed_legacy(
        engine,
        users=[
            {
                "id": 41,
                "username": " Rionero ",
                "display_name": "Rionero",
                "password_hash": password_hash,
            }
        ],
        tenants=[{"id": 1, "tenant_type": "merchant"}],
        memberships=[{"user_id": 41, "tenant_id": 1, "role": "owner"}],
    )


def login(service, *, username=" RIONERO ", password=OLD_PASSWORD, realm=AuthRealm.SELLER):
    return service.login(
        login=username,
        password=password,
        realm=realm,
        client_key="migration-test",
        now=NOW,
    )


def test_existing_eight_character_password_survives_migration_login_and_logout(database):
    config, engine, repository = database
    legacy = seed_seller(engine)
    command.upgrade(config, "20260907_0003")
    imported = repository.find_user_by_login("rionero")
    assert imported is not None
    assert imported.id == uuid5(
        NAMESPACE_URL, "https://marketplacehub.internal/legacy/app_users/41"
    )
    assert imported.display_name == "Rionero"
    assert imported.password_hash == OLD_HASH
    assert imported.realms == {AuthRealm.SELLER}

    service = AuthService(repository, MemoryLoginRateLimiter())
    issued = login(service)
    assert issued.principal.user_id == imported.id
    assert service.authenticate(issued.token, now=NOW + timedelta(minutes=1)) is not None
    upgraded = repository.find_user_by_id(imported.id)
    assert upgraded is not None and upgraded.password_hash.startswith("$argon2id$")
    assert verify_password(upgraded.password_hash, OLD_PASSWORD)
    with engine.connect() as connection:
        assert connection.scalar(sa.select(legacy["users"].c.password_hash)) == OLD_HASH
        assert connection.scalar(sa.select(auth_sessions.c.token_hash)) == hash_session_token(
            issued.token
        )
        link = connection.execute(sa.text("SELECT * FROM auth_legacy_user_links")).mappings().one()
        assert link["legacy_user_id"] == 41
        assert str(link["user_id"]).replace("-", "") == imported.id.hex
    service.logout(issued.token, now=NOW + timedelta(minutes=2))
    assert service.authenticate(issued.token, now=NOW + timedelta(minutes=3)) is None
    # The second login reads the upgraded hash, not the old identity table.
    assert login(service).principal.user_id == imported.id


@pytest.mark.parametrize(
    "password,realm",
    [
        ("incorrect", AuthRealm.SELLER),
        (OLD_PASSWORD, AuthRealm.AGENCY),
        (OLD_PASSWORD, AuthRealm.PLATFORM),
    ],
)
def test_rejected_legacy_login_never_rehashes_or_creates_sessions(database, password, realm):
    config, engine, repository = database
    seed_seller(engine)
    command.upgrade(config, "20260907_0003")
    with pytest.raises(InvalidCredentialsError, match="Credenziali non valide"):
        login(AuthService(repository, MemoryLoginRateLimiter()), password=password, realm=realm)
    assert repository.find_user_by_login("rionero").password_hash == OLD_HASH
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(auth_sessions)) == 0


def test_portals_preserve_membership_and_agency_boundaries_without_admin_escalation(database):
    config, engine, repository = database
    expected = {
        1: {AuthRealm.SELLER},
        2: {AuthRealm.AGENCY, AuthRealm.SELLER},
        3: set(),
        4: {AuthRealm.SELLER},
        5: set(),
        6: set(),
        7: set(),
        8: {AuthRealm.AGENCY},
        9: {AuthRealm.AGENCY},
        10: {AuthRealm.AGENCY},
        11: set(AuthRealm),
        12: set(),
    }
    seed_legacy(
        engine,
        users=[
            {
                "id": user_id,
                "username": f"user{user_id}",
                "active": 0 if user_id == 4 else 1,
                "is_admin": 1 if user_id == 11 else 0,
            }
            for user_id in expected
        ],
        tenants=[
            {"id": 1, "tenant_type": "merchant"},
            {"id": 2, "tenant_type": "merchant", "status": "suspended"},
            {"id": 3, "tenant_type": "agency"},
            {"id": 4, "tenant_type": "agency", "status": "disabled"},
            {"id": 5, "tenant_type": "agency"},
            {"id": 6, "tenant_type": "agency"},
            {"id": 7, "tenant_type": "agency"},
        ],
        memberships=[
            {"user_id": 1, "tenant_id": 1, "role": "admin"},
            {"user_id": 2, "tenant_id": 3, "role": "admin"},
            {"user_id": 4, "tenant_id": 1},
            {"user_id": 5, "tenant_id": 1, "active": 0},
            {"user_id": 6, "tenant_id": 2},
            {"user_id": 7, "tenant_id": 4},
            {"user_id": 8, "tenant_id": 5},
            {"user_id": 9, "tenant_id": 6},
            {"user_id": 10, "tenant_id": 7},
            {"user_id": 12, "tenant_id": 3, "active": 0},
        ],
        clients=[
            {"agency_tenant_id": 3, "client_tenant_id": 1},
            {"agency_tenant_id": 4, "client_tenant_id": 1},
            {"agency_tenant_id": 5, "client_tenant_id": 3},
            {"agency_tenant_id": 6, "client_tenant_id": 1, "active": 0},
            {"agency_tenant_id": 7, "client_tenant_id": 2},
        ],
    )
    command.upgrade(config, "20260907_0003")
    for user_id, realms in expected.items():
        user = repository.find_user_by_login(f"user{user_id}")
        assert user is not None and user.realms == realms, user_id
        assert user.active is (user_id != 4)
    service = AuthService(repository, MemoryLoginRateLimiter())
    for user_id in (3, 4, 5, 6, 7, 9, 10, 12):
        with pytest.raises(InvalidCredentialsError):
            login(service, username=f"user{user_id}")
        assert repository.find_user_by_login(f"user{user_id}").password_hash == OLD_HASH
    assert login(service, username="user2").principal.realm is AuthRealm.SELLER
    assert login(service, username="user11", realm=AuthRealm.PLATFORM).principal.realm is (
        AuthRealm.PLATFORM
    )


@pytest.mark.parametrize(
    "tenant_type,expected",
    [
        (None, {AuthRealm.PLATFORM}),
        ("merchant", {AuthRealm.PLATFORM, AuthRealm.SELLER}),
        ("agency", {AuthRealm.PLATFORM, AuthRealm.AGENCY}),
    ],
)
def test_global_admin_receives_only_portals_backed_by_active_tenant_types(
    database,
    tenant_type,
    expected,
):
    config, engine, repository = database
    seed_legacy(
        engine,
        users=[{"id": 1, "username": "owner", "is_admin": 1}],
        tenants=[{"id": 1, "tenant_type": tenant_type}] if tenant_type else [],
    )
    command.upgrade(config, "20260907_0003")
    assert repository.find_user_by_login("owner").realms == expected


@pytest.mark.parametrize(
    "encoded",
    [
        "not-a-password-hash",
        OLD_HASH.replace("390000", "1"),
        OLD_HASH.replace("390000", "9999999999999999999999999"),
        OLD_HASH.replace("390000", "-390000"),
        OLD_HASH.replace("pbkdf2_sha256", "pbkdf2_sha1"),
        "pbkdf2_sha256$390000$short$short",
        "pbkdf2_sha256$390000$" + "!" * 24 + "$" + "A" * 43,
        OLD_HASH + "$extra",
        OLD_HASH + "=",
    ],
)
def test_malformed_legacy_hash_is_rejected_without_session_or_rehash(database, encoded):
    config, engine, repository = database
    seed_seller(engine, password_hash=encoded)
    command.upgrade(config, "20260907_0003")
    with pytest.raises(InvalidCredentialsError):
        login(AuthService(repository, MemoryLoginRateLimiter()))
    assert repository.find_user_by_login("rionero").password_hash == encoded
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(auth_sessions)) == 0


@pytest.mark.parametrize("conflict", ["normalized-legacy", "existing-auth"])
def test_identity_conflicts_leave_auth_users_unchanged_and_do_not_advance_revision(
    database, conflict
):
    config, engine, repository = database
    users = [{"id": 1, "username": "Unrelated"}, {"id": 2, "username": " RIONERO "}]
    if conflict == "normalized-legacy":
        users.append({"id": 3, "username": "rionero"})
    else:
        repository.create_user(
            login="rionero",
            display_name="Existing",
            password_hash=hash_password("NewPassword42"),
            realms=[AuthRealm.AGENCY],
            now=NOW,
        )
    seed_legacy(engine, users=users)
    with engine.connect() as connection:
        before = connection.execute(sa.select(auth_users)).all()
    with pytest.raises(RuntimeError):
        command.upgrade(config, "20260907_0003")
    with engine.connect() as connection:
        assert connection.execute(sa.select(auth_users)).all() == before
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "20260907_0002"
        )
    assert repository.find_user_by_login("unrelated") is None


def test_clean_install_has_no_implicit_users_or_portal_grants(database):
    config, engine, _ = database
    command.upgrade(config, "20260907_0003")
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(auth_users)) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM auth_legacy_user_links")) == 0


def test_downgrade_and_reupgrade_preserve_upgraded_password_sessions_and_identity(database):
    config, engine, repository = database
    seed_seller(engine)
    command.upgrade(config, "20260907_0003")
    service = AuthService(repository, MemoryLoginRateLimiter())
    issued = login(service)
    before = repository.find_user_by_login("rionero")
    assert before is not None and before.password_hash.startswith("$argon2id$")
    command.downgrade(config, "20260907_0002")
    assert repository.find_user_by_login("rionero") == before
    assert service.authenticate(issued.token, now=NOW + timedelta(minutes=1)) is not None
    command.upgrade(config, "20260907_0003")
    assert repository.find_user_by_login("rionero") == before
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(auth_users)) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM auth_legacy_user_links")) == 1
    assert service.authenticate(issued.token, now=NOW + timedelta(minutes=2)) is not None


def test_compare_and_swap_cannot_replace_a_concurrently_changed_password(database):
    config, engine, repository = database
    seed_seller(engine)
    command.upgrade(config, "20260907_0003")
    user = repository.find_user_by_login("rionero")
    assert user is not None
    newer_hash = hash_password("ChangedPassword42")
    assert repository.update_password_hash(user.id, OLD_HASH, newer_hash, NOW)
    assert not repository.update_password_hash(user.id, OLD_HASH, "stale-upgrade", NOW)
    assert repository.find_user_by_id(user.id).password_hash == newer_hash
    with pytest.raises(InvalidCredentialsError):
        login(AuthService(repository, MemoryLoginRateLimiter()))
