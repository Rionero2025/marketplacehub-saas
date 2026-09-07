from __future__ import annotations

from uuid import UUID

from marketplace_hub_core.auth.models import AuthenticatedSession
from marketplace_hub_core.tenancy.constants import PERMISSION_LABELS
from marketplace_hub_core.tenancy.repository import SqlWorkspaceRepository


class SellerNotAccessibleError(ValueError):
    pass


class WorkspacePermissionError(ValueError):
    pass


def _priority(grant: dict) -> tuple[int, str]:
    code = grant["role_code"]
    weight = 4 if code.endswith("_OWNER") else 3 if code.endswith("_ADMIN") else 2
    if grant["read_only"] or "VIEW_ONLY" in grant["permissions"]:
        weight = 1
    return weight, str(grant["id"])


def _write_permissions(grant: dict) -> list[str]:
    if (
        grant["read_only"]
        or "VIEW_ONLY" in grant["permissions"]
        or grant["role_code"] == "PLATFORM_SUPPORT"
    ):
        return []
    return sorted(grant["permissions"] - {"VIEW_ONLY", "WORKSPACE_VIEW"})


def _organization(grant: dict) -> dict:
    return {
        "id": str(grant["organization_id"]),
        "name": grant["organization_name"],
        "kind": grant["organization_kind"],
        "role_code": grant["role_code"],
        "role_label": grant["label"],
        "read_only": not bool(_write_permissions(grant)),
        "permissions": sorted(grant["permissions"]),
    }


def _seller(profile: dict, grant: dict) -> dict:
    return {
        "id": str(profile["id"]),
        "organization_id": str(profile["organization_id"]),
        "organization_name": profile["organization_name"],
        "name": profile["name"],
        "legal_name": profile["legal_name"],
        "email": profile["email"],
        "role_code": grant["role_code"],
        "role_label": grant["label"],
        "permissions": sorted(grant["permissions"]),
        "write_permissions": _write_permissions(grant),
    }


class WorkspaceService:
    def __init__(self, repository: SqlWorkspaceRepository) -> None:
        self.repository = repository

    def _available(self, principal: AuthenticatedSession) -> tuple[list[dict], list[dict]]:
        grants = self.repository.grants(principal.user_id)
        direct = {g["organization_id"]: g for g in grants if g["organization_kind"] == "SELLER"}
        sources = grants
        if principal.realm == "platform":
            sources = [g for g in grants if g["organization_kind"] == "PLATFORM"]
        elif principal.realm == "agency":
            sources = [g for g in grants if g["organization_kind"] in {"PLATFORM", "AGENCY"}]

        visible_orgs: dict[str, dict] = {}
        candidates: dict[UUID, list[tuple[dict, dict]]] = {}
        for grant in sources:
            if (
                principal.realm == "platform"
                or (principal.realm == "agency" and grant["organization_kind"] == "AGENCY")
                or (principal.realm == "seller" and grant["organization_kind"] == "SELLER")
            ):
                visible_orgs[str(grant["organization_id"])] = _organization(grant)
            if grant["organization_kind"] == "PLATFORM":
                for org in self.repository.platform_organizations(grant):
                    if principal.realm == "seller" and org["kind"] != "SELLER":
                        continue
                    if principal.realm == "agency" and org["kind"] == "PLATFORM":
                        continue
                    item = _organization(grant)
                    item.update(id=str(org["id"]), name=org["name"], kind=org["kind"])
                    visible_orgs[item["id"]] = item
            for profile in self.repository.sellers_for_grant(grant):
                candidates.setdefault(profile["id"], []).append((profile, grant))

        sellers = []
        direct_allowed: dict[UUID, set[UUID]] = {}
        for options in candidates.values():
            profile = options[0][0]
            platform_options = [o for o in options if o[1]["organization_kind"] == "PLATFORM"]
            if platform_options:
                # Explicit Platform membership retains its separate administrative scope.
                _, chosen = max(platform_options, key=lambda o: _priority(o[1]))
            elif profile["organization_id"] in direct:
                chosen = direct[profile["organization_id"]]
                if chosen["id"] not in direct_allowed:
                    direct_allowed[chosen["id"]] = {
                        row["id"] for row in self.repository.sellers_for_grant(chosen)
                    }
                if profile["id"] not in direct_allowed[chosen["id"]]:
                    continue
            else:
                _, chosen = max(options, key=lambda o: _priority(o[1]))
            if "WORKSPACE_VIEW" not in chosen["permissions"]:
                continue
            seller = _seller(profile, chosen)
            sellers.append(seller)
            organization = _organization(chosen)
            organization.update(
                id=seller["organization_id"], name=seller["organization_name"], kind="SELLER"
            )
            visible_orgs[organization["id"]] = organization
        return (
            sorted(visible_orgs.values(), key=lambda org: (org["name"].casefold(), org["id"])),
            sorted(sellers, key=lambda seller: (seller["name"].casefold(), seller["id"])),
        )

    def overview(self, principal: AuthenticatedSession) -> dict:
        organizations, sellers = self._available(principal)
        previous = self.repository.selected_seller(principal.session_id)
        selected = next((seller for seller in sellers if seller["id"] == str(previous)), None)
        if previous and selected is None:
            self.repository.clear_selection(principal.session_id)
        return {
            "realm": principal.realm.value,
            "organizations": organizations,
            "sellers": sellers,
            "active_seller": selected or (sellers[0] if sellers else None),
            "permission_labels": PERMISSION_LABELS,
        }

    def require_seller(
        self,
        principal: AuthenticatedSession,
        seller_id: UUID,
        *,
        permission: str = "WORKSPACE_VIEW",
        write: bool = False,
    ) -> dict:
        _, sellers = self._available(principal)
        seller = next((item for item in sellers if item["id"] == str(seller_id)), None)
        if seller is None:
            raise SellerNotAccessibleError("Negozio non disponibile.")
        allowed = seller["write_permissions"] if write else seller["permissions"]
        if permission not in allowed:
            raise WorkspacePermissionError("Operazione non autorizzata.")
        return seller

    def select_seller(self, principal: AuthenticatedSession, seller_id: UUID) -> dict:
        self.require_seller(principal, seller_id)
        self.repository.save_selection(principal.session_id, seller_id)
        return self.overview(principal)
