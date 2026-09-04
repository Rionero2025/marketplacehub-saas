from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.dependencies import ApiUser, CurrentUser, TargetTenantUser, ensure_tenant_access, request_token
from api.schemas import (
    AgencyClientLinkRequest,
    TenantActivateResponse,
    TenantCreateRequest,
    TenantMembershipRequest,
    TenantResponse,
    TenantSellerAttachRequest,
)
from api.session_store import switch_session_tenant
from services.tenancy import (
    accessible_tenants_for_user,
    add_membership,
    attach_seller,
    create_tenant,
    link_agency_client,
    tenant_record,
    tenant_seller_ids,
)

router = APIRouter(prefix="/tenants", tags=["tenants"])


def _tenant_response(item: dict, *, active_tenant_id: int = 0) -> TenantResponse:
    return TenantResponse(
        id=int(item["id"]),
        name=str(item.get("name") or ""),
        slug=str(item.get("slug") or ""),
        tenant_type=str(item.get("tenant_type") or "merchant"),
        status=str(item.get("status") or "active"),
        plan_code=str(item.get("plan_code") or ""),
        access_mode=str(item.get("access_mode") or ""),
        role=str(item.get("role") or ""),
        active=int(item["id"]) == int(active_tenant_id),
    )


@router.get("", response_model=list[TenantResponse])
def list_tenants(user: CurrentUser) -> list[TenantResponse]:
    items = accessible_tenants_for_user(user.id, global_admin=user.is_admin)
    return [_tenant_response(item, active_tenant_id=user.active_tenant_id) for item in items]


@router.post("/{tenant_id}/activate", response_model=TenantActivateResponse)
def activate_tenant(tenant_id: int, request: Request, user: CurrentUser) -> TenantActivateResponse:
    ensure_tenant_access(user, tenant_id)
    token = request_token(request)
    context = switch_session_tenant(token, int(tenant_id))
    if not context:
        raise HTTPException(status_code=400, detail="Impossibile attivare il tenant.")
    return TenantActivateResponse(
        active_tenant_id=int(context["id"]),
        name=str(context.get("name") or ""),
        tenant_type=str(context.get("tenant_type") or "merchant"),
        role=str(context.get("role") or ""),
    )


@router.get("/{tenant_id}/sellers")
def tenant_sellers(tenant_id: int, user: TargetTenantUser) -> dict:
    ensure_tenant_access(user, tenant_id)
    # The API switches context explicitly; this endpoint is informational only.
    ids = tenant_seller_ids(int(tenant_id))
    if not user.is_admin:
        if int(tenant_id) == int(user.active_tenant_id):
            ids = [seller_id for seller_id in ids if user.can_access_seller(seller_id)]
        elif user.legacy_seller_ids is not None:
            ids = [seller_id for seller_id in ids if seller_id in user.legacy_seller_ids]
    return {"tenant_id": int(tenant_id), "seller_ids": ids}


def _global_admin(user: ApiUser) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operazione riservata al Platform Admin.")


@router.post("", response_model=TenantResponse, status_code=201)
def admin_create_tenant(payload: TenantCreateRequest, user: CurrentUser) -> TenantResponse:
    _global_admin(user)
    from services.tenant_db import platform_database_scope
    # The new tenant cannot yet be an active session target. Only the explicit
    # platform-admin operation may bootstrap it outside the caller's RLS scope.
    with platform_database_scope():
        tenant_id = create_tenant(
            payload.name,
            slug=payload.slug,
            tenant_type=payload.tenant_type,
            plan_code=payload.plan_code,
            owner_user_id=payload.owner_user_id,
        )
        item = tenant_record(tenant_id) or {}
    item.update({"access_mode": "global_admin", "role": "platform_admin"})
    return _tenant_response(item, active_tenant_id=user.active_tenant_id)


@router.post("/{tenant_id}/members/{user_id}", status_code=204)
def admin_membership(tenant_id: int, user_id: int, payload: TenantMembershipRequest, user: TargetTenantUser):
    _global_admin(user)
    add_membership(int(tenant_id), int(user_id), role=payload.role, active=payload.active)
    return None


@router.post("/{tenant_id}/sellers/{seller_id}", status_code=204)
def admin_attach_seller(tenant_id: int, seller_id: int, payload: TenantSellerAttachRequest, user: TargetTenantUser):
    _global_admin(user)
    attach_seller(int(tenant_id), int(seller_id), transfer=payload.transfer)
    return None


@router.post("/{agency_tenant_id}/clients/{client_tenant_id}", status_code=204)
def admin_link_client(
    agency_tenant_id: int,
    client_tenant_id: int,
    payload: AgencyClientLinkRequest,
    user: CurrentUser,
):
    _global_admin(user)
    link_agency_client(int(agency_tenant_id), int(client_tenant_id), active=payload.active)
    return None
