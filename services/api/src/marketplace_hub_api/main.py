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
from marketplace_hub_core.marketplace_connections.repository import (
    SqlMarketplaceConnectionsRepository,
)
from marketplace_hub_core.marketplace_connections.service import MarketplaceConnectionsService
from marketplace_hub_core.orders.queue import RQOrdersQueue
from marketplace_hub_core.orders.repository import SqlOrdersRepository
from marketplace_hub_core.orders.service import OrdersService
from marketplace_hub_core.readiness import Check, check_database, check_redis, run_checks
from marketplace_hub_core.seller_settings.repository import SqlSellerSettingsRepository
from marketplace_hub_core.seller_settings.service import SellerSettingsService
from marketplace_hub_core.settings import Settings, get_settings
from marketplace_hub_core.tenancy.repository import SqlWorkspaceRepository
from marketplace_hub_core.tenancy.service import WorkspaceService
from redis import Redis

from marketplace_hub_api.auth import create_auth_router
from marketplace_hub_api.marketplace_connections import create_marketplace_connections_router
from marketplace_hub_api.orders import create_orders_router
from marketplace_hub_api.seller_settings import create_seller_settings_router
from marketplace_hub_api.workspace import create_workspace_router


def create_app(
    *,
    settings: Settings | None = None,
    readiness_checks: Mapping[str, Check] | None = None,
    auth_service: AuthService | None = None,
    workspace_service: WorkspaceService | None = None,
    seller_settings_service: SellerSettingsService | None = None,
    marketplace_connections_service: MarketplaceConnectionsService | None = None,
    orders_service: OrdersService | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    checks = readiness_checks or {
        "database": lambda: check_database(app_settings),
        "redis": lambda: check_redis(app_settings),
    }

    engine = None
    redis_client = None
    orders_redis_client = None
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

    if marketplace_connections_service is None:
        marketplace_connections_service = MarketplaceConnectionsService(
            SqlMarketplaceConnectionsRepository(workspace_service.repository.engine),
            workspace_service, app_settings.master_key,
        )

    if orders_service is None:
        # RQ stores binary payloads; never reuse the auth limiter's decoded Redis client.
        orders_redis_client = Redis.from_url(app_settings.redis_url.get_secret_value(),
                                             socket_timeout=5, socket_connect_timeout=5)
        orders_service = OrdersService(
            SqlOrdersRepository(workspace_service.repository.engine), workspace_service,
            SqlMarketplaceConnectionsRepository(workspace_service.repository.engine),
            RQOrdersQueue(orders_redis_client), app_settings.master_key,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            if orders_redis_client is not None:
                orders_redis_client.close()
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
    auth_engine = getattr(auth_service.repository, "engine", None)
    orders_engine = orders_service.repository.engine

    def purge_stale_order_selections():
        return orders_service.selections.purge_stale(limit=50, member_limit=1_000)

    login_maintenance = (
        purge_stale_order_selections
        if auth_engine is orders_engine
        else None
    )
    app.include_router(create_auth_router(
        auth_service, app_settings, after_login=login_maintenance,
    ))
    app.include_router(create_workspace_router(workspace_service, auth_service, app_settings))
    app.include_router(create_seller_settings_router(
        seller_settings_service, auth_service, app_settings,
    ))
    app.include_router(create_marketplace_connections_router(
        marketplace_connections_service, auth_service, app_settings,
    ))
    app.include_router(create_orders_router(orders_service, auth_service, app_settings))

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
