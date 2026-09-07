from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from marketplace_hub_api.main import create_app
from marketplace_hub_core.auth.models import AuthRealm, AuthUser, StoredSession
from marketplace_hub_core.auth.rate_limit import MemoryLoginRateLimiter
from marketplace_hub_core.auth.schema import auth_users, metadata
from marketplace_hub_core.auth.service import AuthService, hash_session_token
from marketplace_hub_core.auth.sql_repository import SqlAuthRepository
from marketplace_hub_core.settings import Settings
from marketplace_hub_core.tenancy.constants import PERMISSION_LABELS, ROLE_LABELS
from marketplace_hub_core.tenancy.repository import SqlWorkspaceRepository
from marketplace_hub_core.tenancy.schema import (
    membership_permissions,
    membership_seller_access,
    memberships,
    organization_relationships,
    organizations,
    permissions,
    roles,
    seller_profiles,
)
from marketplace_hub_core.tenancy.service import (
    SellerNotAccessibleError,
    WorkspacePermissionError,
    WorkspaceService,
)
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool


class WorkspaceFixture:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.now = datetime.now(UTC)
        self.auth_repository = SqlAuthRepository(engine)
        self.auth = AuthService(self.auth_repository, MemoryLoginRateLimiter())
        self.repository = SqlWorkspaceRepository(engine)
        self.service = WorkspaceService(self.repository)
        self.settings = Settings(environment="test")
        self.app = create_app(
            settings=self.settings,
            readiness_checks={"fixture": lambda: None},
            auth_service=self.auth,
            workspace_service=self.service,
        )

    def user(self, *realms: AuthRealm) -> AuthUser:
        return self.auth_repository.create_user(
            login=f"{uuid4()}@example.test",
            display_name="Workspace Test",
            password_hash="unused-session-fixture",
            realms=realms or list(AuthRealm),
            now=self.now,
        )

    def organization(self, kind: str, name: str = "Organizzazione") -> UUID:
        organization_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(organizations.insert().values(
                id=organization_id, name=name, kind=kind, active=True, created_at=self.now,
            ))
        return organization_id

    def seller(self, organization_id: UUID, name: str = "Negozio") -> UUID:
        seller_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(seller_profiles.insert().values(
                id=seller_id, organization_id=organization_id, organization_kind="SELLER",
                name=name, legal_name="Ragione sociale", email="seller@example.test",
                active=True, created_at=self.now, updated_at=self.now,
            ))
        return seller_id

    def membership(
        self, user: AuthUser, organization_id: UUID, role: str,
        *, all_sellers: bool = True, read_only: bool = False,
        permission_codes: tuple[str, ...] = ("WORKSPACE_VIEW", "WORKSPACE_MANAGE", "ACCOUNTING"),
        seller_ids: tuple[UUID, ...] = (),
    ) -> UUID:
        membership_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(memberships.insert().values(
                id=membership_id, user_id=user.id, organization_id=organization_id,
                organization_kind=role.split("_")[0], role_code=role,
                active=True, all_sellers=all_sellers, read_only=read_only, created_at=self.now,
            ))
            if permission_codes:
                connection.execute(membership_permissions.insert(), [
                    {"membership_id": membership_id, "permission_code": code}
                    for code in permission_codes
                ])
            if seller_ids:
                connection.execute(membership_seller_access.insert(), [
                    {"membership_id": membership_id, "seller_id": seller_id}
                    for seller_id in seller_ids
                ])
        return membership_id

    def link(self, agency_id: UUID, seller_organization_id: UUID) -> None:
        with self.engine.begin() as connection:
            connection.execute(organization_relationships.insert().values(
                parent_id=agency_id, child_id=seller_organization_id,
                parent_kind="AGENCY", child_kind="SELLER", active=True,
            ))

    def session(self, user: AuthUser, realm: AuthRealm = AuthRealm.SELLER):
        token = str(uuid4())
        stored = StoredSession(
            id=uuid4(), user_id=user.id, token_hash=hash_session_token(token),
            created_at=self.now, expires_at=self.now + timedelta(hours=1),
        )
        self.auth_repository.save_session(stored, realm)
        principal = self.auth.authenticate(token)
        assert principal is not None
        client = TestClient(self.app)
        client.cookies.set(self.settings.session_cookie_name, token)
        return client, principal


@pytest.fixture
def workspace() -> Iterator[WorkspaceFixture]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(roles.insert(), [
            {"code": code, "organization_kind": code.split("_")[0], "label": label}
            for code, label in ROLE_LABELS.items()
        ])
        connection.execute(permissions.insert(), [
            {"code": code, "label": label} for code, label in PERMISSION_LABELS.items()
        ])
    yield WorkspaceFixture(engine)
    engine.dispose()


def test_workspace_routes_require_authentication(workspace: WorkspaceFixture) -> None:
    client = TestClient(workspace.app)
    for method, path, payload in (
        ("GET", "/v1/workspace", None),
        ("GET", f"/v1/sellers/{uuid4()}", None),
        ("POST", "/v1/workspace/select", {"seller_id": str(uuid4())}),
    ):
        assert client.request(method, path, json=payload).status_code == 401


def test_seller_scope_rejects_foreign_and_tampered_identifiers(
    workspace: WorkspaceFixture,
) -> None:
    user = workspace.user()
    own_org = workspace.organization("SELLER", "Azienda propria")
    foreign_org = workspace.organization("SELLER", "Azienda estranea")
    own = workspace.seller(own_org, "Proprio")
    foreign = workspace.seller(foreign_org, "Estraneo")
    workspace.membership(user, own_org, "SELLER_OWNER")
    client, _principal = workspace.session(user)

    result = client.get("/v1/workspace")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert [item["id"] for item in result.json()["sellers"]] == [str(own)]
    assert client.get(f"/v1/sellers/{own}").json()["legal_name"] == "Ragione sociale"
    assert workspace.repository.profile_in_scope(own, foreign_org) is None

    for denied in (foreign, uuid4()):
        assert client.get(f"/v1/sellers/{denied}").status_code == 404
        attempted = client.post("/v1/workspace/select", json={
            "seller_id": str(denied), "organization_id": str(own_org),
            "role_code": "PLATFORM_OWNER", "all_sellers": True,
        })
        assert attempted.status_code == 404
        assert attempted.json() == {"detail": "Negozio non disponibile."}
    assert client.post("/v1/workspace/select", json={"seller_id": "invalid"}).status_code == 422


@pytest.mark.parametrize("realm", [AuthRealm.SELLER, AuthRealm.AGENCY])
def test_direct_viewer_membership_overrides_agency_owner(
    workspace: WorkspaceFixture, realm: AuthRealm,
) -> None:
    user = workspace.user()
    agency = workspace.organization("AGENCY")
    seller_org = workspace.organization("SELLER")
    seller = workspace.seller(seller_org)
    workspace.link(agency, seller_org)
    workspace.membership(user, agency, "AGENCY_OWNER")
    workspace.membership(user, seller_org, "SELLER_USER", read_only=True)
    client, principal = workspace.session(user, realm)

    response = client.get(f"/v1/sellers/{seller}")
    assert response.status_code == 200
    assert response.json()["role_code"] == "SELLER_USER"
    assert response.json()["write_permissions"] == []
    assert "ACCOUNTING" in response.json()["permissions"]
    assert client.post("/v1/workspace/select", json={"seller_id": str(seller)}).status_code == 200
    with pytest.raises(WorkspacePermissionError):
        workspace.service.require_seller(principal, seller, permission="ACCOUNTING", write=True)


def test_empty_direct_assignment_cannot_be_expanded_by_agency_membership(
    workspace: WorkspaceFixture,
) -> None:
    user = workspace.user()
    agency = workspace.organization("AGENCY")
    seller_org = workspace.organization("SELLER")
    seller = workspace.seller(seller_org)
    workspace.link(agency, seller_org)
    workspace.membership(user, agency, "AGENCY_OWNER")
    workspace.membership(user, seller_org, "SELLER_USER", all_sellers=False)
    client, principal = workspace.session(user, AuthRealm.AGENCY)
    assert client.get("/v1/workspace").json()["sellers"] == []
    assert client.get(f"/v1/sellers/{seller}").status_code == 404
    with pytest.raises(SellerNotAccessibleError):
        workspace.service.require_seller(principal, seller)


def test_assigned_seller_ids_cannot_expand_agency_relationship_scope(
    workspace: WorkspaceFixture,
) -> None:
    user = workspace.user()
    agency = workspace.organization("AGENCY")
    own_org = workspace.organization("SELLER")
    foreign_org = workspace.organization("SELLER")
    own = workspace.seller(own_org)
    excluded = workspace.seller(own_org)
    foreign = workspace.seller(foreign_org)
    workspace.link(agency, own_org)
    workspace.membership(
        user, agency, "AGENCY_USER", all_sellers=False, seller_ids=(own, foreign),
    )
    client, _principal = workspace.session(user, AuthRealm.AGENCY)
    assert [item["id"] for item in client.get("/v1/workspace").json()["sellers"]] == [str(own)]
    assert client.get(f"/v1/sellers/{excluded}").status_code == 404
    assert client.get(f"/v1/sellers/{foreign}").status_code == 404


def test_operator_permissions_allow_only_granted_read_and_write_actions(
    workspace: WorkspaceFixture,
) -> None:
    user = workspace.user()
    org = workspace.organization("SELLER")
    seller = workspace.seller(org)
    workspace.membership(
        user, org, "SELLER_USER", permission_codes=("WORKSPACE_VIEW", "ACCOUNTING"),
    )
    _client, principal = workspace.session(user)
    for write in (False, True):
        assert workspace.service.require_seller(
            principal, seller, permission="ACCOUNTING", write=write,
        )["id"] == str(seller)
        with pytest.raises(WorkspacePermissionError):
            workspace.service.require_seller(principal, seller, permission="CATALOG", write=write)
    with pytest.raises(WorkspacePermissionError):
        workspace.service.require_seller(principal, seller, permission="WORKSPACE_VIEW", write=True)


def test_view_only_permission_overrides_owner_write_role(workspace: WorkspaceFixture) -> None:
    user = workspace.user()
    org = workspace.organization("SELLER")
    seller = workspace.seller(org)
    workspace.membership(
        user, org, "SELLER_OWNER",
        permission_codes=("WORKSPACE_VIEW", "WORKSPACE_MANAGE", "ACCOUNTING", "VIEW_ONLY"),
    )
    client, principal = workspace.session(user)
    assert client.get(f"/v1/sellers/{seller}").json()["write_permissions"] == []
    with pytest.raises(WorkspacePermissionError):
        workspace.service.require_seller(
            principal, seller, permission="WORKSPACE_MANAGE", write=True,
        )


def test_no_menu_permission_does_not_gain_seller_access_from_owner_role(
    workspace: WorkspaceFixture,
) -> None:
    user = workspace.user()
    org = workspace.organization("SELLER")
    seller = workspace.seller(org)
    workspace.membership(user, org, "SELLER_OWNER", permission_codes=())
    client, _principal = workspace.session(user)
    assert client.get("/v1/workspace").json()["sellers"] == []
    assert client.get(f"/v1/sellers/{seller}").status_code == 404


@pytest.mark.parametrize(
    "revocation", ["membership", "seller_org", "agency_org", "relationship", "assignment",
                   "seller", "permission"],
)
def test_revocation_removes_stored_selection_without_relogin(
    workspace: WorkspaceFixture, revocation: str,
) -> None:
    user = workspace.user()
    agency = workspace.organization("AGENCY")
    seller_org = workspace.organization("SELLER")
    seller = workspace.seller(seller_org)
    workspace.link(agency, seller_org)
    membership = workspace.membership(
        user, agency, "AGENCY_USER", all_sellers=False, seller_ids=(seller,),
    )
    client, principal = workspace.session(user, AuthRealm.AGENCY)
    assert client.post("/v1/workspace/select", json={"seller_id": str(seller)}).status_code == 200
    assert workspace.repository.selected_seller(principal.session_id) == seller

    with workspace.engine.begin() as connection:
        if revocation == "membership":
            connection.execute(memberships.update().where(
                memberships.c.id == membership,
            ).values(active=False))
        elif revocation in {"seller_org", "agency_org"}:
            target = seller_org if revocation == "seller_org" else agency
            connection.execute(organizations.update().where(
                organizations.c.id == target,
            ).values(active=False))
        elif revocation == "relationship":
            connection.execute(organization_relationships.update().where(
                organization_relationships.c.parent_id == agency,
            ).values(active=False))
        elif revocation == "assignment":
            connection.execute(membership_seller_access.delete().where(
                membership_seller_access.c.membership_id == membership,
            ))
        elif revocation == "seller":
            connection.execute(seller_profiles.update().where(
                seller_profiles.c.id == seller,
            ).values(active=False))
        else:
            connection.execute(membership_permissions.delete().where(
                membership_permissions.c.membership_id == membership,
                membership_permissions.c.permission_code == "WORKSPACE_VIEW",
            ))

    assert client.get(f"/v1/sellers/{seller}").status_code == 404
    result = client.get("/v1/workspace")
    assert result.status_code == 200
    assert result.json()["sellers"] == []
    assert result.json()["active_seller"] is None
    assert workspace.repository.selected_seller(principal.session_id) is None


def test_inactive_auth_user_loses_workspace_access_immediately(workspace: WorkspaceFixture) -> None:
    user = workspace.user()
    org = workspace.organization("SELLER")
    seller = workspace.seller(org)
    workspace.membership(user, org, "SELLER_OWNER")
    client, principal = workspace.session(user)
    assert client.get(f"/v1/sellers/{seller}").status_code == 200
    with workspace.engine.begin() as connection:
        connection.execute(
            auth_users.update().where(auth_users.c.id == user.id).values(active=False)
        )
    assert client.get("/v1/workspace").status_code == 401
    assert workspace.service.overview(principal)["sellers"] == []


def test_active_seller_is_persisted_per_session_not_per_user(workspace: WorkspaceFixture) -> None:
    user = workspace.user()
    org = workspace.organization("SELLER")
    first = workspace.seller(org, "A")
    second = workspace.seller(org, "B")
    workspace.membership(user, org, "SELLER_OWNER")
    client_a, principal_a = workspace.session(user)
    client_b, principal_b = workspace.session(user)
    assert client_a.post("/v1/workspace/select", json={"seller_id": str(first)}).status_code == 200
    assert client_b.post("/v1/workspace/select", json={"seller_id": str(second)}).status_code == 200
    assert client_a.get("/v1/workspace").json()["active_seller"]["id"] == str(first)
    assert client_b.get("/v1/workspace").json()["active_seller"]["id"] == str(second)
    assert workspace.repository.selected_seller(principal_a.session_id) == first
    assert workspace.repository.selected_seller(principal_b.session_id) == second
    assert client_b.post("/v1/auth/logout").status_code == 204
    assert client_b.get("/v1/workspace").status_code == 401
    assert client_a.get("/v1/workspace").status_code == 200


def test_realm_alone_does_not_grant_agency_or_platform_scope(workspace: WorkspaceFixture) -> None:
    user = workspace.user()
    seller_org = workspace.organization("SELLER")
    seller = workspace.seller(seller_org)
    workspace.membership(user, seller_org, "SELLER_OWNER")
    for realm in (AuthRealm.AGENCY, AuthRealm.PLATFORM):
        client, _principal = workspace.session(user, realm)
        response = client.get("/v1/workspace").json()
        assert response["realm"] == realm.value
        assert response["organizations"] == []
        assert response["sellers"] == []
        assert client.get(f"/v1/sellers/{seller}").status_code == 404


def test_agency_access_in_seller_portal_retains_its_assigned_scope(
    workspace: WorkspaceFixture,
) -> None:
    user = workspace.user()
    agency = workspace.organization("AGENCY")
    org = workspace.organization("SELLER")
    seller = workspace.seller(org)
    workspace.link(agency, org)
    workspace.membership(user, agency, "AGENCY_USER", read_only=True)
    client, _principal = workspace.session(user, AuthRealm.SELLER)
    response = client.get("/v1/workspace").json()
    assert [item["id"] for item in response["sellers"]] == [str(seller)]
    assert {item["kind"] for item in response["organizations"]} == {"SELLER"}
    assert response["sellers"][0]["write_permissions"] == []


def test_highest_inherited_agency_role_wins_without_direct_membership(
    workspace: WorkspaceFixture,
) -> None:
    user = workspace.user()
    first = workspace.organization("AGENCY", "Prima")
    second = workspace.organization("AGENCY", "Seconda")
    org = workspace.organization("SELLER")
    seller = workspace.seller(org)
    for agency in (first, second):
        workspace.link(agency, org)
    workspace.membership(user, first, "AGENCY_USER", read_only=True)
    workspace.membership(user, second, "AGENCY_ADMIN")
    client, _principal = workspace.session(user, AuthRealm.AGENCY)
    item = client.get(f"/v1/sellers/{seller}").json()
    assert item["role_code"] == "AGENCY_ADMIN"
    assert "ACCOUNTING" in item["write_permissions"]
    assert len(client.get("/v1/workspace").json()["sellers"]) == 1


def test_platform_support_cannot_write_or_escape_explicit_seller_assignment(
    workspace: WorkspaceFixture,
) -> None:
    user = workspace.user()
    platform = workspace.organization("PLATFORM")
    own_org = workspace.organization("SELLER", "Consentita")
    foreign_org = workspace.organization("SELLER", "Estranea")
    own = workspace.seller(own_org)
    foreign = workspace.seller(foreign_org)
    workspace.membership(
        user, platform, "PLATFORM_SUPPORT", all_sellers=False, seller_ids=(own,),
    )
    client, principal = workspace.session(user, AuthRealm.PLATFORM)
    response = client.get("/v1/workspace").json()
    assert [item["id"] for item in response["sellers"]] == [str(own)]
    assert str(foreign_org) not in {item["id"] for item in response["organizations"]}
    assert response["sellers"][0]["write_permissions"] == []
    assert client.get(f"/v1/sellers/{foreign}").status_code == 404
    with pytest.raises(WorkspacePermissionError):
        workspace.service.require_seller(principal, own, permission="ACCOUNTING", write=True)


def test_direct_seller_needs_no_agency_parent_and_selection_survives_refresh(
    workspace: WorkspaceFixture,
) -> None:
    user = workspace.user()
    org = workspace.organization("SELLER", "Venditore diretto")
    seller = workspace.seller(org, "Negozio autonomo")
    workspace.membership(user, org, "SELLER_ADMIN")
    client, principal = workspace.session(user)
    result = client.post("/v1/workspace/select", json={"seller_id": str(seller)})
    assert result.status_code == 200
    refreshed_service = WorkspaceService(SqlWorkspaceRepository(workspace.engine))
    assert refreshed_service.overview(principal)["active_seller"]["id"] == str(seller)
    assert workspace.repository.profile_in_scope(seller, org)["name"] == "Negozio autonomo"
