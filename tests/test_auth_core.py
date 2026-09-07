from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from marketplace_hub_core.auth.models import AuthRealm
from marketplace_hub_core.auth.passwords import WeakPasswordError, hash_password, verify_password
from marketplace_hub_core.auth.rate_limit import MemoryLoginRateLimiter
from marketplace_hub_core.auth.repositories import MemoryAuthRepository
from marketplace_hub_core.auth.service import (
    AuthService,
    InvalidCredentialsError,
    LoginRateLimitError,
    hash_session_token,
)

PASSWORD = "CorrectHorse42"
NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def make_service(*, attempts: int = 5, ttl: timedelta = timedelta(hours=12)):
    repository = MemoryAuthRepository()
    user = repository.create_user(
        login="seller@example.test",
        display_name="Seller Test",
        password_hash=hash_password(PASSWORD),
        realms=[AuthRealm.SELLER],
        now=NOW,
    )
    return (
        AuthService(
            repository,
            MemoryLoginRateLimiter(),
            session_ttl=ttl,
            login_attempt_limit=attempts,
            login_window_seconds=900,
        ),
        repository,
        user,
    )


def test_passwords_use_argon2id_and_reject_weak_values() -> None:
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, PASSWORD)
    assert not verify_password(encoded, "incorrect")
    with pytest.raises(WeakPasswordError):
        hash_password("short")


def test_login_requires_the_requested_realm_and_uses_generic_error() -> None:
    service, _, _ = make_service()
    with pytest.raises(InvalidCredentialsError, match="Credenziali non valide"):
        service.login(
            login="seller@example.test",
            password=PASSWORD,
            realm=AuthRealm.AGENCY,
            client_key="127.0.0.1",
            now=NOW,
        )
    with pytest.raises(InvalidCredentialsError, match="Credenziali non valide"):
        service.login(
            login="unknown@example.test",
            password=PASSWORD,
            realm=AuthRealm.SELLER,
            client_key="127.0.0.1",
            now=NOW,
        )


def test_session_is_opaque_revocable_and_expires() -> None:
    service, repository, user = make_service(ttl=timedelta(minutes=30))
    issued = service.login(
        login=" SELLER@EXAMPLE.TEST ",
        password=PASSWORD,
        realm=AuthRealm.SELLER,
        client_key="127.0.0.1",
        now=NOW,
    )
    assert issued.principal.user_id == user.id
    assert issued.token not in repository.sessions
    assert hash_session_token(issued.token) in repository.sessions
    assert service.authenticate(issued.token, now=NOW + timedelta(minutes=29)) is not None
    assert service.authenticate(issued.token, now=NOW + timedelta(minutes=30)) is None

    second = service.login(
        login="seller@example.test",
        password=PASSWORD,
        realm=AuthRealm.SELLER,
        client_key="another-client",
        now=NOW,
    )
    service.logout(second.token, now=NOW + timedelta(minutes=1))
    assert service.authenticate(second.token, now=NOW + timedelta(minutes=2)) is None


def test_login_rate_limit_is_enforced() -> None:
    service, _, _ = make_service(attempts=2)
    for _ in range(2):
        with pytest.raises(InvalidCredentialsError):
            service.login(
                login="seller@example.test",
                password="wrong",
                realm=AuthRealm.SELLER,
                client_key="client",
                now=NOW,
            )
    with pytest.raises(LoginRateLimitError):
        service.login(
            login="seller@example.test",
            password="wrong",
            realm=AuthRealm.SELLER,
            client_key="client",
            now=NOW,
        )
