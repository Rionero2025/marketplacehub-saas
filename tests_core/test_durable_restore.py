from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from services import durable_files as files, object_storage as storage, saved_view_storage as views, lists


@pytest.fixture
def archive(tmp_path, monkeypatch):
    store = storage.LocalObjectStorage(tmp_path/'shared')
    for module in (files, views):
        monkeypatch.setattr(module, 'object_store', lambda: store)
        monkeypatch.setattr(module, 'storage_config', lambda: type('Config', (), {'backend':'local'})())
        monkeypatch.setattr(module, 'CACHE_DIR', tmp_path/module.__name__/'cache')
    monkeypatch.setattr(views, 'LEGACY_DIR', tmp_path/'api-cache')
    path=tmp_path/'metadata.sqlite'
    @contextmanager
    def connect():
        con=sqlite3.connect(path, timeout=10)
        con.row_factory=sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()
    def row(sql,args=()):
        with connect() as con:
            result=con.execute(sql,args).fetchone()
            return dict(result) if result else None
    def execute(sql,args=()):
        with connect() as con:
            return con.execute(sql,args).lastrowid
    with connect() as con:
        con.executescript('''
        CREATE TABLE saved_views(id INTEGER PRIMARY KEY,seller_id INTEGER,name TEXT,snapshot_path TEXT,updated_at TEXT);
        INSERT INTO saved_views VALUES(1,11,'products','','');
        CREATE TABLE price_lists(id INTEGER PRIMARY KEY,local_path TEXT,file_format TEXT,last_download_at TEXT);
        INSERT INTO price_lists VALUES(1,'','','');
        ''')
    for module in (views,lists):
        monkeypatch.setattr(module,'connect',connect)
        monkeypatch.setattr(module,'row',row)
        monkeypatch.setattr(module,'execute',execute)
    return store, row, execute


def test_corrupt_cache_restores_verified_object(archive,tmp_path):
    meta=files.put_bytes(namespace='files',identity='seller_11',filename='file.csv',content=b'valid')
    cached=tmp_path/'old.csv'
    cached.write_bytes(b'corrupt')
    args=dict(storage_key=meta['storage_key'],expected_sha256=meta['sha256'])
    assert files.read_bytes(local_path=cached,**args)==b'valid'
    restored=files.materialize(namespace='files',identity='seller_11',filename='file.csv',preferred_path=cached,**args)
    assert restored.read_bytes()==b'valid'
    assert meta['sha256'] in str(restored)


def test_corrupt_remote_object_is_never_published(archive,tmp_path):
    store,_,_=archive
    store.put_bytes('bad.csv',b'corrupt')
    with pytest.raises(ValueError,match='SHA-256'):
        files.materialize(namespace='files',identity='11',filename='file.csv',storage_key='bad.csv',expected_sha256='0'*64)
    assert not list(files.CACHE_DIR.rglob('file.csv'))


def test_parallel_cache_writers_restore_complete_file(archive):
    content=b'abc123'*10000
    meta=files.put_bytes(namespace='files',identity='11',filename='file.csv',content=content)
    def restore(_):
        return files.materialize(namespace='files',identity='11',filename='file.csv',storage_key=meta['storage_key'],expected_sha256=meta['sha256'])
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths=list(pool.map(restore,range(8)))
    assert len(set(paths))==1 and paths[0].read_bytes()==content
    assert not list(files.CACHE_DIR.rglob('.mh-*'))


def test_versions_have_distinct_cache_paths(archive):
    paths=[]
    for content in (b'old',b'new'):
        meta=files.put_bytes(namespace='files',identity='11',filename='same.csv',content=content)
        paths.append(files.materialize(namespace='files',identity='11',filename='same.csv',storage_key=meta['storage_key'],expected_sha256=meta['sha256']))
    assert paths[0]!=paths[1]
    assert [p.read_bytes() for p in paths]==[b'old',b'new']


def test_saved_view_restores_after_api_cache_loss(archive,monkeypatch,tmp_path):
    expected=pd.DataFrame({'sku':['001','002'],'price':[4.0,8.5]})
    saved=views.save_saved_view_frame(view_id=1,seller_id=11,name='products',frame=expected)
    Path(saved['path']).unlink()
    monkeypatch.setattr(views,'CACHE_DIR',tmp_path/'fresh-worker-cache')
    pd.testing.assert_frame_equal(views.load_saved_view_frame(1),expected)


def test_saved_view_corrupt_cache_is_repaired(archive):
    expected=pd.DataFrame({'sku':['001']})
    saved=views.save_saved_view_frame(view_id=1,seller_id=11,name='products',frame=expected)
    Path(saved['path']).write_bytes(b'corrupt')
    pd.testing.assert_frame_equal(views.load_saved_view_frame(1),expected)


def test_metadata_failure_preserves_old_saved_view(archive,monkeypatch):
    store,_,_=archive
    expected=pd.DataFrame({'sku':['old']})
    saved=views.save_saved_view_frame(view_id=1,seller_id=11,name='products',frame=expected)
    with monkeypatch.context() as patch:
        def fail(*args):
            raise RuntimeError('database unavailable')
        patch.setattr(views,'execute',fail)
        with pytest.raises(RuntimeError):
            views.save_saved_view_frame(view_id=1,seller_id=11,name='products',frame=pd.DataFrame({'sku':['new']}))
    assert storage.sha256_bytes(store.get_bytes(saved['storage_key']))==saved['sha256']
    pd.testing.assert_frame_equal(views.load_saved_view_frame(1),expected)


def test_wrong_seller_cannot_write_saved_view(archive):
    store,_,_=archive
    with pytest.raises(PermissionError):
        views.save_saved_view_frame(view_id=1,seller_id=22,name='products',frame=pd.DataFrame({'sku':['x']}))
    assert not list(store.root.rglob('*.pkl'))


def test_price_list_restores_latest_and_retains_previous_version(archive,tmp_path):
    store,row,_=archive
    source=tmp_path/'products.csv'
    source.write_bytes(b'sku,price\nold,1\n')
    lists.persist_price_list_path(1,source)
    old=row('SELECT * FROM price_lists WHERE id=1')
    source.write_bytes(b'sku,price\nnew,2\n')
    lists.persist_price_list_path(1,source)
    source.write_bytes(b'wrong cached data')
    assert lists.materialize_price_list(1).read_bytes()==b'sku,price\nnew,2\n'
    assert store.get_bytes(old['storage_key'])==b'sku,price\nold,1\n'
