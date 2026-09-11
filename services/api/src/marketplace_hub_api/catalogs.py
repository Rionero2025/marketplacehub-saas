from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from marketplace_hub_core.catalogs.parsing import (
    CatalogFileLimitError,
    CatalogFileValidationError,
)
from marketplace_hub_core.catalogs.repository import (
    CatalogConfirmationError,
    CatalogPriceListNotFoundError,
    CatalogRefreshInProgressError,
    CatalogRefreshJobNotFoundError,
    CatalogSourceRevisionMismatchError,
    CatalogSupplierNotFoundError,
)
from marketplace_hub_core.catalogs.service import (
    CatalogQueueUnavailableError,
    CatalogsService,
    CatalogValidationError,
)
from marketplace_hub_core.seller_settings.security import CredentialStorageUnavailableError
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError, WorkspacePermissionError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from python_multipart.multipart import parse_options_header
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from marketplace_hub_api.catalog_upload import (
    CatalogMultipartError,
    CatalogMultipartLimitError,
    read_catalog_multipart,
)
from marketplace_hub_api.seller_settings import SafeSettingsRoute

MAX_CATALOG_JSON_BYTES = 16 * 1024
CATALOG_JSON_BODY_TIMEOUT_SECONDS = 30


class CatalogJsonError(ValueError):
    pass


class CatalogJsonLimitError(CatalogJsonError):
    pass


class CatalogJsonTimeoutError(CatalogJsonError):
    pass


def _catalog_json_content_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal():
        raise CatalogJsonError("Content-Length non valido.")
    if len(value) > 20:
        raise CatalogJsonLimitError("Il corpo JSON supera il limite consentito.")
    length = int(value)
    if length > MAX_CATALOG_JSON_BYTES:
        raise CatalogJsonLimitError("Il corpo JSON supera il limite consentito.")
    return length


async def read_catalog_json(request: Request) -> bytes:
    """Read one small JSON body only after authentication and authorization."""
    content_type = request.headers.get("content-type")
    if content_type is None or len(content_type) > 1_024:
        raise CatalogJsonError("Content-Type JSON non valido.")
    try:
        media_type, _ = parse_options_header(content_type)
    except (ValueError, IndexError, UnicodeError) as exc:
        raise CatalogJsonError("Content-Type JSON non valido.") from exc
    if media_type.lower() != b"application/json":
        raise CatalogJsonError("È richiesto application/json.")

    declared_length = _catalog_json_content_length(request)
    content = bytearray()
    try:
        async with asyncio.timeout(CATALOG_JSON_BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_CATALOG_JSON_BYTES:
                    raise CatalogJsonLimitError(
                        "Il corpo JSON supera il limite consentito."
                    )
                content.extend(chunk)
    except TimeoutError as exc:
        raise CatalogJsonTimeoutError(
            "Tempo massimo di caricamento superato. Riprova."
        ) from exc
    except CatalogJsonError:
        raise
    except ClientDisconnect as exc:
        raise CatalogJsonError("Il corpo JSON è incompleto.") from exc
    if declared_length is not None and len(content) != declared_length:
        raise CatalogJsonError("Il corpo JSON è incompleto.")
    if not content:
        raise CatalogJsonError("Il corpo JSON è vuoto.")
    return bytes(content)


class SupplierCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str
    notes: str = Field("", max_length=5_000)


class DeleteConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    confirmation: str


class PriceListUrlCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    supplier_id: UUID
    name: str
    provider: Literal["generic", "innpro"] = "generic"
    feed_role: Literal["standard", "full", "light"] = "standard"
    url: SecretStr
    username: SecretStr = SecretStr("")
    password: SecretStr = SecretStr("")


class PriceListUrlUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    url: SecretStr
    credentials_mode: Literal["keep", "replace", "remove"]
    expected_config_revision: int = Field(ge=1, le=2_147_483_646)
    username: SecretStr = SecretStr("")
    password: SecretStr = SecretStr("")


async def parse_catalog_json[CatalogJsonPayload: BaseModel](
    request: Request, payload_type: type[CatalogJsonPayload],
) -> CatalogJsonPayload:
    try:
        content = await read_catalog_json(request)
    except CatalogJsonLimitError as exc:
        raise HTTPException(413, str(exc)) from None
    except CatalogJsonTimeoutError as exc:
        raise HTTPException(408, str(exc)) from None
    except CatalogJsonError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        return payload_type.model_validate_json(content)
    except ValidationError:
        raise HTTPException(
            422, "Dati non validi. Controlla i campi inseriti."
        ) from None


def create_catalogs_router(service: CatalogsService, auth, settings):
    router = APIRouter(
        prefix="/v1/sellers/{seller_id}/catalogs",
        tags=["catalogs"],
        route_class=SafeSettingsRoute,
    )

    def principal(request: Request):
        session = auth.authenticate(request.cookies.get(settings.session_cookie_name))
        if session is None:
            raise HTTPException(401, "Sessione non valida o scaduta.")
        return session

    def execute(operation: Callable):
        try:
            return operation()
        except (SellerNotAccessibleError, CatalogSupplierNotFoundError,
                CatalogPriceListNotFoundError, CatalogRefreshJobNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from None
        except WorkspacePermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except (CatalogValidationError, CatalogConfirmationError,
                CatalogFileValidationError) as exc:
            raise HTTPException(422, str(exc)) from None
        except CatalogFileLimitError as exc:
            raise HTTPException(413, str(exc)) from None
        except (CatalogRefreshInProgressError,
                CatalogSourceRevisionMismatchError) as exc:
            raise HTTPException(409, str(exc)) from None
        except (CatalogQueueUnavailableError, CredentialStorageUnavailableError) as exc:
            raise HTTPException(503, str(exc)) from None
        except IntegrityError:
            raise HTTPException(
                409, "Un fornitore o listino con questo nome è già presente."
            ) from None
        except SQLAlchemyError:
            raise HTTPException(503, "Cataloghi temporaneamente non disponibili.") from None

    async def catalog_writer(seller_id: UUID, request: Request):
        session = await run_in_threadpool(principal, request)
        await run_in_threadpool(execute, lambda: service.authorize_upload(session, seller_id))
        return session

    @router.get("")
    def dashboard(seller_id: UUID, request: Request):
        return execute(lambda: service.read(principal(request), seller_id))

    @router.post("/suppliers", status_code=201)
    async def add_supplier(seller_id: UUID, request: Request):
        session = await catalog_writer(seller_id, request)
        payload = await parse_catalog_json(request, SupplierCreate)
        return await run_in_threadpool(
            execute,
            lambda: service.add_supplier(
                session, seller_id, name=payload.name, notes=payload.notes,
            ),
        )

    @router.delete("/suppliers/{supplier_id}")
    async def delete_supplier(
        seller_id: UUID,
        supplier_id: UUID,
        request: Request,
    ):
        session = await catalog_writer(seller_id, request)
        payload = await parse_catalog_json(request, DeleteConfirmation)
        return await run_in_threadpool(
            execute,
            lambda: service.delete_supplier(
                session, seller_id, supplier_id, payload.confirmation,
            ),
        )

    @router.post("/price-lists", status_code=201)
    async def add_price_list(seller_id: UUID, request: Request):
        session = await catalog_writer(seller_id, request)
        # Authenticate and authorize before Starlette parses or spools the upload.
        try:
            upload = await read_catalog_multipart(request)
        except CatalogMultipartLimitError as exc:
            raise HTTPException(413, str(exc)) from None
        except CatalogMultipartError as exc:
            raise HTTPException(422, str(exc)) from None
        await run_in_threadpool(
            execute,
            lambda: service.authorize_upload_supplier(
                session, seller_id, upload.supplier_id,
            ),
        )
        return await run_in_threadpool(
            execute,
            lambda: service.add_price_list(
                session,
                seller_id,
                supplier_id=upload.supplier_id,
                name=upload.name,
                file_name=upload.file_name,
                media_type=upload.media_type,
                content=upload.content,
                provider=upload.provider,
                feed_role=upload.feed_role,
            ),
        )

    @router.post("/price-lists/url", status_code=202)
    async def add_url_price_list(seller_id: UUID, request: Request):
        session = await catalog_writer(seller_id, request)
        payload = await parse_catalog_json(request, PriceListUrlCreate)
        return await run_in_threadpool(
            execute,
            lambda: service.add_url_price_list(
                session,
                seller_id,
                supplier_id=payload.supplier_id,
                name=payload.name,
                url=payload.url.get_secret_value(),
                username=payload.username.get_secret_value(),
                password=payload.password.get_secret_value(),
                provider=payload.provider,
                feed_role=payload.feed_role,
            ),
        )

    @router.post("/price-lists/{price_list_id}/url")
    async def update_url_price_list(
        seller_id: UUID,
        price_list_id: UUID,
        request: Request,
    ):
        session = await catalog_writer(seller_id, request)
        payload = await parse_catalog_json(request, PriceListUrlUpdate)
        return await run_in_threadpool(
            execute,
            lambda: service.update_url_price_list(
                session,
                seller_id,
                price_list_id,
                url=payload.url.get_secret_value(),
                credentials_mode=payload.credentials_mode,
                expected_config_revision=payload.expected_config_revision,
                username=payload.username.get_secret_value(),
                password=payload.password.get_secret_value(),
            ),
        )

    @router.post("/price-lists/{price_list_id}/refresh", status_code=202)
    def refresh_price_list(
        seller_id: UUID, price_list_id: UUID, request: Request,
    ):
        session = principal(request)
        return execute(lambda: service.refresh_price_list(
            session, seller_id, price_list_id,
        ))

    @router.get("/price-lists/{price_list_id}/jobs/{job_id}")
    def read_refresh_job(
        seller_id: UUID,
        price_list_id: UUID,
        job_id: UUID,
        request: Request,
    ):
        session = principal(request)
        return execute(lambda: service.read_refresh_job(
            session, seller_id, price_list_id, job_id,
        ))

    @router.get("/price-lists/{price_list_id}")
    def price_list_detail(
        seller_id: UUID,
        price_list_id: UUID,
        request: Request,
        limit: int = Query(100, ge=1, le=200),
    ):
        session = principal(request)
        return execute(lambda: service.detail(
            session, seller_id, price_list_id, limit=limit,
        ))

    @router.delete("/price-lists/{price_list_id}")
    async def delete_price_list(
        seller_id: UUID,
        price_list_id: UUID,
        request: Request,
    ):
        session = await catalog_writer(seller_id, request)
        payload = await parse_catalog_json(request, DeleteConfirmation)
        return await run_in_threadpool(
            execute,
            lambda: service.delete_price_list(
                session, seller_id, price_list_id, payload.confirmation,
            ),
        )

    return router
