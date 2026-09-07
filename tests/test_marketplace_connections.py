from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from uuid import uuid4

import httpx
import pytest
import test_tenancy_api
from fastapi.testclient import TestClient
from marketplace_hub_api.main import create_app
from marketplace_hub_core.marketplace_connections.connectors import (
    KAUFLAND_URL,
    WORTEN_URL,
    ConnectionProbeError,
    MarketplaceConnector,
    kaufland_headers,
    normalize_credentials,
    remote_json,
)
from marketplace_hub_core.marketplace_connections.repository import (
    VERIFICATION_KEY,
    SqlMarketplaceConnectionsRepository,
)
from marketplace_hub_core.marketplace_connections.security import decrypt_credentials
from marketplace_hub_core.marketplace_connections.service import MarketplaceConnectionsService
from marketplace_hub_core.seller_settings.schema import seller_marketplace_accounts as accounts
from marketplace_hub_core.seller_settings.security import encrypt_credentials
from marketplace_hub_core.settings import Settings
from marketplace_hub_core.tenancy.schema import memberships
from pydantic import SecretStr
from sqlalchemy import select
from test_seller_settings_api import TEST_MASTER, owner

workspace = test_tenancy_api.workspace
K_KEYS = {"client_key": " synthetic-client-1234 ", "secret_key": " synthetic-secret-abcd "}
W_KEYS = {"api_key": "synthetic-api-key-5678", "shop_id": "123", "api_url": WORTEN_URL}


class ProbeFixture:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.after_request = None
        self.account_response = {"shop_id": 123, "shop_name": "Negozio verificato"}

    async def __call__(self, url, headers):
        self.calls.append((url, headers))
        if self.after_request:
            self.after_request()
        if self.failure:
            raise ConnectionProbeError(self.failure)
        if url.startswith(KAUFLAND_URL):
            return {"data": ["de", {"storefront": " AT "}, {"code": "pl"}, "de"]}
        if "/offers?" in url:
            return {"offers": [], "total_count": 0}
        return self.account_response


@pytest.fixture
def configured(workspace):
    workspace.probe = ProbeFixture()
    workspace.connections = MarketplaceConnectionsService(
        SqlMarketplaceConnectionsRepository(workspace.engine), workspace.service,
        SecretStr(TEST_MASTER), MarketplaceConnector(workspace.probe),
    )
    workspace.settings = Settings(environment="test", master_key=TEST_MASTER, _env_file=None)
    workspace.app = create_app(
        settings=workspace.settings, readiness_checks={"fixture": lambda: None},
        auth_service=workspace.auth, workspace_service=workspace.service,
        marketplace_connections_service=workspace.connections,
    )
    return workspace


def path(seller, account=None, verify=False):
    return (f"/v1/sellers/{seller}/marketplace-connections"
            + (f"/{account}" if account else "") + ("/verify" if verify else ""))


def payload(marketplace="kaufland", name=" Account Europa ", credentials=None):
    return {"marketplace": marketplace, "account_name": name,
            "credentials": credentials or (K_KEYS if marketplace == "kaufland" else W_KEYS)}


def test_every_generic_endpoint_requires_authenticated_session(configured):
    client = TestClient(configured.app)
    seller, account = uuid4(), uuid4()
    for method, url, data in (
        ("GET", path(seller), None), ("POST", path(seller), payload()),
        ("POST", path(seller, account, True), None),
        ("DELETE", path(seller, account), {"confirmation": "ELIMINA"}),
    ):
        response = client.request(method, url, json=data)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    assert configured.probe.calls == []


def test_kaufland_verified_before_encrypted_persistence_and_safe_public_response(configured):
    client, seller, organization, _, _ = owner(configured)
    result = client.post(path(seller), json=payload())
    assert result.status_code == 201
    assert result.headers["cache-control"] == "no-store"
    account = result.json()["accounts"][0]
    assert account == {
        "id": account["id"], "marketplace": "kaufland", "account_name": "Account Europa",
        "active": True, "connection_status": "connected",
        "last_checked_at": account["last_checked_at"], "public_name": None,
        "external_shop_id": None, "storefronts": ["de", "at", "pl"],
        "credential_mask": "••••••••1234", "error_code": None,
    }
    assert result.json()["seller_id"] == str(seller)
    assert result.json()["can_manage"] is True
    assert client.get(path(seller)).json() == result.json()
    assert configured.probe.calls[0][0] == f"{KAUFLAND_URL}/info/storefront"
    with configured.engine.connect() as connection:
        row = connection.execute(select(accounts)).mappings().one()
    assert row["seller_id"] == seller and row["organization_id"] == organization
    assert decrypt_credentials(row["credentials_encrypted"], SecretStr(TEST_MASTER)) == {
        key: value.strip() for key, value in K_KEYS.items()
    }
    for value in K_KEYS.values():
        assert value.strip() not in result.text
        assert value.strip() not in row["credentials_encrypted"]
    assert client.post(path(seller), json=payload()).status_code == 409


def test_worten_original_offers_probe_plus_real_identity_and_legacy_settings_compatible(configured):
    client, seller, _, _, _ = owner(configured)
    result = client.post(path(seller), json=payload("worten"))
    assert result.status_code == 201
    account = result.json()["accounts"][0]
    assert account["marketplace"] == "worten"
    assert account["public_name"] == "Negozio verificato"
    assert account["external_shop_id"] == "123"
    assert account["credential_mask"] == "••••••••5678"
    assert [call[0] for call in configured.probe.calls] == [
        f"{WORTEN_URL}/offers?shop_id=123&max=1", f"{WORTEN_URL}/account?shop_id=123",
    ]
    assert configured.probe.calls[0][1]["Authorization"] == W_KEYS["api_key"]
    assert W_KEYS["api_key"] not in result.text
    legacy = client.get(f"/v1/sellers/{seller}/settings")
    assert legacy.status_code == 200 and legacy.json()["marketplace_accounts"] == []


def test_worten_never_invents_metadata_when_optional_account_unavailable(configured):
    client, seller, _, _, _ = owner(configured)
    configured.probe.account_response = {}
    account = client.post(path(seller), json=payload("worten")).json()["accounts"][0]
    assert account["connection_status"] == "connected"
    assert account["public_name"] is None and account["external_shop_id"] is None


def test_original_worten_country_credentials_can_be_verified_unchanged(configured):
    client, seller, organization, _, _ = owner(configured)
    account_id = uuid4()
    encrypted = encrypt_credentials({**W_KEYS, "country": "pt"}, SecretStr(TEST_MASTER))
    with configured.engine.begin() as connection:
        connection.execute(accounts.insert().values(
            id=account_id, seller_id=seller, organization_id=organization,
            marketplace="worten", account_name="Originale Worten",
            credentials_encrypted=encrypted, active=True,
            created_at=configured.now, updated_at=configured.now,
        ))
    checked = client.post(path(seller, account_id, True))
    assert checked.status_code == 200
    assert checked.json()["accounts"][0]["connection_status"] == "connected"
    with configured.engine.connect() as connection:
        stored = connection.execute(select(accounts.c.credentials_encrypted)).scalar_one()
    assert stored == encrypted


def test_worten_account_must_match_selected_shop_and_not_leak_reflected_api_key(configured):
    client, seller, _, _, _ = owner(configured)
    configured.probe.account_response = {"shop_id": 456, "shop_name": W_KEYS["api_key"]}
    response = client.post(path(seller), json=payload("worten"))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_configuration"
    assert client.get(path(seller)).json()["accounts"] == []
    configured.probe.account_response["shop_id"] = 123
    saved = client.post(path(seller), json=payload("worten"))
    assert saved.json()["accounts"][0]["public_name"] is None
    assert W_KEYS["api_key"] not in saved.text


def test_worten_preserves_original_nonempty_shop_id_and_url_encodes_it(configured):
    client, seller, _, _, _ = owner(configured)
    configured.probe.account_response = {"shop_id": "shop&one", "shop_name": "Shop"}
    response = client.post(path(seller), json=payload("worten", credentials={
        **W_KEYS, "shop_id": " shop&one ",
    }))
    assert response.status_code == 201
    assert configured.probe.calls[0][0] == f"{WORTEN_URL}/offers?shop_id=shop%26one&max=1"
    assert response.json()["accounts"][0]["external_shop_id"] == "shop&one"


def test_arbitrary_legacy_verification_json_never_escapes_public_dto(configured):
    client, seller, _, _, _ = owner(configured)
    client.post(path(seller), json=payload())
    with configured.engine.begin() as connection:
        connection.execute(accounts.update().values(settings_json=json.dumps({VERIFICATION_KEY: {
            "connection_status": {"secret_key": "raw-secret"}, "error_code": ["raw-secret"],
            "public_name": {"bank_account": "private"}, "external_shop_id": ["private"],
            "last_checked_at": {"secret_key": "raw-secret"},
            "storefronts": [{"api_key": "raw-secret"}, "de", "not-a-storefront"],
            "unrelated_secret": "raw-secret",
        }})))
    result = client.get(path(seller))
    assert result.status_code == 200
    account = result.json()["accounts"][0]
    assert account["connection_status"] == "unverified"
    assert account["public_name"] is None and account["external_shop_id"] is None
    assert account["last_checked_at"] is None and account["error_code"] is None
    assert account["storefronts"] == ["de"]
    assert "raw-secret" not in result.text and "private" not in result.text


@pytest.mark.parametrize("code,status", [
    ("invalid_credentials", 422), ("permission_denied", 422),
    ("unexpected_response", 502), ("upstream_unavailable", 502), ("timeout", 504),
])
def test_failed_remote_probe_never_saves_new_account(configured, code, status):
    client, seller, _, _, _ = owner(configured)
    configured.probe.failure = code
    response = client.post(path(seller), json=payload())
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert client.get(path(seller)).json()["accounts"] == []
    assert all(value.strip() not in response.text for value in K_KEYS.values())


@pytest.mark.parametrize("data", [
    payload(credentials={"client_key": "value"}),
    payload("worten", credentials={**W_KEYS, "api_url": "https://attacker.example/api"}),
    payload("worten", credentials={**W_KEYS, "api_url": WORTEN_URL + "?api_key=secret"}),
    payload("worten", credentials={**W_KEYS, "shop_id": " "}),
    payload("amazon"), payload(credentials={**K_KEYS, "api_key": "unexpected-secret"}),
    {**payload(), "credentials": {"secret_key": {"key": "raw-secret"}}},
])
def test_invalid_connector_configuration_and_ssrf_rejected_before_network(configured, data):
    client, seller, _, _, _ = owner(configured)
    response = client.post(path(seller), json=data)
    assert response.status_code == 422
    assert configured.probe.calls == []
    assert "raw-secret" not in response.text and "unexpected-secret" not in response.text
    assert client.get(path(seller)).json()["accounts"] == []


def test_malformed_json_redacted(configured):
    client, seller, _, _, _ = owner(configured)
    response = client.post(path(seller), content='{"credentials":{"secret_key":"raw-secret",',
                           headers={"content-type": "application/json"})
    assert response.status_code == 422 and "raw-secret" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_legacy_account_reverify_preserves_id_ciphertext_settings_and_failed_state(configured):
    client, seller, _, _, _ = owner(configured)
    legacy = client.post(f"/v1/sellers/{seller}/kaufland-accounts", json={
        "account_name": "Legacy", **K_KEYS,
    }).json()["marketplace_accounts"][0]
    account_id = legacy["id"]
    with configured.engine.begin() as connection:
        connection.execute(accounts.update().values(settings_json=json.dumps({"legacy_flag": [1]})))
        original = connection.execute(select(accounts)).mappings().one()
    before = client.get(path(seller)).json()["accounts"][0]
    assert before["connection_status"] == "unverified"
    assert before["last_checked_at"] is None and before["storefronts"] == []
    checked = client.post(path(seller, account_id, True))
    assert checked.status_code == 200
    assert checked.json()["accounts"][0]["connection_status"] == "connected"
    configured.probe.failure = "invalid_credentials"
    failed = client.post(path(seller, account_id, True))
    assert failed.status_code == 200
    assert failed.json()["accounts"][0]["error_code"] == "invalid_credentials"
    assert failed.json()["accounts"][0]["connection_status"] == "error"
    with configured.engine.connect() as connection:
        row = connection.execute(select(accounts)).mappings().one()
    assert row["id"] == original["id"]
    assert row["credentials_encrypted"] == original["credentials_encrypted"]
    assert json.loads(row["settings_json"])["legacy_flag"] == [1]
    assert json.loads(row["settings_json"])[VERIFICATION_KEY]["connection_status"] == "error"


def test_slow_earlier_verify_cannot_overwrite_newer_failure(configured):
    client, seller, _, _, user = owner(configured)
    account = client.post(path(seller), json=payload()).json()["accounts"][0]
    from uuid import UUID
    _, principal = configured.session(user)

    async def scenario():
        first_started = asyncio.Event()
        finish_first = asyncio.Event()
        requests = 0

        async def transport(url, headers):
            nonlocal requests
            requests += 1
            if requests == 1:
                first_started.set()
                await finish_first.wait()
                return {"data": ["de"]}
            raise ConnectionProbeError("invalid_credentials")

        configured.connections.connector = MarketplaceConnector(transport)
        first = asyncio.create_task(configured.connections.verify(
            principal, seller, UUID(account["id"]),
        ))
        await first_started.wait()
        newer = await configured.connections.verify(principal, seller, UUID(account["id"]))
        assert newer["accounts"][0]["connection_status"] == "error"
        finish_first.set()
        return await first

    result = asyncio.run(scenario())
    assert result["accounts"][0]["error_code"] == "invalid_credentials"
    assert result["accounts"][0]["connection_status"] == "error"


def test_unreadable_credentials_and_missing_master_block_before_network(configured):
    client, seller, _, _, _ = owner(configured)
    added = client.post(path(seller), json=payload()).json()["accounts"][0]
    configured.probe.calls.clear()
    configured.connections.master_key = SecretStr("")
    create = client.post(path(seller), json=payload(name="Secondo"))
    assert create.status_code == 503
    checked = client.post(path(seller, added["id"], True))
    assert checked.json()["accounts"][0]["error_code"] == "credentials_unavailable"
    assert checked.json()["accounts"][0]["credential_mask"] == "—"
    assert configured.probe.calls == []


def test_foreign_seller_and_account_cannot_read_verify_or_delete(configured):
    client, seller, _, _, _ = owner(configured)
    other, foreign_seller, _, _, _ = owner(configured)
    account = client.post(path(seller), json=payload()).json()["accounts"][0]
    calls = len(configured.probe.calls)
    assert other.get(path(seller)).status_code == 404
    assert other.post(path(seller), json=payload()).status_code == 404
    assert other.post(path(foreign_seller, account["id"], True)).status_code == 404
    assert other.request("DELETE", path(foreign_seller, account["id"]),
                         json={"confirmation": "ELIMINA"}).status_code == 404
    assert len(configured.probe.calls) == calls
    assert len(client.get(path(seller)).json()["accounts"]) == 1


def test_readonly_and_permission_revocation_cannot_mutate_connections(configured):
    client, seller, _, membership, _ = owner(configured)
    account = client.post(path(seller), json=payload()).json()["accounts"][0]
    with configured.engine.begin() as connection:
        connection.execute(memberships.update().where(memberships.c.id == membership)
                           .values(read_only=True))
    assert client.get(path(seller)).json()["can_manage"] is False
    assert client.post(path(seller), json=payload(name="Secondo")).status_code == 403
    assert client.post(path(seller, account["id"], True)).status_code == 403
    assert client.request("DELETE", path(seller, account["id"]),
                          json={"confirmation": "ELIMINA"}).status_code == 403


@pytest.mark.parametrize("operation", ["connect", "verify"])
def test_revocation_during_remote_request_prevents_persistence(configured, operation):
    client, seller, _, membership, _ = owner(configured)
    account = client.post(path(seller), json=payload()).json()["accounts"][0]
    with configured.engine.connect() as connection:
        original = connection.execute(select(accounts)).mappings().one()

    def revoke():
        with configured.engine.begin() as connection:
            connection.execute(memberships.update().where(memberships.c.id == membership)
                               .values(active=False))

    configured.probe.after_request = revoke
    result = (client.post(path(seller), json=payload(name="Secondo")) if operation == "connect"
              else client.post(path(seller, account["id"], True)))
    assert result.status_code == 404
    with configured.engine.connect() as connection:
        rows = connection.execute(select(accounts)).mappings().all()
    assert len(rows) == 1 and dict(rows[0]) == dict(original)


def test_delete_requires_exact_confirmation_and_survives_new_session(configured):
    client, seller, _, _, user = owner(configured)
    account = client.post(path(seller), json=payload("worten")).json()["accounts"][0]
    for confirmation in ("", "elimina", " ELIMINA "):
        assert client.request("DELETE", path(seller, account["id"]),
                              json={"confirmation": confirmation}).status_code == 422
    assert client.request("DELETE", path(seller, account["id"]),
                          json={"confirmation": "ELIMINA"}).json()["accounts"] == []
    refreshed, _ = configured.session(user)
    assert refreshed.get(path(seller)).json()["accounts"] == []


def test_kaufland_signature_matches_original_exact_full_url_hmac_hex():
    credentials = normalize_credentials("kaufland", K_KEYS)
    url = f"{KAUFLAND_URL}/info/storefront"
    headers = kaufland_headers(url, credentials, "1700000000")
    expected = hmac.new(b"synthetic-secret-abcd", f"GET\n{url}\n\n1700000000".encode(),
                        hashlib.sha256).hexdigest()
    assert headers["Shop-Signature"] == expected
    assert len(expected) == 64 and headers["Shop-Client-Key"] == "synthetic-client-1234"
    assert headers["Shop-Timestamp"] == "1700000000"


@pytest.mark.parametrize("response", [
    {}, {"data": {}}, {"data": [None]}, {"data": ["secret-string"]},
])
def test_http200_does_not_verify_kaufland_without_valid_storefront_shape(response):
    async def transport(url, headers):
        return response
    with pytest.raises(ConnectionProbeError, match="risposta non riconoscibile"):
        asyncio.run(MarketplaceConnector(transport).verify("kaufland", K_KEYS))


@pytest.mark.parametrize("response", [
    {}, {"offers": {}}, {"offers": ["wrong"]}, {"offers": [], "error": "bad-auth"},
])
def test_http200_does_not_verify_worten_error_or_invalid_offer_shape(response):
    async def transport(url, headers):
        return response
    with pytest.raises(ConnectionProbeError) as raised:
        asyncio.run(MarketplaceConnector(transport).verify("worten", W_KEYS))
    assert raised.value.code == "unexpected_response"


def test_authenticated_empty_storefronts_are_valid_without_invented_metadata():
    async def transport(url, headers):
        return {"data": []}
    metadata = asyncio.run(MarketplaceConnector(transport).verify("kaufland", K_KEYS))
    assert metadata.storefronts == []
    assert metadata.public_name is None and metadata.external_shop_id is None


def test_whole_probe_deadline_cancels_slow_transport(monkeypatch):
    real_timeout = asyncio.timeout
    deadlines = []

    def accelerated_timeout(seconds):
        deadlines.append(seconds)
        return real_timeout(0.001)

    async def transport(url, headers):
        await asyncio.sleep(10)
        return {"data": []}

    monkeypatch.setattr(asyncio, "timeout", accelerated_timeout)
    with pytest.raises(ConnectionProbeError) as raised:
        asyncio.run(MarketplaceConnector(transport).verify("kaufland", K_KEYS))
    assert deadlines == [18] and raised.value.code == "timeout"


@pytest.mark.parametrize("status,body,code", [
    (401, b"raw-secret", "invalid_credentials"),
    (403, b"raw-secret", "permission_denied"),
    (302, b"raw-secret", "upstream_unavailable"),
    (429, b"raw-secret", "upstream_unavailable"),
    (200, b"raw-secret", "unexpected_response"),
    (200, b"[]", "unexpected_response"),
    (200, b"x" * 500_001, "unexpected_response"),
], ids=["unauthorized", "forbidden", "redirect", "limited", "nonjson", "array", "oversize"])
def test_real_transport_bounds_body_redacts_errors_and_never_follows_redirects(
    monkeypatch, status, body, code,
):
    calls = []
    client_class = httpx.AsyncClient

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, content=body, headers={"location": "https://evil.example"})

    def client(**options):
        assert options["follow_redirects"] is False and options["trust_env"] is False
        return client_class(transport=httpx.MockTransport(handler), **options)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    with pytest.raises(ConnectionProbeError) as raised:
        asyncio.run(remote_json(f"{KAUFLAND_URL}/info/storefront", {"secret": "raw-secret"}))
    assert raised.value.code == code and "raw-secret" not in str(raised.value)
    assert calls == [f"{KAUFLAND_URL}/info/storefront"]
