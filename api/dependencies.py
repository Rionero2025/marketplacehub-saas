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
        )

    def can(self, permission: str) -> bool:
        return self.is_admin or str(permission) in self.permissions

    def can_access_seller(self, seller_id: int) -> bool:
        return self.is_admin or self.seller_ids is None or int(seller_id) in self.seller_ids


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


def ensure_seller_access(user: ApiUser, seller_id: int) -> int:
    seller_id = int(seller_id)
    if seller_id <= 0 or not user.can_access_seller(seller_id):
        # 404 prevents leaking existence of sellers outside the user's scope.
        raise HTTPException(status_code=404, detail="Seller non disponibile.")
    return seller_id
