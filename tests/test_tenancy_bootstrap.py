from datetime import UTC, datetime

import pytest
from marketplace_hub_core.auth.schema import auth_user_realms, auth_users, metadata
from marketplace_hub_core.tenancy.bootstrap import create_platform_owner
from marketplace_hub_core.tenancy.constants import (
    ALL_PERMISSIONS,
    PERMISSION_LABELS,
    PLATFORM_ID,
    ROLE_LABELS,
)
from marketplace_hub_core.tenancy.schema import (
    membership_permissions,
    memberships,
    organizations,
    permissions,
    roles,
)
from sqlalchemy import create_engine, event, func, insert, select
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(organizations).values(
                id=PLATFORM_ID,
                name="Platform",
                kind="PLATFORM",
                active=True,
                created_at=datetime.now(UTC),
            )
        )
        conn.execute(
            insert(roles).values(
                code="PLATFORM_OWNER",
                organization_kind="PLATFORM",
                label=ROLE_LABELS["PLATFORM_OWNER"],
            )
        )
        conn.execute(
            insert(permissions),
            [{"code": code, "label": label} for code, label in PERMISSION_LABELS.items()],
        )
    yield engine
    engine.dispose()


def create_owner(engine):
    return create_platform_owner(
        engine,
        login="internal@example.test",
        display_name="Internal owner",
        password_hash="synthetic-hash-only",
        now=datetime.now(UTC),
    )


def test_bootstrap_persists_identity_and_membership_together(engine):
    user = create_owner(engine)
    with engine.connect() as conn:
        member = conn.execute(select(memberships)).mappings().one()
        assert member["user_id"] == user.id
        assert member["organization_id"] == PLATFORM_ID
        assert member["role_code"] == "PLATFORM_OWNER"
        assert member["all_sellers"] and not member["read_only"]
        assert (
            set(conn.scalars(select(membership_permissions.c.permission_code))) == ALL_PERMISSIONS
        )
        assert conn.scalar(select(auth_user_realms.c.realm)) == "platform"


def test_bootstrap_failure_rolls_back_identity(engine):
    with engine.begin() as conn:
        conn.execute(permissions.delete().where(permissions.c.code == "WORKSPACE_VIEW"))
    with pytest.raises(IntegrityError):
        create_owner(engine)
    with engine.connect() as conn:
        for table in [auth_users, auth_user_realms, memberships, membership_permissions]:
            assert conn.scalar(select(func.count()).select_from(table)) == 0


def test_bootstrap_does_not_create_identity_under_disabled_platform(engine):
    with engine.begin() as conn:
        conn.execute(organizations.update().values(active=False))
    with pytest.raises(ValueError, match="assente o disattiva"):
        create_owner(engine)
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(auth_users)) == 0
