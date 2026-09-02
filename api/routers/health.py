from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api import API_VERSION
from services.database_config import database_engine
from services.db import row
from services.object_storage import storage_status
from services.shared_cache import cache_info

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"ok": True, "service": "marketplace-hub-api", "api_version": API_VERSION}


@router.get("/ready")
def ready() -> dict:
    try:
        probe = row("SELECT 1 AS ok") or {}
        if int(probe.get("ok") or 0) != 1:
            raise RuntimeError("database probe failed")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database non pronto: {exc}") from exc
    cache = cache_info()
    storage = storage_status()
    return {
        "ok": True,
        "database": database_engine(),
        "cache": {
            "backend": str(cache.get("backend") or ""),
            "connected": bool(cache.get("connected", True)),
        },
        "storage": {
            "backend": str(storage.get("backend") or ""),
            "ready": bool(storage.get("ready")),
        },
    }
