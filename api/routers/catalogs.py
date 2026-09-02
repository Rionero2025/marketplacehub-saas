from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import ApiUser, ensure_seller_access, require_permission
from api.helpers import submit_job
from api.schemas import CatalogMaterializeRequest, JobResponse
from marketplace_core.catalogs import CatalogCore
from services.db import accessible_lists

router = APIRouter(prefix="/sellers/{seller_id}/catalogs", tags=["catalogs"])


def _ensure_list(seller_id: int, price_list_id: int) -> dict:
    for item in accessible_lists(seller_id):
        if int(item.get("id") or 0) == int(price_list_id):
            return item
    raise HTTPException(status_code=404, detail="Listino non disponibile.")


@router.get("")
def catalogs(
    seller_id: int,
    user: ApiUser = Depends(require_permission("suppliers_lists")),
) -> list[dict]:
    seller_id = ensure_seller_access(user, seller_id)
    result = []
    for item in accessible_lists(seller_id):
        result.append({
            "id": int(item.get("id") or 0),
            "name": str(item.get("name") or ""),
            "supplier_name": str(item.get("supplier_name") or ""),
            "owner_seller_id": int(item.get("owner_seller_id") or 0),
            "visibility": str(item.get("visibility") or ""),
            "file_format": str(item.get("file_format") or ""),
            "active": bool(int(item.get("active") or 0)),
        })
    return result


@router.get("/{price_list_id}/products")
def catalog_products(
    seller_id: int,
    price_list_id: int,
    search: str = "",
    min_qty: float = 0,
    min_cost: float = 0,
    max_cost: float = 0,
    destination_country: str = "",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=250, ge=1, le=1000),
    user: ApiUser = Depends(require_permission("work_lists")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    _ensure_list(seller_id, price_list_id)
    page = CatalogCore().query(
        price_list_id,
        search=search,
        min_qty=min_qty,
        min_cost=min_cost,
        max_cost=max_cost,
        destination_country=destination_country,
        offset=offset,
        limit=limit,
    )
    return {"items": page.rows, "total": page.total, "offset": page.offset, "limit": page.limit}


@router.post("/materialize", response_model=JobResponse, status_code=202)
def materialize_catalog(
    seller_id: int,
    payload: CatalogMaterializeRequest,
    user: ApiUser = Depends(require_permission("work_lists")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    _ensure_list(seller_id, payload.price_list_id)
    return submit_job(CatalogCore().build_materialize_job(seller_id, payload.price_list_id))
