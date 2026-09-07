from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from marketplace_hub_core.readiness import Check, check_database, check_redis, run_checks
from marketplace_hub_core.settings import Settings, get_settings


def create_app(
    *,
    settings: Settings | None = None,
    readiness_checks: Mapping[str, Check] | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    checks = readiness_checks or {
        "database": lambda: check_database(app_settings),
        "redis": lambda: check_redis(app_settings),
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield

    app = FastAPI(
        title=app_settings.app_name,
        version=app_settings.version,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid4())
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/health/live", tags=["health"])
    def liveness() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "api",
            "environment": app_settings.environment,
            "version": app_settings.version,
        }

    @app.get("/health/ready", tags=["health"])
    def readiness() -> JSONResponse:
        ready, results = run_checks(checks)
        payload = {
            "status": "ok" if ready else "degraded",
            "service": "api",
            "environment": app_settings.environment,
            "version": app_settings.version,
            "checks": results,
        }
        return JSONResponse(
            payload,
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return app


app = create_app()
