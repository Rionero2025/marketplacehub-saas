from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request, status

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
        )

    def can(self, permission: str) -> bool:
        return self.is_admin or str(permission) in self.permissions

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


def get_current_user(request: Request) -> ApiUser:
    token = request_token(request)
    record = session_user(token)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessione non valida o scaduta.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return ApiUser.from_record(record)


CurrentUser = Annotated[ApiUser, Depends(get_current_user)]


def require_permission(permission: str) -> Callable[[ApiUser], ApiUser]:
    def dependency(user: CurrentUser) -> ApiUser:
        if not user.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Area non autorizzata: {permission}",
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
