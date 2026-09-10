from __future__ import annotations

from datetime import UTC, datetime


def foundation_probe() -> dict[str, str]:
    """Small deterministic job used by readiness and deployment checks."""
    return {"status": "ok", "completed_at": datetime.now(UTC).isoformat()}


def sync_orders(job_id: str) -> None:
    """RQ arguments contain only the durable job identifier, never API credentials."""
    import asyncio
    from uuid import UUID

    from marketplace_hub_core.database import create_database_engine
    from marketplace_hub_core.marketplace_connections.repository import (
        SqlMarketplaceConnectionsRepository,
    )
    from marketplace_hub_core.orders.queue import RQOrdersQueue
    from marketplace_hub_core.orders.repository import SqlOrdersRepository
    from marketplace_hub_core.orders.service import OrdersService
    from marketplace_hub_core.settings import get_settings
    from marketplace_hub_core.tenancy.repository import SqlWorkspaceRepository
    from marketplace_hub_core.tenancy.service import WorkspaceService
    from redis import Redis

    settings = get_settings()
    engine = create_database_engine(settings)
    connection = Redis.from_url(settings.redis_url.get_secret_value(),
                                socket_timeout=5, socket_connect_timeout=5)
    try:
        service = OrdersService(
            SqlOrdersRepository(engine), WorkspaceService(SqlWorkspaceRepository(engine)),
            SqlMarketplaceConnectionsRepository(engine), RQOrdersQueue(connection),
            settings.master_key,
        )
        asyncio.run(service.run_job(UUID(job_id)))
    finally:
        connection.close()
        engine.dispose()


def refresh_catalog(job_id: str) -> None:
    """Refresh one durable catalog feed; RQ never receives source secrets."""
    from uuid import UUID

    from marketplace_hub_core.catalogs.repository import SqlCatalogsRepository
    from marketplace_hub_core.catalogs.service import CatalogsService
    from marketplace_hub_core.database import create_database_engine
    from marketplace_hub_core.settings import get_settings
    from marketplace_hub_core.tenancy.repository import SqlWorkspaceRepository
    from marketplace_hub_core.tenancy.service import WorkspaceService

    settings = get_settings()
    engine = create_database_engine(settings)
    try:
        workspace = WorkspaceService(SqlWorkspaceRepository(engine))
        service = CatalogsService(
            SqlCatalogsRepository(engine),
            workspace,
            master_key=settings.master_key,
        )
        service.run_refresh_job(UUID(job_id))
    finally:
        engine.dispose()
