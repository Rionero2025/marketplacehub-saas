from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from pydantic import SecretStr

from marketplace_hub_core.auth.models import AuthenticatedSession
from marketplace_hub_core.marketplace_connections.connectors import (
    ERRORS,
    ConnectionProbeError,
    MarketplaceConnector,
    normalize_credentials,
)
from marketplace_hub_core.marketplace_connections.repository import (
    VERIFICATION_KEY,
    SqlMarketplaceConnectionsRepository,
    settings_object,
)
from marketplace_hub_core.marketplace_connections.security import (
    credential_mask,
    decrypt_credentials,
)
from marketplace_hub_core.seller_settings.security import (
    CredentialStorageUnavailableError,
    encrypt_credentials,
)
from marketplace_hub_core.seller_settings.service import SellerSettingsValidationError
from marketplace_hub_core.tenancy.service import WorkspaceService


def _public_text(value, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _public_verification(raw) -> dict:
    """Legacy settings may contain arbitrary JSON; never relay it as public metadata."""
    values = raw if isinstance(raw, dict) else {}
    status = values.get("connection_status")
    if status not in ("unverified", "connected", "error"):
        status = "unverified"
    checked_at = _public_text(values.get("last_checked_at"), 64)
    try:
        datetime.fromisoformat(checked_at or "")
    except ValueError:
        checked_at = None
    storefronts = values.get("storefronts")
    if not isinstance(storefronts, list):
        storefronts = []
    error_code = values.get("error_code")
    return {
        "connection_status": status, "last_checked_at": checked_at,
        "public_name": _public_text(values.get("public_name")),
        "external_shop_id": _public_text(values.get("external_shop_id")),
        "storefronts": [code for code in storefronts if isinstance(code, str)
                        and 0 < len(code) <= 5 and code.isascii() and code.isalpha()][:100],
        "error_code": error_code if isinstance(error_code, str) and error_code in ERRORS else None,
    }


class MarketplaceConnectionsService:
    def __init__(
        self, repository: SqlMarketplaceConnectionsRepository, workspace: WorkspaceService,
        master_key: SecretStr, connector: MarketplaceConnector | None = None,
    ) -> None:
        self.repository = repository
        self.workspace = workspace
        self.master_key = master_key
        self.connector = connector or MarketplaceConnector()

    def _manage(self, principal: AuthenticatedSession, seller_id: UUID) -> UUID:
        seller = self.workspace.require_seller(
            principal, seller_id, permission="WORKSPACE_MANAGE", write=True,
        )
        return UUID(seller["organization_id"])

    def read(self, principal: AuthenticatedSession, seller_id: UUID) -> dict:
        seller = self.workspace.require_seller(principal, seller_id)
        rows = self.repository.list(seller_id, UUID(seller["organization_id"]))
        result = []
        for row in rows:
            verification = _public_verification(
                settings_object(row["settings_json"]).get(VERIFICATION_KEY, {}),
            )
            result.append({
                "id": str(row["id"]), "marketplace": row["marketplace"],
                "account_name": row["account_name"], "active": row["active"],
                **verification,
                "credential_mask": credential_mask(
                    row["marketplace"], row["credentials_encrypted"], self.master_key,
                ),
            })
        return {
            "seller_id": str(seller_id),
            "can_manage": "WORKSPACE_MANAGE" in seller["write_permissions"], "accounts": result,
        }

    async def connect(
        self, principal: AuthenticatedSession, seller_id: UUID, marketplace: str,
        account_name: str, credentials: dict[str, str],
    ) -> dict:
        organization_id = self._manage(principal, seller_id)
        account_name = account_name.strip()
        if not account_name or len(account_name) > 200:
            raise SellerSettingsValidationError("Indica un nome account di massimo 200 caratteri.")
        credentials = normalize_credentials(marketplace, credentials)
        # Fail locally before contacting the marketplace when secure storage is absent.
        encrypted = encrypt_credentials(credentials, self.master_key)
        started_at = datetime.now(UTC).isoformat()
        verified = await self.connector.verify(marketplace, credentials)
        self._manage(principal, seller_id)  # A grant may be revoked during the remote request.
        self.repository.add(seller_id, organization_id, marketplace, account_name, encrypted, {
            "connection_status": "connected", "last_checked_at": datetime.now(UTC).isoformat(),
            "verification_started_at": started_at, "error_code": None, **asdict(verified),
        })
        return self.read(principal, seller_id)

    async def verify(
        self, principal: AuthenticatedSession, seller_id: UUID, account_id: UUID,
    ) -> dict:
        organization_id = self._manage(principal, seller_id)
        account = self.repository.get(seller_id, organization_id, account_id)
        verification = {
            "connection_status": "error", "public_name": None, "external_shop_id": None,
            "storefronts": [], "error_code": None,
            "verification_started_at": datetime.now(UTC).isoformat(),
        }
        try:
            credentials = decrypt_credentials(account["credentials_encrypted"], self.master_key)
            metadata = await self.connector.verify(account["marketplace"], credentials)
            verification.update(connection_status="connected", **asdict(metadata))
        except CredentialStorageUnavailableError:
            verification["error_code"] = "credentials_unavailable"
        except ConnectionProbeError as exc:
            verification["error_code"] = exc.code
        self._manage(principal, seller_id)
        verification["last_checked_at"] = datetime.now(UTC).isoformat()
        self.repository.record_verification(seller_id, organization_id, account_id, verification)
        return self.read(principal, seller_id)

    def delete(
        self, principal: AuthenticatedSession, seller_id: UUID, account_id: UUID,
        confirmation: str,
    ) -> dict:
        organization_id = self._manage(principal, seller_id)
        if confirmation != "ELIMINA":
            raise SellerSettingsValidationError("Scrivi ELIMINA per confermare la rimozione.")
        self.repository.delete(seller_id, organization_id, account_id)
        return self.read(principal, seller_id)
