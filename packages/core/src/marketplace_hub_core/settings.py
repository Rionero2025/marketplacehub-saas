from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MH_",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "Marketplace Hub API"
    version: str = "0.1.0"
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://marketplace_hub:marketplace_hub@localhost:5432/marketplace_hub"
    )
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    db_pool_size: int = 5
    db_max_overflow: int = 10
    readiness_timeout_seconds: float = 2.0
    session_cookie_name: str = "mh_session"
    session_ttl_hours: int = 12
    login_attempt_limit: int = 5
    login_window_seconds: int = 900
    allowed_origins: str = "http://localhost:3000"

    @property
    def secure_cookies(self) -> bool:
        return self.environment in {"staging", "production"}

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_render_postgres_url(cls, value: object) -> object:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        if raw.startswith("postgres://"):
            return "postgresql+psycopg://" + raw.removeprefix("postgres://")
        if raw.startswith("postgresql://"):
            return "postgresql+psycopg://" + raw.removeprefix("postgresql://")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
