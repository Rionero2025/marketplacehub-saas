from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "database.toml"


def _clean_engine(value: Any) -> str:
    engine = str(value or "sqlite").strip().lower()
    aliases = {
        "postgres": "postgresql",
        "postgresql": "postgresql",
        "pgsql": "postgresql",
        "sqlite": "sqlite",
        "sqlite3": "sqlite",
    }
    return aliases.get(engine, "sqlite")


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _database_url_settings() -> dict[str, Any]:
    """Parse DATABASE_URL / MARKETPLACE_HUB_DATABASE_URL when provided.

    Render exposes a private PostgreSQL connection string through DATABASE_URL.
    Individual MARKETPLACE_HUB_PG_* variables still take precedence.
    """
    raw = str(
        os.getenv("MARKETPLACE_HUB_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not raw:
        return {}

    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"postgres", "postgresql"}:
        return {}

    query = parse_qs(parsed.query)
    database = unquote((parsed.path or "").lstrip("/"))
    return {
        "engine": "postgresql",
        "postgresql_host": parsed.hostname or "127.0.0.1",
        "postgresql_port": int(parsed.port or 5432),
        "postgresql_database": database or "marketplace_hub",
        "postgresql_user": unquote(parsed.username or "marketplace_hub"),
        "postgresql_password": unquote(parsed.password or ""),
        "postgresql_sslmode": (query.get("sslmode") or ["prefer"])[0],
    }


def load_database_config() -> dict[str, Any]:
    """Load DB config with cloud-friendly environment variable precedence."""
    file_config: dict[str, Any] = {}
    try:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("rb") as handle:
                parsed = tomllib.load(handle)
            if isinstance(parsed, dict):
                file_config = dict(parsed)
    except (OSError, tomllib.TOMLDecodeError):
        file_config = {}

    url_config = _database_url_settings()
    merged = {**file_config, **url_config}
    default_engine = merged.get("engine", "sqlite")
    engine = _clean_engine(os.getenv("MARKETPLACE_HUB_DB_ENGINE", default_engine))

    cfg = {
        **merged,
        "engine": engine,
        "postgresql_host": os.getenv(
            "MARKETPLACE_HUB_PG_HOST", merged.get("postgresql_host", "127.0.0.1")
        ),
        "postgresql_port": _coerce_int(
            os.getenv("MARKETPLACE_HUB_PG_PORT", merged.get("postgresql_port", 5432)),
            5432,
        ),
        "postgresql_database": os.getenv(
            "MARKETPLACE_HUB_PG_DATABASE",
            merged.get("postgresql_database", "marketplace_hub"),
        ),
        "postgresql_user": os.getenv(
            "MARKETPLACE_HUB_PG_USER", merged.get("postgresql_user", "marketplace_hub")
        ),
        "postgresql_password": os.getenv(
            "MARKETPLACE_HUB_PG_PASSWORD", merged.get("postgresql_password", "")
        ),
        "postgresql_sslmode": os.getenv(
            "MARKETPLACE_HUB_PG_SSLMODE", merged.get("postgresql_sslmode", "prefer")
        ),
        "postgresql_pool_min": max(
            1,
            _coerce_int(
                os.getenv(
                    "MARKETPLACE_HUB_PG_POOL_MIN",
                    merged.get("postgresql_pool_min", 2),
                ),
                2,
            ),
        ),
        "postgresql_pool_max": max(
            1,
            _coerce_int(
                os.getenv(
                    "MARKETPLACE_HUB_PG_POOL_MAX",
                    merged.get("postgresql_pool_max", 12),
                ),
                12,
            ),
        ),
        "postgresql_connect_timeout": max(
            1,
            _coerce_int(
                os.getenv(
                    "MARKETPLACE_HUB_PG_CONNECT_TIMEOUT",
                    merged.get("postgresql_connect_timeout", 10),
                ),
                10,
            ),
        ),
    }
    if cfg["postgresql_pool_max"] < cfg["postgresql_pool_min"]:
        cfg["postgresql_pool_max"] = cfg["postgresql_pool_min"]
    return cfg


def database_engine() -> str:
    return str(load_database_config().get("engine") or "sqlite")


def postgresql_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or load_database_config())
    return {
        "host": str(cfg.get("postgresql_host") or "127.0.0.1"),
        "port": _coerce_int(cfg.get("postgresql_port"), 5432),
        "dbname": str(cfg.get("postgresql_database") or "marketplace_hub"),
        "user": str(cfg.get("postgresql_user") or "marketplace_hub"),
        "password": str(cfg.get("postgresql_password") or ""),
        "sslmode": str(cfg.get("postgresql_sslmode") or "prefer"),
        "connect_timeout": _coerce_int(cfg.get("postgresql_connect_timeout"), 10),
    }


def database_config_public(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or load_database_config())
    return {
        "engine": _clean_engine(cfg.get("engine")),
        "config_path": str(CONFIG_PATH),
        "postgresql_host": str(cfg.get("postgresql_host") or "127.0.0.1"),
        "postgresql_port": _coerce_int(cfg.get("postgresql_port"), 5432),
        "postgresql_database": str(cfg.get("postgresql_database") or "marketplace_hub"),
        "postgresql_user": str(cfg.get("postgresql_user") or "marketplace_hub"),
        "postgresql_sslmode": str(cfg.get("postgresql_sslmode") or "prefer"),
        "postgresql_pool_min": _coerce_int(cfg.get("postgresql_pool_min"), 2),
        "postgresql_pool_max": _coerce_int(cfg.get("postgresql_pool_max"), 12),
        "postgresql_password_configured": bool(str(cfg.get("postgresql_password") or "")),
        "database_url_configured": bool(
            os.getenv("MARKETPLACE_HUB_DATABASE_URL") or os.getenv("DATABASE_URL")
        ),
    }
