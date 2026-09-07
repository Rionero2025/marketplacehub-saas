from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from marketplace_hub_api.main import create_app
from marketplace_hub_core.auth.models import AuthRealm
from marketplace_hub_core.auth.passwords import hash_password
from marketplace_hub_core.auth.rate_limit import MemoryLoginRateLimiter
from marketplace_hub_core.auth.repositories import MemoryAuthRepository
from marketplace_hub_core.auth.service import AuthService
from marketplace_hub_core.settings import Settings


def make_client(*, environment: str = "test") -> TestClient:
    repository = MemoryAuthRepository()
    repository.create_user(
        login="seller@example.test",
        display_name="Seller Test",
        password_hash=hash_password("CorrectHorse42"),
        realms=[AuthRealm.SELLER],
        now=datetime.now(UTC),
    )
    service = AuthService(repository, MemoryLoginRateLimiter())
    settings = Settings(environment=environment)
    return TestClient(
        create_app(
            settings=settings,
            readiness_checks={"fixture": lambda: None},
            auth_service=service,
        )
    )


def test_login_session_and_logout_lifecycle() -> None:
    client = make_client()
    response = client.post(
        "/v1/auth/login",
        json={"login": "seller@example.test", "password": "CorrectHorse42", "realm": "seller"},
    )
    assert response.status_code == 200
    assert response.json()["realm"] == "seller"
    assert "token" not in response.json()
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert client.get("/v1/auth/session").status_code == 200
    assert client.post("/v1/auth/logout").status_code == 204
    assert client.get("/v1/auth/session").status_code == 401


def test_staging_cookie_is_secure_and_wrong_realm_is_generic() -> None:
    client = make_client(environment="staging")
    response = client.post(
        "/v1/auth/login",
        json={"login": "seller@example.test", "password": "CorrectHorse42", "realm": "agency"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Credenziali non valide."}

    valid = client.post(
        "/v1/auth/login",
        json={"login": "seller@example.test", "password": "CorrectHorse42", "realm": "seller"},
    )
    assert "Secure" in valid.headers["set-cookie"]


def test_login_payload_is_validated() -> None:
    client = make_client()
    empty_login = {"login": "", "password": "x", "realm": "seller"}
    invalid_realm = {"login": "x", "password": "x", "realm": "other"}
    assert client.post("/v1/auth/login", json=empty_login).status_code == 422
    assert client.post("/v1/auth/login", json=invalid_realm).status_code == 422
