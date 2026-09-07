from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from marketplace_hub_core.auth import AuthService
from marketplace_hub_core.seller_settings.repository import MarketplaceAccountNotFoundError
from marketplace_hub_core.seller_settings.security import CredentialStorageUnavailableError
from marketplace_hub_core.seller_settings.service import (
    SellerSettingsService,
    SellerSettingsValidationError,
)
from marketplace_hub_core.settings import Settings
from marketplace_hub_core.tenancy.service import (
    SellerNotAccessibleError,
    WorkspacePermissionError,
)
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.exc import IntegrityError, SQLAlchemyError


class SafeSettingsRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                response = await original(request)
            except RequestValidationError:
                # The default FastAPI validation response includes raw input. This
                # route accepts credentials, so even malformed JSON must be redacted.
                raise HTTPException(
                    422, "Dati non validi. Controlla i campi inseriti.",
                    headers={"cache-control": "no-store"},
                ) from None
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "cache-control": "no-store"}
                raise
            response.headers["cache-control"] = "no-store"
            return response

        return safe_handler


class SellerSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str
    legal_name: str
    email: str


class KauflandAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    account_name: str
    client_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")


class KauflandAccountDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    confirmation: str


def create_seller_settings_router(
    service: SellerSettingsService, auth: AuthService, settings: Settings,
):
    router = APIRouter(
        prefix="/v1/sellers", tags=["seller-settings"], route_class=SafeSettingsRoute,
    )

    def principal(request: Request):
        session = auth.authenticate(request.cookies.get(settings.session_cookie_name))
        if session is None:
            raise HTTPException(401, "Sessione non valida o scaduta.")
        return session

    def execute(operation: Callable):
        try:
            return operation()
        except (SellerNotAccessibleError, MarketplaceAccountNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from None
        except WorkspacePermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except SellerSettingsValidationError as exc:
            raise HTTPException(422, str(exc)) from None
        except CredentialStorageUnavailableError as exc:
            raise HTTPException(503, str(exc)) from None
        except IntegrityError:
            raise HTTPException(409, "Configurazione già presente o non disponibile.") from None
        except SQLAlchemyError:
            raise HTTPException(503, "Impostazioni temporaneamente non disponibili.") from None

    @router.get("/{seller_id}/settings")
    def read_settings(seller_id: UUID, request: Request):
        session = principal(request)
        return execute(lambda: service.read(session, seller_id))

    @router.put("/{seller_id}/settings")
    def update_settings(seller_id: UUID, payload: SellerSettingsUpdate, request: Request):
        session = principal(request)
        return execute(lambda: service.update(session, seller_id, payload.model_dump()))

    @router.post("/{seller_id}/kaufland-accounts", status_code=201)
    def add_account(seller_id: UUID, payload: KauflandAccountCreate, request: Request):
        session = principal(request)
        return execute(lambda: service.add_account(
            session, seller_id, payload.account_name, payload.client_key, payload.secret_key,
        ))

    @router.delete("/{seller_id}/kaufland-accounts/{account_id}")
    def delete_account(
        seller_id: UUID, account_id: UUID, payload: KauflandAccountDelete, request: Request,
    ):
        session = principal(request)
        return execute(lambda: service.delete_account(
            session, seller_id, account_id, payload.confirmation,
        ))

    return router
