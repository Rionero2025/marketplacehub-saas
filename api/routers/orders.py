from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query

from api.dependencies import ApiUser, ensure_seller_access, require_permission
from api.helpers import load_account, submit_job
from api.schemas import JobResponse, OrderSyncRequest
from marketplace_core.orders import OrderQuery, OrderScope, OrdersCore

router = APIRouter(prefix="/sellers/{seller_id}/orders", tags=["orders"])


@router.get("")
def order_page(
    seller_id: int,
    account_id: int = Query(gt=0),
    marketplace: str = Query(min_length=1),
    environment: str = "live",
    date_from: date | None = None,
    date_to: date | None = None,
    search: str = "",
    status: list[str] = Query(default=[]),
    supplier: list[str] = Query(default=[]),
    storefront: list[str] = Query(default=[]),
    limit: int = Query(default=250, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: ApiUser = Depends(require_permission("marketplace_orders")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    load_account(seller_id, account_id, marketplace=marketplace)
    page = OrdersCore().page(
        OrderScope(seller_id, account_id, marketplace, environment),
        OrderQuery(
            date_from=date_from,
            date_to=date_to,
            statuses=tuple(status),
            suppliers=tuple(supplier),
            storefronts=tuple(storefront),
            search=search,
            limit=limit,
            offset=offset,
            include_raw=False,
        ),
    )
    return {
        "items": list(page.items),
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "has_more": page.has_more,
        "page_number": page.page_number,
        "page_count": page.page_count,
    }


@router.post("/sync", response_model=JobResponse, status_code=202)
def sync_orders(
    seller_id: int,
    account_id: int,
    payload: OrderSyncRequest,
    user: ApiUser = Depends(require_permission("marketplace_orders")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    load_account(seller_id, account_id, marketplace=payload.marketplace)
    scope = OrderScope(seller_id, account_id, payload.marketplace, payload.environment)
    request = OrdersCore().build_sync_job(
        scope,
        maximum=payload.maximum,
        include_tracking_details=payload.include_tracking_details,
        date_from=payload.date_from,
        date_to=payload.date_to,
    )
    return submit_job(request)
