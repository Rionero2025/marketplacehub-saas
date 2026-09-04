from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, AsyncIterator, Callable

from fastapi import Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from api.session_store import session_user

COOKIE_NAME = "mh_session"


@dataclass(frozen=True, slots=True)
class ApiUser:
    id: int
    username: str
    display_name: str
    is_admin: bool
    permissions: frozenset[str]
    seller_ids: frozenset[int] | None
    expires_at: int
    tenant_ids: frozenset[int] = frozenset()
    active_tenant_id: int = 0
    active_tenant_name: str = ""
    active_tenant_type: str = ""
    tenant_role: str = ""
    legacy_seller_ids: frozenset[int] | None = None

    @classmethod
    def from_record(cls, record: dict) -> "ApiUser":
        sellers = record.get("seller_ids")
        seller_scope = None if sellers is None else frozenset(int(v) for v in sellers)
        return cls(
            id=int(record.get("id") or 0),
            username=str(record.get("username") or ""),
            display_name=str(record.get("display_name") or ""),
            is_admin=bool(record.get("is_admin")),
            permissions=frozenset(str(v) for v in record.get("permissions") or ()),
            seller_ids=seller_scope,
            expires_at=int(record.get("expires_at") or 0),
            tenant_ids=frozenset(int(v) for v in record.get("tenant_ids") or ()),
            active_tenant_id=int(record.get("active_tenant_id") or 0),
            active_tenant_name=str(record.get("active_tenant_name") or ""),
            active_tenant_type=str(record.get("active_tenant_type") or ""),
            tenant_role=str(record.get("tenant_role") or ""),
            legacy_seller_ids=None if record.get("legacy_seller_ids") is None else frozenset(int(v) for v in record["legacy_seller_ids"]),
        )

    def can(self, permission: str) -> bool:
        return self.is_admin or str(permission) in self.permissions

    def can_write(self) -> bool:
        return self.is_admin or self.tenant_role in {"owner", "admin", "manager", "operator"}

    def can_access_tenant(self, tenant_id: int) -> bool:
        return int(tenant_id) in self.tenant_ids

    def can_access_seller(self, seller_id: int) -> bool:
        # v315: even Platform Admin is restricted to the active tenant boundary.
        # To operate on another tenant it must explicitly switch tenant context.
        if self.active_tenant_id <= 0:
            return False
        if self.seller_ids is None:
            return False
        return int(seller_id) in self.seller_ids


def request_token(request: Request) -> str:
    auth = str(request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(request.cookies.get(COOKIE_NAME) or "").strip()


async def get_current_user(request: Request) -> AsyncIterator[ApiUser]:
    token = request_token(request)
    record = await run_in_threadpool(session_user, token)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessione non valida o scaduta.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = ApiUser.from_record(record)
    from services.tenant_db import tenant_database_scope
    # Set ContextVars in the request task, so FastAPI copies them into sync
    # dependencies/endpoints. A sync generator sets them only in a worker-thread
    # copy, losing the tenant and resetting tokens in a different Context.
    with tenant_database_scope(user.active_tenant_id):
        yield user


CurrentUser = Annotated[ApiUser, Depends(get_current_user)]


async def get_target_tenant_user(tenant_id: int, user: CurrentUser) -> AsyncIterator[ApiUser]:
    """Authorize an explicit tenant before binding its database transaction scope."""
    from services.tenant_db import tenant_database_scope
    ensure_tenant_access(user, tenant_id)
    with tenant_database_scope(tenant_id):
        yield user


TargetTenantUser = Annotated[ApiUser, Depends(get_target_tenant_user)]


def require_permission(permission: str) -> Callable[..., ApiUser]:
    def dependency(request: Request, user: CurrentUser) -> ApiUser:
        if not user.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Area non autorizzata: {permission}",
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not user.can_write():
            raise HTTPException(status_code=403, detail="Il ruolo nel workspace consente solo la lettura.")
        # v318: UI permissions and SaaS-plan entitlements are separate layers.
        # Platform Admin can support any active tenant, but ordinary tenant users
        # cannot call an API area disabled by their subscription.
        if not user.is_admin:
            from services.entitlements import feature_enabled, tenant_entitlements
            if not feature_enabled(user.active_tenant_id, permission):
                ent = tenant_entitlements(user.active_tenant_id)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "PLAN_ENTITLEMENT_REQUIRED",
                        "feature": str(permission),
                        "plan_code": str(ent.get("plan_code") or ""),
                    },
                )
        return user
    return dependency


def ensure_tenant_access(user: ApiUser, tenant_id: int) -> int:
    tenant_id = int(tenant_id)
    if tenant_id <= 0 or not user.can_access_tenant(tenant_id):
        raise HTTPException(status_code=404, detail="Tenant non disponibile.")
    return tenant_id


def ensure_seller_access(user: ApiUser, seller_id: int) -> int:
    seller_id = int(seller_id)
    if seller_id <= 0 or not user.can_access_seller(seller_id):
        # 404 prevents leaking existence of sellers outside the active tenant.
        raise HTTPException(status_code=404, detail="Seller non disponibile.")
    return seller_id
