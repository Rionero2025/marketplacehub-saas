from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from marketplace_core.contracts import JobRequest
from services import background_jobs, entitlements
from services.postgresql_backend import PostgreSQLCompatConnection
from tests_core.test_postgresql_tenant_boundaries import postgres_database


def test_postgresql_concurrent_order_sync_dedup(postgres_database, monkeypatch):
    @contextmanager
    def connect():
        with postgres_database() as con:
            yield PostgreSQLCompatConnection(con)
    monkeypatch.setattr(background_jobs, "connect", connect)
    monkeypatch.setattr(background_jobs, "_SCHEMA_READY", False)
    monkeypatch.setattr(entitlements, "require_tenant_feature", lambda *a: None)
    monkeypatch.setattr(entitlements, "limit_for", lambda *a: None)
    usage = []
    monkeypatch.setattr(entitlements, "record_usage", lambda *a: usage.append(a))
    background_jobs.ensure_job_schema()
    request = JobRequest("orders.kaufland.sync", tenant_id="11", seller_id=110,
                         payload={"account_id": 1, "maximum": 1000})
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: background_jobs.enqueue_job(request), range(8)))
    assert len({receipt.job_id for receipt in receipts}) == 1
    assert len(usage) == 1
    other = background_jobs.enqueue_job(JobRequest(
        request.kind, tenant_id="22", seller_id=220, payload=request.payload))
    assert other.job_id != receipts[0].job_id
    with connect() as con:
        assert con.execute("SELECT count(*) AS count FROM background_jobs").fetchone()["count"] == 2
