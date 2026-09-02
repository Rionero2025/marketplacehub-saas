from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from marketplace_core.contracts import JobRequest
from services.db import execute_many, json_text, rows
from services.kaufland_buybox_fast import QuickBuyboxNeedsFullCheck, quick_buybox_check


@dataclass(frozen=True, slots=True)
class BuyBoxScope:
    seller_id: int
    account_id: int
    marketplace: str
    environment: str = "live"
    price_list_id: int | None = None
    channel_code: str = "WRT_PT_ONLINE"

    @property
    def marketplace_key(self) -> str:
        return str(self.marketplace or "").strip().lower()


@dataclass(frozen=True, slots=True)
class BuyBoxQuery:
    storefronts: tuple[str, ...] = field(default_factory=tuple)
    statuses: tuple[str, ...] = field(default_factory=tuple)
    search: str = ""
    limit: int = 250
    offset: int = 0
    include_details: bool = False

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit deve essere maggiore di zero")
        if self.offset < 0:
            raise ValueError("offset non può essere negativo")


@dataclass(frozen=True, slots=True)
class BuyBoxPage:
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


class BuyBoxCore:
    """UI-independent Buy Box boundary.

    Current Streamlit pages can call this class today. The exact same methods can
    later be exposed by FastAPI and executed by background workers.
    """

    def build_refresh_job(
        self,
        scope: BuyBoxScope,
        *,
        mode: str,
        storefronts: tuple[str, ...] = (),
        skus: tuple[str, ...] = (),
        tasks: tuple[dict[str, Any], ...] = (),
        own_seller_pseudonyms: tuple[str, ...] = (),
        max_workers: int = 20,
    ) -> JobRequest:
        mode_key = str(mode or "quick").strip().lower()
        if mode_key not in {"quick", "full"}:
            raise ValueError("mode Buy Box supportato: quick/full")
        return JobRequest(
            kind=f"buybox.{scope.marketplace_key}.{mode_key}",
            seller_id=scope.seller_id,
            payload={
                "account_id": scope.account_id,
                "environment": scope.environment,
                "price_list_id": scope.price_list_id,
                "channel_code": scope.channel_code,
                "storefronts": list(storefronts),
                "skus": list(skus),
                "tasks": list(tasks),
                "own_seller_pseudonyms": list(own_seller_pseudonyms),
                "max_workers": max(1, int(max_workers)),
            },
        )

    def saved_page(self, scope: BuyBoxScope, query: BuyBoxQuery) -> BuyBoxPage:
        market = scope.marketplace_key
        params: list[Any] = []
        where: list[str] = []

        if market == "kaufland":
            table = "kaufland_buybox_account_checks"
            where.extend([
                "seller_id=?", "marketplace_account_id=?", "environment=?"
            ])
            params.extend([scope.seller_id, scope.account_id, scope.environment])
            search_cols = ("sku", "ean", "product_title", "supplier_name", "status")
            order_by = "checked_at DESC, storefront, sku"
        elif market == "worten":
            if scope.price_list_id is None:
                raise ValueError("price_list_id è obbligatorio per Worten")
            table = "worten_buybox_checks"
            where.extend([
                "seller_id=?", "marketplace_account_id=?", "price_list_id=?", "channel_code=?"
            ])
            params.extend([
                scope.seller_id, scope.account_id, scope.price_list_id, scope.channel_code
            ])
            search_cols = ("sku", "ean", "category_label", "status")
            order_by = "checked_at DESC, ean, sku"
        else:
            raise ValueError(f"Marketplace Buy Box non supportato: {market}")

        if query.storefronts and market == "kaufland":
            placeholders = ",".join("?" for _ in query.storefronts)
            where.append(f"storefront IN ({placeholders})")
            params.extend(query.storefronts)
        if query.statuses:
            placeholders = ",".join("?" for _ in query.statuses)
            where.append(f"status IN ({placeholders})")
            params.extend(query.statuses)
        search = str(query.search or "").strip()
        if search:
            like = f"%{search}%"
            where.append("(" + " OR ".join(f"{col} LIKE ?" for col in search_cols) + ")")
            params.extend([like] * len(search_cols))

        where_sql = " AND ".join(where)
        count_row = rows(
            f"SELECT COUNT(*) AS total FROM {table} WHERE {where_sql}", tuple(params)
        )
        total = int((count_row[0].get("total") if count_row else 0) or 0)

        columns = "*" if query.include_details else "*"
        # details_json is deliberately excluded from list endpoints unless requested.
        if not query.include_details:
            if market == "kaufland":
                columns = """id,seller_id,marketplace_account_id,matched_price_list_id,
                    matched_saved_view_id,supplier_name,price_list_name,cost_match_source,
                    cost_match_count,storefront,environment,ean,sku,original_sku,product_title,
                    inventory_status,inventory_amount,id_product,id_unit,status,our_rank,
                    winner_seller,winner_price,winner_shipping,winner_total,our_price,our_shipping,
                    our_total,target_price,currency,offer_count,error_type,error,purchase_cost_eur,
                    shipping_cost_eur,total_cost_eur,commission_pct,commission_fixed_eur,
                    commission_source,profit_eur,profit_pct,profit_status,minimum_price,
                    minimum_price_source,checked_at"""
            else:
                columns = """id,seller_id,marketplace_account_id,price_list_id,channel_code,
                    ean,sku,original_sku,product_sku,category_code,category_label,status,our_rank,
                    winner_shop_id,winner_shop_name,winner_price,winner_shipping,winner_total,
                    our_price,our_shipping,our_total,currency,offer_count,competitor_visible,
                    purchase_cost_eur,shipping_cost_eur,total_cost_eur,commission_pct,
                    commission_source,profit_at_buybox_eur,margin_at_buybox_pct,economic_status,
                    error,checked_at"""

        items = rows(
            f"SELECT {columns} FROM {table} WHERE {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*params, query.limit, query.offset),
        )
        return BuyBoxPage(
            items=tuple(items), total=total, limit=query.limit, offset=query.offset,
            has_more=query.offset + len(items) < total,
        )

    def saved_summary(self, scope: BuyBoxScope) -> dict[str, Any]:
        market = scope.marketplace_key
        if market == "kaufland":
            result = rows(
                """SELECT COUNT(*) AS total, MAX(checked_at) AS latest_checked_at
                   FROM kaufland_buybox_account_checks
                   WHERE seller_id=? AND marketplace_account_id=? AND environment=?""",
                (scope.seller_id, scope.account_id, scope.environment),
            )
        elif market == "worten":
            if scope.price_list_id is None:
                raise ValueError("price_list_id è obbligatorio per Worten")
            result = rows(
                """SELECT COUNT(*) AS total, MAX(checked_at) AS latest_checked_at
                   FROM worten_buybox_checks
                   WHERE seller_id=? AND marketplace_account_id=? AND price_list_id=? AND channel_code=?""",
                (scope.seller_id, scope.account_id, scope.price_list_id, scope.channel_code),
            )
        else:
            raise ValueError(f"Marketplace Buy Box non supportato: {market}")
        item = dict(result[0]) if result else {}
        return {
            "total": int(item.get("total") or 0),
            "latest_checked_at": str(item.get("latest_checked_at") or ""),
        }


def kaufland_previous_checks(
    self, scope: BuyBoxScope, tasks: list[dict[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    storefronts = sorted({
        str(item.get("paese") or "").strip().lower() for item in tasks
        if str(item.get("paese") or "").strip()
    })
    if not storefronts:
        return {}
    placeholders = ",".join("?" for _ in storefronts)
    saved = rows(
        f"""SELECT * FROM kaufland_buybox_account_checks
            WHERE seller_id=? AND marketplace_account_id=? AND environment=?
              AND storefront IN ({placeholders})""",
        (scope.seller_id, scope.account_id, scope.environment, *storefronts),
    )
    return {
        (
            str(item.get("storefront") or "").strip().lower(),
            str(item.get("sku") or "").strip(),
        ): dict(item)
        for item in saved
    }

def persist_kaufland_checks(
    self, scope: BuyBoxScope, results: list[dict[str, Any]]
) -> int:
    if not results:
        return 0
    payloads: list[dict[str, Any]] = []
    for result in results:
        payloads.append({
            "seller_id": scope.seller_id,
            "marketplace_account_id": scope.account_id,
            "matched_price_list_id": result.get("matched_price_list_id"),
            "matched_saved_view_id": result.get("matched_saved_view_id"),
            "supplier_name": result.get("supplier_name", ""),
            "price_list_name": result.get("price_list_name", ""),
            "cost_match_source": result.get("cost_match_source", ""),
            "cost_match_count": result.get("cost_match_count", 0),
            "storefront": result["paese"],
            "environment": scope.environment,
            "ean": result["ean"],
            "sku": result["sku"],
            "original_sku": result.get("original_sku", ""),
            "product_title": result.get("product_title", ""),
            "inventory_status": result.get("inventory_status", ""),
            "inventory_amount": result.get("inventory_amount"),
            "id_product": result.get("id_product"),
            "id_unit": result.get("id_unit"),
            "status": result["status"],
            "our_rank": result.get("our_rank"),
            "winner_seller": result.get("winner_seller", ""),
            "winner_price": result.get("winner_price"),
            "winner_shipping": result.get("winner_shipping"),
            "winner_total": result.get("winner_total"),
            "our_price": result.get("our_price"),
            "our_shipping": result.get("our_shipping"),
            "our_total": result.get("our_total"),
            "minimum_price": result.get("minimum_price"),
            "minimum_price_source": result.get("minimum_price_source", ""),
            "target_price": result.get("target_price"),
            "currency": result.get("currency", ""),
            "delivery_min": result.get("delivery_min"),
            "delivery_max": result.get("delivery_max"),
            "own_delivery_min": result.get("own_delivery_min"),
            "own_delivery_max": result.get("own_delivery_max"),
            "own_handling_time": result.get("own_handling_time"),
            "logistics_status": result.get("logistics_status", ""),
            "offer_count": result.get("offer_count", 0),
            "error_type": result.get("error_type", ""),
            "error": result.get("error", ""),
            "details_json": json_text(result.get("details", {})),
            "purchase_cost_eur": result.get("purchase_cost_eur"),
            "shipping_cost_eur": result.get("shipping_cost_eur"),
            "total_cost_eur": result.get("total_cost_eur"),
            "commission_pct": result.get("commission_pct"),
            "commission_fixed_eur": result.get("commission_fixed_eur", 0),
            "commission_source": result.get("commission_source", ""),
            "current_commission_eur": result.get("current_commission_eur"),
            "current_commission_effective_pct": result.get("current_commission_effective_pct"),
            "actual_order_commission_pct": result.get("actual_order_commission_pct"),
            "actual_order_commission_local": result.get("actual_order_commission_local"),
            "actual_order_commission_currency": result.get("actual_order_commission_currency", ""),
            "actual_order_id": result.get("actual_order_id", ""),
            "target_sales_price": result.get("target_sales_price"),
            "target_sales_price_eur": result.get("target_sales_price_eur"),
            "target_source": result.get("target_source", ""),
            "target_commission_eur": result.get("target_commission_eur"),
            "target_commission_effective_pct": result.get("target_commission_effective_pct"),
            "profit_eur": result.get("profit_eur"),
            "profit_pct": result.get("profit_pct"),
            "profit_status": result.get("profit_status", ""),
            "checked_at": result["checked_at"],
        })
    columns = list(payloads[0])
    immutable = {"seller_id", "marketplace_account_id", "storefront", "environment", "sku"}
    mutable = [column for column in columns if column not in immutable]
    return execute_many(
        f"""INSERT INTO kaufland_buybox_account_checks({','.join(columns)})
            VALUES({','.join('?' for _ in columns)})
            ON CONFLICT(marketplace_account_id,storefront,environment,sku)
            DO UPDATE SET {','.join(f'{column}=excluded.{column}' for column in mutable)}""",
        [tuple(payload[column] for column in columns) for payload in payloads],
    )

    def run_kaufland_quick_batch(
        self,
        client: Any,
        tasks: list[dict[str, Any]],
        *,
        previous_by_offer: Mapping[tuple[str, str], Mapping[str, Any]],
        own_seller_pseudonyms: Any,
        checked_at: str,
        max_workers: int = 20,
        on_progress: Callable[[int, int, dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Run the already-proven one-request-per-offer Kaufland quick path.

        Persistence stays outside this method so the caller/worker can commit in
        chunks and retry safely. No Streamlit dependency exists here.
        """
        if not tasks:
            return []

        def one(item: dict[str, Any]) -> dict[str, Any]:
            key = (
                str(item.get("paese") or "").strip().lower(),
                str(item.get("SKU inviato") or "").strip(),
            )
            previous = dict(previous_by_offer.get(key) or {})
            try:
                result = quick_buybox_check(
                    client,
                    item,
                    cached=previous,
                    own_seller_pseudonyms=own_seller_pseudonyms,
                    checked_at=checked_at,
                )
                return {
                    "kind": "ok", "result": result,
                    "previous_status": str(previous.get("status") or ""),
                }
            except QuickBuyboxNeedsFullCheck as error:
                return {"kind": "needs_full", "item": item, "error": str(error)}
            except Exception as error:  # network/API errors belong to the result
                return {"kind": "error", "item": item, "error": str(error)}

        results: list[dict[str, Any]] = []
        workers = min(max(1, int(max_workers)), len(tasks))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(one, item) for item in tasks]
            for completed, future in enumerate(as_completed(futures), 1):
                outcome = future.result()
                results.append(outcome)
                if on_progress is not None:
                    on_progress(completed, len(tasks), outcome)
        return results
