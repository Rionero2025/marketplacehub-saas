import math
from uuid import UUID

from pydantic import SecretStr

from marketplace_hub_core.auth.models import AuthenticatedSession
from marketplace_hub_core.seller_settings.profit_sharing import normalized_percentages
from marketplace_hub_core.seller_settings.repository import SqlSellerSettingsRepository
from marketplace_hub_core.seller_settings.security import encrypt_credentials, masked_client_key
from marketplace_hub_core.tenancy.service import WorkspaceService


class SellerSettingsValidationError(ValueError):
    pass


class SellerSettingsService:
    def __init__(
        self, repository: SqlSellerSettingsRepository, workspace: WorkspaceService,
        master_key: SecretStr,
    ) -> None:
        self.repository = repository
        self.workspace = workspace
        self.master_key = master_key

    def read(self, principal: AuthenticatedSession, seller_id: UUID) -> dict:
        seller = self.workspace.require_seller(principal, seller_id)
        result = self.repository.read(seller_id, UUID(seller["organization_id"]))
        result["our_profit_pct"], result["partner_profit_pct"] = normalized_percentages(
            result["our_profit_pct"], result["partner_profit_pct"],
        )
        result["can_manage"] = "WORKSPACE_MANAGE" in seller["write_permissions"]
        for account in result["marketplace_accounts"]:
            account["client_key_masked"] = masked_client_key(
                account.pop("credentials_encrypted"), self.master_key,
            )
        return result

    def update(self, principal: AuthenticatedSession, seller_id: UUID, values: dict) -> dict:
        seller = self.workspace.require_seller(
            principal, seller_id, permission="WORKSPACE_MANAGE", write=True,
        )
        values = dict(values)
        for key in ("name", "legal_name", "email"):
            values[key] = values[key].strip()
        if not values["name"]:
            raise SellerSettingsValidationError("Indica il nome del negozio.")
        our, partner = values["our_profit_pct"], values["partner_profit_pct"]
        if any(not math.isfinite(pct) or not 0 <= pct <= 100 for pct in (our, partner)):
            raise SellerSettingsValidationError(
                "Le percentuali devono essere comprese tra 0 e 100."
            )
        if abs((our + partner) - 100.0) > 0.01:
            raise SellerSettingsValidationError("La somma delle percentuali deve essere 100%.")
        self.repository.update(seller_id, UUID(seller["organization_id"]), values)
        return self.read(principal, seller_id)

    def add_account(
        self, principal: AuthenticatedSession, seller_id: UUID,
        account_name: str, client_key: SecretStr, secret_key: SecretStr,
    ) -> dict:
        seller = self.workspace.require_seller(
            principal, seller_id, permission="WORKSPACE_MANAGE", write=True,
        )
        account_name = account_name.strip()
        client = client_key.get_secret_value().strip()
        secret = secret_key.get_secret_value().strip()
        if not account_name or not (client or secret):
            raise SellerSettingsValidationError("Indica il nome account e almeno una chiave API.")
        encrypted = encrypt_credentials(
            {"client_key": client, "secret_key": secret}, self.master_key,
        )
        self.repository.add_account(
            seller_id, UUID(seller["organization_id"]), account_name, encrypted,
        )
        return self.read(principal, seller_id)

    def delete_account(
        self, principal: AuthenticatedSession, seller_id: UUID, account_id: UUID,
        confirmation: str,
    ) -> dict:
        seller = self.workspace.require_seller(
            principal, seller_id, permission="WORKSPACE_MANAGE", write=True,
        )
        if confirmation != "ELIMINA":
            raise SellerSettingsValidationError("Scrivi ELIMINA per confermare la rimozione.")
        self.repository.delete_account(seller_id, UUID(seller["organization_id"]), account_id)
        return self.read(principal, seller_id)
