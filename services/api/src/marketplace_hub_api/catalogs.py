from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from marketplace_hub_core.catalogs.parsing import (
    CatalogFileLimitError,
    CatalogFileValidationError,
)
from marketplace_hub_core.catalogs.repository import (
    CatalogConfirmationError,
    CatalogPriceListNotFoundError,
    CatalogSupplierNotFoundError,
)
from marketplace_hub_core.catalogs.service import CatalogsService, CatalogValidationError
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError, WorkspacePermissionError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from marketplace_hub_api.catalog_upload import (
    CatalogMultipartError,
    CatalogMultipartLimitError,
    read_catalog_multipart,
)
from marketplace_hub_api.seller_settings import SafeSettingsRoute


class SupplierCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str
    notes: str = Field("", max_length=5_000)


class DeleteConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    confirmation: str


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
                CatalogPriceListNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from None
        except WorkspacePermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except (CatalogValidationError, CatalogConfirmationError,
                CatalogFileValidationError) as exc:
            raise HTTPException(422, str(exc)) from None
        except CatalogFileLimitError as exc:
            raise HTTPException(413, str(exc)) from None
        except IntegrityError:
            raise HTTPException(
                409, "Un fornitore o listino con questo nome è già presente."
            ) from None
        except SQLAlchemyError:
            raise HTTPException(503, "Cataloghi temporaneamente non disponibili.") from None

    @router.get("")
    def dashboard(seller_id: UUID, request: Request):
        return execute(lambda: service.read(principal(request), seller_id))

    @router.post("/suppliers", status_code=201)
    def add_supplier(seller_id: UUID, payload: SupplierCreate, request: Request):
        session = principal(request)
        return execute(lambda: service.add_supplier(
            session, seller_id, name=payload.name, notes=payload.notes,
        ))

    @router.delete("/suppliers/{supplier_id}")
    def delete_supplier(
        seller_id: UUID,
        supplier_id: UUID,
        payload: DeleteConfirmation,
        request: Request,
    ):
        session = principal(request)
        return execute(lambda: service.delete_supplier(
            session, seller_id, supplier_id, payload.confirmation,
        ))

    @router.post("/price-lists", status_code=201)
    async def add_price_list(seller_id: UUID, request: Request):
        session = await run_in_threadpool(principal, request)
        # Authenticate and authorize before Starlette parses or spools the upload.
        await run_in_threadpool(execute, lambda: service.authorize_upload(session, seller_id))
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
            ),
        )

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
    def delete_price_list(
        seller_id: UUID,
        price_list_id: UUID,
        payload: DeleteConfirmation,
        request: Request,
    ):
        session = principal(request)
        return execute(lambda: service.delete_price_list(
            session, seller_id, price_list_id, payload.confirmation,
        ))

    return router
