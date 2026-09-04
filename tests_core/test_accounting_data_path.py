"""Raw marketplace fixtures -> original calculations -> real DB -> Seller HTTP API.

Only external transport, catalog loading and FX rates are replaced. These are
synthetic orders; expectations come from the audited original accounting rules.
"""
from contextlib import contextmanager
from datetime import date
import sqlite3

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import dependencies
from api.routers import accounting as router
from marketplace_core.accounting import AccountingCore, AccountingPeriod, AccountingScope
from services import accounting, db, entitlements, shared_cache
from tests_core.test_postgresql_tenant_boundaries import postgres_database


DAY = date(2026, 9, 4)
PATH = '/sellers/1/accounting'
PARAMS = {'account_id': 11, 'date_from': DAY.isoformat(), 'date_to': DAY.isoformat()}
SKU = 'Supplier_Name_PRODUCT-1_63.59_80.00'
EAN = '8435484015312'


@pytest.fixture(params=['sqlite', 'postgres'])
def pipeline(request, monkeypatch):
    if request.param == 'postgres':
        factory = request.getfixturevalue('postgres_database')
        from services.postgresql_backend import PostgreSQLCompatConnection
        @contextmanager
        def connect():
            with factory() as raw:
                yield PostgreSQLCompatConnection(raw)
    else:
        con = sqlite3.connect(':memory:', check_same_thread=False)
        con.row_factory = lambda cursor, values: dict(zip((c[0] for c in cursor.description), values))
        con.execute('PRAGMA foreign_keys=ON')
        monkeypatch.setenv('MARKETPLACE_HUB_DB_ENGINE', 'sqlite')
        @contextmanager
        def connect():
            with con:
                yield con
    monkeypatch.setattr(db, 'connect', connect)
    monkeypatch.setattr(accounting, 'connect', connect)
    monkeypatch.setattr(shared_cache, '_BACKEND', shared_cache.LocalSharedCache())
    with connect() as connection:
        connection.executescript('''
        CREATE TABLE sellers(id INTEGER PRIMARY KEY,name TEXT,active INTEGER,our_profit_pct REAL,partner_profit_pct REAL);
        CREATE TABLE marketplace_accounts(id INTEGER PRIMARY KEY,seller_id INTEGER,marketplace TEXT,account_name TEXT,active INTEGER);
        CREATE TABLE price_lists(id INTEGER PRIMARY KEY);
        INSERT INTO sellers VALUES(1,'Seller A',1,35,65),(2,'Seller B',1,0,100);
        INSERT INTO marketplace_accounts VALUES(11,1,'kaufland','Account A',1),(21,2,'kaufland','Account B',1);
        ''')
    accounting.ensure_schema()
    monkeypatch.setattr(accounting, 'load_supplier_catalogs', lambda seller_id: [])
    monkeypatch.setattr(accounting, 'get_ecb_rates', lambda: {'rates': {'EUR': 1, 'PLN': 4}})
    user = {'id': 1, 'is_admin': False, 'active_tenant_id': 11, 'tenant_ids': [11],
            'seller_ids': [1], 'permissions': ['accounting'], 'tenant_role': 'operator'}
    monkeypatch.setattr(dependencies, 'session_user', lambda token: user)
    monkeypatch.setattr(entitlements, 'feature_enabled', lambda *args: True)
    app = FastAPI()
    app.include_router(router.router)
    with TestClient(app) as client:
        yield client, connect
    if request.param == 'sqlite':
        con.close()


def raw_order(**fields):
    return {'id_order': 'SYNTHETIC-1', 'id_order_unit': '101', 'id_offer': SKU,
            'ts_created_iso': '2026-09-04T10:00:00Z', 'status': 'need_to_be_sent',
            'storefront': 'de', 'currency': 'EUR', 'price': 12000, 'shipping_rate': 500,
            'revenue_gross': 10800, 'product': {'title': 'Synthetic coffee machine', 'eans': [EAN]},
            **fields}


def sync(monkeypatch, payloads, seller=1, account=11):
    monkeypatch.setattr(accounting, 'fetch_order_units', lambda *args, **kwargs: payloads)
    monkeypatch.setattr(accounting.KauflandClient, 'order_unit',
        lambda self, unit_id: {'data': next(p for p in payloads if p['id_order_unit'] == unit_id)})
    monkeypatch.setattr(accounting.KauflandClient, 'order', lambda *args: {'data': {}})
    return AccountingCore().synchronize(AccountingScope(seller, account, 'kaufland'),
        {'client_key': 'synthetic', 'secret_key': 'synthetic'}, AccountingPeriod(DAY, DAY), full=True)


def read(client, **params):
    response = client.get(PATH + '/rows', params={**PARAMS, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_raw_api_to_persisted_row_catalog_precedence_and_manual_resync(pipeline, monkeypatch):
    client, connect = pipeline
    catalog = accounting.CatalogSource('Supplier_Name', accounting.normalize_supplier('Supplier_Name'),
        'Selected wholesale', 'synthetic.csv', '2026-09-04',
        pd.DataFrame([{'ean': EAN, 'sku': 'PRODUCT-1', 'cost': 60}]))
    monkeypatch.setattr(accounting, 'load_supplier_catalogs', lambda seller_id: [catalog] if seller_id == 1 else [])
    stats = sync(monkeypatch, [raw_order()])
    assert stats['new'] == 1
    with connect() as con:
        persisted = con.execute('SELECT * FROM accounting_order_lines WHERE seller_id=1').fetchone()
    result = read(client)
    row = result['items'][0]
    for key, value in {'composite_sku': SKU, 'ean': EAN, 'product_title': 'Synthetic coffee machine',
                       'quantity': 1, 'purchase_cost_eur': 60, 'sale_eur': 125,
                       'commission_eur': 12, 'payout_eur': 113}.items():
        assert persisted[key] == value
        assert row[key] == value
    assert row['order_date'] == '2026-09-04'
    assert 'match EAN esatto' in row['cost_source']
    assert row['net_revenue_eur'] == row['gross_margin_eur'] == 53
    assert row['partner_share_eur'] == 34.45 and row['our_share_eur'] == 18.55
    assert row['revenue_pct'] == pytest.approx(53 / 60, abs=0.000001)
    assert 'raw_json' not in row
    # Re-importing the same payload is idempotent, even after a manual correction.
    response = client.patch(PATH + f"/rows/{row['id']}", params={'account_id': 11}, json={
        'fields': {'purchase_cost_eur': 55, 'extra_cost_eur': 3, 'supplier_order_number': 'SUP-001'},
        'expected': row['edit_values']})
    assert response.status_code == 200, response.text
    sync(monkeypatch, [raw_order()])
    result = read(client)
    row = result['items'][0]
    assert result['total'] == 1 and row['purchase_cost_eur'] == 55
    assert row['gross_margin_eur'] == 58 and row['net_revenue_eur'] == 55
    assert row['supplier_order_number'] == 'SUP-001'
    assert row['cost_source'] == 'Modifica manuale persistente'
    # Same marketplace identifiers in another account cannot leak into this Seller.
    sync(monkeypatch, [raw_order(price=99900)], seller=2, account=21)
    assert read(client)['total'] == 1
    assert client.get(PATH + '/rows', params={**PARAMS, 'account_id': 21}).status_code == 404
    assert client.get('/sellers/2/accounting/rows', params={**PARAMS, 'account_id': 21}).status_code == 404


def test_embedded_cost_fx_missing_cancel_and_return_rules(pipeline, monkeypatch):
    client, _ = pipeline
    sync(monkeypatch, [
        raw_order(id_order='FX', id_order_unit='201', storefront='pl', currency='PLN',
                  price=48000, shipping_rate=2000, revenue_gross=43200),
        raw_order(id_order='MISSING', id_order_unit='202', id_offer='unknown'),
        raw_order(id_order='CANCEL', id_order_unit='203', status='cancelled'),
        raw_order(id_order='RETURN', id_order_unit='204', status='returned'),
    ])
    rows = {row['order_id']: row for row in read(client)['items']}
    fx = rows['FX']
    assert fx['purchase_cost_eur'] == 63.59 and fx['sale_eur'] == 125
    assert fx['commission_eur'] == 12 and fx['payout_eur'] == 113
    assert fx['net_revenue_eur'] == 49.41
    assert 'SKU composito' in fx['cost_source'] and 'PLN' in fx['financial_source']
    missing = rows['MISSING']
    assert missing['purchase_cost_eur'] is None and missing['net_revenue_eur'] is None
    assert missing['partner_share_eur'] is None
    assert read(client, missing_only=True)['total'] == 1
    for field in ('sale_eur', 'purchase_cost_eur', 'commission_eur', 'payout_eur', 'net_revenue_eur'):
        assert rows['CANCEL'][field] == 0
    returned = rows['RETURN']
    assert returned['sale_eur'] == returned['commission_eur'] == returned['payout_eur'] == 0
    assert returned['refund_eur'] == 125 and returned['purchase_cost_eur'] == 63.59
    assert returned['net_revenue_eur'] == -63.59


def test_worten_quantity_nested_sku_and_actual_sale_reach_seller_api(pipeline, monkeypatch):
    client, connect = pipeline
    with connect() as con:
        con.execute("UPDATE marketplace_accounts SET marketplace='worten' WHERE id=11")
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'orders': [{'order_id': 'WORTEN-TEST', 'created_date': '2026-09-04T10:00:00Z',
                'order_state': 'SHIPPED', 'currency_iso_code': 'EUR',
                'order_lines': [{'order_line_id': '301', 'offer_id': 'opaque-id',
                    'metadata': {'offer_sku': SKU}, 'ean': EAN, 'product_title': 'Synthetic Worten product',
                    'quantity': 2, 'price': 200, 'shipping_price': 5, 'commission_fee': 20}]}], 'total_count': 1}
    monkeypatch.setattr(accounting.requests, 'get', lambda *args, **kwargs: Response())
    AccountingCore().synchronize(AccountingScope(1, 11, 'worten'),
        {'base_url': 'https://synthetic.example/api', 'api_key': 'synthetic'}, AccountingPeriod(DAY, DAY), full=True)
    row = read(client)['items'][0]
    assert row['composite_sku'] == SKU and row['product_title'] == 'Synthetic Worten product'
    assert row['ean'] == EAN and row['quantity'] == 2
    assert row['purchase_cost_eur'] == 127.18
    # Mirakl price is already the line amount; only the unit cost is multiplied.
    assert row['sale_eur'] == 205 and row['commission_eur'] == 20 and row['payout_eur'] == 185
    assert row['net_revenue_eur'] == 57.82
