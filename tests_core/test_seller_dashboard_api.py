from contextlib import contextmanager
from datetime import date
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import dependencies
from api.routers import dashboard as router
from marketplace_core import seller_dashboard as core
from services import accounting, dashboard, db
from tests_core.test_postgresql_tenant_boundaries import postgres_database


def seed(con):
    con.executescript('''
    CREATE TABLE sellers(id INTEGER PRIMARY KEY,name TEXT,legal_name TEXT,our_profit_pct REAL,partner_profit_pct REAL,active INTEGER);
    CREATE TABLE marketplace_accounts(id INTEGER PRIMARY KEY,seller_id INTEGER,marketplace TEXT,account_name TEXT,active INTEGER,credentials_encrypted TEXT);
    CREATE TABLE accounting_manual_overrides(marketplace_account_id INTEGER,marketplace TEXT,row_key TEXT,field_name TEXT,value_json TEXT);
    INSERT INTO sellers VALUES(1,'Seller A','',35,65,1),(2,'Seller B','',0,100,1);
    INSERT INTO marketplace_accounts VALUES(11,1,'kaufland','A',1,'SECRET-A'),(12,1,'worten','B',1,'SECRET-B'),(21,2,'kaufland','Private',1,'SECRET-C');
    INSERT INTO accounting_manual_overrides VALUES(11,'kaufland','r2','purchase_cost_eur','25');
    ''')
    numbers={'seller_id','marketplace_account_id','quantity','sale_eur','purchase_cost_eur','commission_eur','refund_eur','payout_eur','extra_cost_eur'}
    con.execute('CREATE TABLE accounting_order_lines(id INTEGER PRIMARY KEY,'+','.join(k+(' REAL' if k in numbers else ' TEXT') for k in dashboard.DASHBOARD_ACCOUNTING_COLUMNS.split(','))+',raw_json TEXT)')
    def insert(i,*,account=11,seller=1,order='A',created='2026-09-04',sale=100,cost=60,payout=90,status='Spedito',ean='123'):
        values=dict.fromkeys(dashboard.DASHBOARD_ACCOUNTING_COLUMNS.split(','),'')
        values.update(seller_id=seller,marketplace_account_id=account,marketplace='worten' if account==12 else 'kaufland',row_key=f'r{i}',order_id=order,order_created=created,quantity=1,sale_eur=sale,purchase_cost_eur=cost,payout_eur=payout,commission_eur=10,refund_eur=0,extra_cost_eur=0,raw_status=status,status_label=status,product_title='Product '+ean,ean=ean,synced_at='2026-09-04T12:00:00Z')
        keys=['id',*values,'raw_json'];con.execute('INSERT INTO accounting_order_lines('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',(i,*values.values(),'PRIVATE-RAW'))
    insert(1,created='2026-09-03T22:30:00Z')
    insert(2,sale=50,cost=20,payout=45)
    insert(3,order='CANCEL',sale=999,cost=30,payout=900,status='Cancellato')
    insert(4,order='MISSING',sale=80,cost=None,payout=70,ean='456')
    insert(5,order='PREVIOUS',created='03/09/2026',sale=40,cost=10,payout=35)
    insert(6,order='UNDATED',created='bad-date')
    insert(7,account=12,order='B',sale=20,cost=10,payout=18)
    insert(8,account=21,seller=2,order='PRIVATE',sale=1000)
    insert(9,account=21,seller=1,order='BROKEN-ASSOCIATION',sale=9999)


@pytest.fixture
def dashboard_database(monkeypatch):
    con=sqlite3.connect(':memory:',check_same_thread=False)
    con.row_factory=lambda cursor,values:dict(zip((c[0] for c in cursor.description),values))
    @contextmanager
    def connect():
        with con: yield con
    monkeypatch.setattr(db,'connect',connect)
    monkeypatch.setattr(accounting,'connect',connect)
    seed(con)
    yield con
    con.close()


def snapshot(**kwargs):
    return core.snapshot(1,date_from=date(2026,9,4),date_to=date(2026,9,4),**kwargs)


def check_values(result):
    assert result['summary']['sales']==250
    assert result['summary']['profit']==58
    assert result['summary']['orders']==4
    assert result['summary']['missing_profit_rows']==1
    assert result['summary']['our_amount']==20.3
    assert result['summary']['partner_amount']==37.7
    assert result['previous']['sales']==40
    assert result['previous']['profit']==25
    assert result['undated_rows']==1
    assert result['top_products'][0]['quantity']==3
    assert result['top_products'][0]['margin_eur']==58
    assert result['trend'][0]['sales']==250
    assert result['details']['total']==5
    assert all(r['order_id'] not in {'PRIVATE','BROKEN-ASSOCIATION'} for r in result['details']['items'])


def test_snapshot_matches_legacy_rules_and_scopes(dashboard_database):
    check_values(snapshot())


def test_account_filter_details_pagination_and_manual_edit_refresh(dashboard_database):
    result=snapshot(account_id=11,view='orders',limit=1,offset=1)
    assert result['summary']['sales']==230
    assert result['summary']['orders']==3
    assert result['details']['total']==3
    assert len(result['details']['items'])==1
    missing=snapshot(view='missing')
    assert missing['details']['total']==1
    assert missing['details']['items'][0]['net_revenue_eur'] is None
    assert missing['details']['items'][0]['missing_reason']=='Costo acquisto mancante'
    dashboard_database.execute("UPDATE accounting_manual_overrides SET value_json='30'")
    assert snapshot()['summary']['profit']==53
    # Search affects detail only, never silently changes the totals above it.
    filtered=snapshot(search='MISSING')
    assert filtered['details']['total']==1
    assert filtered['summary']['sales']==250


def test_single_accounting_read_and_no_raw_payloads(dashboard_database,monkeypatch):
    seen=[];original=db.rows
    def rows(sql,params=()):seen.append(sql);return original(sql,params)
    monkeypatch.setattr(db,'rows',rows)
    result=snapshot()
    assert sum('FROM accounting_order_lines' in q for q in seen)==1
    assert not any('raw_json' in q or 'credentials_encrypted' in q for q in seen)
    assert 'SECRET' not in str(result) and 'PRIVATE-RAW' not in str(result)
    with pytest.raises(LookupError):snapshot(account_id=21)


def test_dashboard_http_denies_cross_seller_account_and_invalid_filters(dashboard_database,monkeypatch):
    monkeypatch.setattr(dependencies,'session_user',lambda token:{'id':1,'is_admin':True,'active_tenant_id':11,'tenant_ids':[11],'seller_ids':[1],'permissions':['dashboard']})
    monkeypatch.setattr(router,'tenant_entitlements',lambda tid:{'plan_name':'Enterprise','active':True})
    monkeypatch.setattr(router,'recent_jobs',lambda **kw:[{'id':'own','seller_id':1,'tenant_id':11},{'id':'other','seller_id':1,'tenant_id':22}])
    app=FastAPI();app.include_router(router.router)
    params={'date_from':'2026-09-04','date_to':'2026-09-04'}
    with TestClient(app) as client:
        result=client.get('/sellers/1/dashboard',params=params)
        assert result.status_code==200
        check_values(result.json())
        assert [j['id'] for j in result.json()['jobs']]==['own']
        assert client.get('/sellers/2/dashboard',params=params).status_code==404
        assert client.get('/sellers/1/dashboard',params={**params,'account_id':21}).status_code==404
        for bad in [{'limit':101},{'offset':-1},{'view':'private'},{'date_to':'2026-09-01'}]:
            assert client.get('/sellers/1/dashboard',params={**params,**bad}).status_code==422


def test_postgresql_dashboard_matches_sqlite_with_explicit_seller_filter(postgres_database,monkeypatch):
    from services.postgresql_backend import PostgreSQLCompatConnection
    @contextmanager
    def connect():
        with postgres_database() as raw:yield PostgreSQLCompatConnection(raw)
    monkeypatch.setattr(db,'connect',connect);monkeypatch.setattr(accounting,'connect',connect)
    with connect() as con:seed(con)
    check_values(snapshot())
    with pytest.raises(LookupError):snapshot(account_id=21)


def test_dashboard_requires_session_permission_and_plan_before_read(monkeypatch):
    from services import entitlements
    record={'id':1,'is_admin':False,'active_tenant_id':11,'tenant_ids':[11],'seller_ids':[1],'permissions':[],'tenant_role':'viewer'}
    monkeypatch.setattr(dependencies,'session_user',lambda token:record)
    monkeypatch.setattr(router,'snapshot',lambda *a,**kw:pytest.fail('Unauthorized request reached accounting data'))
    monkeypatch.setattr(entitlements,'feature_enabled',lambda *a:False)
    monkeypatch.setattr(entitlements,'tenant_entitlements',lambda *a:{'plan_code':'free'})
    app=FastAPI();app.include_router(router.router)
    with TestClient(app) as client:
        params={'date_from':'2026-09-04','date_to':'2026-09-04'}
        assert client.get('/sellers/1/dashboard',params=params).status_code==403
        record['permissions']=['dashboard']
        assert client.get('/sellers/1/dashboard',params=params).status_code==403
        monkeypatch.setattr(dependencies,'session_user',lambda token:None)
        assert client.get('/sellers/1/dashboard',params=params).status_code==401
