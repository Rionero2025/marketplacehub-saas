from contextlib import contextmanager
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api import dependencies
from api.routers import catalogs
from services import catalog_sharing, db, entitlements
from services.postgresql_backend import PostgreSQLCompatConnection
from tests_core.test_postgresql_tenant_boundaries import postgres_database


@pytest.fixture(params=['sqlite', 'postgresql'])
def catalog_database(request, tmp_path, monkeypatch):
    if request.param == 'postgresql':
        raw_connect = request.getfixturevalue('postgres_database')
        @contextmanager
        def connect():
            with raw_connect() as raw:
                yield PostgreSQLCompatConnection(raw)
    else:
        path = tmp_path/'catalogs.sqlite'
        @contextmanager
        def connect():
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            try:
                with con:
                    yield con
            finally:
                con.close()
    monkeypatch.setattr(db, 'connect', connect)
    monkeypatch.setattr(catalog_sharing, 'ensure_catalog_sharing_schema', lambda: None)
    monkeypatch.setattr(db, 'cache_get_or_set', lambda namespace, key, factory, **kw: factory())
    with connect() as con:
        con.executescript('''
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE sellers(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE tenant_sellers(seller_id INTEGER,tenant_id INTEGER,active INTEGER);
            CREATE TABLE price_lists(id INTEGER PRIMARY KEY,name TEXT,supplier_id INTEGER,
                owner_seller_id INTEGER,owner_tenant_id INTEGER,sharing_scope TEXT,active INTEGER);
            CREATE TABLE catalog_tenant_access(resource_type TEXT,resource_id INTEGER,tenant_id INTEGER,permission TEXT);
            CREATE TABLE agency_clients(agency_tenant_id INTEGER,client_tenant_id INTEGER,active INTEGER);
            INSERT INTO suppliers VALUES(1,'alpha'),(2,'Beta');
            INSERT INTO sellers VALUES(1,'First'),(2,'Other'),(3,'Agency');
            INSERT INTO tenant_sellers VALUES(1,11,1),(2,22,1),(3,33,1);
        ''')
    return connect


@pytest.fixture
def catalog_client(catalog_database, monkeypatch):
    monkeypatch.setattr(dependencies, 'session_user', lambda token: {
        'id':1, 'active_tenant_id':11, 'tenant_ids':[11], 'tenant_role':'owner',
        'seller_ids':[1], 'permissions':['suppliers_lists'],
    })
    monkeypatch.setattr(entitlements, 'feature_enabled', lambda *args: True)
    app = FastAPI()
    app.include_router(catalogs.router, prefix='/api/v1')
    with TestClient(app) as client:
        yield client


def test_empty_catalog_endpoint_returns_200(catalog_client):
    response = catalog_client.get('/api/v1/sellers/1/catalogs')
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_catalog_endpoint_preserves_order_grants_and_unique_rows(catalog_database, catalog_client):
    with catalog_database() as con:
        con.executescript('''
            INSERT INTO price_lists VALUES
            (1,'zeta',1,1,11,'tenant',1),
            (2,'Alpha',1,2,22,'platform',1),
            (3,'alpha',1,2,22,'tenant',1),
            (4,'agency feed',2,3,33,'agency',1),
            (5,'private other',1,2,22,'tenant',1),
            (6,'inactive',1,1,11,'tenant',0),
            (7,'revoked grant',1,2,22,'tenant',1);
            INSERT INTO catalog_tenant_access VALUES
            ('price_list',3,11,'use'),('price_list',3,11,'manage'),
            ('price_list',7,11,'revoked');
            INSERT INTO agency_clients VALUES(33,11,1);
        ''')
    response = catalog_client.get('/api/v1/sellers/1/catalogs')
    assert response.status_code == 200, response.text
    assert [item['id'] for item in response.json()] == [2,3,1,4]
    assert catalog_client.get('/api/v1/sellers/2/catalogs').status_code == 404
