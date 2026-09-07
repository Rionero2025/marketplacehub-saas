from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, select

from marketplace_hub_core.seller_settings.schema import (
    seller_marketplace_accounts as accounts,
)
from marketplace_hub_core.tenancy.schema import seller_profiles as profiles
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError


class MarketplaceAccountNotFoundError(ValueError):
    pass


class SqlSellerSettingsRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _scope(seller_id: UUID, organization_id: UUID):
        return (
            profiles.c.id == seller_id,
            profiles.c.organization_id == organization_id,
            profiles.c.active.is_(True),
        )

    def read(self, seller_id: UUID, organization_id: UUID) -> dict:
        with self.engine.connect() as connection:
            profile = connection.execute(
                select(
                    profiles.c.id.label("seller_id"), profiles.c.name,
                    profiles.c.legal_name, profiles.c.email,
                )
                .where(*self._scope(seller_id, organization_id))
            ).mappings().first()
            if profile is None:
                raise SellerNotAccessibleError("Negozio non disponibile.")
            # Service consumes ciphertext internally to build the legacy client-key mask.
            safe_accounts = connection.execute(
                select(
                    accounts.c.id, accounts.c.marketplace, accounts.c.account_name,
                    accounts.c.active,
                    accounts.c.credentials_encrypted,
                    (accounts.c.credentials_encrypted != "").label("credentials_configured"),
                ).where(
                    accounts.c.seller_id == seller_id,
                    accounts.c.organization_id == organization_id,
                ).order_by(accounts.c.marketplace, accounts.c.account_name, accounts.c.id)
            ).mappings().all()
        result = dict(profile)
        result["seller_id"] = str(result["seller_id"])
        result["marketplace_accounts"] = [dict(row, id=str(row["id"])) for row in safe_accounts]
        return result

    def update(self, seller_id: UUID, organization_id: UUID, values: dict) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(
                profiles.update().where(*self._scope(seller_id, organization_id)).values(
                    name=values["name"], legal_name=values["legal_name"], email=values["email"],
                    updated_at=datetime.now(UTC),
                )
            )
            if result.rowcount != 1:
                raise SellerNotAccessibleError("Negozio non disponibile.")

    def add_account(
        self, seller_id: UUID, organization_id: UUID, account_name: str, encrypted: str,
    ) -> None:
        with self.engine.begin() as connection:
            profile = connection.execute(
                select(profiles.c.id).where(*self._scope(seller_id, organization_id))
            ).first()
            if profile is None:
                raise SellerNotAccessibleError("Negozio non disponibile.")
            now = datetime.now(UTC)
            connection.execute(accounts.insert().values(
                id=uuid4(), seller_id=seller_id, organization_id=organization_id,
                marketplace="kaufland", account_name=account_name,
                credentials_encrypted=encrypted, active=True, created_at=now, updated_at=now,
            ))

    def delete_account(self, seller_id: UUID, organization_id: UUID, account_id: UUID) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(accounts.delete().where(
                accounts.c.id == account_id, accounts.c.seller_id == seller_id,
                accounts.c.organization_id == organization_id, accounts.c.marketplace == "kaufland",
            ))
            if result.rowcount != 1:
                raise MarketplaceAccountNotFoundError("Account marketplace non disponibile.")
