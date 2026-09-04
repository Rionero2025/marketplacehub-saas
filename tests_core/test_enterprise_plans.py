import json
from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import dependencies
from api.routers import plans
from api.routers.onboarding import signup_plans
from services import entitlements as ent, onboarding, db
from tests_core.test_saas_onboarding_v320 import signup_database
from tests_core.test_postgresql_tenant_boundaries import postgres_database


def test_public_catalog_has_six_agreed_prices(signup_database):
    result = signup_plans()
    assert [(p['code'],p['monthly_price_cents']) for p in result['plans']] == [
        ('free',0),('bronze',2500),('silver',4900),('gold',7900),('platinum',15000),('enterprise',25000)]
    assert result['trial_plan_code'] == 'enterprise'


def test_admin_plan_edits_survive_schema_restart(signup_database):
    ent.ensure_entitlement_schema()
    ent.update_plan_configuration('bronze',name='Bronze',monthly_price_cents=2700,
                                  features=['dashboard'],limits={'monthly_orders':123})
    ent._SCHEMA_READY = False
    ent.ensure_entitlement_schema()
    assert ent.plan_record('bronze')['monthly_price_cents'] == 2700
    assert ent.plan_record('bronze')['features'] == ['dashboard']
    assert ent.plan_record('bronze')['limits']['monthly_orders'] == 123


def test_agency_signup_gets_enterprise_trial_without_platform_privileges(signup_database):
    result=onboarding.register_merchant(company_name='Agency Test',username='agency-test',
        password='Synthetic-password-123',tenant_type='agency',plan_code='silver')
    tid=result['tenant']['id']
    assert result['tenant']['tenant_type']=='agency'
    assert result['billing']['status']=='trialing'
    assert result['tenant']['plan_code']=='enterprise'
    user=db.row('SELECT * FROM app_users WHERE id=?',(result['user_id'],))
    assert user['is_admin']==0
    assert not {'database','backup_transfer'} & set(json.loads(user['permissions_json']))
    actual=ent.tenant_entitlements(tid,use_cache=False)
    assert all(actual['features'][key] for key in ent.STANDARD_FEATURES)
    event=db.row("SELECT metadata_json FROM onboarding_events WHERE tenant_id=? AND event_type='signup_completed'",(tid,))
    assert json.loads(event['metadata_json'])['requested_plan_code']=='silver'


def test_plan_edit_endpoint_rejects_seller_owner(monkeypatch):
    monkeypatch.setattr(dependencies,'session_user',lambda token:{'id':1,'active_tenant_id':11,
        'tenant_ids':[11],'tenant_role':'owner','seller_ids':[1],'permissions':[]})
    app=FastAPI();app.include_router(plans.router)
    with TestClient(app) as client:
        response=client.put('/plans/bronze',json={'name':'Bronze','monthly_price_cents':0,'features':[],'limits':{}})
    assert response.status_code==403


def test_agency_client_list_uses_requested_agency_and_user_access(monkeypatch):
    from api.routers import tenants
    monkeypatch.setattr(dependencies,'session_user',lambda token:{'id':1,'active_tenant_id':11,
        'tenant_ids':[11,22,33],'tenant_role':'owner','seller_ids':[],'permissions':[]})
    monkeypatch.setattr(tenants,'tenant_record',lambda tid:{'tenant_type':'agency' if tid==11 else 'merchant'})
    monkeypatch.setattr(tenants,'accessible_tenants_for_user',lambda *a,**kw:[
        {'id':22,'name':'Assigned','tenant_type':'merchant'},
        {'id':33,'name':'Other agency client','tenant_type':'merchant'}])
    def assigned(sql,args):
        assert args==(11,)
        return [{'client_tenant_id':22},{'client_tenant_id':44}]
    monkeypatch.setattr(db,'rows',assigned)
    app=FastAPI();app.include_router(tenants.router)
    with TestClient(app) as client:
        response=client.get('/tenants/11/agency-clients')
        assert response.status_code==200
        assert [x['id'] for x in response.json()]==[22]
        assert client.get('/tenants/22/agency-clients').status_code==404
        assert client.get('/tenants/44/agency-clients').status_code==404


def test_postgresql_seed_and_commercial_edits_survive_restart(postgres_database,monkeypatch):
    from services import tenancy
    from services.postgresql_backend import PostgreSQLCompatConnection
    @contextmanager
    def connect():
        with postgres_database() as raw:
            yield PostgreSQLCompatConnection(raw)
    monkeypatch.setattr(db,'connect',connect)
    monkeypatch.setattr(tenancy,'ensure_tenancy_schema',lambda:None)
    monkeypatch.setattr(ent,'_SCHEMA_READY',False)
    with connect() as con:
        con.execute('CREATE TABLE tenants(id INTEGER PRIMARY KEY,tenant_type TEXT,plan_code TEXT,updated_at TEXT)')
    assert len(ent.list_plans(public_only=True))==6
    ent.update_plan_configuration('gold',name='Gold',monthly_price_cents=8100,features=['dashboard'],limits={})
    ent._SCHEMA_READY=False
    ent.ensure_entitlement_schema()
    assert ent.plan_record('gold')['monthly_price_cents']==8100
