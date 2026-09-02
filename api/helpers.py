from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException

from marketplace_core.jobs import JobsCore
from services.db import row


def load_account(seller_id: int, account_id: int, *, marketplace: str = "") -> dict[str, Any]:
    params: list[Any] = [int(account_id), int(seller_id)]
    sql = "SELECT * FROM marketplace_accounts WHERE id=? AND seller_id=? AND active=1"
    market = str(marketplace or "").strip().lower()
    if market:
        sql += " AND lower(marketplace)=?"
        params.append(market)
    account = row(sql, tuple(params))
    if not account:
        raise HTTPException(status_code=404, detail="Account marketplace non disponibile.")
    return account


def submit_job(request) -> dict[str, Any]:
    core = JobsCore()
    receipt = core.submit(request)
    # Local background execution is useful for dev/single-container staging only.
    # In SaaS production this remains off and a dedicated worker claims the job.
    if str(os.getenv("MARKETPLACE_HUB_API_LOCAL_JOBS") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }:
        core.start_local(receipt.job_id)
    snapshot = core.snapshot(receipt.job_id)
    if snapshot is None:
        return {"job_id": receipt.job_id, "status": receipt.status}
    return {
        "job_id": snapshot.job_id,
        "kind": snapshot.kind,
        "status": snapshot.status,
        "seller_id": snapshot.seller_id,
        "tenant_id": snapshot.tenant_id,
        "progress_current": snapshot.progress_current,
        "progress_total": snapshot.progress_total,
        "progress_pct": snapshot.progress_pct,
        "message": snapshot.message,
        "result": dict(snapshot.result),
        "error": snapshot.error,
        "created_at": snapshot.created_at,
        "started_at": snapshot.started_at,
        "finished_at": snapshot.finished_at,
    }
