from __future__ import annotations

from fastapi import APIRouter, Depends

from api.dependencies import ApiUser, ensure_seller_access, require_permission
from api.helpers import load_account, submit_job
from api.schemas import AccountingJobRequest, JobResponse
from marketplace_core.accounting import AccountingCore, AccountingPeriod, AccountingScope
from services.security import decrypt_dict

router = APIRouter(prefix="/sellers/{seller_id}/accounting", tags=["accounting"])


@router.get("/status")
def accounting_status(
    seller_id: int,
    account_id: int,
    marketplace: str,
    user: ApiUser = Depends(require_permission("accounting")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    account = load_account(seller_id, account_id, marketplace=marketplace)
    credentials = decrypt_dict(account.get("credentials_encrypted") or "")
    status = AccountingCore().status(
        AccountingScope(seller_id, account_id, marketplace), credentials
    )
    return {
        "environment": status.environment,
        "sync_state": status.sync_state,
        "cache_summary": status.cache_summary,
    }


@router.post("/sync", response_model=JobResponse, status_code=202)
def accounting_sync(
    seller_id: int,
    account_id: int,
    payload: AccountingJobRequest,
    user: ApiUser = Depends(require_permission("accounting")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    load_account(seller_id, account_id, marketplace=payload.marketplace)
    request = AccountingCore().build_sync_job(
        AccountingScope(seller_id, account_id, payload.marketplace),
        AccountingPeriod(payload.date_from, payload.date_to),
        full=payload.full,
    )
    return submit_job(request)


@router.post("/refresh-costs", response_model=JobResponse, status_code=202)
def refresh_costs(
    seller_id: int,
    account_id: int,
    payload: AccountingJobRequest,
    user: ApiUser = Depends(require_permission("accounting")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    load_account(seller_id, account_id, marketplace=payload.marketplace)
    request = AccountingCore().build_refresh_costs_job(
        AccountingScope(seller_id, account_id, payload.marketplace),
        AccountingPeriod(payload.date_from, payload.date_to),
    )
    return submit_job(request)
