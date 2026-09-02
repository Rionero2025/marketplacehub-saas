from __future__ import annotations

from fastapi import APIRouter, Depends

from api.dependencies import ApiUser, ensure_seller_access, require_permission
from api.schemas import MarketplaceAccountResponse, SellerResponse
from services.db import rows

router = APIRouter(prefix="/sellers", tags=["sellers"])


@router.get("", response_model=list[SellerResponse])
def list_sellers(
    user: ApiUser = Depends(require_permission("dashboard")),
) -> list[SellerResponse]:
    # v315: sellers are always constrained by the active tenant, including
    # Platform Admin. Cross-tenant work requires an explicit tenant switch.
    ids = sorted(user.seller_ids or ())
    if not ids:
        return []
    items = rows(
        "SELECT id,name,legal_name,active FROM sellers "
        "WHERE active=1 AND id IN (" + ",".join("?" for _ in ids) + ") ORDER BY name",
        tuple(ids),
    )
    return [
        SellerResponse(
            id=int(item["id"]),
            name=str(item.get("name") or ""),
            legal_name=str(item.get("legal_name") or ""),
            active=bool(int(item.get("active") or 0)),
        )
        for item in items
    ]


@router.get("/{seller_id}/accounts", response_model=list[MarketplaceAccountResponse])
def seller_accounts(
    seller_id: int,
    user: ApiUser = Depends(require_permission("dashboard")),
) -> list[MarketplaceAccountResponse]:
    seller_id = ensure_seller_access(user, seller_id)
    items = rows(
        """SELECT id,seller_id,marketplace,account_name,active
           FROM marketplace_accounts WHERE seller_id=? AND active=1
           ORDER BY marketplace,account_name""",
        (seller_id,),
    )
    return [MarketplaceAccountResponse(**{
        "id": int(item["id"]),
        "seller_id": int(item["seller_id"]),
        "marketplace": str(item.get("marketplace") or ""),
        "account_name": str(item.get("account_name") or ""),
        "active": bool(int(item.get("active") or 0)),
    }) for item in items]
