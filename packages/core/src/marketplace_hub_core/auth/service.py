from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from marketplace_hub_core.auth.models import (
    AuthenticatedSession,
    AuthRealm,
    IssuedSession,
    StoredSession,
)
from marketplace_hub_core.auth.passwords import hash_password, verify_password
from marketplace_hub_core.auth.rate_limit import LoginRateLimiter
from marketplace_hub_core.auth.repositories import AuthRepository


class InvalidCredentialsError(ValueError):
    pass


class LoginRateLimitError(ValueError):
    pass


_DUMMY_PASSWORD_HASH = hash_password("TimingGuard42")


def normalize_login(login: str) -> str:
    return login.strip().casefold()


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(
        self,
        repository: AuthRepository,
        rate_limiter: LoginRateLimiter,
        *,
        session_ttl: timedelta = timedelta(hours=12),
        login_attempt_limit: int = 5,
        login_window_seconds: int = 900,
    ) -> None:
        self.repository = repository
        self.rate_limiter = rate_limiter
        self.session_ttl = session_ttl
        self.login_attempt_limit = login_attempt_limit
        self.login_window_seconds = login_window_seconds

    def login(
        self,
        *,
        login: str,
        password: str,
        realm: AuthRealm,
        client_key: str,
        now: datetime | None = None,
    ) -> IssuedSession:
        current_time = now or datetime.now(UTC)
        normalized = normalize_login(login)
        limiter_key = hashlib.sha256(f"{client_key}:{normalized}".encode()).hexdigest()
        if self.rate_limiter.is_blocked(
            limiter_key,
            now=current_time,
            limit=self.login_attempt_limit,
            window_seconds=self.login_window_seconds,
        ):
            raise LoginRateLimitError("Troppi tentativi. Riprova più tardi.")

        user = self.repository.find_user_by_login(normalized)
        password_hash = user.password_hash if user else _DUMMY_PASSWORD_HASH
        password_valid = verify_password(password_hash, password)
        if user is None or not user.active or realm not in user.realms or not password_valid:
            self.rate_limiter.record_failure(
                limiter_key,
                now=current_time,
                window_seconds=self.login_window_seconds,
            )
            raise InvalidCredentialsError("Credenziali non valide.")
        self.rate_limiter.clear(limiter_key)

        token = secrets.token_urlsafe(48)
        token_hash = hash_session_token(token)
        expires_at = current_time + self.session_ttl
        stored = StoredSession(
            id=uuid4(),
            user_id=user.id,
            token_hash=token_hash,
            created_at=current_time,
            expires_at=expires_at,
        )
        self.repository.save_session(stored, realm)
        principal = AuthenticatedSession(
            session_id=stored.id,
            user_id=user.id,
            login=user.login,
            display_name=user.display_name,
            realm=realm,
            expires_at=expires_at,
        )
        return IssuedSession(token=token, principal=principal)

    def authenticate(
        self, token: str | None, *, now: datetime | None = None
    ) -> AuthenticatedSession | None:
        if not token:
            return None
        current_time = now or datetime.now(UTC)
        found = self.repository.find_session(hash_session_token(token))
        if not found:
            return None
        session, realm = found
        if session.revoked_at is not None or session.expires_at <= current_time:
            return None
        user = self.repository.find_user_by_id(session.user_id)
        if user is None or not user.active or realm not in user.realms:
            return None
        self.repository.touch_session(session.id, current_time)
        return AuthenticatedSession(
            session_id=session.id,
            user_id=user.id,
            login=user.login,
            display_name=user.display_name,
            realm=realm,
            expires_at=session.expires_at,
        )

    def logout(self, token: str | None, *, now: datetime | None = None) -> None:
        if token:
            self.repository.revoke_session(hash_session_token(token), now or datetime.now(UTC))
