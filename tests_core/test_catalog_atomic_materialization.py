from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3
from threading import Barrier

import pandas as pd
import pytest

from marketplace_core.catalogs import CatalogCore
from services import db, lists, shared_cache
from services.postgresql_backend import PostgreSQLCompatConnection
from tests_core.test_postgresql_tenant_boundaries import postgres_database


@pytest.fixture(params=["sqlite", "postgresql"])
def catalog_database(request, tmp_path, monkeypatch):
    if request.param == "postgresql":
        pg_connect = request.getfixturevalue("postgres_database")
        @contextmanager
        def connect():
            with pg_connect() as con:
                yield PostgreSQLCompatConnection(con)
    else:
        monkeypatch.setenv("MARKETPLACE_HUB_DB_ENGINE", "sqlite")
        @contextmanager
        def connect():
            con = sqlite3.connect(tmp_path/'catalog.db', timeout=10)
            con.row_factory = sqlite3.Row
            try:
                with con:
                    yield con
            finally:
                con.close()
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(shared_cache, "_BACKEND", shared_cache.LocalSharedCache())
    monkeypatch.setattr(lists, "normalize", lambda frame: frame)
    CatalogCore().ensure_schema()
    with connect() as con:
        for price_list_id in (7, 8):
            con.execute("INSERT INTO catalog_products(price_list_id,row_no,sku) VALUES(?,0,'OLD')", (price_list_id,))
            con.execute("INSERT INTO catalog_materializations(price_list_id,source_fingerprint,row_count,status) VALUES(?,'old',1,'ready')", (price_list_id,))
    source = tmp_path/'source.csv'
    source.write_text('sku,cost\nNEW,12\n', encoding='utf-8')
    return connect, source


def snapshot(connect, price_list_id=7):
    with connect() as con:
        products = [dict(row) for row in con.execute("SELECT * FROM catalog_products WHERE price_list_id=? ORDER BY row_no", (price_list_id,)).fetchall()]
        metadata = dict(con.execute("SELECT * FROM catalog_materializations WHERE price_list_id=?", (price_list_id,)).fetchone())
    return products, metadata


def test_readers_keep_previous_catalog_until_complete_publish(catalog_database, monkeypatch):
    connect, source = catalog_database
    before = snapshot(connect)
    core = CatalogCore()
    monkeypatch.setattr(core, '_source_chunks', lambda *a: iter([
        pd.DataFrame([{'sku': 'NEW1', 'cost': 12}]),
        pd.DataFrame([{'sku': 'NEW2', 'cost': 15}]),
    ]))
    def progress(*args):
        assert snapshot(connect) == before
        # Another connection can persist progress while staging is in use.
        with connect() as con:
            con.execute("UPDATE catalog_materializations SET message='progress' WHERE price_list_id=8")
    result = core.materialize(7, source, progress=progress)
    products, metadata = snapshot(connect)
    assert result['rows'] == metadata['row_count'] == 2
    assert [r['sku'] for r in products] == ['NEW1', 'NEW2']
    assert [r['cost'] for r in products] == [12, 15]
    assert metadata['status'] == 'ready'
    assert snapshot(connect, 8)[0][0]['sku'] == 'OLD'


def test_parser_failure_preserves_products_and_metadata(catalog_database, monkeypatch):
    connect, source = catalog_database
    before = snapshot(connect)
    def broken(*args):
        yield pd.DataFrame([{'sku': 'PARTIAL'}])
        raise ValueError('synthetic parser failure')
    core = CatalogCore()
    monkeypatch.setattr(core, '_source_chunks', broken)
    with pytest.raises(ValueError, match='parser failure'):
        core.materialize(7, source)
    assert snapshot(connect) == before


def test_publish_failure_after_delete_rolls_back(catalog_database, monkeypatch):
    connect, source = catalog_database
    before = snapshot(connect)
    @contextmanager
    def failing_connect():
        with connect() as con:
            class FailingConnection:
                def __getattr__(self, name):
                    return getattr(con, name)
                def execute(self, sql, params=()):
                    if sql.startswith('INSERT INTO catalog_products(') and 'SELECT' in sql:
                        raise RuntimeError('synthetic publication failure')
                    return con.execute(sql, params)
            yield FailingConnection()
    monkeypatch.setattr(db, 'connect', failing_connect)
    core = CatalogCore()
    monkeypatch.setattr(core, '_source_chunks', lambda *a: iter([pd.DataFrame([{'sku':'NEW'}])]))
    with pytest.raises(RuntimeError, match='publication failure'):
        core.materialize(7, source)
    assert snapshot(connect) == before


def test_empty_catalog_is_a_complete_empty_version(catalog_database, monkeypatch):
    connect, source = catalog_database
    core = CatalogCore()
    monkeypatch.setattr(core, '_source_chunks', lambda *a: iter([]))
    assert core.materialize(7, source)['rows'] == 0
    products, metadata = snapshot(connect)
    assert products == [] and metadata['row_count'] == 0 and metadata['status'] == 'ready'


def test_concurrent_publications_never_mix_versions(catalog_database, monkeypatch):
    connect, source = catalog_database
    barrier = Barrier(2)
    def run(prefix):
        core = CatalogCore()
        core._source_chunks = lambda *a: iter([pd.DataFrame([{'sku': prefix+'1'}, {'sku': prefix+'2'}])])
        return core.materialize(7, source, progress=lambda *a: barrier.wait(timeout=10))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ['A', 'B']))
    products, metadata = snapshot(connect)
    assert all(result['rows'] == 2 for result in results)
    assert [row['sku'] for row in products] in (['A1','A2'], ['B1','B2'])
    assert metadata['row_count'] == 2


def test_source_change_aborts_publication(catalog_database, monkeypatch):
    connect, source = catalog_database
    before = snapshot(connect)
    core = CatalogCore()
    monkeypatch.setattr(core, '_source_chunks', lambda *a: iter([pd.DataFrame([{'sku':'NEW'}])]))
    with pytest.raises(RuntimeError, match='cambiato'):
        core.materialize(7, source, progress=lambda *a: source.write_text('changed source'))
    assert snapshot(connect) == before


def test_csv_failure_after_first_chunk_does_not_restart_parser(tmp_path, monkeypatch):
    source = tmp_path/'source.csv'
    source.write_text('sku,cost\nNEW,12\n')
    def chunks(*a, **kw):
        yield pd.DataFrame([{'sku':'NEW'}])
        raise ValueError('partial CSV')
    monkeypatch.setattr(pd, 'read_csv', chunks)
    monkeypatch.setattr(lists, 'read_list', lambda *a: pytest.fail('Must not duplicate already emitted rows'))
    with pytest.raises(ValueError, match='partial CSV'):
        list(CatalogCore()._source_chunks(source, 1000))
