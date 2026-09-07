from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from marketplace_hub_core.auth.models import AuthRealm, AuthUser, StoredSession


class AuthRepository(Protocol):
    def find_user_by_login(self, normalized_login: str) -> AuthUser | None: ...

    def find_user_by_id(self, user_id: UUID) -> AuthUser | None: ...

    def create_user(
        self,
        *,
        login: str,
        display_name: str,
        password_hash: str,
        realms: Iterable[AuthRealm],
        now: datetime,
    ) -> AuthUser: ...

    def save_session(self, session: StoredSession, realm: AuthRealm) -> None: ...

    def find_session(self, token_hash: str) -> tuple[StoredSession, AuthRealm] | None: ...

    def touch_session(self, session_id: UUID, now: datetime) -> None: ...

    def revoke_session(self, token_hash: str, now: datetime) -> None: ...


class MemoryAuthRepository:
    def __init__(self) -> None:
        self.users: dict[UUID, AuthUser] = {}
        self.sessions: dict[str, tuple[StoredSession, AuthRealm]] = {}

    def find_user_by_login(self, normalized_login: str) -> AuthUser | None:
        return next((user for user in self.users.values() if user.login == normalized_login), None)

    def find_user_by_id(self, user_id: UUID) -> AuthUser | None:
        return self.users.get(user_id)

    def create_user(
        self,
        *,
        login: str,
        display_name: str,
        password_hash: str,
        realms: Iterable[AuthRealm],
        now: datetime,
    ) -> AuthUser:
        from uuid import uuid4

        if self.find_user_by_login(login):
            raise ValueError("Login già esistente.")
        user = AuthUser(uuid4(), login, display_name, password_hash, True, frozenset(realms))
        self.users[user.id] = user
        return user

    def save_session(self, session: StoredSession, realm: AuthRealm) -> None:
        self.sessions[session.token_hash] = (session, realm)

    def find_session(self, token_hash: str) -> tuple[StoredSession, AuthRealm] | None:
        return self.sessions.get(token_hash)

    def touch_session(self, session_id: UUID, now: datetime) -> None:
        return None

    def revoke_session(self, token_hash: str, now: datetime) -> None:
        found = self.sessions.get(token_hash)
        if found:
            session, realm = found
            self.sessions[token_hash] = (
                StoredSession(
                    session.id,
                    session.user_id,
                    session.token_hash,
                    session.created_at,
                    session.expires_at,
                    now,
                ),
                realm,
            )
