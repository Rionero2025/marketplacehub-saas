from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from api.dependencies import ApiUser, CurrentUser, ensure_tenant_access
from api.schemas import (
    BillingActivateRequest,
    BillingCancelRequest,
    BillingPaymentFailedRequest,
    BillingPaymentSuccessRequest,
    BillingPlanChangeRequest,
    BillingResumeRequest,
    BillingSnapshotResponse,
    BillingSuspendRequest,
    BillingTrialRequest,
)
from services.billing import (
    activate_subscription,
    billing_events,
    billing_snapshot,
    cancel_subscription,
    record_payment_failed,
    record_payment_success,
    refresh_subscription_state,
    resume_subscription,
    schedule_plan_change,
    start_trial,
    suspend_subscription,
)

router = APIRouter(tags=["billing"])


def _platform_admin(user: ApiUser) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operazione riservata al Platform Admin.")


def _response(item: dict) -> BillingSnapshotResponse:
    if not item:
        raise HTTPException(status_code=404, detail="Abbonamento non disponibile.")
    return BillingSnapshotResponse(**{
        key: item.get(key)
        for key in BillingSnapshotResponse.model_fields
        if key in item
    })


@router.get("/tenants/{tenant_id}/billing", response_model=BillingSnapshotResponse)
def get_billing(tenant_id: int, user: CurrentUser) -> BillingSnapshotResponse:
    ensure_tenant_access(user, tenant_id)
    return _response(billing_snapshot(int(tenant_id)))


@router.get("/tenants/{tenant_id}/billing/events")
def get_billing_events(tenant_id: int, user: CurrentUser, limit: int = Query(default=100, ge=1, le=500)):
    ensure_tenant_access(user, tenant_id)
    return billing_events(int(tenant_id), limit=limit)


@router.post("/tenants/{tenant_id}/billing/trial", response_model=BillingSnapshotResponse)
def trial(tenant_id: int, payload: BillingTrialRequest, user: CurrentUser) -> BillingSnapshotResponse:
    _platform_admin(user); ensure_tenant_access(user, tenant_id)
    try:
        return _response(start_trial(int(tenant_id), payload.plan_code, days=payload.days))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/billing/activate", response_model=BillingSnapshotResponse)
def activate(tenant_id: int, payload: BillingActivateRequest, user: CurrentUser) -> BillingSnapshotResponse:
    _platform_admin(user); ensure_tenant_access(user, tenant_id)
    try:
        return _response(activate_subscription(
            int(tenant_id), payload.plan_code, billing_interval=payload.billing_interval,
            provider=payload.provider, period_start=payload.period_start, period_end=payload.period_end,
            external_customer_id=payload.external_customer_id,
            external_subscription_id=payload.external_subscription_id, reference=payload.reference,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/billing/payment-success", response_model=BillingSnapshotResponse)
def payment_success(tenant_id: int, payload: BillingPaymentSuccessRequest, user: CurrentUser) -> BillingSnapshotResponse:
    _platform_admin(user); ensure_tenant_access(user, tenant_id)
    try:
        return _response(record_payment_success(
            int(tenant_id), amount_cents=payload.amount_cents, currency=payload.currency,
            reference=payload.reference, external_event_id=payload.external_event_id or None,
            paid_at=payload.paid_at, period_end=payload.period_end,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/billing/payment-failed", response_model=BillingSnapshotResponse)
def payment_failed(tenant_id: int, payload: BillingPaymentFailedRequest, user: CurrentUser) -> BillingSnapshotResponse:
    _platform_admin(user); ensure_tenant_access(user, tenant_id)
    try:
        return _response(record_payment_failed(
            int(tenant_id), grace_days=payload.grace_days, reference=payload.reference,
            external_event_id=payload.external_event_id or None, failed_at=payload.failed_at,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/billing/suspend", response_model=BillingSnapshotResponse)
def suspend(tenant_id: int, payload: BillingSuspendRequest, user: CurrentUser) -> BillingSnapshotResponse:
    _platform_admin(user); ensure_tenant_access(user, tenant_id)
    return _response(suspend_subscription(int(tenant_id), reason=payload.reason))


@router.post("/tenants/{tenant_id}/billing/resume", response_model=BillingSnapshotResponse)
def resume(tenant_id: int, payload: BillingResumeRequest, user: CurrentUser) -> BillingSnapshotResponse:
    _platform_admin(user); ensure_tenant_access(user, tenant_id)
    try:
        return _response(resume_subscription(int(tenant_id), reason=payload.reason))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/billing/cancel", response_model=BillingSnapshotResponse)
def cancel(tenant_id: int, payload: BillingCancelRequest, user: CurrentUser) -> BillingSnapshotResponse:
    _platform_admin(user); ensure_tenant_access(user, tenant_id)
    try:
        return _response(cancel_subscription(int(tenant_id), at_period_end=payload.at_period_end, reason=payload.reason))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/billing/plan-change", response_model=BillingSnapshotResponse)
def plan_change(tenant_id: int, payload: BillingPlanChangeRequest, user: CurrentUser) -> BillingSnapshotResponse:
    _platform_admin(user); ensure_tenant_access(user, tenant_id)
    try:
        return _response(schedule_plan_change(
            int(tenant_id), payload.plan_code, immediate=payload.immediate, effective_at=payload.effective_at,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/billing/refresh", response_model=BillingSnapshotResponse)
def refresh(tenant_id: int, user: CurrentUser) -> BillingSnapshotResponse:
    _platform_admin(user); ensure_tenant_access(user, tenant_id)
    return _response(refresh_subscription_state(int(tenant_id)))
