from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, insert, select, update

from marketplace_hub_core.auth.models import AuthRealm, AuthUser, StoredSession
from marketplace_hub_core.auth.repositories import AuthRepository
from marketplace_hub_core.auth.schema import auth_sessions, auth_user_realms, auth_users


class SqlAuthRepository(AuthRepository):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def find_user_by_login(self, normalized_login: str) -> AuthUser | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(auth_users).where(auth_users.c.login == normalized_login)
            ).mappings().one_or_none()
            return self._with_realms(connection, row) if row else None

    def find_user_by_id(self, user_id: UUID) -> AuthUser | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(auth_users).where(auth_users.c.id == user_id)
            ).mappings().one_or_none()
            return self._with_realms(connection, row) if row else None

    @staticmethod
    def _with_realms(connection, row) -> AuthUser:
        realms = connection.execute(
            select(auth_user_realms.c.realm).where(auth_user_realms.c.user_id == row["id"])
        ).scalars()
        return AuthUser(
            id=row["id"],
            login=row["login"],
            display_name=row["display_name"],
            password_hash=row["password_hash"],
            active=row["active"],
            realms=frozenset(AuthRealm(realm) for realm in realms),
        )

    def create_user(
        self,
        *,
        login: str,
        display_name: str,
        password_hash: str,
        realms: Iterable[AuthRealm],
        now: datetime,
    ) -> AuthUser:
        user_id = uuid4()
        realm_set = frozenset(realms)
        with self.engine.begin() as connection:
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
                insert(auth_user_realms),
                [{"user_id": user_id, "realm": realm.value} for realm in realm_set],
            )
        return AuthUser(user_id, login, display_name, password_hash, True, realm_set)

    def save_session(self, session: StoredSession, realm: AuthRealm) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(auth_sessions).values(
                    id=session.id,
                    user_id=session.user_id,
                    realm=realm.value,
                    token_hash=session.token_hash,
                    created_at=session.created_at,
                    expires_at=session.expires_at,
                    last_seen_at=session.created_at,
                    revoked_at=session.revoked_at,
                )
            )

    def find_session(self, token_hash: str) -> tuple[StoredSession, AuthRealm] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(auth_sessions).where(auth_sessions.c.token_hash == token_hash)
            ).mappings().one_or_none()
        if row is None:
            return None
        return (
            StoredSession(
                id=row["id"],
                user_id=row["user_id"],
                token_hash=row["token_hash"],
                created_at=self._as_utc(row["created_at"]),
                expires_at=self._as_utc(row["expires_at"]),
                revoked_at=self._as_utc(row["revoked_at"]) if row["revoked_at"] else None,
            ),
            AuthRealm(row["realm"]),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def touch_session(self, session_id: UUID, now: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(auth_sessions)
                .where(auth_sessions.c.id == session_id)
                .values(last_seen_at=now)
            )

    def revoke_session(self, token_hash: str, now: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(auth_sessions)
                .where(auth_sessions.c.token_hash == token_hash)
                .where(auth_sessions.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
