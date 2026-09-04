from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import ApiUser, ensure_seller_access, require_permission
from marketplace_core.seller_dashboard import snapshot
from services.background_jobs import recent_jobs
from services.entitlements import tenant_entitlements

router = APIRouter(prefix='/sellers/{seller_id}/dashboard', tags=['dashboard'])


@router.get('')
def seller_dashboard(seller_id: int, date_from: date, date_to: date,
                     account_id: int | None = Query(default=None, gt=0),
                     view: Literal['lines','orders','missing'] = 'lines',
                     search: str = Query(default='', max_length=200),
                     offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
                     user: ApiUser = Depends(require_permission('dashboard'))) -> dict:
    ensure_seller_access(user, seller_id)
    if date_to < date_from:
        raise HTTPException(status_code=422, detail='La data finale non può precedere quella iniziale.')
    try:
        result = snapshot(seller_id, date_from=date_from, date_to=date_to, account_id=account_id,
                          view=view, search=search, offset=offset, limit=limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    ent = tenant_entitlements(user.active_tenant_id)
    result['plan'] = {key: ent.get(key) for key in ('plan_code','plan_name','active')}
    # Include progress with the same explicit tenant/seller boundary as /jobs.
    result['jobs'] = [{key: item.get(key) for key in ('id','kind','status','progress_pct','message','error','created_at')}
                      for item in recent_jobs(seller_id=seller_id, limit=8)
                      if int(item.get('seller_id') or 0) == seller_id
                      and int(item.get('tenant_id') or 0) == user.active_tenant_id]
    return result
