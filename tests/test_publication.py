import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import test_catalog_work as work
from fastapi.testclient import TestClient
from marketplace_hub_core.publication.connectors import PublicationConnector, RemoteFailure
from marketplace_hub_core.publication.models import Rules
from marketplace_hub_core.publication.pricing import prepare, worten_csv
from marketplace_hub_core.publication.schema import items, jobs
from marketplace_hub_core.publication.service import PublicationError, PublicationService
from marketplace_hub_core.seller_settings.schema import seller_marketplace_accounts
from marketplace_hub_core.seller_settings.security import encrypt_credentials
from pydantic import SecretStr
from sqlalchemy import update

workspace = work.workspace
configured = work.configured
KEY = SecretStr("publication-test-only-master")


class Queue:
    def __init__(self):
        self.calls = []

    def enqueue(self, job_id):
        self.calls.append(job_id)


def setup(configured, connector=None):
    client, seller, org, recipe, account, user = work.setup(configured, 3)
    view = client.post(
        work.root(seller) + "/views",
        json={
            "recipe": recipe,
            "name": "Pubblicazione",
            "account_ids": [account],
        },
    )
    assert view.status_code == 201, view.text
    _, principal = configured.session(user)
    with configured.engine.begin() as c:
        c.execute(
            update(seller_marketplace_accounts)
            .where(seller_marketplace_accounts.c.id == UUID(account))
            .values(
                credentials_encrypted=encrypt_credentials(
                    {"client_key": "client", "secret_key": "secret"}, KEY
                )
            )
        )
    rules = Rules(
        account_id=account,
        view_id=view.json()["id"],
        revision=1,
        shipping_group="12",
        warehouse="34",
        composite_sku=True,
    )
    queue = Queue()
    service = PublicationService(configured.engine, configured.service, KEY, queue, connector)
    return service, principal, seller, rules, client


def test_preview_submission_worker_receipt_and_duplicate_delivery(configured):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "sellerapi-playground.kaufland.com"
        return httpx.Response(201, json={"data": {"id_unit": 123}})

    service, principal, seller, rules, client = setup(
        configured, PublicationConnector(httpx.MockTransport(handle))
    )
    job = service.preview(principal, seller, rules)
    assert job["status"] == "draft" and not calls
    assert job["rows"][0]["price"] == "14.85"  # (10 + 1 shipping) * 1.35
    assert "credentials" not in json.dumps(job) and "payload_json" not in json.dumps(job)
    chosen = [UUID(r["id"]) for r in job["rows"][:2]]
    service.submit(principal, seller, UUID(job["id"]), chosen)
    assert not calls and len(service.queue.calls) == 1
    with pytest.raises(PublicationError):
        service.submit(principal, seller, UUID(job["id"]), chosen)
    service.run(UUID(job["id"]))
    service.run(UUID(job["id"]))
    result = service.detail(principal, seller, UUID(job["id"]))
    assert result["status"] == "finished"
    assert result["counts"] == {"accepted": 2, "skipped": 1}
    assert len(calls) == 2
    assert json.loads(calls[0].content)["listing_price"] == 1485


def test_timeout_not_retried_resume_excludes_uncertain_rows(configured):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("ambiguous", request=request)
        return httpx.Response(201, json={"data": {}})

    service, principal, seller, rules, _ = setup(
        configured, PublicationConnector(httpx.MockTransport(handle))
    )
    j = service.preview(principal, seller, rules)
    job_id = UUID(j["id"])
    service.submit(principal, seller, job_id, [UUID(r["id"]) for r in j["rows"]])
    service.run(job_id)
    result = service.detail(principal, seller, job_id)
    assert result["status"] == "interrupted" and result["counts"] == {"unknown": 1, "pending": 2}
    pending = [UUID(r["id"]) for r in result["rows"] if r["status"] == "pending"]
    service.submit(principal, seller, job_id, pending)
    service.run(job_id)
    assert len(calls) == 3
    assert service.detail(principal, seller, job_id)["counts"] == {"unknown": 1, "accepted": 2}


def test_stale_running_recovery_and_old_execution_fenced(configured):
    service, principal, seller, rules, _ = setup(configured)
    j = service.preview(principal, seller, rules)
    job_id = UUID(j["id"])
    service.submit(principal, seller, job_id, [UUID(r["id"]) for r in j["rows"]])
    with configured.engine.begin() as c:
        c.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(status="running", updated_at=datetime.now(UTC) - timedelta(minutes=4))
        )
        c.execute(
            update(items).where(items.c.id == UUID(j["rows"][0]["id"])).values(status="sending")
        )
    recovered = service.detail(principal, seller, job_id)
    assert recovered["status"] == "interrupted" and recovered["counts"] == {
        "unknown": 1,
        "pending": 2,
    }


def test_account_disabled_after_submit_never_sends(configured):
    def handle(request):
        pytest.fail("Revoked account must not make external request")

    service, principal, seller, rules, _ = setup(
        configured, PublicationConnector(httpx.MockTransport(handle))
    )
    j = service.preview(principal, seller, rules)
    job_id = UUID(j["id"])
    service.submit(principal, seller, job_id, [UUID(j["rows"][0]["id"])])
    with configured.engine.begin() as c:
        c.execute(
            update(seller_marketplace_accounts)
            .where(seller_marketplace_accounts.c.id == rules.account_id)
            .values(active=False)
        )
    service.run(job_id)
    assert service.detail(principal, seller, job_id)["status"] == "interrupted"


def test_api_auth_scope_confirmation_and_stale_view(configured):
    service, principal, seller, rules, client = setup(configured)
    path = f"/v1/sellers/{seller}/publication"
    anonymous = TestClient(configured.app)
    assert anonymous.post(path + "/preview", content="bad").status_code == 401
    response = client.post(path + "/preview", json=rules.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    j = response.json()
    assert (
        client.post(
            path + f"/jobs/{j['id']}/submit",
            json={"confirmation": "WRONG", "selected": [j["rows"][0]["id"]]},
        ).status_code
        == 422
    )
    other, other_seller, _, _, _, _ = work.setup(configured)
    assert other.get(path).status_code == 404
    assert client.get(f"/v1/sellers/{other_seller}/publication/jobs/{j['id']}").status_code == 404
    changed = client.put(
        work.root(seller) + f"/views/{rules.view_id}",
        json={"name": "Changed", "account_ids": [str(rules.account_id)], "expected_revision": 1},
    )
    assert changed.status_code == 200
    with pytest.raises(PublicationError, match="modificate"):
        service.submit(principal, seller, UUID(j["id"]), [UUID(j["rows"][0]["id"])])


def test_api_read_only_cannot_prepare_or_submit(configured):
    client, seller, _, _ = work.api.owner(configured, read_only=True)
    path = f"/v1/sellers/{seller}/publication"
    assert client.get(path).status_code == 200
    assert client.post(path + "/preview", content="bad").status_code == 403


def test_signing_exact_body_and_worten_multipart():
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(201, json={"import_id": 456})

    connector = PublicationConnector(httpx.MockTransport(handle))
    rules = Rules(account_id=uuid4(), view_id=uuid4(), revision=1)
    connector.send(
        "kaufland",
        {"client_key": "key", "secret_key": "secret"},
        rules,
        [{"ean": "123", "name": "caffè"}],
    )
    r = captured[-1]
    signed = "\n".join([r.method, str(r.url), r.content.decode(), r.headers["Shop-Timestamp"]])
    assert (
        r.headers["Shop-Signature"]
        == hmac.new(b"secret", signed.encode(), hashlib.sha256).hexdigest()
    )
    assert connector.send(
        "worten", {"api_key": "key", "shop_id": "42"}, rules, [{"sku": "A", "price": "10.50"}]
    ) == ("submitted", "import_456")
    r = captured[-1]
    assert r.url.params["import_mode"] == "NORMAL" and r.url.params["shop_id"] == "42"
    assert b'name="file"' in r.content and b'name="import_mode"' not in r.content
    assert worten_csv([{"sku": "A"}]).startswith(b"\xef\xbb\xbfsku;product-id;")


@pytest.mark.parametrize(
    "status,uncertain", [(400, False), (401, False), (429, False), (500, True), (503, True)]
)
def test_no_automatic_post_retry(status, uncertain):
    calls = []
    connector = PublicationConnector(
        httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(status))
    )
    with pytest.raises(RemoteFailure) as error:
        connector.request(
            "kaufland", {"client_key": "k", "secret_key": "s"}, "POST", "/units/", payload={}
        )
    assert error.value.uncertain is uncertain and len(calls) == 1


def test_arithmetic_filters_currency_and_composite_sku():
    rules = Rules(account_id=uuid4(), view_id=uuid4(), revision=1, composite_sku=True)
    row = {
        "ean": "6930460000040",
        "sku": "old",
        "cost": "37.93",
        "total_cost": "39.93",
        "quantity": "258",
        "weight_kg": "1.03",
    }
    public, payload = prepare(row, "InnPro", "kaufland", rules)
    assert public["cost"] == "39.93" and public["price"] == "53.91"
    assert public["sku"] == "InnPro_6930460000040_39.93_43.92"
    assert payload["listing_price"] == 5391
    assert (
        prepare(
            row,
            "InnPro",
            "kaufland",
            rules.model_copy(update={"weight_mode": "above", "weight_from": 1}),
        )
        is None
    )
    assert (
        prepare(
            {**row, "weight_kg": None},
            "InnPro",
            "kaufland",
            rules.model_copy(update={"weight_mode": "above", "weight_from": 1}),
        )
        is not None
    )
    _, converted = prepare(
        row, "InnPro", "kaufland", rules.model_copy(update={"storefront": "cz", "multiplier": 25})
    )
    assert converted["listing_price"] == 134775
    # Original Kaufland keeps loss rows; original Worten applies minimum profit.
    loss = rules.model_copy(update={"margin": 0, "commission": 15})
    assert prepare(row, "InnPro", "kaufland", loss) is not None
    assert prepare(row, "InnPro", "worten", loss) is None
