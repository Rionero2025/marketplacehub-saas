from __future__ import annotations

"""Product sales analytics built on the same accounting rows as the Dashboard.

The module intentionally contains no marketplace API calls.  All statistics are
computed from ``accounting_order_lines`` after the Dashboard has applied manual
accounting overrides, cancellation zeroing and profit formulas.  This keeps the
Top Products views fast and aligned with Contabilità/Dashboard values.
"""

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from services.cecotec_orders import clean_text


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _quantity(value: Any) -> float:
    number = _number(value)
    return max(number or 0.0, 0.0)


def product_identity(item: Mapping[str, Any]) -> str:
    """Stable product key: EAN first, then composite SKU, then normalized title."""
    ean = clean_text(item.get("ean")).strip()
    if ean:
        return f"ean:{ean}"
    sku = clean_text(item.get("composite_sku")).strip()
    if sku:
        return f"sku:{sku.casefold()}"
    title = " ".join(clean_text(item.get("product_title")).split()).casefold()
    if title:
        return f"title:{title}"
    # Avoid collapsing unrelated anonymous rows if old cached data lacks all product fields.
    row_key = clean_text(item.get("row_key") or item.get("order_id")).strip()
    return f"row:{row_key}" if row_key else "row:unknown"


def product_display_name(item: Mapping[str, Any]) -> str:
    title = clean_text(item.get("product_title")).strip()
    if title:
        return title
    ean = clean_text(item.get("ean")).strip()
    if ean:
        return f"EAN {ean}"
    sku = clean_text(item.get("composite_sku")).strip()
    if sku:
        return sku
    return "Prodotto senza nome"


def is_sold_line(item: Mapping[str, Any]) -> bool:
    """True only for economically sold quantities.

    Dashboard/Accounting zero cancelled, fully refunded and equivalent rows.  We
    additionally require a positive sale value so those rows never inflate the
    product ranking merely because their cached quantity is still 1.
    """
    return _quantity(item.get("quantity")) > 0 and float(_number(item.get("sale_eur")) or 0.0) > 0.005


def previous_period_range(start: date, end: date) -> tuple[date, date]:
    start, end = min(start, end), max(start, end)
    days = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return previous_start, previous_end


def filter_product_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    seller_ids: Iterable[int] | None = None,
    marketplaces: Iterable[str] | None = None,
    countries: Iterable[str] | None = None,
    suppliers: Iterable[str] | None = None,
    search: str = "",
    sold_only: bool = True,
) -> list[dict[str, Any]]:
    seller_set = {int(value) for value in (seller_ids or []) if value is not None}
    marketplace_set = {clean_text(value).casefold() for value in (marketplaces or []) if clean_text(value)}
    country_set = {clean_text(value).upper() for value in (countries or []) if clean_text(value)}
    supplier_set = {clean_text(value).casefold() for value in (suppliers or []) if clean_text(value)}
    query = " ".join(clean_text(search).casefold().split())

    output: list[dict[str, Any]] = []
    for source in rows:
        item = dict(source)
        if sold_only and not is_sold_line(item):
            continue
        if seller_set and int(item.get("seller_id") or 0) not in seller_set:
            continue
        if marketplace_set and clean_text(item.get("marketplace")).casefold() not in marketplace_set:
            continue
        if country_set and clean_text(item.get("country_code")).upper() not in country_set:
            continue
        if supplier_set and clean_text(item.get("supplier")).casefold() not in supplier_set:
            continue
        if query:
            haystack = " ".join(
                [
                    clean_text(item.get("product_title")),
                    clean_text(item.get("ean")),
                    clean_text(item.get("composite_sku")),
                    clean_text(item.get("supplier")),
                ]
            ).casefold()
            if query not in haystack:
                continue
        output.append(item)
    return output


def _unique_join(values: Iterable[Any]) -> str:
    cleaned = sorted({clean_text(value).strip() for value in values if clean_text(value).strip()}, key=str.casefold)
    return ", ".join(cleaned)


def _order_identity(item: Mapping[str, Any], fallback: int) -> str:
    seller_id = int(item.get("seller_id") or 0)
    account_id = int(item.get("marketplace_account_id") or 0)
    marketplace = clean_text(item.get("marketplace")).casefold()
    order_id = clean_text(item.get("order_id")).strip()
    if order_id:
        return f"{seller_id}|{account_id}|{marketplace}|{order_id}"
    return f"row|{seller_id}|{clean_text(item.get('row_key')) or fallback}"


def margin_review_queue(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return sold orders whose margin still has at least one unresolved line.

    The queue is intentionally built from the same already-filtered Dashboard rows
    used by Product Stats.  It contains no API calls and carries enough identifiers
    to open the exact Seller/account in Contabilità and show the affected orders in
    its editable grid.  One order can contain multiple unresolved lines but is
    counted only once in ``order_count``.
    """
    items: list[dict[str, Any]] = []
    order_keys: set[str] = set()
    for index, source in enumerate(rows):
        item = dict(source)
        if not is_sold_line(item):
            continue
        if _number(item.get("net_revenue_eur")) is not None:
            continue
        identity = _order_identity(item, index)
        order_keys.add(identity)
        items.append(
            {
                "order_identity": identity,
                "seller_id": int(item.get("seller_id") or 0),
                "seller_name": clean_text(item.get("seller_name")),
                "marketplace_account_id": int(item.get("marketplace_account_id") or 0),
                "marketplace": clean_text(item.get("marketplace")).casefold(),
                "order_id": clean_text(item.get("order_id")),
                "row_key": clean_text(item.get("row_key")),
                "order_date": item.get("order_date"),
                "missing_reason": clean_text(item.get("missing_reason")),
            }
        )
    return {
        "order_count": len(order_keys),
        "row_count": len(items),
        "items": items,
    }


def _aggregate_bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    quantity = sum(_quantity(item.get("quantity")) for item in rows)
    sales = sum(float(_number(item.get("sale_eur")) or 0.0) for item in rows)
    commission = sum(float(_number(item.get("commission_eur")) or 0.0) for item in rows)
    refunds = sum(float(_number(item.get("refund_eur")) or 0.0) for item in rows)
    extra_cost = sum(float(_number(item.get("extra_cost_eur")) or 0.0) for item in rows)

    purchase_known = [_number(item.get("purchase_cost_eur")) for item in rows]
    purchase_missing = sum(value is None for value in purchase_known)
    purchase = sum(float(value or 0.0) for value in purchase_known)

    payout_known = [_number(item.get("payout_eur")) for item in rows]
    payout_missing = sum(value is None for value in payout_known)
    payout = sum(float(value or 0.0) for value in payout_known)

    margin_known = [_number(item.get("net_revenue_eur")) for item in rows]
    margin_missing = sum(value is None for value in margin_known)
    # v266: never hide the margin that is already determinable.  Missing rows are
    # excluded from this partial sum and remain explicitly counted so the UI can
    # warn that the value can still change after Accounting is completed.
    margin = round(sum(float(value or 0.0) for value in margin_known if value is not None), 2)

    our_known = [_number(item.get("our_share_eur")) for item in rows]
    our_share = None if any(value is None for value in our_known) else round(sum(float(value or 0.0) for value in our_known), 2)
    partner_known = [_number(item.get("partner_share_eur")) for item in rows]
    partner_share = None if any(value is None for value in partner_known) else round(sum(float(value or 0.0) for value in partner_known), 2)

    order_ids = {_order_identity(item, index) for index, item in enumerate(rows)}
    average_price = (sales / quantity) if quantity > 0 else 0.0
    # A percentage over total sales would be misleading while some margins are
    # unknown, therefore the euro amount stays visible but the percentage waits
    # until every sold line in the bucket is complete.
    margin_pct = (margin / sales * 100.0) if not margin_missing and abs(sales) >= 0.005 else None

    return {
        "quantity": round(quantity, 4),
        "orders": len(order_ids),
        "sales_eur": round(sales, 2),
        "average_price_eur": round(average_price, 2),
        "purchase_cost_eur": None if purchase_missing else round(purchase, 2),
        "purchase_missing_rows": purchase_missing,
        "commission_eur": round(commission, 2),
        "refund_eur": round(refunds, 2),
        "payout_eur": None if payout_missing else round(payout, 2),
        "payout_missing_rows": payout_missing,
        "extra_cost_eur": round(extra_cost, 2),
        "margin_eur": margin,
        "margin_pct": round(margin_pct, 2) if margin_pct is not None else None,
        "margin_missing_rows": margin_missing,
        "our_share_eur": our_share,
        "partner_share_eur": partner_share,
    }


def aggregate_product_stats(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate sold lines into one row per product."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in rows:
        item = dict(source)
        if not is_sold_line(item):
            continue
        grouped[product_identity(item)].append(item)

    output: list[dict[str, Any]] = []
    for key, bucket in grouped.items():
        first = bucket[0]
        values = _aggregate_bucket(bucket)
        eans = [item.get("ean") for item in bucket]
        skus = [item.get("composite_sku") for item in bucket]
        names = [product_display_name(item) for item in bucket if product_display_name(item)]
        values.update(
            {
                "product_key": key,
                "product_title": max(names, key=len) if names else product_display_name(first),
                "ean": _unique_join(eans),
                "composite_sku": _unique_join(skus),
                "suppliers": _unique_join(item.get("supplier") for item in bucket),
                "seller_names": _unique_join(item.get("seller_name") for item in bucket),
                "seller_count": len({int(item.get("seller_id") or 0) for item in bucket}),
                "marketplaces": _unique_join(item.get("marketplace") for item in bucket),
                "countries": _unique_join(clean_text(item.get("country_code")).upper() for item in bucket),
                "first_order_date": min((item.get("order_date") for item in bucket if item.get("order_date")), default=None),
                "last_order_date": max((item.get("order_date") for item in bucket if item.get("order_date")), default=None),
            }
        )
        output.append(values)

    output.sort(key=lambda item: (-float(item.get("quantity") or 0), -float(item.get("sales_eur") or 0), clean_text(item.get("product_title")).casefold()))
    return output


def product_totals(stats: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = [dict(item) for item in stats]
    quantity = sum(float(item.get("quantity") or 0.0) for item in values)
    sales = sum(float(item.get("sales_eur") or 0.0) for item in values)
    orders = sum(int(item.get("orders") or 0) for item in values)
    margin_missing = sum(int(item.get("margin_missing_rows") or 0) for item in values)
    # v266: total margin is the sum of every margin already known, even if a few
    # sold lines still need manual completion in Accounting.
    margin = round(sum(float(item.get("margin_eur") or 0.0) for item in values), 2)
    return {
        "products": len(values),
        "quantity": round(quantity, 4),
        "sales_eur": round(sales, 2),
        # This is a product-level order count and can double-count a multi-product order.
        "product_order_occurrences": orders,
        "margin_eur": margin,
        "margin_missing_rows": margin_missing,
    }


def percentage_delta(current: Any, previous: Any) -> float | None:
    current_number = _number(current)
    previous_number = _number(previous)
    if current_number is None or previous_number is None:
        return None
    if abs(previous_number) < 1e-9:
        return None if abs(current_number) < 1e-9 else 100.0
    return round((current_number - previous_number) / abs(previous_number) * 100.0, 2)


def merge_previous_period(
    current: Iterable[Mapping[str, Any]],
    previous: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    previous_map = {clean_text(item.get("product_key")): dict(item) for item in previous}
    output: list[dict[str, Any]] = []
    for source in current:
        item = dict(source)
        before = previous_map.get(clean_text(item.get("product_key")), {})
        item.update(
            {
                "previous_quantity": float(before.get("quantity") or 0.0),
                "quantity_delta_pct": percentage_delta(item.get("quantity"), before.get("quantity") or 0.0),
                "previous_sales_eur": float(before.get("sales_eur") or 0.0),
                "sales_delta_pct": percentage_delta(item.get("sales_eur"), before.get("sales_eur") or 0.0),
                "previous_margin_eur": before.get("margin_eur") if before else 0.0,
                "margin_delta_pct": percentage_delta(item.get("margin_eur"), before.get("margin_eur") if before else 0.0),
            }
        )
        output.append(item)
    return output


def sort_product_stats(stats: Iterable[Mapping[str, Any]], mode: str) -> list[dict[str, Any]]:
    values = [dict(item) for item in stats]
    mode_key = clean_text(mode).casefold()
    if "fatturato" in mode_key or "vendite" in mode_key:
        key = lambda item: (-float(item.get("sales_eur") or 0), -float(item.get("quantity") or 0))
    elif "margine" in mode_key:
        # v266 ranks by the currently determinable margin.  Incomplete products
        # remain visibly flagged by margin_missing_rows instead of being hidden.
        key = lambda item: (
            -float(item.get("margin_eur") or 0),
            -float(item.get("sales_eur") or 0),
        )
    else:
        key = lambda item: (-float(item.get("quantity") or 0), -float(item.get("sales_eur") or 0))
    values.sort(key=lambda item: (*key(item), clean_text(item.get("product_title")).casefold()))
    return values


def product_rows(rows: Iterable[Mapping[str, Any]], product_key: str) -> list[dict[str, Any]]:
    key = clean_text(product_key)
    return [dict(item) for item in rows if is_sold_line(item) and product_identity(item) == key]


def aggregate_dimension(rows: Iterable[Mapping[str, Any]], dimension: str) -> list[dict[str, Any]]:
    """Aggregate one selected product by seller/marketplace/country/supplier/date."""
    allowed = {"seller_name", "marketplace", "country_code", "supplier", "order_date"}
    if dimension not in allowed:
        raise ValueError(f"Dimensione non supportata: {dimension}")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labels: dict[str, Any] = {}
    for source in rows:
        item = dict(source)
        if not is_sold_line(item):
            continue
        raw = item.get(dimension)
        if dimension == "country_code":
            raw = clean_text(raw).upper() or "N/D"
        elif dimension == "order_date":
            raw = raw or "N/D"
        else:
            raw = clean_text(raw).strip() or "N/D"
        key = str(raw)
        labels[key] = raw
        grouped[key].append(item)
    output = []
    for key, bucket in grouped.items():
        values = _aggregate_bucket(bucket)
        values[dimension] = labels[key]
        output.append(values)
    output.sort(key=lambda item: (-float(item.get("quantity") or 0), str(item.get(dimension))))
    return output


def aggregate_product_orders(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labels: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(rows):
        item = dict(source)
        if not is_sold_line(item):
            continue
        key = _order_identity(item, index)
        grouped[key].append(item)
        labels[key] = item
    output: list[dict[str, Any]] = []
    for key, bucket in grouped.items():
        sample = labels[key]
        values = _aggregate_bucket(bucket)
        values.update(
            {
                "order_date": sample.get("order_date"),
                "order_id": clean_text(sample.get("order_id")),
                "seller_name": clean_text(sample.get("seller_name")),
                "marketplace": clean_text(sample.get("marketplace")),
                "country_code": clean_text(sample.get("country_code")).upper(),
                "supplier": _unique_join(item.get("supplier") for item in bucket),
            }
        )
        output.append(values)
    output.sort(key=lambda item: (item.get("order_date") or date.min, clean_text(item.get("order_id"))), reverse=True)
    return output
