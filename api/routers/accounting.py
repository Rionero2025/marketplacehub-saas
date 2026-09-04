from __future__ import annotations

from datetime import date
import math
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import Field, model_validator

from api.dependencies import ApiUser, ensure_seller_access, require_permission
from api.helpers import load_account, submit_job
from api.schemas import AccountingJobRequest, JobResponse, ApiModel
from marketplace_core.accounting import AccountingCore, AccountingPeriod, AccountingScope
from marketplace_core import seller_accounting
from services import accounting as legacy
from services import db
from services.profit_sharing import seller_profit_settings
from services.security import decrypt_dict

router = APIRouter(prefix="/sellers/{seller_id}/accounting", tags=["accounting"])


class RowEdit(ApiModel):
    fields: dict[str, str | float | int | None] = Field(min_length=1, max_length=16)
    expected: dict[str, str | float | int | None]

    @model_validator(mode='after')
    def validate_fields(self):
        kinds = legacy.ACCOUNTING_INLINE_EDIT_FIELDS
        if not set(self.fields) <= kinds.keys() or set(self.expected) != kinds.keys():
            raise ValueError('Campi contabili non validi o versione originale della riga mancante.')
        for values in (self.fields, self.expected):
            for name, value in values.items():
                if kinds[name] in {'text','identifier'}:
                    if value is not None and (not isinstance(value, str) or len(value) > 4000):
                        raise ValueError('Testo non valido o troppo lungo.')
                elif value is not None:
                    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or abs(value) > 1e12:
                        raise ValueError('Inserisci un importo numerico finito.')
                    if kinds[name] == 'integer' and (value < 1 or value != int(value)):
                        raise ValueError('La quantità deve essere un intero positivo.')
        return self


class CatalogSelection(ApiModel):
    enabled_ids: list[Annotated[int, Field(gt=0)]] = Field(max_length=1000)


def valid_period(date_from: date, date_to: date):
    if date_to < date_from:
        raise HTTPException(422, 'La data finale non può precedere quella iniziale.')


@router.get('/accounts')
def accounting_accounts(seller_id: int, user: ApiUser = Depends(require_permission('accounting'))) -> list[dict]:
    ensure_seller_access(user, seller_id)
    return db.rows('SELECT id,seller_id,marketplace,account_name,active FROM marketplace_accounts WHERE seller_id=? ORDER BY active DESC,marketplace,account_name,id', (seller_id,))


@router.get('/rows')
def accounting_rows(seller_id: int, account_id: int, date_from: date, date_to: date,
                    search: str = Query('', max_length=200), missing_only: bool = False,
                    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                    suppliers: list[str] = Query(default=[], max_length=100),
                    statuses: list[str] = Query(default=[], max_length=100),
                    countries: list[str] = Query(default=[], max_length=100),
                    user: ApiUser = Depends(require_permission('accounting'))) -> dict:
    ensure_seller_access(user, seller_id)
    valid_period(date_from, date_to)
    try:
        return seller_accounting.list_rows(seller_id, account_id, date_from, date_to, search, missing_only, offset, limit,
                                           suppliers=suppliers, statuses=statuses, countries=countries)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.patch('/rows/{row_id}')
def accounting_edit(seller_id: int, account_id: int, row_id: int, payload: RowEdit,
                    user: ApiUser = Depends(require_permission('accounting'))) -> dict:
    ensure_seller_access(user, seller_id)
    try:
        return seller_accounting.save_row(seller_id, account_id, row_id, payload.fields, payload.expected)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except legacy.AccountingEditConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/catalogs')
def accounting_catalogs(seller_id: int, user: ApiUser = Depends(require_permission('accounting'))) -> dict:
    ensure_seller_access(user, seller_id)
    return seller_accounting.catalog_selection(seller_id)


@router.put('/catalogs')
def save_catalogs(seller_id: int, payload: CatalogSelection,
                 user: ApiUser = Depends(require_permission('accounting'))) -> dict:
    ensure_seller_access(user, seller_id)
    allowed = {r['price_list_id'] for r in legacy.accounting_catalog_options(seller_id)}
    if not set(payload.enabled_ids) <= allowed:
        raise HTTPException(404, 'Listino non disponibile per questo Seller.')
    return legacy.save_accounting_catalog_selection(seller_id, payload.enabled_ids)


@router.get('/export.xlsx')
def accounting_export(seller_id: int, account_id: int, date_from: date, date_to: date,
                      search: str = Query('', max_length=200), missing_only: bool = False,
                      suppliers: list[str] = Query(default=[], max_length=100),
                      statuses: list[str] = Query(default=[], max_length=100),
                      countries: list[str] = Query(default=[], max_length=100),
                      user: ApiUser = Depends(require_permission('accounting'))):
    ensure_seller_access(user, seller_id)
    valid_period(date_from, date_to)
    try:
        seller, account, records = seller_accounting.filtered_rows(seller_id, account_id, date_from, date_to, search, missing_only,
                                                                 suppliers=suppliers, statuses=statuses, countries=countries)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if len(records) > 20000:
        raise HTTPException(413, 'Esportazione limitata a 20.000 righe: restringi il periodo o i filtri.')
    if not records:
        raise HTTPException(404, 'Nessuna riga da esportare con questi filtri.')
    settings = seller_profit_settings(seller)
    content = legacy.export_xlsx_bytes(records, our_profit_pct=settings['our_pct'], partner_profit_pct=settings['partner_pct'], partner_name=seller['name'])
    filename = f'contabilita-{seller_id}-{account_id}-{date_from}-{date_to}.xlsx'
    return Response(content, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"', 'Cache-Control':'no-store'})


@router.get("/status")
def accounting_status(
    seller_id: int,
    account_id: int,
    marketplace: str,
    user: ApiUser = Depends(require_permission("accounting")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    account = load_account(seller_id, account_id, marketplace=marketplace)
    credentials = decrypt_dict(account.get("credentials_encrypted") or "")
    status = AccountingCore().status(
        AccountingScope(seller_id, account_id, marketplace), credentials
    )
    return {
        "environment": status.environment,
        "sync_state": status.sync_state,
        "cache_summary": status.cache_summary,
    }


@router.post("/sync", response_model=JobResponse, status_code=202)
def accounting_sync(
    seller_id: int,
    account_id: int,
    payload: AccountingJobRequest,
    user: ApiUser = Depends(require_permission("accounting")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    load_account(seller_id, account_id, marketplace=payload.marketplace)
    valid_period(payload.date_from, payload.date_to)
    request = AccountingCore().build_sync_job(
        AccountingScope(seller_id, account_id, payload.marketplace),
        AccountingPeriod(payload.date_from, payload.date_to),
        full=payload.full,
    )
    return submit_job(request)


@router.post("/refresh-costs", response_model=JobResponse, status_code=202)
def refresh_costs(
    seller_id: int,
    account_id: int,
    payload: AccountingJobRequest,
    user: ApiUser = Depends(require_permission("accounting")),
) -> dict:
    seller_id = ensure_seller_access(user, seller_id)
    load_account(seller_id, account_id, marketplace=payload.marketplace)
    valid_period(payload.date_from, payload.date_to)
    request = AccountingCore().build_refresh_costs_job(
        AccountingScope(seller_id, account_id, payload.marketplace),
        AccountingPeriod(payload.date_from, payload.date_to),
    )
    return submit_job(request)
