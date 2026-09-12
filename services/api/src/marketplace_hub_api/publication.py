from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from marketplace_hub_core.catalogs.repository import CatalogPriceListNotFoundError
from marketplace_hub_core.publication.connectors import RemoteFailure
from marketplace_hub_core.publication.models import Confirm, EditDraft, Rules
from marketplace_hub_core.publication.service import PublicationError
from marketplace_hub_core.seller_settings.repository import MarketplaceAccountNotFoundError
from marketplace_hub_core.seller_settings.security import CredentialStorageUnavailableError
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError, WorkspacePermissionError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from marketplace_hub_api.catalogs import parse_catalog_json
from marketplace_hub_api.seller_settings import SafeSettingsRoute


def create_publication_router(service, auth, settings):
    router = APIRouter(
        prefix="/v1/sellers/{seller_id}/publication",
        tags=["publication"],
        route_class=SafeSettingsRoute,
    )

    def principal(request):
        p = auth.authenticate(request.cookies.get(settings.session_cookie_name))
        if not p:
            raise HTTPException(401, "Sessione scaduta.")
        return p

    def execute(call):
        try:
            return call()
        except WorkspacePermissionError:
            raise HTTPException(
                403, "Non hai il permesso di pubblicare per questo negozio."
            ) from None
        except (
            SellerNotAccessibleError,
            MarketplaceAccountNotFoundError,
            CatalogPriceListNotFoundError,
        ):
            raise HTTPException(404, "Risorsa non disponibile.") from None
        except PublicationError as e:
            raise HTTPException(422, str(e)) from None
        except IntegrityError:
            raise HTTPException(
                409, "Un invio per questo account è già in corso. Consulta lo storico."
            ) from None
        except (RemoteFailure, CredentialStorageUnavailableError):
            raise HTTPException(
                502,
                "Impossibile leggere la configurazione dal marketplace. "
                "Verifica collegamento e ambiente selezionato.",
            ) from None
        except SQLAlchemyError:
            raise HTTPException(
                503, "Servizio pubblicazione momentaneamente non disponibile."
            ) from None

    @router.get("")
    async def index(seller_id: UUID, request: Request):
        p = principal(request)
        return await run_in_threadpool(execute, lambda: service.index(p, seller_id))

    @router.get("/options/{account_id}")
    async def options(
        seller_id: UUID,
        account_id: UUID,
        request: Request,
        storefront: Literal["de", "cz", "sk", "at", "pl", "fr", "it", "pt"] = "de",
        playground: bool = True,
    ):
        p = principal(request)
        return await run_in_threadpool(
            execute, lambda: service.options(p, seller_id, account_id, storefront, playground)
        )

    @router.post("/preview", status_code=201)
    async def preview(seller_id: UUID, request: Request):
        p = principal(request)
        await run_in_threadpool(execute, lambda: service.scope(p, seller_id, True))
        body = await parse_catalog_json(request, Rules)
        return await run_in_threadpool(execute, lambda: service.preview(p, seller_id, body))

    @router.get("/jobs/{job_id}")
    async def detail(seller_id: UUID, job_id: UUID, request: Request):
        p = principal(request)
        return await run_in_threadpool(execute, lambda: service.detail(p, seller_id, job_id))

    @router.post("/jobs/{job_id}/edit")
    async def edit(seller_id: UUID, job_id: UUID, request: Request):
        p = principal(request)
        await run_in_threadpool(execute, lambda: service.scope(p, seller_id, True))
        body = await parse_catalog_json(request, EditDraft, maximum=8388608)
        return await run_in_threadpool(execute, lambda: service.edit(p, seller_id, job_id, body))

    @router.post("/jobs/{job_id}/submit")
    async def submit(seller_id: UUID, job_id: UUID, request: Request):
        p = principal(request)
        await run_in_threadpool(execute, lambda: service.scope(p, seller_id, True))
        body = await parse_catalog_json(request, Confirm, maximum=1048576)
        return await run_in_threadpool(
            execute, lambda: service.submit(p, seller_id, job_id, body.selected, body.version)
        )

    return router
