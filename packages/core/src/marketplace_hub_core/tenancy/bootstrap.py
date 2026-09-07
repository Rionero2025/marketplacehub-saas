from datetime import datetime
from uuid import uuid4

from sqlalchemy import Engine, insert, select

from marketplace_hub_core.auth.models import AuthRealm, AuthUser
from marketplace_hub_core.auth.schema import auth_user_realms, auth_users
from marketplace_hub_core.tenancy.constants import ALL_PERMISSIONS, PLATFORM_ID
from marketplace_hub_core.tenancy.schema import (
    membership_permissions,
    memberships,
    organizations,
)


def create_platform_owner(
    engine: Engine, *, login: str, display_name: str, password_hash: str, now: datetime
) -> AuthUser:
    """Internal CLI only: create identity and root membership in one transaction."""
    user_id, membership_id = uuid4(), uuid4()
    with engine.begin() as connection:
        platform = connection.scalar(
            select(organizations.c.id).where(
                organizations.c.id == PLATFORM_ID,
                organizations.c.kind == "PLATFORM",
                organizations.c.active.is_(True),
            )
        )
        if platform is None:
            raise ValueError(
                "Organizzazione Platform assente o disattiva. Verificare le migrazioni."
            )
        connection.execute(
            insert(auth_users).values(
                id=user_id,
                login=login,
                display_name=display_name,
                password_hash=password_hash,
                active=True,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(auth_user_realms).values(user_id=user_id, realm=AuthRealm.PLATFORM.value)
        )
        connection.execute(
            insert(memberships).values(
                id=membership_id,
                user_id=user_id,
                organization_id=PLATFORM_ID,
                organization_kind="PLATFORM",
                role_code="PLATFORM_OWNER",
                active=True,
                all_sellers=True,
                read_only=False,
                created_at=now,
            )
        )
        connection.execute(
            insert(membership_permissions),
            [
                {"membership_id": membership_id, "permission_code": code}
                for code in sorted(ALL_PERMISSIONS)
            ],
        )
    return AuthUser(
        user_id, login, display_name, password_hash, True, frozenset([AuthRealm.PLATFORM])
    )
