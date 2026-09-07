from __future__ import annotations

from collections.abc import Callable, Mapping
from time import perf_counter
from typing import Any

from redis import Redis
from sqlalchemy import text

from .database import create_database_engine
from .settings import Settings

Check = Callable[[], None]


def check_database(settings: Settings) -> None:
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def check_redis(settings: Settings) -> None:
    client = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=settings.readiness_timeout_seconds,
        socket_connect_timeout=settings.readiness_timeout_seconds,
    )
    try:
        client.ping()
    finally:
        client.close()


def run_checks(checks: Mapping[str, Check]) -> tuple[bool, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, Any]] = {}
    all_up = True
    for name, check in checks.items():
        start = perf_counter()
        try:
            check()
            status: dict[str, Any] = {"status": "up"}
        except Exception as exc:  # health response intentionally normalizes provider errors
            all_up = False
            status = {"status": "down", "detail": type(exc).__name__}
        status["latency_ms"] = round((perf_counter() - start) * 1000, 2)
        result[name] = status
    return all_up, result
