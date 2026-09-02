from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.dependencies import ApiUser, ensure_seller_access, require_permission
from api.helpers import load_account
from marketplace_core.buybox import BuyBoxCore, BuyBoxQuery, BuyBoxScope

router = APIRouter(prefix="/sellers/{seller_id}/buybox", tags=["buybox"])


@router.get("/summary")
def buybox_summary(
    seller_id: int,
    account_id: int,
    marketplace: str,
    environment: str = "live",
    price_list_id: int | None = None,
    channel_code: str = "WRT_PT_ONLINE",
    user: ApiUser = Depends(require_permission("buybox")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    load_account(seller_id, account_id, marketplace=marketplace)
    return BuyBoxCore().saved_summary(BuyBoxScope(
        seller_id, account_id, marketplace, environment, price_list_id, channel_code
    ))


@router.get("")
def buybox_page(
    seller_id: int,
    account_id: int,
    marketplace: str,
    environment: str = "live",
    price_list_id: int | None = None,
    channel_code: str = "WRT_PT_ONLINE",
    search: str = "",
    status: list[str] = Query(default=[]),
    storefront: list[str] = Query(default=[]),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=250, ge=1, le=1000),
    user: ApiUser = Depends(require_permission("buybox")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    load_account(seller_id, account_id, marketplace=marketplace)
    page = BuyBoxCore().saved_page(
        BuyBoxScope(seller_id, account_id, marketplace, environment, price_list_id, channel_code),
        BuyBoxQuery(
            storefronts=tuple(storefront), statuses=tuple(status), search=search,
            offset=offset, limit=limit, include_details=False,
        ),
    )
    return {
        "items": list(page.items), "total": page.total, "limit": page.limit,
        "offset": page.offset, "has_more": page.has_more,
        "page_number": page.page_number, "page_count": page.page_count,
    }
