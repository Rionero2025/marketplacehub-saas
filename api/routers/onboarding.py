from __future__ import annotations

import os, time
from fastapi import APIRouter, Depends, HTTPException, Response, status

from api.dependencies import ApiUser, CurrentUser, COOKIE_NAME, ensure_seller_access, require_permission
from api.schemas import (
    BillingSnapshotResponse, MarketplaceConnectRequest, MarketplaceConnectResponse,
    OnboardingSignupRequest, OnboardingSignupResponse, OnboardingStatusResponse,
    SellerResponse, TenantResponse, UserResponse,
)
from api.session_store import issue_session, session_user
from services.onboarding import connect_marketplace, onboarding_status, public_plan_codes, register_merchant

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _user_response(record: dict) -> UserResponse:
    sellers = record.get("seller_ids")
    return UserResponse(
        id=int(record.get("id") or 0), username=str(record.get("username") or ""),
        display_name=str(record.get("display_name") or ""), is_admin=bool(record.get("is_admin")),
        permissions=sorted(str(x) for x in record.get("permissions") or []),
        seller_ids=None if sellers is None else sorted(int(x) for x in sellers),
        expires_at=int(record.get("expires_at") or 0), tenant_ids=sorted(int(x) for x in record.get("tenant_ids") or []),
        active_tenant_id=int(record.get("active_tenant_id") or 0), active_tenant_name=str(record.get("active_tenant_name") or ""),
        active_tenant_type=str(record.get("active_tenant_type") or ""), tenant_role=str(record.get("tenant_role") or ""),
    )


@router.get("/plans")
def signup_plans() -> dict:
    return {"plan_codes": sorted(public_plan_codes())}


@router.post("/signup", response_model=OnboardingSignupResponse, status_code=201)
def signup(payload: OnboardingSignupRequest, response: Response) -> OnboardingSignupResponse:
    try:
        created = register_merchant(**payload.model_dump(exclude={"remember"}))
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = issue_session(int(created["user_id"]), remember=payload.remember)
    current = session_user(session.token) or {}
    secure = str(os.getenv("MARKETPLACE_HUB_COOKIE_SECURE", "1")).strip().lower() not in {"0","false","no","off"}
    response.set_cookie(COOKIE_NAME, session.token, max_age=max(1, session.expires_at-int(time.time())), httponly=True, secure=secure, samesite="lax", path="/")
    tenant = created["tenant"]; seller = created["seller"]; billing = created["billing"]
    return OnboardingSignupResponse(
        token=session.token, expires_at=session.expires_at, user=_user_response(current),
        tenant=TenantResponse(id=int(tenant["id"]), name=str(tenant.get("name") or ""), slug=str(tenant.get("slug") or ""), tenant_type=str(tenant.get("tenant_type") or "merchant"), status=str(tenant.get("status") or "active"), plan_code=str(tenant.get("plan_code") or ""), access_mode="direct", role="owner", active=True),
        seller=SellerResponse(id=int(seller["id"]), name=str(seller.get("name") or ""), legal_name=str(seller.get("legal_name") or ""), active=bool(int(seller.get("active") or 0))),
        billing=BillingSnapshotResponse(**{k: billing.get(k) for k in BillingSnapshotResponse.model_fields}),
    )


@router.get("/status", response_model=OnboardingStatusResponse)
def status_view(user: CurrentUser) -> OnboardingStatusResponse:
    return OnboardingStatusResponse(**onboarding_status(user.active_tenant_id))


@router.post("/marketplaces", response_model=MarketplaceConnectResponse)
def add_marketplace(payload: MarketplaceConnectRequest, user: ApiUser = Depends(require_permission("seller_management"))) -> MarketplaceConnectResponse:
    seller_id = ensure_seller_access(user, payload.seller_id)
    try:
        item = connect_marketplace(tenant_id=user.active_tenant_id, seller_id=seller_id, marketplace=payload.marketplace, account_name=payload.account_name, credentials=payload.credentials, validate=payload.verify_credentials)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MarketplaceConnectResponse(**item)
