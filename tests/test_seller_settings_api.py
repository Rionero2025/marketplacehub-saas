from __future__ import annotations

import base64
import hashlib
import json
from uuid import uuid4

import pytest
import test_tenancy_api
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from marketplace_hub_api.main import create_app
from marketplace_hub_core.seller_settings.profit_sharing import normalized_percentages
from marketplace_hub_core.seller_settings.schema import (
    seller_commercial_settings,
    seller_marketplace_accounts,
)
from marketplace_hub_core.settings import Settings
from marketplace_hub_core.tenancy.schema import (
    membership_permissions,
    memberships,
    organizations,
)
from pydantic import SecretStr
from sqlalchemy import select
from test_tenancy_api import WorkspaceFixture

workspace = test_tenancy_api.workspace

TEST_MASTER = "test-only-synthetic-master-key"
PROFILE = {
    "name": " Negozio aggiornato ", "legal_name": " Azienda aggiornata ",
    "email": " contatto libero ", "our_profit_pct": 35, "partner_profit_pct": 65,
}
KEYS = {
    "account_name": " Kaufland Europa ", "client_key": " client-value-1234 ",
    "secret_key": " secret-value-abcd ",
}


@pytest.fixture
def configured(workspace: WorkspaceFixture) -> WorkspaceFixture:
    workspace.settings = Settings(environment="test", master_key=TEST_MASTER, _env_file=None)
    workspace.app = create_app(
        settings=workspace.settings, readiness_checks={"fixture": lambda: None},
        auth_service=workspace.auth, workspace_service=workspace.service,
    )
    return workspace


def owner(workspace: WorkspaceFixture, **membership_options):
    user = workspace.user()
    organization = workspace.organization("SELLER")
    seller = workspace.seller(organization)
    member = workspace.membership(user, organization, "SELLER_OWNER", **membership_options)
    client, _ = workspace.session(user)
    return client, seller, organization, member, user


def settings_path(seller):
    return f"/v1/sellers/{seller}/settings"


def account_path(seller, account=None):
    return f"/v1/sellers/{seller}/kaufland-accounts" + (f"/{account}" if account else "")


def test_all_seller_settings_routes_require_session(configured):
    client = TestClient(configured.app)
    seller = uuid4()
    for method, path, payload in (
        ("GET", settings_path(seller), None),
        ("PUT", settings_path(seller), PROFILE),
        ("POST", account_path(seller), KEYS),
        ("DELETE", account_path(seller, uuid4()), {"confirmation": "ELIMINA"}),
    ):
        response = client.request(method, path, json=payload)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"


def test_edit_profile_split_survives_new_session_and_updates_workspace(configured):
    client, seller, _, _, user = owner(configured)
    initial = client.get(settings_path(seller))
    assert initial.status_code == 200
    assert initial.json()["our_profit_pct"] == 0
    assert initial.json()["partner_profit_pct"] == 100
    assert initial.json()["can_manage"] is True
    saved = client.put(settings_path(seller), json=PROFILE)
    assert saved.status_code == 200
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json()["name"] == "Negozio aggiornato"
    assert saved.json()["email"] == "contatto libero"
    client.post("/v1/auth/logout")
    refreshed, _ = configured.session(user)
    assert refreshed.get(settings_path(seller)).json() == saved.json()
    assert refreshed.get("/v1/workspace").json()["active_seller"]["name"] == "Negozio aggiornato"
    assert refreshed.get(settings_path(seller)).json()["our_profit_pct"] == 35


@pytest.mark.parametrize("our,partner,expected", [
    (None, None, (0, 100)), ("bad", "bad", (0, 100)),
    (float("nan"), float("inf"), (0, 100)),
    (-1, 102, (0, 100)), (125, -1, (100, 0)),
    (35, 40, (35, 65)), (20.123456, 79.876544, (20.1235, 79.8765)),
    (50, 49.995, (50, 49.995)),
])
def test_legacy_percentage_normalization(our, partner, expected):
    assert normalized_percentages(our, partner) == expected


@pytest.mark.parametrize("our,partner", [(-1, 101), (101, -1), (35, 60), (50, 49.98),
                                         ("NaN", 100), (0, "Infinity")])
def test_invalid_split_rejected_atomically(configured, our, partner):
    client, seller, _, _, _ = owner(configured)
    before = client.get(settings_path(seller)).json()
    response = client.put(settings_path(seller), json={
        **PROFILE, "our_profit_pct": our, "partner_profit_pct": partner,
    })
    assert response.status_code == 422
    assert client.get(settings_path(seller)).json() == before


def test_tolerance_and_blank_optional_fields_follow_original_rules(configured):
    client, seller, _, _, _ = owner(configured)
    saved = client.put(settings_path(seller), json={
        **PROFILE, "legal_name": " ", "email": " ",
        "our_profit_pct": 50, "partner_profit_pct": 49.995,
    })
    assert saved.status_code == 200
    assert saved.json()["legal_name"] == saved.json()["email"] == ""
    assert saved.json()["partner_profit_pct"] == 49.995
    assert client.put(settings_path(seller), json={**PROFILE, "name": " "}).status_code == 422


def test_legacy_malformed_split_normalized_on_read_without_mutating(configured):
    client, seller, _, _, _ = owner(configured)
    with configured.engine.begin() as connection:
        connection.execute(seller_commercial_settings.insert().values(
            seller_id=seller, our_profit_pct=35, partner_profit_pct=10,
        ))
    result = client.get(settings_path(seller)).json()
    assert (result["our_profit_pct"], result["partner_profit_pct"]) == (35, 65)
    with configured.engine.connect() as connection:
        value = connection.execute(select(seller_commercial_settings.c.partner_profit_pct)).scalar()
        assert value == 10


def test_credentials_encrypted_with_original_format_and_only_mask_returned(configured):
    client, seller, organization, _, _ = owner(configured)
    added = client.post(account_path(seller), json=KEYS)
    assert added.status_code == 201
    account = added.json()["marketplace_accounts"][0]
    assert account == {
        "id": account["id"], "marketplace": "kaufland", "account_name": "Kaufland Europa",
        "active": True, "credentials_configured": True, "client_key_masked": "••••••••1234",
    }
    assert client.get(settings_path(seller)).json() == added.json()
    with configured.engine.connect() as connection:
        row = connection.execute(select(seller_marketplace_accounts)).mappings().one()
    assert row["seller_id"] == seller and row["organization_id"] == organization
    ciphertext = row["credentials_encrypted"]
    for key in ("client-value-1234", "secret-value-abcd"):
        assert key not in ciphertext
        assert key not in added.text
    fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(TEST_MASTER.encode()).digest()))
    assert json.loads(fernet.decrypt(ciphertext.encode())) == {
        "client_key": "client-value-1234", "secret_key": "secret-value-abcd",
    }
    assert client.post(account_path(seller), json=KEYS).status_code == 409


@pytest.mark.parametrize("key_field", ["client_key", "secret_key"])
def test_original_add_accepts_either_key_without_connection_request(configured, key_field):
    client, seller, _, _, _ = owner(configured)
    result = client.post(account_path(seller), json={
        "account_name": "Account", key_field: "single-synthetic-value",
    })
    assert result.status_code == 201
    assert result.json()["marketplace_accounts"][0]["credentials_configured"] is True


@pytest.mark.parametrize("payload", [
    {"account_name": "", "client_key": "value"},
    {"account_name": "Account", "client_key": " ", "secret_key": " "},
    {"account_name": "Account", "client_key": {"secret": "secret-value-abcd"}},
    {"client_key": "secret-value-abcd"},
    {**KEYS, "organization_id": "secret-value-abcd"},
])
def test_validation_errors_never_echo_credential_input(configured, payload):
    client, seller, _, _, _ = owner(configured)
    response = client.post(account_path(seller), json=payload)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "secret-value-abcd" not in response.text
    assert "client-value-1234" not in response.text
    assert client.get(settings_path(seller)).json()["marketplace_accounts"] == []


def test_malformed_json_does_not_echo_secret(configured):
    client, seller, _, _, _ = owner(configured)
    response = client.post(account_path(seller), content='{"secret_key":"secret-value-abcd",',
                           headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert "secret-value-abcd" not in response.text


def test_missing_master_only_blocks_credential_creation(configured):
    configured.settings.master_key = SecretStr("")
    configured.app = create_app(
        settings=configured.settings, auth_service=configured.auth,
        workspace_service=configured.service, readiness_checks={"fixture": lambda: None},
    )
    client, seller, _, _, _ = owner(configured)
    assert client.put(settings_path(seller), json=PROFILE).status_code == 200
    response = client.post(account_path(seller), json=KEYS)
    assert response.status_code == 503
    assert "secret-value-abcd" not in response.text
    assert client.get(settings_path(seller)).json()["marketplace_accounts"] == []


def test_unreadable_legacy_ciphertext_returns_safe_fallback(configured):
    client, seller, organization, _, _ = owner(configured)
    with configured.engine.begin() as connection:
        connection.execute(seller_marketplace_accounts.insert().values(
            id=uuid4(), seller_id=seller, organization_id=organization,
            marketplace="kaufland", account_name="Importato", credentials_encrypted="invalid",
            active=False, created_at=configured.now, updated_at=configured.now,
        ))
    response = client.get(settings_path(seller))
    assert response.status_code == 200
    account = response.json()["marketplace_accounts"][0]
    assert account["active"] is False
    assert account["client_key_masked"] == "—"
    assert "credentials_encrypted" not in account


@pytest.mark.parametrize("options", [
    {"read_only": True},
    {"permission_codes": ("WORKSPACE_VIEW", "ACCOUNTING")},
    {"permission_codes": ("WORKSPACE_VIEW", "WORKSPACE_MANAGE", "VIEW_ONLY")},
])
def test_read_only_and_missing_manage_permission_cannot_mutate(configured, options):
    client, seller, _, _, _ = owner(configured, **options)
    assert client.get(settings_path(seller)).json()["can_manage"] is False
    for method, path, payload in (
        ("PUT", settings_path(seller), PROFILE), ("POST", account_path(seller), KEYS),
        ("DELETE", account_path(seller, uuid4()), {"confirmation": "ELIMINA"}),
    ):
        assert client.request(method, path, json=payload).status_code == 403


@pytest.mark.parametrize("revocation", ["membership", "organization", "permission"])
def test_access_revocation_is_checked_on_each_mutation(configured, revocation):
    client, seller, organization, membership, _ = owner(configured)
    assert client.get(settings_path(seller)).json()["can_manage"] is True
    with configured.engine.begin() as connection:
        if revocation == "membership":
            connection.execute(memberships.update().where(
                memberships.c.id == membership,
            ).values(active=False))
        elif revocation == "organization":
            connection.execute(organizations.update().where(
                organizations.c.id == organization,
            ).values(active=False))
        else:
            connection.execute(membership_permissions.delete().where(
                membership_permissions.c.membership_id == membership,
                membership_permissions.c.permission_code == "WORKSPACE_MANAGE",
            ))
    assert client.put(settings_path(seller), json=PROFILE).status_code == (
        403 if revocation == "permission" else 404
    )


def test_foreign_seller_and_account_identifiers_do_not_cross_scope(configured):
    own_client, own_seller, _, _, _ = owner(configured)
    foreign_client, foreign_seller, _, _, _ = owner(configured)
    foreign_account = foreign_client.post(account_path(foreign_seller), json=KEYS).json()[
        "marketplace_accounts"
    ][0]["id"]
    for method, path, payload in (
        ("GET", settings_path(foreign_seller), None),
        ("PUT", settings_path(foreign_seller), PROFILE),
        ("POST", account_path(foreign_seller), KEYS),
        ("DELETE", account_path(foreign_seller, foreign_account), {"confirmation": "ELIMINA"}),
        ("DELETE", account_path(own_seller, foreign_account), {"confirmation": "ELIMINA"}),
    ):
        assert own_client.request(method, path, json=payload).status_code == 404
    accounts = foreign_client.get(settings_path(foreign_seller)).json()["marketplace_accounts"]
    assert len(accounts) == 1


def test_delete_requires_exact_confirmation_and_persists(configured):
    client, seller, _, _, user = owner(configured)
    account = client.post(account_path(seller), json=KEYS).json()["marketplace_accounts"][0]["id"]
    for word in ("", "elimina", " ELIMINA "):
        assert client.request("DELETE", account_path(seller, account), json={
            "confirmation": word,
        }).status_code == 422
    response = client.request(
        "DELETE", account_path(seller, account), json={"confirmation": "ELIMINA"},
    )
    assert response.status_code == 200
    assert response.json()["marketplace_accounts"] == []
    refreshed, _ = configured.session(user)
    assert refreshed.get(settings_path(seller)).json()["marketplace_accounts"] == []


def test_settings_master_key_alias_and_redaction(monkeypatch):
    monkeypatch.delenv("MH_MASTER_KEY", raising=False)
    monkeypatch.setenv("MARKETPLACE_HUB_MASTER_KEY", TEST_MASTER)
    settings = Settings(_env_file=None)
    assert settings.master_key.get_secret_value() == TEST_MASTER
    assert TEST_MASTER not in repr(settings)
    monkeypatch.setenv("MH_MASTER_KEY", "preferred-test-value")
    assert Settings(_env_file=None).master_key.get_secret_value() == "preferred-test-value"
