from collections.abc import Callable
from inspect import isawaitable
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from marketplace_hub_core.auth import AuthService
from marketplace_hub_core.marketplace_connections.connectors import ConnectionProbeError
from marketplace_hub_core.marketplace_connections.service import MarketplaceConnectionsService
from marketplace_hub_core.seller_settings.repository import MarketplaceAccountNotFoundError
from marketplace_hub_core.seller_settings.security import CredentialStorageUnavailableError
from marketplace_hub_core.seller_settings.service import SellerSettingsValidationError
from marketplace_hub_core.settings import Settings
from marketplace_hub_core.tenancy.service import (
    SellerNotAccessibleError,
    WorkspacePermissionError,
)
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from marketplace_hub_api.seller_settings import SafeSettingsRoute


class MarketplaceCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    client_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    api_key: SecretStr | None = None
    shop_id: SecretStr | None = None
    api_url: SecretStr | None = None


class MarketplaceConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    marketplace: Literal["kaufland", "worten"]
    account_name: str
    credentials: MarketplaceCredentials


class MarketplaceConnectionDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    confirmation: str


def create_marketplace_connections_router(
    service: MarketplaceConnectionsService, auth: AuthService, settings: Settings,
):
    router = APIRouter(
        prefix="/v1/sellers", tags=["marketplace-connections"], route_class=SafeSettingsRoute,
    )

    def principal(request: Request):
        session = auth.authenticate(request.cookies.get(settings.session_cookie_name))
        if session is None:
            raise HTTPException(401, "Sessione non valida o scaduta.")
        return session

    async def execute(operation: Callable):
        try:
            result = operation()
            return await result if isawaitable(result) else result
        except (SellerNotAccessibleError, MarketplaceAccountNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from None
        except WorkspacePermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except SellerSettingsValidationError as exc:
            raise HTTPException(422, str(exc)) from None
        except ConnectionProbeError as exc:
            raise HTTPException(
                exc.status_code, {"code": exc.code, "message": exc.message},
            ) from None
        except CredentialStorageUnavailableError:
            raise HTTPException(503, {
                "code": "credentials_unavailable",
                "message": "Salvataggio sicuro delle credenziali temporaneamente non disponibile.",
            }) from None
        except IntegrityError:
            raise HTTPException(409, "Un account con questo nome è già presente.") from None
        except SQLAlchemyError:
            raise HTTPException(503, "Collegamenti temporaneamente non disponibili.") from None

    path = "/{seller_id}/marketplace-connections"

    @router.get(path)
    async def read_connections(seller_id: UUID, request: Request):
        session = principal(request)
        return await execute(lambda: service.read(session, seller_id))

    @router.post(path, status_code=201)
    async def connect_marketplace(
        seller_id: UUID, payload: MarketplaceConnectionCreate, request: Request,
    ):
        session = principal(request)
        credentials = {
            key: value.get_secret_value() for key, value in payload.credentials.model_dump().items()
            if value is not None
        }
        return await execute(lambda: service.connect(
            session, seller_id, payload.marketplace, payload.account_name, credentials,
        ))

    @router.post(path + "/{account_id}/verify")
    async def verify_connection(seller_id: UUID, account_id: UUID, request: Request):
        session = principal(request)
        return await execute(lambda: service.verify(session, seller_id, account_id))

    @router.delete(path + "/{account_id}")
    async def delete_connection(
        seller_id: UUID, account_id: UUID, payload: MarketplaceConnectionDelete, request: Request,
    ):
        session = principal(request)
        return await execute(lambda: service.delete(
            session, seller_id, account_id, payload.confirmation,
        ))

    return router
