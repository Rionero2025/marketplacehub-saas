from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request, Response, status

from api.dependencies import COOKIE_NAME, CurrentUser, request_token
from api.schemas import LoginRequest, LoginResponse, UserResponse
from api.session_store import issue_session, revoke_session
from services.user_access import authenticate_user
from services.tenancy import accessible_tenants_for_user

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_response(user) -> UserResponse:
    sellers = None if user.seller_ids is None else sorted(user.seller_ids)
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
        permissions=sorted(user.permissions),
        seller_ids=sellers,
        expires_at=user.expires_at,
        tenant_ids=sorted(user.tenant_ids),
        active_tenant_id=user.active_tenant_id,
        active_tenant_name=user.active_tenant_name,
        active_tenant_type=user.active_tenant_type,
        tenant_role=user.tenant_role,
    )


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response) -> LoginResponse:
    record = authenticate_user(payload.username, payload.password)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide o utente disattivato.",
        )
    # The selected portal never grants a role. Check eligibility before issuing
    # a session/cookie, and choose an already-authorized workspace of that type.
    tenant_id = None
    is_admin = bool(int(record.get("is_admin") or 0))
    if payload.area == "admin" and not is_admin:
        raise HTTPException(status_code=403, detail="Accesso riservato agli amministratori della piattaforma.")
    if payload.area in {"seller", "agency"}:
        tenant_type = "agency" if payload.area == "agency" else "merchant"
        eligible = [item for item in accessible_tenants_for_user(int(record["id"]), global_admin=is_admin)
                    if item.get("tenant_type") == tenant_type and item.get("status") == "active"]
        eligible.sort(key=lambda item: (item.get("access_mode") != "direct", str(item.get("name") or "").lower(), int(item["id"])))
        if not eligible:
            label = "Agenzia" if payload.area == "agency" else "Seller"
            raise HTTPException(status_code=403, detail=f"Questo account non ha un workspace {label} accessibile. Scegli l’altro accesso o contatta il gestore.")
        tenant_id = int(eligible[0]["id"])
    session = issue_session(int(record["id"]), remember=payload.remember, **({"tenant_id": tenant_id} if tenant_id is not None else {}))
    secure = str(os.getenv("MARKETPLACE_HUB_COOKIE_SECURE", "1")).strip().lower() not in {
        "0", "false", "no", "off"
    }
    max_age = max(1, session.expires_at - __import__("time").time().__int__())
    response.set_cookie(
        key=COOKIE_NAME,
        value=session.token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    from api.dependencies import ApiUser
    from api.session_store import session_user
    current = session_user(session.token) or {}
    user = ApiUser.from_record(current)
    return LoginResponse(
        token=session.token,
        expires_at=session.expires_at,
        user=_user_response(user),
    )


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    token = request_token(request)
    if token:
        revoke_session(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=UserResponse)
def me(user: CurrentUser) -> UserResponse:
    return _user_response(user)
