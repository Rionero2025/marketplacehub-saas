from __future__ import annotations

from datetime import UTC, datetime, timedelta

from marketplace_hub_core.auth.models import AuthRealm, StoredSession
from marketplace_hub_core.auth.passwords import hash_password
from marketplace_hub_core.auth.schema import metadata
from marketplace_hub_core.auth.service import hash_session_token
from marketplace_hub_core.auth.sql_repository import SqlAuthRepository
from sqlalchemy import create_engine


def test_sql_repository_persists_users_realms_and_hashed_sessions() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    repository = SqlAuthRepository(engine)
    now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    user = repository.create_user(
        login="agency@example.test",
        display_name="Agency Test",
        password_hash=hash_password("CorrectHorse42"),
        realms=[AuthRealm.AGENCY],
        now=now,
    )
    loaded = repository.find_user_by_login("agency@example.test")
    assert loaded == user

    raw_token = "a-token-that-never-enters-the-database"
    session = StoredSession(
        id=__import__("uuid").uuid4(),
        user_id=user.id,
        token_hash=hash_session_token(raw_token),
        created_at=now,
        expires_at=now + timedelta(hours=12),
    )
    repository.save_session(session, AuthRealm.AGENCY)
    assert repository.find_session(raw_token) is None
    stored = repository.find_session(hash_session_token(raw_token))
    assert stored is not None and stored[1] is AuthRealm.AGENCY
    repository.revoke_session(hash_session_token(raw_token), now + timedelta(minutes=1))
    revoked = repository.find_session(hash_session_token(raw_token))
    assert revoked is not None and revoked[0].revoked_at is not None
