from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from marketplace_hub_core.auth import AuthService
from marketplace_hub_core.settings import Settings
from marketplace_hub_core.tenancy.service import (
    SellerNotAccessibleError,
    WorkspacePermissionError,
    WorkspaceService,
)
from pydantic import BaseModel


class SellerSelection(BaseModel):
    seller_id: UUID


def create_workspace_router(service: WorkspaceService, auth: AuthService, settings: Settings):
    router = APIRouter(prefix="/v1", tags=["workspace"])

    def principal(request: Request, response: Response):
        session = auth.authenticate(request.cookies.get(settings.session_cookie_name))
        if session is None:
            raise HTTPException(401, "Sessione non valida o scaduta.")
        response.headers["cache-control"] = "no-store"
        return session

    @router.get("/workspace")
    def workspace(request: Request, response: Response):
        return service.overview(principal(request, response))

    @router.post("/workspace/select")
    def select_seller(payload: SellerSelection, request: Request, response: Response):
        session = principal(request, response)
        try:
            return service.select_seller(session, payload.seller_id)
        except SellerNotAccessibleError as exc:
            raise HTTPException(404, str(exc)) from exc
        except WorkspacePermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    @router.get("/sellers/{seller_id}")
    def seller_profile(seller_id: UUID, request: Request, response: Response):
        session = principal(request, response)
        try:
            return service.require_seller(session, seller_id)
        except SellerNotAccessibleError as exc:
            raise HTTPException(404, str(exc)) from exc
        except WorkspacePermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    return router
