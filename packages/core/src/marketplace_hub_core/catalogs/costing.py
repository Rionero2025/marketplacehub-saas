from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, desc, func, select
from sqlalchemy.exc import SQLAlchemyError

from marketplace_hub_core.catalogs.schema import (
    seller_price_list_products as products,
)
from marketplace_hub_core.catalogs.schema import (
    seller_price_lists as price_lists,
)
from marketplace_hub_core.catalogs.schema import (
    seller_suppliers as suppliers,
)
from marketplace_hub_core.tenancy.schema import seller_profiles
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError

_GTIN = re.compile(r"(?:\d{8}|\d{12,14})")
_EAN_KEYS = {"ean", "ean13", "barcode", "gtin", "gtin13", "product_ean"}
_INNPRO_ALIAS = re.compile(r"(?<![a-z0-9])inn(?:[\s._-]*)pro(?![a-z0-9])")
ORDER_COST_REFRESH_BATCH_ROWS = 250


def _supplier_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(character for character in text.casefold() if character.isalnum())


def _is_innpro_supplier(value: Any) -> bool:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(character for character in text if not unicodedata.combining(character))
    return _INNPRO_ALIAS.search(text) is not None


def _clear_catalog_details(details: dict) -> None:
    for key in tuple(details):
        if str(key).casefold().startswith("catalog_"):
            details.pop(key, None)


def _ean(value: Any) -> str:
    text = str(value or "").strip().removesuffix(".0")
    return text if _GTIN.fullmatch(text) else ""


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.quantize(Decimal('0.01')):.2f}"


def _order_supplier(item: dict) -> str:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    explicit = str(details.get("sku_supplier") or "").strip()
    if explicit:
        return explicit
    sku = str(item.get("sku") or "")
    parts = sku.rsplit("_", 3)
    return parts[0].strip() if len(parts) == 4 else ""


def _ean_from_raw(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return _ean(value)
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _EAN_KEYS:
                found = _ean(child)
                if found:
                    return found
        for child in value.values():
            if isinstance(child, dict | list | tuple):
                found = _ean_from_raw(child)
                if found:
                    return found
    elif isinstance(value, list | tuple):
        for child in value:
            found = _ean_from_raw(child)
            if found:
                return found
    return ""


def _order_ean(item: dict) -> str:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    sku = str(item.get("sku") or "")
    parts = sku.rsplit("_", 3)
    composite_code = parts[1] if len(parts) == 4 else ""
    for value in (
        item.get("ean"),
        details.get("order_ean"),
        details.get("sku_product_code"),
        composite_code,
    ):
        found = _ean(value)
        if found:
            return found
    return _ean_from_raw(item.get("_catalog_raw", item.get("raw")))


def _supplier_compatible(source: Any, requested: Any) -> bool:
    """Match the supplier exactly as the original Accounting search does."""
    source_is_innpro = _is_innpro_supplier(source)
    requested_is_innpro = _is_innpro_supplier(requested)
    if source_is_innpro or requested_is_innpro:
        # Provider identity alone cannot turn a user-named supplier such as
        # "Finn Products" into InnPro. Both sides must carry an unequivocal
        # InnPro token before applying the legacy exact/containment ordering.
        if not (source_is_innpro and requested_is_innpro):
            return False
    source_key = _supplier_key(source)
    requested_key = _supplier_key(requested)
    if not requested_key:
        return True
    return (
        source_key == requested_key
        or requested_key in source_key
        or source_key in requested_key
    )


class SqlCatalogCostResolver:
    """Apply the original InnPro accounting rule to normalized order rows.

    An InnPro order may use only the active LIGHT feed and an exact GTIN match.
    FULL prices are intentionally absent from the query, so a missing LIGHT
    match stays visibly uncosted instead of silently selecting retail content.
    """

    def __init__(self, engine) -> None:
        self.engine = engine

    @staticmethod
    def applies_to(items: list[dict]) -> bool:
        return any(_is_innpro_supplier(_order_supplier(item)) for item in items)

    @staticmethod
    def lock_scope(connection, organization_id: UUID, seller_id: UUID) -> None:
        """Serialize InnPro cost reads and LIGHT mutations for one Seller.

        Locking a stable Seller row also covers insertion of the first/new LIGHT
        list, which cannot be protected by locking only the catalogue rows that
        already exist. SQLite needs a no-op write because it ignores FOR UPDATE.
        """
        conditions = (
            seller_profiles.c.id == seller_id,
            seller_profiles.c.organization_id == organization_id,
            seller_profiles.c.active.is_(True),
        )
        if connection.dialect.name == "sqlite":
            result = connection.execute(seller_profiles.update().where(
                *conditions,
            ).values(updated_at=seller_profiles.c.updated_at))
            found = result.rowcount == 1
        else:
            found = connection.execute(select(seller_profiles.c.id).where(
                *conditions,
            ).with_for_update()).first() is not None
        if not found:
            raise SellerNotAccessibleError("Negozio non disponibile.")

    def _innpro_candidates(
        self,
        organization_id: UUID,
        seller_id: UUID,
        eans: set[str],
        *,
        connection=None,
    ) -> dict[str, list[dict]]:
        if not eans:
            return {}
        statement = select(
            products.c.ean,
            products.c.cost,
            products.c.sku,
            products.c.source_row,
            price_lists.c.id.label("price_list_id"),
            price_lists.c.name.label("price_list_name"),
            price_lists.c.last_success_at,
            price_lists.c.created_at,
            price_lists.c.updated_at,
            suppliers.c.name.label("supplier_name"),
        ).join(
            price_lists,
            (price_lists.c.id == products.c.price_list_id)
            & (price_lists.c.organization_id == products.c.organization_id)
            & (price_lists.c.seller_id == products.c.seller_id)
            & (price_lists.c.active_version_number == products.c.version_number),
        ).join(
            suppliers,
            (suppliers.c.id == price_lists.c.supplier_id)
            & (suppliers.c.organization_id == price_lists.c.organization_id)
            & (suppliers.c.seller_id == price_lists.c.seller_id),
        ).where(
            products.c.organization_id == organization_id,
            products.c.seller_id == seller_id,
            products.c.ean.in_(tuple(eans)),
            price_lists.c.provider == "innpro",
            price_lists.c.feed_role == "light",
            products.c.cost > 0,
        ).order_by(
            products.c.ean,
            # Streamlit prioritizes the last successful catalogue acquisition.
            # A failed check or metadata edit must not promote stale prices.
            desc(func.coalesce(price_lists.c.last_success_at, price_lists.c.created_at)),
            price_lists.c.id,
            products.c.source_row,
        )
        grouped: dict[str, list[dict]] = defaultdict(list)
        if connection is not None:
            for row in connection.execute(statement).mappings():
                grouped[str(row["ean"])].append(dict(row))
        else:
            with self.engine.connect() as active:
                for row in active.execute(statement).mappings():
                    grouped[str(row["ean"])].append(dict(row))
        return dict(grouped)

    @staticmethod
    def _appropriate_candidate(candidates: list[dict], requested_supplier: str):
        """Return the newest successful LIGHT belonging to the requested supplier."""
        return next(
            (
                candidate for candidate in candidates
                if _supplier_compatible(candidate["supplier_name"], requested_supplier)
            ),
            None,
        )

    def apply(
        self,
        organization_id: UUID,
        seller_id: UUID,
        items: list[dict],
        *,
        connection=None,
        fail_on_lookup_error: bool = False,
    ) -> list[dict]:
        innpro_rows = [
            item for item in items if _is_innpro_supplier(_order_supplier(item))
        ]
        if not innpro_rows:
            return items
        eans = {_order_ean(item) for item in innpro_rows}
        eans.discard("")
        lookup_unavailable = False
        try:
            if connection is not None and not fail_on_lookup_error:
                # PostgreSQL marks a transaction failed after a SQL error. A
                # savepoint lets a rolling worker persist the safe NULL cost
                # even while catalogue migrations are briefly incomplete.
                with connection.begin_nested():
                    candidates = self._innpro_candidates(
                        organization_id, seller_id, eans, connection=connection,
                    )
            else:
                candidates = self._innpro_candidates(
                    organization_id, seller_id, eans, connection=connection,
                )
        except SQLAlchemyError:
            if fail_on_lookup_error:
                # The catalog activation shares this transaction; propagating
                # prevents a LIGHT switch with stale historical order costs.
                raise
            # A worker can briefly overlap the database migration during a
            # rolling deploy. InnPro must fail closed here: retaining the
            # normalized composite-SKU cost would violate the LIGHT-only rule.
            candidates = {}
            lookup_unavailable = True

        for item in innpro_rows:
            details = item.get("details")
            if not isinstance(details, dict):
                details = {}
                item["details"] = details
            if details.get("zero_economic_reason"):
                continue
            ean = _order_ean(item)
            if ean:
                item["ean"] = ean
            match = self._appropriate_candidate(
                candidates.get(ean, []), _order_supplier(item),
            )
            manual_cost = str(item.get("purchase_cost_source") or "").strip().casefold() == (
                "modifica manuale persistente"
            )
            warnings = [
                str(value) for value in (item.get("monetary_warnings") or [])
                if not str(value).startswith("Costo ")
            ]
            if match is None:
                _clear_catalog_details(details)
                if manual_cost and _decimal(
                    item.get("purchase_cost_eur", item.get("purchase_cost"))
                ) is not None:
                    # The original refresh preserves an explicit manual
                    # override when no catalogue can price the row. It must no
                    # longer advertise the deleted catalogue as its source.
                    details.update({
                        "purchase_cost_method": "Modifica manuale persistente",
                        "purchase_unit_cost_eur": None,
                    })
                    item["monetary_warnings"] = warnings
                    continue
                item["purchase_cost"] = item["purchase_cost_eur"] = None
                item["profit_amount"] = item["profit_amount_eur"] = None
                item["profit_pct"] = None
                item["purchase_cost_source"] = (
                    "Costo non calcolabile: listino InnPro LIGHT temporaneamente non disponibile"
                    if lookup_unavailable else
                    "Costo non calcolabile: EAN non trovato nel listino InnPro LIGHT attivo"
                    if ean else
                    "Costo non calcolabile: EAN mancante; InnPro richiede il listino LIGHT"
                )
                details.update({
                    "purchase_cost_method": "Costo non calcolabile",
                    "purchase_unit_cost_eur": None,
                })
                warnings.append(item["purchase_cost_source"])
                item["monetary_warnings"] = warnings
                continue

            unit_cost = _decimal(match["cost"])
            quantity = _decimal(item.get("quantity")) or Decimal("1")
            total_cost = unit_cost * quantity if unit_cost is not None else None
            # A zero payout is a legitimate amount, while _decimal deliberately
            # rejects zero for supplier prices.
            try:
                payout = Decimal(str(item.get("payout_amount_eur")))
                if not payout.is_finite():
                    payout = None
            except (InvalidOperation, TypeError, ValueError):
                payout = None
            profit = payout - total_cost if payout is not None and total_cost is not None else None
            source = (
                f"Listino InnPro LIGHT · {match['price_list_name']} · "
                f"match EAN esatto {ean}"
            )
            item["purchase_cost"] = item["purchase_cost_eur"] = _money(total_cost)
            item["purchase_cost_source"] = source
            item["profit_amount"] = item["profit_amount_eur"] = _money(profit)
            item["profit_pct"] = _money(
                profit / total_cost * Decimal("100")
                if profit is not None and total_cost else None
            )
            details.update({
                "purchase_cost_method": "Listino fornitore",
                "purchase_unit_cost_eur": _money(unit_cost),
                "catalog_provider": "innpro",
                "catalog_feed_role": "light",
                "catalog_price_list_id": str(match["price_list_id"]),
                "catalog_price_list_name": str(match["price_list_name"]),
                "catalog_supplier": str(match["supplier_name"]),
                "catalog_matched_ean": ean,
                "catalog_matched_sku": str(match["sku"] or ""),
            })
            item["monetary_warnings"] = warnings
        return items

    def refresh_saved_orders(
        self,
        organization_id: UUID,
        seller_id: UUID,
        *,
        connection,
    ) -> dict[str, int]:
        """Recalculate saved InnPro rows inside a LIGHT activation transaction."""
        from marketplace_hub_core.orders.projections import project_order
        from marketplace_hub_core.orders.schema import order_lines

        now = datetime.now(UTC)
        counts = {
            "examined": 0,
            "matched": 0,
            "missing": 0,
            "preserved": 0,
            "cancelled": 0,
            "invalid": 0,
        }
        last_line_id: UUID | None = None
        update_statement = None
        projected_keys: tuple[str, ...] = ()

        while True:
            page_query = select(
                order_lines.c.id,
                order_lines.c.canonical_json,
                order_lines.c.raw_json,
            ).where(
                order_lines.c.organization_id == organization_id,
                order_lines.c.seller_id == seller_id,
            )
            if last_line_id is not None:
                page_query = page_query.where(order_lines.c.id > last_line_id)
            stored_page = list(connection.execute(
                page_query.order_by(order_lines.c.id)
                .limit(ORDER_COST_REFRESH_BATCH_ROWS)
                .with_for_update()
            ).mappings())
            if not stored_page:
                break
            last_line_id = stored_page[-1]["id"]

            rows: list[tuple[UUID, dict]] = []
            for stored_row in stored_page:
                try:
                    item = json.loads(stored_row["canonical_json"])
                except (TypeError, ValueError):
                    counts["invalid"] += 1
                    continue
                if not isinstance(item, dict):
                    counts["invalid"] += 1
                    continue
                if not _is_innpro_supplier(_order_supplier(item)):
                    continue
                item["_catalog_raw"] = stored_row["raw_json"]
                rows.append((stored_row["id"], item))

            items = [item for _, item in rows]
            if not items:
                continue
            counts["examined"] += len(items)
            previous_sources = [
                str(item.get("purchase_cost_source") or "") for item in items
            ]
            self.apply(
                organization_id,
                seller_id,
                items,
                connection=connection,
                fail_on_lookup_error=True,
            )

            if update_statement is None:
                projected_keys = tuple(project_order(items[0]))
                update_statement = order_lines.update().where(
                    order_lines.c.id == bindparam("target_line_id"),
                    order_lines.c.organization_id == organization_id,
                    order_lines.c.seller_id == seller_id,
                ).values(
                    **{key: bindparam(f"value_{key}") for key in projected_keys},
                    canonical_json=bindparam("value_canonical_json"),
                    search_text=bindparam("value_search_text"),
                    projection_updated_at=bindparam("value_projection_updated_at"),
                )

            updates = []
            for (line_id, item), previous_source in zip(
                rows, previous_sources, strict=True,
            ):
                details = (
                    item.get("details") if isinstance(item.get("details"), dict) else {}
                )
                if details.get("zero_economic_reason"):
                    counts["cancelled"] += 1
                elif str(item.get("purchase_cost_source") or "").startswith(
                    "Listino InnPro LIGHT"
                ):
                    counts["matched"] += 1
                elif (
                    previous_source.strip().casefold() == "modifica manuale persistente"
                    and str(item.get("purchase_cost_source") or "") == previous_source
                ):
                    counts["preserved"] += 1
                else:
                    counts["missing"] += 1
                projection = project_order(item)
                public = {key: value for key, value in item.items() if key != "_catalog_raw"}
                public_details = public.get("details") or {}
                updates.append({
                    "target_line_id": line_id,
                    **{f"value_{key}": projection[key] for key in projected_keys},
                    "value_canonical_json": json.dumps(
                        public, ensure_ascii=False, allow_nan=False,
                    ),
                    "value_search_text": " ".join(
                        str(public.get(key) or "") for key in (
                            "order_id", "external_line_id", "product_name", "ean", "sku",
                        )
                    ).casefold() + " " + " ".join(
                        str(public_details.get(key) or "")
                        for key in ("tracking", "carrier")
                    ).casefold(),
                    "value_projection_updated_at": now,
                })
            connection.execute(update_statement, updates)
        return counts


__all__ = ["SqlCatalogCostResolver"]
