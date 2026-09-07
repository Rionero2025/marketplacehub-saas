from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MH_",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "Marketplace Hub API"
    version: str = "0.1.0"
    database_url: SecretStr = Field(
        default=SecretStr(
            "postgresql+psycopg://marketplace_hub:marketplace_hub@localhost:5432/marketplace_hub"
        ),
        validation_alias=AliasChoices("MH_DATABASE_URL", "DATABASE_URL"),
    )
    redis_url: SecretStr = Field(
        default=SecretStr("redis://localhost:6379/0"),
        validation_alias=AliasChoices("MH_REDIS_URL", "MARKETPLACE_HUB_REDIS_URL"),
    )
    master_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("MH_MASTER_KEY", "MARKETPLACE_HUB_MASTER_KEY"),
    )
    db_pool_size: int = 5
    db_max_overflow: int = 10
    readiness_timeout_seconds: float = 2.0
    session_cookie_name: str = "mh_session"
    session_ttl_hours: int = 12
    login_attempt_limit: int = 5
    login_window_seconds: int = 900
    allowed_origins: str = "http://localhost:3000"
    cookie_secure_override: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("MH_COOKIE_SECURE", "MARKETPLACE_HUB_COOKIE_SECURE"),
    )

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure_override is not None:
            return self.cookie_secure_override
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
