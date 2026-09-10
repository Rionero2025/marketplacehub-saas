from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response, status
from marketplace_hub_core.auth import AuthRealm, AuthService
from marketplace_hub_core.auth.service import InvalidCredentialsError, LoginRateLimitError
from marketplace_hub_core.settings import Settings
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)
    realm: AuthRealm


class SessionResponse(BaseModel):
    user_id: str
    login: str
    display_name: str
    realm: AuthRealm
    expires_at: str


def session_payload(principal) -> SessionResponse:
    return SessionResponse(
        user_id=str(principal.user_id),
        login=principal.login,
        display_name=principal.display_name,
        realm=principal.realm,
        expires_at=principal.expires_at.isoformat(),
    )


def _best_effort(operation: Callable[[], object]) -> None:
    try:
        operation()
    except Exception:
        # UI-state maintenance must never alter an authentication result.
        pass


def create_auth_router(
    service: AuthService,
    settings: Settings,
    *,
    after_login: Callable[[], object] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/auth", tags=["authentication"])
    cookie = settings.session_cookie_name

    @router.post("/login", response_model=SessionResponse)
    def login(
        payload: LoginRequest,
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
    ) -> SessionResponse:
        client_key = request.client.host if request.client else "unknown"
        try:
            issued = service.login(
                login=payload.login,
                password=payload.password,
                realm=payload.realm,
                client_key=client_key,
            )
        except InvalidCredentialsError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        except LoginRateLimitError as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

        if after_login is not None:
            background_tasks.add_task(_best_effort, after_login)

        response.set_cookie(
            key=cookie,
            value=issued.token,
            max_age=int(timedelta(hours=settings.session_ttl_hours).total_seconds()),
            expires=issued.principal.expires_at,
            path="/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )
        response.headers["cache-control"] = "no-store"
        return session_payload(issued.principal)

    @router.get("/session", response_model=SessionResponse)
    def current_session(request: Request, response: Response) -> SessionResponse:
        token = request.cookies.get(cookie)
        principal = service.authenticate(token)
        if principal is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sessione non valida o scaduta.")
        response.headers["cache-control"] = "no-store"
        return session_payload(principal)

    @router.post(
        "/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def logout(
        response: Response,
        request: Request,
    ) -> Response:
        token = request.cookies.get(cookie)
        service.logout(token)
        response.delete_cookie(
            cookie,
            path="/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )
        response.headers["cache-control"] = "no-store"
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    return router
