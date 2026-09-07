from fastapi.testclient import TestClient
from marketplace_hub_api.main import create_app
from marketplace_hub_core.settings import Settings


def test_liveness_identifies_service_environment_and_version() -> None:
    app = create_app(settings=Settings(environment="test", version="test-version"))
    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "api",
        "environment": "test",
        "version": "test-version",
    }
    assert response.headers["x-request-id"]


def test_readiness_reports_each_dependency_without_exposing_error_text() -> None:
    def database_ok() -> None:
        return None

    def redis_down() -> None:
        raise RuntimeError("redis://user:secret@example.invalid")

    app = create_app(
        settings=Settings(environment="test"),
        readiness_checks={"database": database_ok, "redis": redis_down},
    )
    response = TestClient(app).get("/health/ready", headers={"x-request-id": "trace-123"})

    assert response.status_code == 503
    assert response.headers["x-request-id"] == "trace-123"
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["status"] == "up"
    assert body["checks"]["redis"]["status"] == "down"
    assert body["checks"]["redis"]["detail"] == "RuntimeError"
    assert "secret" not in response.text
