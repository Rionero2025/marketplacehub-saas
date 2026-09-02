from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.dependencies import CurrentUser, ensure_seller_access
from api.schemas import JobResponse
from marketplace_core.jobs import JobsCore
from services.background_jobs import recent_jobs

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _snapshot_dict(item) -> dict:
    return {
        "job_id": item.job_id,
        "kind": item.kind,
        "status": item.status,
        "seller_id": item.seller_id,
        "tenant_id": item.tenant_id,
        "progress_current": item.progress_current,
        "progress_total": item.progress_total,
        "progress_pct": item.progress_pct,
        "message": item.message,
        "result": dict(item.result),
        "error": item.error,
        "created_at": item.created_at,
        "started_at": item.started_at,
        "finished_at": item.finished_at,
    }


@router.get("/{job_id}", response_model=JobResponse)
def job_status(job_id: str, user: CurrentUser) -> dict:
    item = JobsCore().snapshot(job_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Job non trovato.")
    if item.tenant_id is not None and int(item.tenant_id) != int(user.active_tenant_id):
        raise HTTPException(status_code=404, detail="Job non trovato.")
    if item.seller_id is None:
        if not user.is_admin:
            raise HTTPException(status_code=404, detail="Job non trovato.")
    else:
        ensure_seller_access(user, item.seller_id)
    return _snapshot_dict(item)


@router.get("", response_model=list[JobResponse])
def jobs(
    user: CurrentUser,
    seller_id: int | None = None,
    kind_prefix: str = "",
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    if seller_id is None:
        if not user.is_admin:
            # Non-admin users must pick one of their sellers to prevent cross-seller scans.
            raise HTTPException(status_code=400, detail="Indica seller_id.")
    else:
        seller_id = ensure_seller_access(user, seller_id)
    items = recent_jobs(seller_id=seller_id, kind_prefix=kind_prefix, limit=limit)
    result = []
    for item in items:
        if str(item.get("tenant_id") or "") and int(item.get("tenant_id") or 0) != int(user.active_tenant_id):
            continue
        if item.get("seller_id") is not None and not user.can_access_seller(int(item["seller_id"])):
            continue
        result.append({
            "job_id": str(item.get("id") or ""),
            "kind": str(item.get("kind") or ""),
            "status": str(item.get("status") or ""),
            "seller_id": int(item["seller_id"]) if item.get("seller_id") is not None else None,
            "tenant_id": int(item["tenant_id"]) if str(item.get("tenant_id") or "").isdigit() else None,
            "progress_current": int(item.get("progress_current") or 0),
            "progress_total": int(item.get("progress_total") or 0),
            "progress_pct": float(item.get("progress_pct") or 0),
            "message": str(item.get("message") or ""),
            "result": item.get("result") or {},
            "error": str(item.get("error") or ""),
            "created_at": str(item.get("created_at") or ""),
            "started_at": str(item.get("started_at") or ""),
            "finished_at": str(item.get("finished_at") or ""),
        })
    return result
