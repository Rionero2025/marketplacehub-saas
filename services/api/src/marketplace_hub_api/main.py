from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from marketplace_hub_core.auth import AuthService
from marketplace_hub_core.auth.rate_limit import RedisLoginRateLimiter
from marketplace_hub_core.auth.sql_repository import SqlAuthRepository
from marketplace_hub_core.database import create_database_engine
from marketplace_hub_core.readiness import Check, check_database, check_redis, run_checks
from marketplace_hub_core.seller_settings.repository import SqlSellerSettingsRepository
from marketplace_hub_core.seller_settings.service import SellerSettingsService
from marketplace_hub_core.settings import Settings, get_settings
from marketplace_hub_core.tenancy.repository import SqlWorkspaceRepository
from marketplace_hub_core.tenancy.service import WorkspaceService
from redis import Redis

from marketplace_hub_api.auth import create_auth_router
from marketplace_hub_api.seller_settings import create_seller_settings_router
from marketplace_hub_api.workspace import create_workspace_router


def create_app(
    *,
    settings: Settings | None = None,
    readiness_checks: Mapping[str, Check] | None = None,
    auth_service: AuthService | None = None,
    workspace_service: WorkspaceService | None = None,
    seller_settings_service: SellerSettingsService | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    checks = readiness_checks or {
        "database": lambda: check_database(app_settings),
        "redis": lambda: check_redis(app_settings),
    }

    engine = None
    redis_client = None
    if auth_service is None:
        engine = create_database_engine(app_settings)
        redis_client = Redis.from_url(
            app_settings.redis_url.get_secret_value(), decode_responses=True
        )
        auth_service = AuthService(
            SqlAuthRepository(engine),
            RedisLoginRateLimiter(redis_client),
            session_ttl=timedelta(hours=app_settings.session_ttl_hours),
            login_attempt_limit=app_settings.login_attempt_limit,
            login_window_seconds=app_settings.login_window_seconds,
        )

    if workspace_service is None:
        if engine is None:
            engine = create_database_engine(app_settings)
        workspace_service = WorkspaceService(SqlWorkspaceRepository(engine))

    if seller_settings_service is None:
        seller_settings_service = SellerSettingsService(
            SqlSellerSettingsRepository(workspace_service.repository.engine),
            workspace_service,
            app_settings.master_key,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            if redis_client is not None:
                redis_client.close()
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title=app_settings.app_name,
        version=app_settings.version,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["content-type", "x-request-id"],
    )
    app.include_router(create_auth_router(auth_service, app_settings))
    app.include_router(create_workspace_router(workspace_service, auth_service, app_settings))
    app.include_router(create_seller_settings_router(
        seller_settings_service, auth_service, app_settings,
    ))

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
