from marketplace_hub_core.settings import Settings


def test_settings_keep_connections_secret_in_serialized_output() -> None:
    settings = Settings(
        environment="test",
        database_url="postgresql+psycopg://user:database-secret@db/example",
        redis_url="redis://:redis-secret@redis:6379/0",
    )

    serialized = str(settings)
    assert "database-secret" not in serialized
    assert "redis-secret" not in serialized
    assert settings.database_url.get_secret_value().endswith("@db/example")


def test_render_postgres_url_uses_the_installed_psycopg_driver() -> None:
    settings = Settings(database_url="postgresql://user:secret@render-db/example")

    assert settings.database_url.get_secret_value().startswith("postgresql+psycopg://")


def test_existing_staging_environment_names_are_accepted(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@db.example/app")
    monkeypatch.setenv("MARKETPLACE_HUB_REDIS_URL", "redis://cache.example:6379/0")
    monkeypatch.setenv("MARKETPLACE_HUB_COOKIE_SECURE", "1")
    settings = Settings()
    assert settings.database_url.get_secret_value().startswith("postgresql+psycopg://")
    assert settings.redis_url.get_secret_value() == "redis://cache.example:6379/0"
    assert settings.secure_cookies
