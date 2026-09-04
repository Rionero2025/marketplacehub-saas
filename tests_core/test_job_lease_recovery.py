from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from marketplace_core.contracts import JobRequest
from services import background_jobs as jobs, entitlements
from services.postgresql_backend import PostgreSQLCompatConnection
from tests_core.test_order_job_dedup import job_database
from tests_core.test_postgresql_tenant_boundaries import postgres_database


@pytest.fixture(params=['sqlite', 'postgresql'])
def lease_database(request, monkeypatch):
    if request.param == 'sqlite':
        connect, _ = request.getfixturevalue('job_database')
        return connect
    pg = request.getfixturevalue('postgres_database')
    @contextmanager
    def connect():
        with pg() as con:
            yield PostgreSQLCompatConnection(con)
    monkeypatch.setattr(jobs, 'connect', connect)
    monkeypatch.setattr(jobs, '_SCHEMA_READY', False)
    monkeypatch.setattr(entitlements, 'require_tenant_feature', lambda *a: None)
    monkeypatch.setattr(entitlements, 'limit_for', lambda *a: None)
    monkeypatch.setattr(entitlements, 'record_usage', lambda *a: None)
    jobs.ensure_job_schema()
    return connect


def claimed(connect, kind='orders.kaufland.sync', *, stale=True):
    receipt = jobs.enqueue_job(JobRequest(kind, tenant_id='11', seller_id=110, payload={'account_id':1}))
    job = jobs.claim_job(receipt.job_id, worker_id='old-worker')
    if stale:
        stamp = (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(timespec='seconds')
        with connect() as con:
            con.execute('UPDATE background_jobs SET heartbeat_at=?,started_at=? WHERE id=?', (stamp,stamp,job['id']))
    return job


def state(connect, job):
    with connect() as con:
        return dict(con.execute('SELECT * FROM background_jobs WHERE id=?', (job['id'],)).fetchone())


@pytest.mark.parametrize('kind', ['orders.kaufland.sync', 'orders.worten.sync', 'catalog.materialize'])
def test_stale_idempotent_import_is_requeued(lease_database, kind):
    job = claimed(lease_database, kind)
    assert jobs.recover_stale_jobs() == {'requeued':1, 'review':0}
    current = state(lease_database, job)
    assert current['status'] == 'queued' and current['worker_id'] == ''
    assert jobs.recover_stale_jobs() == {'requeued':0, 'review':0}
    assert jobs.claim_job(job['id'], worker_id='new-worker')['attempts'] == 2


@pytest.mark.parametrize('kind', ['packlink.drafts.mass', 'tracking.documents.analyze'])
def test_uncertain_external_actions_require_review(lease_database, kind):
    job = claimed(lease_database, kind)
    assert jobs.recover_stale_jobs() == {'requeued':0, 'review':1}
    assert state(lease_database, job)['status'] == 'error'
    assert 'verificare' in state(lease_database, job)['error']


def test_attempt_limit_stops_automatic_requeue(lease_database):
    job = claimed(lease_database)
    with lease_database() as con:
        con.execute('UPDATE background_jobs SET attempts=3 WHERE id=?', (job['id'],))
    assert jobs.recover_stale_jobs() == {'requeued':0, 'review':1}


def test_fresh_heartbeat_prevents_false_recovery(lease_database):
    job = claimed(lease_database)
    assert jobs.heartbeat_job(job)
    assert jobs.recover_stale_jobs() == {'requeued':0, 'review':0}
    assert state(lease_database, job)['status'] == 'running'


def test_stale_worker_cannot_overwrite_new_claim(lease_database):
    old = claimed(lease_database)
    jobs.recover_stale_jobs()
    new = jobs.claim_job(old['id'], worker_id='new-worker')
    before = state(lease_database, new)
    jobs.complete_job(old['id'], {'wrong':True}, claim=old)
    jobs.fail_job(old['id'], 'stale error', claim=old)
    jobs.update_job_progress(old['id'], 9, 10, 'stale progress', claim=old)
    assert not jobs.heartbeat_job(old)
    assert state(lease_database, new) == before
    jobs.complete_job(new['id'], {'saved':1}, claim=new)
    assert state(lease_database, new)['status'] == 'done'


def test_independent_heartbeat_runs_during_silent_handler(lease_database, monkeypatch):
    job = claimed(lease_database, stale=False)
    pulsed = Event()
    original = jobs.heartbeat_job
    def heartbeat(current):
        result = original(current)
        pulsed.set()
        return result
    def handler(current):
        assert pulsed.wait(timeout=5), 'No heartbeat while handler was busy'
        return {'saved':1}
    monkeypatch.setattr(jobs, 'heartbeat_job', heartbeat)
    monkeypatch.setattr(jobs, 'execute_claimed_job', handler)
    jobs._run_claimed(job, heartbeat_interval=0.01)
    assert state(lease_database, job)['status'] == 'done'


def test_concurrent_recovery_requeues_once(lease_database):
    job = claimed(lease_database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: jobs.recover_stale_jobs(), range(2)))
    assert sum(result['requeued'] for result in results) == 1
    assert sum(result['review'] for result in results) == 0
    assert state(lease_database, job)['status'] == 'queued'


def test_recovery_honors_worker_kind_scope(lease_database):
    order = claimed(lease_database)
    catalog = claimed(lease_database, 'catalog.materialize')
    assert jobs.recover_stale_jobs(kind_prefix='orders.') == {'requeued':1, 'review':0}
    assert state(lease_database, order)['status'] == 'queued'
    assert state(lease_database, catalog)['status'] == 'running'


def test_handler_failure_keeps_current_claim_error(lease_database, monkeypatch):
    job = claimed(lease_database, stale=False)
    def handler(current):
        raise ValueError('Import source unavailable')
    monkeypatch.setattr(jobs, 'execute_claimed_job', handler)
    jobs._run_claimed(job, heartbeat_interval=0.01)
    current = state(lease_database, job)
    assert current['status'] == 'error'
    assert current['error'] == 'Import source unavailable'
    assert not jobs.heartbeat_job(job)
