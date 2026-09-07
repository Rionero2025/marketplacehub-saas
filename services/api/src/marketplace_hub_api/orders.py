from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from marketplace_hub_core.orders.repository import OrdersNotFoundError
from marketplace_hub_core.orders.service import (
    OrdersQueueUnavailableError,
    OrdersService,
    OrdersValidationError,
)
from marketplace_hub_core.seller_settings.repository import MarketplaceAccountNotFoundError
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError, WorkspacePermissionError
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from marketplace_hub_api.seller_settings import SafeSettingsRoute


class OrdersSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    account_id: UUID
    environment: Literal["live", "playground"] = "live"
    maximum: Literal[500, 1000, 5000] | None = 1000
    include_details: bool = True


def create_orders_router(service: OrdersService, auth, settings):
    router = APIRouter(prefix="/v1/sellers/{seller_id}/orders", tags=["orders"],
                       route_class=SafeSettingsRoute)

    def principal(request):
        session = auth.authenticate(request.cookies.get(settings.session_cookie_name))
        if session is None:
            raise HTTPException(401, "Sessione non valida o scaduta.")
        return session

    def execute(operation):
        try:
            return operation()
        except (SellerNotAccessibleError, MarketplaceAccountNotFoundError,
                OrdersNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from None
        except WorkspacePermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except OrdersValidationError as exc:
            raise HTTPException(422, str(exc)) from None
        except OrdersQueueUnavailableError as exc:
            raise HTTPException(503, str(exc)) from None
        except SQLAlchemyError:
            raise HTTPException(503, "Ordini temporaneamente non disponibili.") from None

    @router.post("/sync", status_code=202)
    def sync(seller_id: UUID, payload: OrdersSyncRequest, request: Request):
        session = principal(request)
        return execute(lambda: service.sync(session, seller_id, **payload.model_dump()))

    @router.get("")
    def list_orders(
        seller_id: UUID, account_id: UUID, request: Request,
        environment: Literal["live", "playground"] = "live",
        page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
        search: str = Query("", max_length=200), status: list[str] = Query([], max_length=100),
        storefront: list[str] = Query([], max_length=100), date_from: date | None = None,
        date_to: date | None = None,
    ):
        session = principal(request)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "La data iniziale deve precedere la data finale.")
        # Match Streamlit's created.dt.date filter: UTC calendar days; the UI may
        # format timestamps in Europe/Rome without shifting the filter boundaries.
        start = datetime.combine(date_from, time.min, UTC) if date_from else None
        if date_to == date.max:
            raise HTTPException(422, "Data finale fuori intervallo.")
        end = datetime.combine(date_to + timedelta(days=1), time.min, UTC) if date_to else None
        return execute(lambda: service.list(
            session, seller_id, account_id, environment, page=page, page_size=page_size,
            search=search.strip(), status=status, storefront=storefront,
            date_from=start, date_to=end,
        ))

    @router.get("/jobs/{job_id}")
    def read_job(seller_id: UUID, job_id: UUID, request: Request):
        session = principal(request)
        return execute(lambda: service.read_job(session, seller_id, job_id))

    @router.get("/{line_id}")
    def order_detail(seller_id: UUID, line_id: UUID, account_id: UUID, request: Request,
                     environment: Literal["live", "playground"] = "live"):
        session = principal(request)
        return execute(lambda: service.item(session, seller_id, account_id, environment, line_id))

    return router
