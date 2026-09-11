from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
import test_tenancy_api
from marketplace_hub_api.main import create_app
from marketplace_hub_core.catalogs import queue as catalog_queue
from marketplace_hub_core.catalogs.queue import RQCatalogsQueue
from marketplace_hub_core.catalogs.repository import SqlCatalogsRepository
from marketplace_hub_core.catalogs.schema import seller_price_list_refresh_jobs
from marketplace_hub_core.catalogs.service import JOB_MESSAGES, CatalogsService
from marketplace_hub_core.settings import Settings
from pydantic import SecretStr

workspace = test_tenancy_api.workspace


class RecoverableQueue:
    def __init__(self) -> None:
        self.jobs: list[UUID] = []
        self.states: dict[UUID, str] = {}
        self.state_error = False

    def enqueue(self, job_id: UUID) -> None:
        self.jobs.append(job_id)

    def state(self, job_id: UUID | str) -> str:
        if self.state_error:
            raise RuntimeError("temporary redis detail")
        return self.states.get(UUID(str(job_id)), "queued")


@pytest.fixture
def configured(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    queue = RecoverableQueue()
    settings = Settings(
        environment="test",
        master_key=SecretStr("catalog-recovery-test-key"),
        _env_file=None,
    )
    catalogs = CatalogsService(
        repository,
        workspace.service,
        queue,
        settings.master_key,
    )
    app = create_app(
        settings=settings,
        readiness_checks={"fixture": lambda: None},
        auth_service=workspace.auth,
        workspace_service=workspace.service,
        catalogs_service=catalogs,
    )
    workspace.settings = settings
    workspace.catalogs = catalogs
    workspace.catalog_repository = repository
    workspace.catalog_queue = queue
    workspace.app = app
    return workspace


def _owner(configured):
    user = configured.user()
    organization_id = configured.organization("SELLER")
    seller_id = configured.seller(organization_id)
    configured.membership(
        user,
        organization_id,
        "SELLER_OWNER",
        permission_codes=("WORKSPACE_VIEW", "CATALOG"),
    )
    client, _ = configured.session(user)
    return client, seller_id, organization_id


def _catalog_path(seller_id, suffix="") -> str:
    return f"/v1/sellers/{seller_id}/catalogs{suffix}"


def _create_feed(configured):
    client, seller_id, organization_id = _owner(configured)
    supplier = client.post(
        _catalog_path(seller_id, "/suppliers"),
        json={"name": "InnPro", "notes": ""},
    )
    assert supplier.status_code == 201
    created = client.post(
        _catalog_path(seller_id, "/price-lists/url"),
        json={
            "supplier_id": supplier.json()["created_supplier_id"],
            "name": "Feed InnPro",
            "url": "https://feeds.example.com/catalog.csv?token=private",
            "username": "",
            "password": "",
        },
    )
    assert created.status_code == 202
    return client, seller_id, organization_id, created.json()


@pytest.mark.parametrize(
    "rq_state",
    ["failed", "stopped", "canceled", "finished", "stale"],
)
def test_dashboard_recovers_terminal_rq_job(configured, rq_state: str):
    client, seller_id, _, created = _create_feed(configured)
    job_id = UUID(created["job"]["id"])
    configured.catalog_queue.states[job_id] = rq_state

    dashboard = client.get(_catalog_path(seller_id))

    assert dashboard.status_code == 200
    job = dashboard.json()["price_lists"][0]["latest_job"]
    assert job["status"] == "error"
    assert job["error_code"] == "worker_interrupted"
    assert job["message"] == JOB_MESSAGES["worker_interrupted"]


def test_job_read_recovers_interrupted_worker(configured):
    client, seller_id, _, created = _create_feed(configured)
    price_list_id = created["price_list"]["id"]
    job_id = UUID(created["job"]["id"])
    configured.catalog_queue.states[job_id] = "finished"

    response = client.get(
        _catalog_path(
            seller_id,
            f"/price-lists/{price_list_id}/jobs/{job_id}",
        )
    )

    assert response.status_code == 200
    assert response.json()["job"]["status"] == "error"
    assert response.json()["job"]["error_code"] == "worker_interrupted"


def test_refresh_releases_old_missing_job_only_after_grace(configured):
    client, seller_id, organization_id, created = _create_feed(configured)
    price_list_id = created["price_list"]["id"]
    old_job_id = UUID(created["job"]["id"])
    configured.catalog_queue.states[old_job_id] = "missing"

    immediate = client.post(
        _catalog_path(seller_id, f"/price-lists/{price_list_id}/refresh"),
        json={},
    )
    assert immediate.status_code == 202
    assert immediate.json()["job"]["id"] == str(old_job_id)
    assert configured.catalog_queue.jobs == [old_job_id]

    with configured.engine.begin() as connection:
        connection.execute(
            seller_price_list_refresh_jobs.update()
            .where(
                seller_price_list_refresh_jobs.c.id == old_job_id,
                seller_price_list_refresh_jobs.c.organization_id == organization_id,
                seller_price_list_refresh_jobs.c.seller_id == seller_id,
            )
            .values(created_at=datetime.now(UTC) - timedelta(seconds=31))
        )

    recovered = client.post(
        _catalog_path(seller_id, f"/price-lists/{price_list_id}/refresh"),
        json={},
    )

    assert recovered.status_code == 202
    new_job_id = UUID(recovered.json()["job"]["id"])
    assert new_job_id != old_job_id
    assert configured.catalog_queue.jobs == [old_job_id, new_job_id]
    old_job = configured.catalog_repository.job(
        organization_id,
        seller_id,
        old_job_id,
    )
    assert old_job["status"] == "error"
    assert old_job["error_code"] == "worker_interrupted"


def test_refresh_does_not_guess_when_redis_state_is_unavailable(configured):
    client, seller_id, _, created = _create_feed(configured)
    price_list_id = created["price_list"]["id"]
    job_id = UUID(created["job"]["id"])
    configured.catalog_queue.state_error = True

    response = client.post(
        _catalog_path(seller_id, f"/price-lists/{price_list_id}/refresh"),
        json={},
    )

    assert response.status_code == 202
    assert response.json()["job"]["id"] == str(job_id)
    assert configured.catalog_queue.jobs == [job_id]


def _queue_adapter(connection=object()) -> RQCatalogsQueue:
    adapter = object.__new__(RQCatalogsQueue)
    adapter.connection = connection
    return adapter


def test_rq_catalog_queue_state_uses_prefixed_job_id(monkeypatch):
    connection = object()
    captured = {}

    def fetch(job_id, *, connection):
        captured.update(job_id=job_id, connection=connection)
        return SimpleNamespace(
            get_status=lambda refresh: SimpleNamespace(value="queued"),
            last_heartbeat=None,
        )

    monkeypatch.setattr(catalog_queue.Job, "fetch", fetch)

    assert _queue_adapter(connection).state(UUID(int=12)) == "queued"
    assert captured == {
        "job_id": f"catalog-feed-{UUID(int=12)}",
        "connection": connection,
    }


def test_rq_catalog_queue_reports_missing_and_stale(monkeypatch):
    def missing(*_args, **_kwargs):
        raise catalog_queue.NoSuchJobError

    monkeypatch.setattr(catalog_queue.Job, "fetch", missing)
    assert _queue_adapter().state(UUID(int=13)) == "missing"

    stale_job = SimpleNamespace(
        get_status=lambda refresh: SimpleNamespace(value="started"),
        last_heartbeat=datetime.now(UTC) - timedelta(minutes=6),
    )
    monkeypatch.setattr(
        catalog_queue.Job,
        "fetch",
        lambda *_args, **_kwargs: stale_job,
    )
    assert _queue_adapter().state(UUID(int=14)) == "stale"
