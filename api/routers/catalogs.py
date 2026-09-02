from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import ApiUser, ensure_seller_access, require_permission
from api.helpers import submit_job
from api.schemas import (
    CatalogMaterializeRequest, CatalogSharingRequest, CatalogSharingResponse, JobResponse,
    SupplierSharingRequest, SupplierSharingResponse,
)
from marketplace_core.catalogs import CatalogCore
from services.db import accessible_lists
from services.catalog_sharing import (
    catalog_policy, set_price_list_sharing, set_supplier_sharing, supplier_policy,
)

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
            "sharing_scope": str(item.get("sharing_scope") or item.get("catalog_scope") or "tenant"),
            "owner_tenant_id": int(item.get("owner_tenant_id") or item.get("catalog_owner_tenant_id") or 0),
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


def _can_manage_catalogs(user: ApiUser) -> None:
    if user.is_admin:
        return
    if str(user.tenant_role or "").lower() not in {"owner", "admin", "manager"}:
        raise HTTPException(status_code=403, detail="Ruolo tenant non autorizzato alla condivisione cataloghi.")


@router.get("/{price_list_id}/sharing", response_model=CatalogSharingResponse)
def get_catalog_sharing(
    seller_id: int,
    price_list_id: int,
    user: ApiUser = Depends(require_permission("suppliers_lists")),
) -> CatalogSharingResponse:
    seller_id = ensure_seller_access(user, seller_id)
    _ensure_list(seller_id, price_list_id)
    item = catalog_policy(price_list_id)
    if not item:
        raise HTTPException(status_code=404, detail="Listino non disponibile.")
    return CatalogSharingResponse(
        id=int(item["id"]),
        owner_tenant_id=int(item.get("owner_tenant_id") or 0),
        sharing_scope=str(item.get("sharing_scope") or "tenant"),
        visibility=str(item.get("visibility") or "private"),
        tenant_ids=[int(v) for v in item.get("tenant_ids") or []],
    )


@router.put("/{price_list_id}/sharing", response_model=CatalogSharingResponse)
def update_catalog_sharing(
    seller_id: int,
    price_list_id: int,
    payload: CatalogSharingRequest,
    user: ApiUser = Depends(require_permission("suppliers_lists")),
) -> CatalogSharingResponse:
    seller_id = ensure_seller_access(user, seller_id)
    _ensure_list(seller_id, price_list_id)
    _can_manage_catalogs(user)
    try:
        item = set_price_list_sharing(
            price_list_id,
            actor_tenant_id=user.active_tenant_id,
            scope=payload.scope,
            tenant_ids=payload.tenant_ids,
            permission=payload.permission,
            platform_admin=user.is_admin,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CatalogSharingResponse(
        id=int(item["id"]),
        owner_tenant_id=int(item.get("owner_tenant_id") or 0),
        sharing_scope=str(item.get("sharing_scope") or "tenant"),
        visibility=str(item.get("visibility") or "private"),
        tenant_ids=[int(v) for v in item.get("tenant_ids") or []],
    )


@router.get("/suppliers/{supplier_id}/sharing", response_model=SupplierSharingResponse)
def get_supplier_sharing(
    seller_id: int,
    supplier_id: int,
    user: ApiUser = Depends(require_permission("suppliers_lists")),
) -> SupplierSharingResponse:
    seller_id = ensure_seller_access(user, seller_id)
    item = supplier_policy(supplier_id)
    if not item:
        raise HTTPException(status_code=404, detail="Fornitore non disponibile.")
    # Do not reveal suppliers that have no catalogue visible in the active tenant
    # unless the tenant itself owns them.
    visible_supplier_ids = {int(x.get("supplier_id") or 0) for x in accessible_lists(seller_id)}
    if int(supplier_id) not in visible_supplier_ids and int(item.get("owner_tenant_id") or 0) != user.active_tenant_id:
        raise HTTPException(status_code=404, detail="Fornitore non disponibile.")
    return SupplierSharingResponse(
        id=int(item["id"]),
        owner_tenant_id=int(item.get("owner_tenant_id") or 0),
        sharing_scope=str(item.get("sharing_scope") or "tenant"),
    )


@router.put("/suppliers/{supplier_id}/sharing", response_model=SupplierSharingResponse)
def update_supplier_sharing(
    seller_id: int,
    supplier_id: int,
    payload: SupplierSharingRequest,
    user: ApiUser = Depends(require_permission("suppliers_lists")),
) -> SupplierSharingResponse:
    ensure_seller_access(user, seller_id)
    _can_manage_catalogs(user)
    try:
        item = set_supplier_sharing(
            supplier_id,actor_tenant_id=user.active_tenant_id,scope=payload.scope,
            platform_admin=user.is_admin,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SupplierSharingResponse(
        id=int(item["id"]),
        owner_tenant_id=int(item.get("owner_tenant_id") or 0),
        sharing_scope=str(item.get("sharing_scope") or "tenant"),
    )
