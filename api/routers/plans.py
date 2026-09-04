from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from api.dependencies import ApiUser, CurrentUser, TargetTenantUser, ensure_tenant_access
from api.schemas import (
    EntitlementOverrideRequest,
    PlanResponse,
    TenantEntitlementsResponse,
    TenantPlanUpdateRequest,
)
from services.entitlements import (
    clear_entitlement_override,
    list_plans,
    set_entitlement_override,
    set_tenant_plan,
    tenant_entitlements,
    update_plan_configuration,
)

router = APIRouter(tags=["plans"])


def _plan_response(item: dict) -> PlanResponse:
    return PlanResponse(
        code=str(item.get("code") or ""),
        name=str(item.get("name") or ""),
        tenant_type=str(item.get("tenant_type") or "merchant"),
        public=bool(item.get("public")),
        monthly_price_cents=int(item.get("monthly_price_cents") or 0),
        currency=str(item.get("currency") or "EUR"),
        features=[str(x) for x in item.get("features") or []],
        limits=dict(item.get("limits") or {}),
    )


def _entitlement_response(item: dict) -> TenantEntitlementsResponse:
    return TenantEntitlementsResponse(
        tenant_id=int(item.get("tenant_id") or 0),
        plan_code=str(item.get("plan_code") or ""),
        plan_name=str(item.get("plan_name") or ""),
        status=str(item.get("status") or ""),
        active=bool(item.get("active")),
        monthly_price_cents=int(item.get("monthly_price_cents") or 0),
        currency=str(item.get("currency") or "EUR"),
        features={str(k): bool(v) for k, v in (item.get("features") or {}).items()},
        limits=dict(item.get("limits") or {}),
        usage=dict(item.get("usage") or {}),
        remaining=dict(item.get("remaining") or {}),
    )


@router.get("/plans", response_model=list[PlanResponse])
def public_plans(user: CurrentUser, include_internal: bool = Query(default=False)):
    # The authenticated endpoint can expose internal plans only to Platform Admin.
    # Public pricing UI can later receive a separate unauthenticated route at the edge.
    internal = bool(include_internal and user.is_admin)
    return [_plan_response(item) for item in list_plans(public_only=not internal)]


@router.get("/tenants/{tenant_id}/entitlements", response_model=TenantEntitlementsResponse)
def entitlements(tenant_id: int, user: TargetTenantUser) -> TenantEntitlementsResponse:
    ensure_tenant_access(user, tenant_id)
    item = tenant_entitlements(int(tenant_id))
    if not item:
        raise HTTPException(status_code=404, detail="Entitlements non disponibili.")
    return _entitlement_response(item)


def _platform_admin(user: ApiUser) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operazione riservata al Platform Admin.")


class PlanConfigurationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    monthly_price_cents: int = Field(ge=0, le=10000000)
    features: list[str]
    limits: dict[str, int | None]


@router.put("/plans/{code}", response_model=PlanResponse)
def configure_plan(code: str, payload: PlanConfigurationRequest, user: CurrentUser) -> PlanResponse:
    _platform_admin(user)
    try:
        return _plan_response(update_plan_configuration(code, **payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/tenants/{tenant_id}/plan", response_model=TenantEntitlementsResponse)
def update_plan(tenant_id: int, payload: TenantPlanUpdateRequest, user: TargetTenantUser) -> TenantEntitlementsResponse:
    _platform_admin(user)
    ensure_tenant_access(user, tenant_id)
    try:
        item = set_tenant_plan(int(tenant_id), payload.plan_code, status=payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _entitlement_response(item)


@router.put("/tenants/{tenant_id}/entitlements/{key}", response_model=TenantEntitlementsResponse)
def set_override(
    tenant_id: int,
    key: str,
    payload: EntitlementOverrideRequest,
    user: TargetTenantUser,
) -> TenantEntitlementsResponse:
    _platform_admin(user)
    ensure_tenant_access(user, tenant_id)
    try:
        set_entitlement_override(
            int(tenant_id),
            key,
            kind=payload.kind,
            enabled=payload.enabled,
            limit_value=payload.limit_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _entitlement_response(tenant_entitlements(int(tenant_id), use_cache=False))


@router.delete("/tenants/{tenant_id}/entitlements/{kind}/{key}", response_model=TenantEntitlementsResponse)
def delete_override(tenant_id: int, kind: str, key: str, user: TargetTenantUser) -> TenantEntitlementsResponse:
    _platform_admin(user)
    ensure_tenant_access(user, tenant_id)
    if kind not in {"feature", "limit"}:
        raise HTTPException(status_code=400, detail="Tipo override non valido.")
    clear_entitlement_override(int(tenant_id), key, kind)
    return _entitlement_response(tenant_entitlements(int(tenant_id), use_cache=False))
