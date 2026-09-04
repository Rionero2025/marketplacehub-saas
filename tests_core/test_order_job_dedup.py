from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3

import pytest

from marketplace_core.contracts import JobRequest
from services import background_jobs, entitlements


@pytest.fixture
def job_database(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    @contextmanager
    def connect():
        con = sqlite3.connect(path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()
    monkeypatch.setattr(background_jobs, "connect", connect)
    monkeypatch.setattr(background_jobs, "_SCHEMA_READY", False)
    monkeypatch.setenv("MARKETPLACE_HUB_DB_ENGINE", "sqlite")
    monkeypatch.setattr(entitlements, "require_tenant_feature", lambda *a: None)
    monkeypatch.setattr(entitlements, "limit_for", lambda *a: None)
    usage = []
    monkeypatch.setattr(entitlements, "record_usage", lambda *a: usage.append(a))
    background_jobs.ensure_job_schema()
    return connect, usage


def request(tenant="11", seller=110, **payload):
    return JobRequest("orders.kaufland.sync", tenant_id=tenant, seller_id=seller,
                      payload={"account_id": 1, "maximum": 1000, **payload})


def test_concurrent_identical_order_requests_share_one_job(job_database):
    connect, usage = job_database
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: background_jobs.enqueue_job(request()), range(8)))
    assert len({r.job_id for r in receipts}) == 1
    assert len(usage) == 1
    with connect() as con:
        assert con.execute("SELECT count(*) FROM background_jobs").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["queued", "running"])
def test_existing_job_reused_even_at_quota_limit(job_database, monkeypatch, status):
    connect, usage = job_database
    first = background_jobs.enqueue_job(request())
    with connect() as con:
        # Old jobs may serialize keys in a different order/with whitespace.
        con.execute("UPDATE background_jobs SET status=?,payload_json=? WHERE id=?",
                    (status, '{"maximum": 1000, "account_id": 1}', first.job_id))
    monkeypatch.setattr(entitlements, "limit_for", lambda *a: 1)
    monkeypatch.setattr(entitlements, "tenant_resource_usage", lambda *a: pytest.fail("Reused job must not check quota"))
    again = background_jobs.enqueue_job(request())
    assert again.job_id == first.job_id and again.status == status
    assert len(usage) == 1


@pytest.mark.parametrize("status", ["done", "error", "cancelled"])
def test_terminal_job_allows_new_sync(job_database, status):
    connect, usage = job_database
    first = background_jobs.enqueue_job(request())
    with connect() as con:
        con.execute("UPDATE background_jobs SET status=? WHERE id=?", (status, first.job_id))
    assert background_jobs.enqueue_job(request()).job_id != first.job_id
    assert len(usage) == 2


def test_different_tenants_accounts_and_parameters_remain_independent(job_database):
    receipts = [background_jobs.enqueue_job(item) for item in (
        request(), request(tenant="22"), request(seller=111),
        request(account_id=2), request(maximum=2000),
        request(include_tracking_details=False),
        JobRequest("orders.worten.sync", tenant_id="11", seller_id=110, payload={"account_id": 1}),
    )]
    assert len({item.job_id for item in receipts}) == len(receipts)
