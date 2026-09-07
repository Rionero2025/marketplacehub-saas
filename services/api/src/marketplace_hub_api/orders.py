from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from marketplace_hub_core.orders.filters import OrderFilters
from marketplace_hub_core.orders.repository import OrdersNotFoundError
from marketplace_hub_core.orders.service import (
    OrdersQueueUnavailableError,
    OrdersService,
    OrdersValidationError,
)
from marketplace_hub_core.seller_settings.repository import MarketplaceAccountNotFoundError
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError, WorkspacePermissionError
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from marketplace_hub_api.seller_settings import SafeSettingsRoute


class OrdersSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    account_id: UUID
    environment: Literal["live", "playground"] = "live"
    maximum: Literal[500, 1000, 5000] | None = 1000
    include_details: bool = True


class OrdersSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    account_id: UUID
    environment: Literal["live", "playground"] = "live"
    selection_id: UUID
    filters: OrderFilters
    action: Literal["select_all", "clear", "set"]
    line_id: UUID | None = None
    selected: StrictBool | None = None


class OrdersExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    account_id: UUID
    environment: Literal["live", "playground"] = "live"
    selection_id: UUID
    filters: OrderFilters
    kind: Literal["selected", "filtered"]


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
        except ValidationError:
            raise HTTPException(422, "Filtri ordine non validi.") from None
        except OrdersQueueUnavailableError as exc:
            raise HTTPException(503, str(exc)) from None
        except SQLAlchemyError:
            raise HTTPException(503, "Ordini temporaneamente non disponibili.") from None

    @router.post("/sync", status_code=202)
    def sync(seller_id: UUID, payload: OrdersSyncRequest, request: Request):
        session = principal(request)
        return execute(lambda: service.sync(session, seller_id, **payload.model_dump()))

    @router.post("/selection")
    def change_selection(seller_id: UUID, payload: OrdersSelectionRequest, request: Request):
        session = principal(request)
        values = payload.model_dump(exclude={"filters"})
        return execute(lambda: service.select(session, seller_id, filters=payload.filters,
                                               **values))

    @router.post("/export")
    def export_orders(seller_id: UUID, payload: OrdersExportRequest, request: Request):
        session = principal(request)
        values = payload.model_dump(exclude={"filters"})
        content = execute(lambda: service.export(
            session, seller_id, filters=payload.filters,
            authenticate=lambda: principal(request), **values,
        ))
        return StreamingResponse(content, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="ordini-{payload.kind}.csv"',
            "Cache-Control": "no-store",
        })

    @router.get("")
    def list_orders(
        seller_id: UUID, account_id: UUID, request: Request,
        environment: Literal["live", "playground"] = "live",
        page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
        search: str = Query("", max_length=200), status: list[str] = Query([], max_length=100),
        storefront: list[str] = Query([], max_length=100), date_from: date | None = None,
        date_to: date | None = None,
        currency: list[str] = Query([], max_length=100),
        carrier: list[str] = Query([], max_length=100),
        status_selection: Literal["all", "selected"] = "all",
        storefront_selection: Literal["all", "selected"] = "all",
        currency_selection: Literal["all", "selected"] = "all",
        tracking: Literal["all", "present", "missing"] = "all",
        commission: Literal["all", "present", "missing"] = "all",
        amount_min: Decimal | None = None, amount_max: Decimal | None = None,
    ):
        session = principal(request)
        criteria = execute(lambda: OrderFilters(
            search=search, statuses=status if status or status_selection == "selected" else None,
            storefronts=storefront if storefront or storefront_selection == "selected" else None,
            currencies=currency if currency or currency_selection == "selected" else None,
            carriers=carrier, tracking=tracking, commission=commission,
            amount_min=amount_min, amount_max=amount_max, date_from=date_from, date_to=date_to,
        ))
        return execute(lambda: service.list(
            session, seller_id, account_id, environment, page=page, page_size=page_size,
            criteria=criteria,
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
