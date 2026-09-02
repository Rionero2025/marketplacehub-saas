from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AccountingScope:
    seller_id: int
    account_id: int
    marketplace: str

    @property
    def marketplace_key(self) -> str:
        return str(self.marketplace or "").strip().lower()


@dataclass(frozen=True, slots=True)
class AccountingPeriod:
    date_from: date
    date_to: date

    def __post_init__(self) -> None:
        if self.date_to < self.date_from:
            raise ValueError("date_to non può precedere date_from")


@dataclass(frozen=True, slots=True)
class AccountingStatus:
    environment: str
    sync_state: dict[str, Any]
    cache_summary: dict[str, Any]


class AccountingCore:
    """Stable application boundary for accounting use-cases.

    The current implementation delegates the proven calculations to
    ``services.accounting``.  UI-specific code only supplies inputs and renders
    outputs.  This boundary can therefore be reused unchanged by FastAPI and
    background workers during the SaaS migration.
    """

    @staticmethod
    def _service():
        # Lazy import keeps the Core package cheap to import and avoids pulling
        # marketplace integrations into processes which don't need them.
        from services import accounting
        return accounting

    def status(
        self,
        scope: AccountingScope,
        credentials: Mapping[str, Any],
    ) -> AccountingStatus:
        svc = self._service()
        environment = svc.accounting_sync_environment(
            scope.marketplace_key, credentials
        )
        sync_state = svc.accounting_sync_state(
            scope.seller_id,
            scope.account_id,
            scope.marketplace_key,
            environment=environment,
        )
        cache_summary = svc.accounting_cache_summary(
            scope.seller_id, scope.account_id, scope.marketplace_key
        )
        return AccountingStatus(
            environment=environment,
            sync_state=dict(sync_state),
            cache_summary=dict(cache_summary),
        )

    def catalog_selection(self, scope: AccountingScope) -> dict[str, Any]:
        return self._service().accounting_catalog_selection(scope.seller_id)

    def save_catalog_selection(
        self, scope: AccountingScope, price_list_ids: list[int]
    ) -> None:
        self._service().save_accounting_catalog_selection(
            scope.seller_id, price_list_ids
        )

    def synchronize(
        self,
        scope: AccountingScope,
        credentials: Mapping[str, Any],
        period: AccountingPeriod,
        *,
        full: bool = False,
    ) -> dict[str, Any]:
        return self._service().synchronize_accounting_orders(
            credentials,
            seller_id=scope.seller_id,
            account_id=scope.account_id,
            marketplace=scope.marketplace_key,
            date_from=period.date_from,
            date_to=period.date_to,
            full=full,
        )

    def refresh_costs(
        self,
        scope: AccountingScope,
        period: AccountingPeriod,
    ) -> dict[str, int]:
        return self._service().refresh_accounting_costs(
            scope.seller_id,
            scope.account_id,
            scope.marketplace_key,
            date_from=period.date_from,
            date_to=period.date_to,
        )

    def rows(
        self,
        scope: AccountingScope,
        *,
        period: AccountingPeriod | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if period is not None:
            kwargs = {
                "date_from": period.date_from,
                "date_to": period.date_to,
            }
        return self._service().accounting_rows(
            scope.seller_id,
            scope.account_id,
            scope.marketplace_key,
            **kwargs,
        )
