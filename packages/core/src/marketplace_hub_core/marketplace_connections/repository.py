import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, select

from marketplace_hub_core.seller_settings.repository import MarketplaceAccountNotFoundError
from marketplace_hub_core.seller_settings.schema import seller_marketplace_accounts as accounts
from marketplace_hub_core.tenancy.schema import seller_profiles as profiles
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError

VERIFICATION_KEY = "marketplace_hub_connection_v1"


def settings_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


class SqlMarketplaceConnectionsRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _scope(seller_id: UUID, organization_id: UUID):
        return accounts.c.seller_id == seller_id, accounts.c.organization_id == organization_id

    @staticmethod
    def _require_profile(connection, seller_id: UUID, organization_id: UUID) -> None:
        exists = connection.execute(select(profiles.c.id).where(
            profiles.c.id == seller_id, profiles.c.organization_id == organization_id,
            profiles.c.active.is_(True),
        )).first()
        if exists is None:
            raise SellerNotAccessibleError("Negozio non disponibile.")

    def list(self, seller_id: UUID, organization_id: UUID) -> list[dict]:
        with self.engine.connect() as connection:
            self._require_profile(connection, seller_id, organization_id)
            rows = connection.execute(select(accounts).where(
                *self._scope(seller_id, organization_id),
            ).order_by(accounts.c.marketplace, accounts.c.account_name, accounts.c.id)).mappings()
            return [dict(row) for row in rows]

    def get(self, seller_id: UUID, organization_id: UUID, account_id: UUID) -> dict:
        with self.engine.connect() as connection:
            self._require_profile(connection, seller_id, organization_id)
            row = connection.execute(select(accounts).where(
                *self._scope(seller_id, organization_id), accounts.c.id == account_id,
            )).mappings().first()
            if row is None:
                raise MarketplaceAccountNotFoundError("Account marketplace non disponibile.")
            return dict(row)

    def add(
        self, seller_id: UUID, organization_id: UUID, marketplace: str,
        account_name: str, encrypted: str, verification: dict,
    ) -> None:
        with self.engine.begin() as connection:
            self._require_profile(connection, seller_id, organization_id)
            now = datetime.now(UTC)
            connection.execute(accounts.insert().values(
                id=uuid4(), seller_id=seller_id, organization_id=organization_id,
                marketplace=marketplace, account_name=account_name,
                credentials_encrypted=encrypted, active=True,
                settings_json=json.dumps({VERIFICATION_KEY: verification}, ensure_ascii=False),
                created_at=now, updated_at=now,
            ))

    def record_verification(
        self, seller_id: UUID, organization_id: UUID, account_id: UUID, verification: dict,
    ) -> None:
        with self.engine.begin() as connection:
            self._require_profile(connection, seller_id, organization_id)
            # Lock the current account before merging: verification never replaces
            # imported credentials, account identity or unrelated source settings.
            row = connection.execute(select(accounts.c.settings_json).where(
                *self._scope(seller_id, organization_id), accounts.c.id == account_id,
            ).with_for_update()).first()
            if row is None:
                raise MarketplaceAccountNotFoundError("Account marketplace non disponibile.")
            settings = settings_object(row.settings_json)
            if not settings and row.settings_json not in ("", "{}", None):
                settings["legacy_settings_original"] = row.settings_json
            previous = settings.get(VERIFICATION_KEY)
            previous_started = None
            try:
                if isinstance(previous, dict):
                    previous_started = datetime.fromisoformat(
                        previous.get("verification_started_at", ""),
                    )
                current_started = datetime.fromisoformat(verification["verification_started_at"])
                superseded = previous_started is not None and previous_started > current_started
            except (ValueError, TypeError):
                superseded = False
            if superseded:
                # A slower earlier check must not overwrite the result of a newer check.
                return
            settings[VERIFICATION_KEY] = verification
            connection.execute(accounts.update().where(
                *self._scope(seller_id, organization_id), accounts.c.id == account_id,
            ).values(settings_json=json.dumps(settings, ensure_ascii=False),
                     updated_at=datetime.now(UTC)))

    def delete(self, seller_id: UUID, organization_id: UUID, account_id: UUID) -> None:
        with self.engine.begin() as connection:
            self._require_profile(connection, seller_id, organization_id)
            result = connection.execute(accounts.delete().where(
                *self._scope(seller_id, organization_id), accounts.c.id == account_id,
            ))
            if result.rowcount != 1:
                raise MarketplaceAccountNotFoundError("Account marketplace non disponibile.")
