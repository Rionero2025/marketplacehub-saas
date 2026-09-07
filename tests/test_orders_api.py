import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import test_tenancy_api
from fastapi.testclient import TestClient
from marketplace_hub_api.main import create_app
from marketplace_hub_core.marketplace_connections.repository import (
    SqlMarketplaceConnectionsRepository,
)
from marketplace_hub_core.orders.repository import SqlOrdersRepository
from marketplace_hub_core.orders.schema import order_lines, order_sync_jobs
from marketplace_hub_core.orders.service import OrdersService
from marketplace_hub_core.seller_settings.security import encrypt_credentials
from marketplace_hub_core.settings import Settings
from marketplace_hub_core.tenancy.schema import memberships
from pydantic import SecretStr
from sqlalchemy import select
from test_seller_settings_api import TEST_MASTER
from test_seller_settings_api import owner as settings_owner

workspace = test_tenancy_api.workspace


class FakeQueue:
    def __init__(self):
        self.jobs = {}
        self.fail = False

    def enqueue(self, job_id):
        if self.fail:
            raise RuntimeError("redis://private-password@host")
        self.jobs[str(job_id)] = "queued"

    def state(self, job_id):
        return self.jobs.get(str(job_id), "missing")


def row(identifier="line-1", **extra):
    return {"external_line_id": identifier, "order_id": "ORDER-1", "marketplace": "kaufland",
            "created_at": "2026-09-07T10:30:00+00:00", "status": "sent", "status_label": "Spedito",
            "storefront": "de", "currency": "EUR", "product_name": "Prodotto test",
            "ean": "1234567890123", "sku": "supplier_1234567890123_10_15", "quantity": "1",
            "sale_amount": "20.00", "sale_amount_eur": "20.00", "commission_amount": "2.00",
            "purchase_cost": "10.00", "profit_amount": "8.00", "monetary_warnings": [],
            "details": {"tracking": "tracking-test", "api_key": "must-not-escape"},
            "raw": {"customer": {"private": "private-raw-not-public"}}, **extra}


class FakeFetcher:
    def __init__(self):
        self.items = [row()]
        self.calls = []
        self.before_batch = None
        self.failure = None
        self.summary = {}

    async def __call__(self, marketplace, credentials, *, environment, maximum, include_details,
                       on_batch, on_progress, before_request):
        self.calls.append((marketplace, credentials, environment, maximum, include_details))
        await before_request()
        await on_progress("downloading")
        if self.before_batch:
            self.before_batch()
        if self.failure:
            raise self.failure
        await on_batch(self.items, len(self.items), len(self.items))
        return self.summary


@pytest.fixture
def configured(workspace):
    workspace.queue = FakeQueue()
    workspace.fetcher = FakeFetcher()
    workspace.order_repository = SqlOrdersRepository(workspace.engine)
    workspace.account_repository = SqlMarketplaceConnectionsRepository(workspace.engine)
    workspace.orders = OrdersService(workspace.order_repository, workspace.service,
                                     workspace.account_repository, workspace.queue,
                                     SecretStr(TEST_MASTER), workspace.fetcher)
    workspace.settings = Settings(environment="test", master_key=TEST_MASTER, _env_file=None)
    workspace.app = create_app(settings=workspace.settings, auth_service=workspace.auth,
                               workspace_service=workspace.service, orders_service=workspace.orders,
                               readiness_checks={"test": lambda: None})
    return workspace


def owner(configured, **options):
    return settings_owner(configured, permission_codes=("WORKSPACE_VIEW", "WORKSPACE_MANAGE",
                                                        "LOGISTICS"), **options)


def account(configured, seller, organization, marketplace="kaufland", status="connected"):
    encrypted = encrypt_credentials({"client_key": "client-test", "secret_key": "secret-test"},
                                     SecretStr(TEST_MASTER))
    configured.account_repository.add(seller, organization, marketplace, str(uuid4()), encrypted,
                                      {"connection_status": status})
    return configured.account_repository.list(seller, organization)[-1]["id"]


def path(seller, suffix=""):
    return f"/v1/sellers/{seller}/orders{suffix}"


def start(client, seller, account_id, **options):
    return client.post(path(seller, "/sync"), json={"account_id": str(account_id), **options})


def read(client, seller, account_id, **options):
    return client.get(path(seller), params={"account_id": str(account_id), **options})


def run(configured, response):
    job_id = UUID(response.json()["job"]["id"])
    asyncio.run(configured.orders.run_job(job_id))
    return job_id


def test_all_orders_routes_require_auth_without_dispatch(configured):
    client = TestClient(configured.app)
    seller, account_id = uuid4(), uuid4()
    assert read(client, seller, account_id).status_code == 401
    assert start(client, seller, account_id).status_code == 401
    assert client.get(path(seller, f"/jobs/{uuid4()}")).status_code == 401
    assert client.get(path(seller, f"/{uuid4()}"),
                      params={"account_id": str(account_id)}).status_code == 401
    assert configured.queue.jobs == {}


def test_queue_then_worker_persists_decimal_fields_and_never_public_raw(configured):
    client, seller, organization, _, user = owner(configured)
    account_id = account(configured, seller, organization)
    started = start(client, seller, account_id)
    assert started.status_code == 202 and started.json()["job"]["status"] == "queued"
    assert started.headers["cache-control"] == "no-store"
    assert read(client, seller, account_id).json()["items"] == []
    job_id = run(configured, started)
    refreshed, _ = configured.session(user)
    response = read(refreshed, seller, account_id)
    assert response.status_code == 200 and response.json()["can_sync"] is True
    data = response.json()
    assert data["total"] == 1 and data["latest_job"]["status"] == "done"
    assert data["latest_job"]["progress"] == 100 and data["latest_job"]["processed"] == 1
    item = data["items"][0]
    assert item["sale_amount"] == item["sale_amount_eur"] == "20.00"
    assert item["details"] == {"tracking": "tracking-test"}
    assert "private-raw" not in response.text and "must-not-escape" not in response.text
    assert "secret-test" not in response.text and "client-test" not in response.text
    detail = client.get(path(seller, f"/{item['id']}"), params={"account_id": str(account_id)})
    assert detail.json()["item"] == item
    assert client.get(path(seller, f"/jobs/{job_id}")).json()["job"]["status"] == "done"
    with configured.engine.connect() as connection:
        saved_job = connection.execute(select(order_sync_jobs)).mappings().one()
        saved_line = connection.execute(select(order_lines)).mappings().one()
    assert "secret-test" not in str(dict(saved_job))
    assert "private-raw" in saved_line["raw_json"]
    assert saved_line["organization_id"] == organization
    assert configured.fetcher.calls[0][1]["secret_key"] == "secret-test"


def test_duplicate_active_sync_reused_and_upsert_keeps_history(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    first = start(client, seller, account_id)
    again = start(client, seller, account_id, maximum=5000)
    assert first.json() == again.json() and len(configured.queue.jobs) == 1
    configured.fetcher.items = [row(), row("line-2", product_name="Altro prodotto")]
    run(configured, first)
    original = read(client, seller, account_id).json()["items"]
    configured.fetcher.items = [row(sale_amount="25.50", status="returned")]
    second = start(client, seller, account_id, maximum=500)
    run(configured, second)
    updated = read(client, seller, account_id).json()["items"]
    assert len(updated) == 2
    by_id = {item["external_line_id"]: item for item in updated}
    assert by_id["line-1"]["id"] == original[0]["id"]
    assert by_id["line-1"]["sale_amount"] == "25.50"
    assert by_id["line-2"]["product_name"] == "Altro prodotto"


def test_limits_and_environment_are_original_options_and_isolate_cache(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    for maximum in (500, 1000, 5000, None):
        response = start(client, seller, account_id, maximum=maximum)
        run(configured, response)
        assert configured.fetcher.calls[-1][3] == maximum
    assert start(client, seller, account_id, maximum=123).status_code == 422
    assert start(client, seller, account_id, maximum=True).status_code == 422
    sandbox = start(client, seller, account_id, environment="playground", include_details=False)
    configured.fetcher.items = [row(product_name="Playground")]
    run(configured, sandbox)
    assert read(client, seller, account_id, environment="playground").json()["items"][0][
        "product_name"] == "Playground"
    assert read(client, seller, account_id).json()["items"][0]["product_name"] == "Prodotto test"
    assert configured.fetcher.calls[-1][4] is False


def test_worten_live_only_and_unverified_accounts_cannot_dispatch(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization, "worten")
    assert start(client, seller, account_id, environment="playground").status_code == 422
    assert start(client, seller, account_id).status_code == 202
    other_seller = configured.seller(organization)
    unverified = account(configured, other_seller, organization, status="unverified")
    assert start(client, other_seller, unverified).status_code == 422
    assert read(client, other_seller, unverified).json()["can_sync"] is False


def test_foreign_accounts_jobs_and_lines_never_cross_scope(configured):
    client, seller, organization, _, _ = owner(configured)
    foreign, foreign_seller, foreign_org, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    foreign_account = account(configured, foreign_seller, foreign_org)
    result = start(client, seller, account_id)
    job_id = run(configured, result)
    line_id = read(client, seller, account_id).json()["items"][0]["id"]
    assert read(foreign, seller, account_id).status_code == 404
    assert read(client, seller, foreign_account).status_code == 404
    assert start(foreign, foreign_seller, account_id).status_code == 404
    assert foreign.get(path(foreign_seller, f"/jobs/{job_id}")).status_code == 404
    assert foreign.get(path(foreign_seller, f"/{line_id}"),
                       params={"account_id": str(foreign_account)}).status_code == 404


def test_logistics_permission_required_and_readonly_cannot_sync(configured):
    client, seller, organization, membership, _ = owner(configured, read_only=True)
    account_id = account(configured, seller, organization)
    assert read(client, seller, account_id).status_code == 200
    assert read(client, seller, account_id).json()["can_sync"] is False
    assert start(client, seller, account_id).status_code == 403
    denied, denied_seller, denied_org, _, _ = settings_owner(configured)
    denied_account = account(configured, denied_seller, denied_org)
    assert read(denied, denied_seller, denied_account).status_code == 403
    assert start(denied, denied_seller, denied_account).status_code == 403


@pytest.mark.parametrize("when", ["before_request", "before_save"])
def test_permission_revocation_stops_background_job_and_writes(configured, when):
    client, seller, organization, membership, _ = owner(configured)
    account_id = account(configured, seller, organization)
    started = start(client, seller, account_id)

    def revoke():
        with configured.engine.begin() as connection:
            connection.execute(memberships.update().where(memberships.c.id == membership)
                               .values(active=False))

    if when == "before_request":
        revoke()
    else:
        configured.fetcher.before_batch = revoke
    job_id = run(configured, started)
    job = configured.order_repository.job(job_id)
    assert job["status"] == "error" and job["error_code"] == "permission_revoked"
    with configured.engine.connect() as connection:
        assert connection.execute(select(order_lines)).first() is None
    if when == "before_request":
        assert configured.fetcher.calls == []


def test_deleted_account_stops_queued_job_and_hides_history(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    first = start(client, seller, account_id)
    run(configured, first)
    second = start(client, seller, account_id)
    configured.account_repository.delete(seller, organization, account_id)
    job_id = run(configured, second)
    assert configured.order_repository.job(job_id)["error_code"] == "account_unavailable"
    assert read(client, seller, account_id).status_code == 404
    with configured.engine.connect() as connection:
        assert connection.execute(select(order_lines)).first() is not None


def test_queue_failure_is_clear503_with_failed_durable_job(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    configured.queue.fail = True
    response = start(client, seller, account_id)
    assert response.status_code == 503 and "private-password" not in response.text
    latest = read(client, seller, account_id).json()["latest_job"]
    assert latest["status"] == "error" and latest["error_code"] == "queue_unavailable"
    assert configured.fetcher.calls == []


@pytest.mark.parametrize("state", ["missing", "failed", "stopped", "stale", "finished"])
def test_lost_or_terminated_rq_jobs_recovered_and_can_be_restarted(configured, state):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    response = start(client, seller, account_id)
    job_id = UUID(response.json()["job"]["id"])
    configured.queue.jobs[str(job_id)] = state
    with configured.engine.begin() as connection:
        connection.execute(order_sync_jobs.update().where(order_sync_jobs.c.id == job_id).values(
            created_at=datetime.now(UTC) - timedelta(minutes=2),
        ))
    recovered = client.get(path(seller, f"/jobs/{job_id}"))
    assert recovered.json()["job"]["error_code"] == "worker_interrupted"
    retried = start(client, seller, account_id)
    assert retried.status_code == 202 and retried.json()["job"]["id"] != str(job_id)


def test_missing_new_enqueue_has_grace_period_and_finished_job_not_run_twice(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    started = start(client, seller, account_id)
    job_id = UUID(started.json()["job"]["id"])
    configured.queue.jobs.clear()
    assert client.get(path(seller, f"/jobs/{job_id}")).json()["job"]["status"] == "queued"
    run(configured, started)
    run(configured, started)
    assert len(configured.fetcher.calls) == 1


def test_filters_pagination_search_literal_and_original_utc_calendar_dates(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    configured.fetcher.items = [
        row("first", product_name="100% prodotto", created_at="2026-09-06T22:30:00Z"),
        row("second", product_name="Qualcosa", status="returned", storefront="at"),
        row("third", created_at="2026-09-07T22:30:00Z"),
    ]
    run(configured, start(client, seller, account_id))
    filtered = read(client, seller, account_id, search="%", date_from="2026-09-06",
                    date_to="2026-09-06").json()
    assert filtered["total"] == 1 and filtered["items"][0]["external_line_id"] == "first"
    assert read(client, seller, account_id, date_from="2026-09-07",
                date_to="2026-09-07").json()["total"] == 2
    assert read(client, seller, account_id, date_to="9999-12-31").status_code == 422
    assert read(client, seller, account_id, status="returned", storefront="at").json()["total"] == 1
    paged = read(client, seller, account_id, page=2, page_size=1).json()
    assert paged["total"] == 3 and len(paged["items"]) == 1
    assert paged["filters"] == {"statuses": ["returned", "sent"], "storefronts": ["at", "de"]}
    assert read(client, seller, account_id, page_size=1000).status_code == 422


def test_same_line_id_in_distinct_orders_is_not_overwritten(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization, marketplace="worten")
    configured.fetcher.items = [
        row("1", order_id="W1", marketplace="worten"),
        row("1", order_id="W2", marketplace="worten"),
    ]
    run(configured, start(client, seller, account_id))
    original = read(client, seller, account_id).json()["items"]
    assert {item["order_id"] for item in original} == {"W1", "W2"}
    configured.fetcher.items = [row("1", order_id="W1", marketplace="worten", sale_amount="30.00")]
    run(configured, start(client, seller, account_id))
    by_order = {item["order_id"]: item for item in read(client, seller, account_id).json()["items"]}
    assert len(by_order) == 2
    assert by_order["W1"]["sale_amount"] == "30.00"
    assert by_order["W2"]["sale_amount"] == "20.00"


def test_resync_preserves_tracking_and_verification_not_old_economics_or_event_dates(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    checked_at = "2026-09-07T08:00:00+00:00"
    configured.fetcher.items = [row(details={
        "tracking": "TRACK-ORIGINAL", "carrier": "DHL", "detail_checked_at": checked_at,
        "received_at": "2026-09-06T08:00:00+00:00", "received_source": "API",
    })]
    run(configured, start(client, seller, account_id))
    configured.fetcher.items = [row(
        sale_amount=None, commission_amount=None, purchase_cost=None, profit_amount=None,
        details={"tracking": "", "carrier": None, "received_at": None, "received_source": ""},
        raw={},
    )]
    run(configured, start(client, seller, account_id))
    response = read(client, seller, account_id, search="track-original").json()
    assert response["total"] == 1
    saved = response["items"][0]
    assert saved["details"]["tracking"] == "TRACK-ORIGINAL"
    assert saved["details"]["carrier"] == "DHL"
    assert saved["details"]["detail_checked_at"] == checked_at
    assert saved["details"]["received_at"] is None and saved["details"]["received_source"] == ""
    for field in ("sale_amount", "commission_amount", "purchase_cost", "profit_amount"):
        assert saved[field] is None
    assert read(client, seller, account_id, search="dhl").json()["total"] == 1
    run(configured, start(client, seller, account_id, environment="playground"))
    sandbox = read(client, seller, account_id, environment="playground").json()["items"][0]
    assert not sandbox["details"].get("tracking")
    assert not sandbox["details"].get("detail_checked_at")
    configured.fetcher.items = [row(details={
        "tracking": "TRACK-NEW", "carrier": "GLS", "detail_checked_at": "2026-09-08T08:00:00Z",
    })]
    run(configured, start(client, seller, account_id))
    assert read(client, seller, account_id, search="TRACK-ORIGINAL").json()["total"] == 0
    assert read(client, seller, account_id, search="GLS").json()["total"] == 1


def test_worker_errors_safe_and_prior_cache_retained(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    run(configured, start(client, seller, account_id))
    configured.fetcher.failure = RuntimeError("api-key=private-secret; customer-private")
    run(configured, start(client, seller, account_id))
    response = read(client, seller, account_id)
    assert response.json()["total"] == 1
    assert response.json()["latest_job"]["status"] == "error"
    assert "private-secret" not in response.text and "customer-private" not in response.text


def test_worker_completes_with_visible_warning_and_details_summary(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    configured.fetcher.items = [row(monetary_warnings=["Costo di acquisto non disponibile."])]
    configured.fetcher.summary = {"warning_count": 1, "details_checked": 2}
    run(configured, start(client, seller, account_id))
    latest = read(client, seller, account_id).json()["latest_job"]
    assert latest["status"] == "done" and "1 righe con avvisi" in latest["message"]
    assert "Dettagli verificati: 2" in latest["message"]


def test_real_connector_normalizer_and_worker_integrate_without_network(configured):
    from marketplace_hub_core.orders.connectors import OrdersConnector

    client, seller, organization, _, _ = owner(configured)
    account_id = account(configured, seller, organization)
    seen_statuses = []

    def handler(request):
        if request.url.host == "www.ecb.europa.eu":
            return httpx.Response(503)
        assert request.headers["shop-client-key"] == "client-test"
        assert request.url.path == "/v2/order-units"
        status = request.url.params["status"]
        seen_statuses.append(status)
        units = [{
            "id_order_unit": 1, "id_order": "O1", "status": "open", "storefront": "de",
            "price": 2000, "revenue_gross": 1800,
            "product": {"title": "Prodotto reale normalizzato", "eans": ["1234567890123"]},
            "id_offer": "supplier_1234567890123_10_15", "ts_created_iso": "2026-09-07T10:00:00Z",
        }] if status == "open" else []
        return httpx.Response(200, json={"data": units})

    async def no_wait(seconds):
        return None

    configured.orders.fetcher = OrdersConnector(
        transport=httpx.MockTransport(handler), pause=no_wait,
    ).fetch_orders
    run(configured, start(client, seller, account_id, include_details=False))
    result = read(client, seller, account_id).json()
    assert result["latest_job"]["status"] == "done"
    assert len(seen_statuses) == 8 and result["total"] == 1
    assert result["items"][0]["product_name"] == "Prodotto reale normalizzato"
    assert result["items"][0]["external_line_id"] == "1"
