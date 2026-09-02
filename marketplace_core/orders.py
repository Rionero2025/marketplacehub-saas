from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

from marketplace_core.contracts import JobRequest


@dataclass(frozen=True, slots=True)
class OrderScope:
    seller_id: int
    account_id: int
    marketplace: str
    environment: str = "live"

    @property
    def marketplace_key(self) -> str:
        return str(self.marketplace or "").strip().lower()


@dataclass(frozen=True, slots=True)
class OrderQuery:
    date_from: date | None = None
    date_to: date | None = None
    statuses: tuple[str, ...] = field(default_factory=tuple)
    suppliers: tuple[str, ...] = field(default_factory=tuple)
    storefronts: tuple[str, ...] = field(default_factory=tuple)
    currencies: tuple[str, ...] = field(default_factory=tuple)
    carriers: tuple[str, ...] = field(default_factory=tuple)
    search: str = ""
    limit: int = 250
    offset: int = 0
    include_raw: bool = False

    def __post_init__(self) -> None:
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValueError("date_to non può precedere date_from")
        if self.limit < 1:
            raise ValueError("limit deve essere maggiore di zero")
        if self.offset < 0:
            raise ValueError("offset non può essere negativo")


@dataclass(frozen=True, slots=True)
class OrderPage:
    items: tuple[Mapping[str, Any], ...]
    total: int
    limit: int
    offset: int
    has_more: bool

    @property
    def page_number(self) -> int:
        return (self.offset // max(1, self.limit)) + 1

    @property
    def page_count(self) -> int:
        if self.total <= 0:
            return 0
        return (self.total + self.limit - 1) // self.limit


class OrdersCore:
    """Streamlit-independent order application boundary.

    This core keeps the proven marketplace services intact while enforcing bounded
    reads for list screens. It is ready to be called by Streamlit today and by
    FastAPI/background workers later.
    """

    def archive_info(self, scope: OrderScope) -> dict[str, Any]:
        if scope.marketplace_key == "kaufland":
            from services.kaufland_orders import saved_orders_archive_info

            return saved_orders_archive_info(
                scope.seller_id, scope.account_id, scope.environment
            )

        from services.cecotec_orders import cached_order_facets

        return cached_order_facets(
            scope.seller_id, scope.account_id, scope.marketplace_key
        )

    def page(self, scope: OrderScope, query: OrderQuery) -> OrderPage:
        if scope.marketplace_key == "kaufland":
            from services.kaufland_orders import saved_orders_page

            result = saved_orders_page(
                scope.seller_id,
                scope.account_id,
                scope.environment,
                date_from=query.date_from,
                date_to=query.date_to,
                statuses=query.statuses,
                storefronts=query.storefronts,
                currencies=query.currencies,
                carriers=query.carriers,
                search=query.search,
                limit=query.limit,
                offset=query.offset,
                include_raw=query.include_raw,
            )
        else:
            from services.cecotec_orders import cached_orders_page

            result = cached_orders_page(
                scope.seller_id,
                scope.account_id,
                scope.marketplace_key,
                date_from=query.date_from,
                date_to=query.date_to,
                statuses=query.statuses,
                suppliers=query.suppliers,
                search=query.search,
                limit=query.limit,
                offset=query.offset,
                include_raw=query.include_raw,
            )

        return OrderPage(
            items=tuple(result.get("items") or ()),
            total=int(result.get("total") or 0),
            limit=int(result.get("limit") or query.limit),
            offset=int(result.get("offset") or query.offset),
            has_more=bool(result.get("has_more")),
        )


    def build_sync_job(
        self,
        scope: OrderScope,
        *,
        maximum: int | None = 1000,
        include_tracking_details: bool = True,
    ) -> JobRequest:
        market = scope.marketplace_key
        if market != "kaufland":
            raise ValueError("v305 background sync supporta Kaufland; Worten segue nel worker successivo")
        return JobRequest(
            kind="orders.kaufland.sync",
            seller_id=scope.seller_id,
            payload={
                "account_id": scope.account_id,
                "environment": scope.environment,
                "maximum": maximum,
                "include_tracking_details": bool(include_tracking_details),
            },
        )

    def sync_kaufland(
        self,
        scope: OrderScope,
        client: Any,
        *,
        maximum: int | None = 1000,
        include_tracking_details: bool = True,
        progress=None,
    ) -> dict[str, Any]:
        if scope.marketplace_key != "kaufland":
            raise ValueError("sync_kaufland richiede marketplace='kaufland'")
        from services.kaufland_orders import sync_orders

        return sync_orders(
            client,
            scope.seller_id,
            scope.account_id,
            scope.environment,
            maximum=maximum,
            include_tracking_details=include_tracking_details,
            progress=progress,
        )

    def sync_normalized(
        self,
        scope: OrderScope,
        credentials: Mapping[str, Any],
        *,
        date_from: date,
        date_to: date,
    ) -> int:
        """Sync Kaufland/Worten into the normalized shared order cache."""
        from services.cecotec_orders import (
            fetch_kaufland_orders,
            fetch_worten_orders,
            upsert_order_cache,
        )

        market = scope.marketplace_key
        if market == "kaufland":
            fresh = fetch_kaufland_orders(
                credentials,
                account_id=scope.account_id,
                date_from=date_from,
                date_to=date_to,
            )
        elif market == "worten":
            fresh = fetch_worten_orders(
                credentials,
                account_id=scope.account_id,
                date_from=date_from,
                date_to=date_to,
            )
        else:
            raise ValueError(f"Marketplace ordini non supportato: {market}")
        return upsert_order_cache(
            scope.seller_id, scope.account_id, market, fresh
        )
