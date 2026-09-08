"""Order arithmetic ported from Streamlit v271; no database or network dependencies.

Kaufland: services/kaufland_orders.py. Worten: services/worten.py and
services/accounting.py. The canonical boundary retains original currencies,
unknown amounts and the source of every derived financial value.
See docs/blocks/B20_1_ORDERS_SOURCE_CONTRACT.md for intentional limits.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

# Ported from services/kaufland_orders.py (v271).


STOREFRONT_INFO = {
    "de": ("Germania", "DE", "EUR"),
    "at": ("Austria", "AT", "EUR"),
    "fr": ("Francia", "FR", "EUR"),
    "it": ("Italia", "IT", "EUR"),
    "pl": ("Polonia", "PL", "PLN"),
    "cz": ("Rep. Ceca", "CZ", "CZK"),
    "sk": ("Slovacchia", "SK", "EUR"),
}


STATUS_LABELS = {
    "open": "Aperto",
    "need_to_be_sent": "Da spedire",
    "sent": "Spedito",
    "sent_and_autopaid": "Spedito e pagato automaticamente",
    "received": "Ricevuto",
    "returned": "Restituito",
    "returned_paid": "Reso rimborsato",
    "cancelled": "Cancellato",
    "canceled": "Cancellato",
}


SHIPPED_ORDER_UNIT_STATUSES = {
    "sent",
    "sent_and_autopaid",
    "received",
    "returned",
    "returned_paid",
}


RECEIVED_TIMESTAMP_KEYS = (
    "order_received_timestamp_iso",
    "ts_received_iso",
    "received_at",
    "received_at_iso",
    "ts_delivered_iso",
    "delivered_at",
    "delivery_date",
)


SHIPPED_TIMESTAMP_KEYS = (
    "order_sent_timestamp_iso",
    "ts_sent_iso",
    "sent_at_iso",
    "sent_at",
    "ts_shipped_iso",
    "shipped_at_iso",
    "shipped_at",
)


PAYMENT_RELEASE_TIMESTAMP_KEYS = (
    "revenue_released_timestamp_iso",
    "revenue_released_at",
    "payout_timestamp_iso",
    "payout_at",
    "payment_timestamp_iso",
    "paid_at_iso",
    "paid_at",
)


COMMISSION_KEYS = (
    "commission",
    "commission_amount",
    "commission_gross",
    "marketplace_fee",
    "marketplace_commission",
)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _minor_money(value):
    if value in (None, ""):
        return None
    try:
        parsed = _finite(value)
        return None if parsed is None else round(parsed / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _minor_money_value(value):
    """Read a Kaufland monetary value which may be scalar or an amount object."""
    if isinstance(value, dict):
        for key in ("amount", "value", "gross", "total"):
            if value.get(key) not in (None, ""):
                return _minor_money(value.get(key))
        return None
    return _minor_money(value)


def _parse_iso(value) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_seconds(value) -> str:
    parsed = _parse_iso(value)
    return parsed.isoformat(timespec="seconds") if parsed else ""


def _received_timestamp(raw: dict, status: str) -> tuple[str, str]:
    if status not in SHIPPED_ORDER_UNIT_STATUSES:
        return "", ""
    for key in RECEIVED_TIMESTAMP_KEYS:
        value = _iso_seconds(raw.get(key))
        if value:
            return value, f"API Kaufland: {key}"
    return "", ""


def _shipped_timestamp(raw: dict, status: str) -> tuple[str, str]:
    if status not in SHIPPED_ORDER_UNIT_STATUSES:
        return "", ""
    for key in SHIPPED_TIMESTAMP_KEYS:
        value = _iso_seconds(raw.get(key))
        if value:
            return value, f"API Kaufland: {key}"
    if status == "sent":
        value = _iso_seconds(raw.get("ts_updated_iso"))
        if value:
            return value, "API Kaufland: passaggio a sent (ts_updated_iso)"
    return "", ""


def _payment_release_timestamp(raw: dict, status: str) -> tuple[str, str]:
    """Return Kaufland's actual proceeds-release time when it is available."""
    for key in PAYMENT_RELEASE_TIMESTAMP_KEYS:
        value = _iso_seconds(raw.get(key))
        if value:
            return value, f"API Kaufland: {key}"
    if status == "sent_and_autopaid":
        value = _iso_seconds(raw.get("ts_updated_iso"))
        if value:
            return (
                value,
                "API Kaufland: stato sent_and_autopaid (ts_updated_iso)",
            )
    return "", ""


def _first_ean(product: dict) -> str:
    values = product.get("eans") or product.get("ean") or []
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, list | tuple):
        values = [values]
    return next((_text(value) for value in values if _text(value)), "")


TRACKING_KEYS = {
    "tracking_numbers",
    "tracking_number",
    "tracking_code",
    "tracking_id",
    "parcel_number",
    "shipment_number",
    # Additional Mirakl aliases from accounting._tracking_text.
    "tracking",
    "shipping_tracking",
    "shipment_tracking",
}


TRACKING_CARRIER_KEYS = {
    "carrier_code",
    "carrier_name",
    "tracking_provider",
    "shipping_provider",
    "shipping_carrier",
    "carrier",
    "provider",
    "shipping_carrier_code",
    "logistic_partner",
    "agency",
}


def _tracking_values(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, dict):
        result: list[str] = []
        matching_values = [
            nested_value for key, nested_value in value.items() if key in TRACKING_KEYS
        ]
        for nested_value in matching_values:
            result.extend(_tracking_values(nested_value))
        return result
    if isinstance(value, list | tuple | set):
        result = []
        for nested_value in value:
            result.extend(_tracking_values(nested_value))
        return result
    return [part.strip() for part in str(value).split(",") if part.strip()]


def extract_tracking(raw: dict) -> tuple[str, str]:
    """Extract shipment data from direct and nested Kaufland responses."""
    carriers: list[str] = []
    tracking_numbers: list[str] = []

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return

        node_tracking: list[str] = []
        for key in TRACKING_KEYS:
            if key in value:
                node_tracking.extend(_tracking_values(value.get(key)))
        if node_tracking:
            tracking_numbers.extend(node_tracking)
            for key in TRACKING_CARRIER_KEYS:
                carrier = _text(value.get(key))
                if carrier:
                    carriers.append(carrier)
                    break

        # Top-level shipment responses may contain a carrier while tracking is
        # stored in a child object or array.
        if value is raw:
            for key in TRACKING_CARRIER_KEYS:
                carrier = _text(value.get(key))
                if carrier:
                    carriers.append(carrier)
                    break

        for nested_value in value.values():
            if isinstance(nested_value, dict | list):
                visit(nested_value)

    visit(raw)
    carrier = ", ".join(dict.fromkeys(value for value in carriers if value))
    tracking = ", ".join(dict.fromkeys(value for value in tracking_numbers if value))
    return carrier, tracking


def find_order_unit(raw, id_order_unit: str) -> dict:
    """Find a specific order unit inside an order-detail response."""
    wanted = _text(id_order_unit)
    if isinstance(raw, list):
        for item in raw:
            found = find_order_unit(item, wanted)
            if found:
                return found
        return {}
    if not isinstance(raw, dict):
        return {}
    if _text(raw.get("id_order_unit")) == wanted:
        return raw
    for value in raw.values():
        if isinstance(value, dict | list):
            found = find_order_unit(value, wanted)
            if found:
                return found
    return {}


def _explicit_commission(raw: dict):
    for key in COMMISSION_KEYS:
        value = _minor_money_value(raw.get(key))
        if value is not None:
            return abs(value), f"API Kaufland: {key}"
    fees = raw.get("fees")
    if isinstance(fees, dict):
        for key in COMMISSION_KEYS:
            value = _minor_money_value(fees.get(key))
            if value is not None:
                return abs(value), f"API Kaufland: fees.{key}"
    if isinstance(fees, list):
        commission_total = 0.0
        found = False
        for fee in fees:
            if not isinstance(fee, dict):
                continue
            fee_type = _text(fee.get("type") or fee.get("name") or fee.get("code")).lower()
            if "commission" not in fee_type:
                continue
            value = _minor_money_value(fee)
            if value is not None:
                commission_total += abs(value)
                found = True
        if found:
            return round(commission_total, 2), "API Kaufland: fees"
    return None, ""


def _float_or_none(value):
    if value in (None, ""):
        return None
    try:
        return _finite(value)
    except (TypeError, ValueError):
        return None


def composed_sku_order_financials(
    sku,
    ean,
    payout_eur,
) -> dict:
    """Read cost/minimum from supplier_product-code_cost_minimum.

    The product-code component is deliberately opaque: it may be an EAN, a
    supplier SKU, or any other identifier. Only the final two numeric
    components have a fixed meaning.
    """
    text = _text(sku)
    expected_ean = _text(ean).removesuffix(".0")
    parts = text.rsplit("_", 3)
    empty = {
        "sku_supplier": "",
        "sku_ean": "",
        "sku_product_code": "",
        "order_ean": expected_ean,
        "sku_ean_matches_order": None,
        "sku_ean_note": "SKU composto non riconosciuto",
        "purchase_cost_eur": None,
        "minimum_price_sku_eur": None,
        "order_profit_eur": None,
        "order_profit_pct": None,
        "purchase_cost_method": "Costo non calcolabile",
        "purchase_cost_source": "Costo non calcolabile",
    }
    if len(parts) != 4 or not parts[0] or not parts[1]:
        return empty
    try:
        purchase = float(parts[2].replace(",", "."))
        minimum = float(parts[3].replace(",", "."))
    except (TypeError, ValueError):
        return empty
    if not math.isfinite(purchase) or not math.isfinite(minimum) or purchase <= 0 or minimum <= 0:
        return empty
    payout = _float_or_none(payout_eur)
    profit = round(payout - purchase, 2) if payout is not None else None
    profit_pct = (
        round(profit / purchase * 100.0, 2) if profit is not None and purchase > 0 else None
    )
    ean_matches = parts[1] == expected_ean if expected_ean else None
    code_note = (
        "Codice prodotto SKU ed EAN ordine coincidono"
        if ean_matches is True
        else (
            f"Codice prodotto SKU {parts[1]} · EAN ordine {expected_ean}"
            if ean_matches is False
            else (f"Codice prodotto SKU {parts[1]} · EAN ordine non disponibile")
        )
    )
    return {
        "sku_supplier": parts[0],
        "sku_ean": parts[1],
        "sku_product_code": parts[1],
        "order_ean": expected_ean,
        "sku_ean_matches_order": ean_matches,
        "sku_ean_note": code_note,
        "purchase_cost_eur": round(purchase, 2),
        "minimum_price_sku_eur": round(minimum, 2),
        "order_profit_eur": profit,
        "order_profit_pct": profit_pct,
        "purchase_cost_method": "SKU composto",
        "purchase_cost_source": "Terzo valore dello SKU composto",
    }


def merge_order_unit(base: dict, detail: dict) -> dict:
    """Prefer the detailed API response while retaining list-only attributes."""
    merged = dict(base)
    for key, value in detail.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    if isinstance(base.get("product"), dict) or isinstance(detail.get("product"), dict):
        merged["product"] = {
            **(base.get("product") if isinstance(base.get("product"), dict) else {}),
            **(detail.get("product") if isinstance(detail.get("product"), dict) else {}),
        }
    return merged


# Ported from services/worten.py (v271).


def _number(value, default: float | None = None) -> float | None:
    """Return a numeric Mirakl value without depending on one response version."""
    if isinstance(value, dict):
        for key in (
            "amount",
            "price",
            "value",
            "unit_price",
            "unitPrice",
            "shipping_price",
            "shippingPrice",
            "rate",
            "percentage",
            "percent",
            "commission_rate",
        ):
            if key in value:
                return _number(value.get(key), default)
        return default
    try:
        if value in (None, ""):
            return default
        return _finite(str(value).replace(",", ".").strip().rstrip("%").strip())
    except (TypeError, ValueError):
        return default


def _value(item: dict, *keys, default=None):
    """Read either snake_case or kebab-case Mirakl fields."""
    if not isinstance(item, dict):
        return default
    normalized = {str(key).lower().replace("-", "_"): value for key, value in item.items()}
    for key in keys:
        candidate = normalized.get(str(key).lower().replace("-", "_"))
        if candidate is not None:
            return candidate
    return default


def _commissionable_breakdown_amount(value) -> float | None:
    if not isinstance(value, dict):
        return None
    parts = _value(value, "parts", default=[]) or []
    amounts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if _value(part, "commissionable", default=False) is not True:
            continue
        amount = _number(_value(part, "amount"))
        if amount is not None:
            amounts.append(amount)
    return sum(amounts) if amounts else None


def commission_rate_from_order_line(line: dict) -> dict | None:
    """Extract the effective commission percentage returned by Mirakl OR11."""
    if not isinstance(line, dict):
        return None
    direct_rate = _number(
        _value(
            line,
            "commission_rate",
            "commission_percentage",
            "commission_fee_rate",
        )
    )
    if direct_rate is not None and 0 <= direct_rate <= 100:
        return {
            "rate": round(direct_rate, 4),
            "fee": _number(_value(line, "commission_fee")),
            "base": None,
            "method": "commission_rate",
        }

    fee = _number(_value(line, "commission_fee"))
    if fee is None:
        total_commission = _number(_value(line, "total_commission"))
        commission_vat = _number(_value(line, "commission_vat"), 0.0) or 0.0
        if total_commission is not None:
            fee = total_commission - commission_vat
    if fee is None:
        return None

    price_base = _commissionable_breakdown_amount(
        _value(line, "price_amount_breakdown", default={})
    )
    shipping_base = _commissionable_breakdown_amount(
        _value(line, "shipping_price_amount_breakdown", default={})
    )
    breakdown_values = [value for value in (price_base, shipping_base) if value is not None]
    base = sum(breakdown_values) if breakdown_values else None
    if base is None:
        base = _number(_value(line, "total_price"))
    if base is None:
        price = _number(_value(line, "price"), 0.0) or 0.0
        shipping = _number(_value(line, "shipping_price"), 0.0) or 0.0
        base = price + shipping
    if base <= 0:
        return None
    rate = fee / base * 100
    if not 0 <= rate <= 100:
        return None
    return {
        "rate": round(rate, 4),
        "fee": fee,
        "base": base,
        "method": "commission_fee/base",
    }


# Ported from services/accounting.py (v271).


def _status_search_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, Mapping):
            value = " ".join(_text(item) for item in value.values())
        elif isinstance(value, list | tuple | set):
            value = " ".join(_text(item) for item in value)
        text = _text(value)
        if text:
            parts.append(text)
    normalized = unicodedata.normalize("NFKD", " ".join(parts).lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _zero_economics_reason(*values: Any) -> str:
    """Return the reason that forces every economic value to zero."""
    text = _status_search_text(*values)
    if not text:
        return ""
    padded = f" {text} "
    if " no stock " in padded or " out of stock " in padded or " nostock " in padded:
        return "no stock"
    if re.search(r"\brimborsat", text) or re.search(r"\b(?:fully |partially )?refunded\b", text):
        return "rimborsato"
    if re.search(r"\bannullare\b", text):
        return "annullare"
    if re.search(r"\bannullat[oaie]?\b", text):
        return "annullato"
    if re.search(r"\bcancellat[oaie]?\b", text) or re.search(r"\bcancell?ed\b", text):
        return "cancellato"
    if re.search(r"\bcancel(?:led|ed)?\b", text) or re.search(r"\brefus(?:ed|al)?\b", text):
        return "cancellato"
    return ""


def _refund_amount(line: Mapping[str, Any]) -> float:
    for key in (
        "total_refunded",
        "total_refund",
        "refunded_amount",
        "refund_amount",
        "total_refund_amount",
        "price_refunded",
        "amount_refunded",
    ):
        value = _number(_value(line, key))
        if value is not None:
            return max(0.0, value)
    refunds = _value(line, "refunds", "order_line_refunds", default=[])
    total = 0.0
    found = False
    if isinstance(refunds, list):
        for refund in refunds:
            if not isinstance(refund, Mapping):
                continue
            value = _number(
                _value(
                    refund,
                    "total_amount",
                    "amount",
                    "refund_amount",
                    "price_amount",
                    "total_refund",
                    "refunded_amount",
                )
            )
            if value is not None:
                total += abs(value)
                found = True
    return round(total, 2) if found else 0.0


def _mirakl_sale_amount(line: Mapping[str, Any], quantity: int) -> float | None:
    total = _number(
        _value(
            line,
            "total_price",
            "total_amount",
            "order_line_total",
            "line_total",
            "total_price_including_tax",
        )
    )
    if total is not None:
        return round(total, 2)
    unit = _number(_value(line, "unit_price", "price_unit"))
    shipping = _number(_value(line, "shipping_price", "shipping_amount"), 0.0) or 0.0
    if unit is not None:
        return round(unit * max(1, quantity) + shipping, 2)
    price = _number(_value(line, "price", "price_amount"))
    if price is None:
        return None
    # In Mirakl OR11 ``price`` is normally already the line amount. Only
    # multiply when a dedicated unit-price field proves it is unitary.
    return round(price + shipping, 2)


def _mirakl_commission_amount(line: Mapping[str, Any]) -> float | None:
    total = _number(_value(line, "total_commission", "commission_total"))
    if total is not None:
        return round(abs(total), 2)
    fee = _number(_value(line, "commission_fee", "commission_amount"))
    vat = _number(_value(line, "commission_vat", "commission_tax"), 0.0) or 0.0
    if fee is not None:
        return round(abs(fee) + abs(vat), 2)
    extracted = commission_rate_from_order_line(dict(line))
    if extracted and extracted.get("fee") is not None:
        return round(abs(float(extracted["fee"])), 2)
    return None


def _mirakl_direct_payout(line: Mapping[str, Any]) -> float | None:
    for key in (
        "payout_amount",
        "seller_amount",
        "shop_amount",
        "amount_paid",
        "transferred_amount",
        "payment_amount",
        "net_amount",
        "net_proceeds",
    ):
        value = _number(_value(line, key))
        if value is not None:
            return round(value, 2)
    return None


def _currency_code(line: Mapping[str, Any], order: Mapping[str, Any]) -> str:
    value = _value(line, "currency_iso_code", "currency", "currency_code")
    if not value:
        value = _value(order, "currency_iso_code", "currency", "currency_code")
    if isinstance(value, Mapping):
        value = _value(value, "iso_code", "code", "currency")
    return _text(value).upper() or "EUR"


# Ported from services/cecotec_orders.py (v271).


WORTEN_ORDER_STATUS_LABELS: dict[str, str] = {
    "STAGING": "In preparazione",
    "WAITING_ACCEPTANCE": "In attesa di accettazione",
    "WAITING_DEBIT": "In attesa di addebito",
    "WAITING_DEBIT_PAYMENT": "In attesa del pagamento",
    "SHIPPING": "In attesa di spedizione",
    "SHIPPED": "Spedito",
    "TO_COLLECT": "Disponibile per il ritiro",
    "RECEIVED": "Ricevuto",
    "CLOSED": "Chiuso",
    "REFUSED": "Rifiutato",
    "CANCELED": "Cancellato",
    "CANCELLED": "Cancellato",
    "RETURNED": "Restituito",
    "REFUNDED": "Rimborsato",
    "FULLY_REFUNDED": "Rimborsato",
    "PARTIALLY_REFUNDED": "Rimborsato parzialmente",
    # Stati Mirakl deprecati che possono essere ancora presenti nello storico.
    "WAITING_REFUND": "In attesa di rimborso",
    "WAITING_REFUND_PAYMENT": "In attesa del pagamento del rimborso",
}


def _finite(value: Any) -> float | None:
    """Reject booleans, NaN and infinities before the original float arithmetic."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _decimal(value: Any, places: int = 2) -> str | None:
    number = _finite(value)
    if number is None:
        return None
    return f"{round(number, places):.{places}f}"


def _safe_text(value: Any, limit: int = 1000) -> str:
    if isinstance(value, dict | list | tuple | bool) or value is None:
        return ""
    return str(value).strip()[:limit]


def _sku_cost(
    sku: str,
    ean: str,
    quantity: int,
    marketplace: str,
) -> tuple[float | None, dict]:
    """The rightmost four components are authoritative, even for non-EAN codes."""
    parsed = composed_sku_order_financials(sku, ean, None)
    unit_cost = parsed.get("purchase_cost_eur")
    if marketplace == "worten":
        # accounting.resolve_purchase_cost uses the cecotec_orders parser:
        # a positive embedded cost is usable even when minimum is nonnumeric.
        parts = sku.rsplit("_", 3)
        if len(parts) == 4 and parts[0].strip() and parts[1].strip():
            candidate = _number(parts[2])
            unit_cost = candidate if candidate is not None and candidate > 0 else None
            parsed.update(
                {
                    "sku_supplier": parts[0].strip(),
                    "sku_product_code": parts[1].strip(),
                    "sku_ean_matches_order": parts[1].strip() == ean if ean else None,
                    "minimum_price_sku_eur": _number(parts[3]),
                }
            )
    details = {
        "sku_supplier": parsed.get("sku_supplier", ""),
        "sku_product_code": parsed.get("sku_product_code", ""),
        "sku_ean_matches_order": parsed.get("sku_ean_matches_order"),
        "sku_ean_note": parsed.get("sku_ean_note", ""),
        "purchase_unit_cost_eur": _decimal(unit_cost),
        "minimum_price_sku_eur": _decimal(parsed.get("minimum_price_sku_eur")),
    }
    return (None if unit_cost is None else round(unit_cost * quantity, 2)), details


def _kaufland_values(raw: dict) -> dict:
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    storefront = _safe_text(raw.get("storefront"), 20).lower()
    status = _safe_text(raw.get("status"), 100).lower()
    default_currency = STOREFRONT_INFO.get(storefront, ("", "", ""))[2]
    currency = _safe_text(raw.get("currency"), 10).upper() or default_currency
    price = _minor_money(raw.get("price"))
    shipping = _minor_money(raw.get("shipping_rate"))
    gross = _minor_money(raw.get("revenue_gross"))
    sale = (
        round((price or 0) + (shipping or 0), 2)
        if (price is not None or shipping is not None)
        else None
    )
    commission, source = _explicit_commission(raw)
    if commission is None and price is not None and gross is not None:
        commission = round(max(0, price - gross), 2)
        source = "Calcolato: prezzo prodotto - revenue_gross (API Kaufland)"
    if sale is not None and commission is not None:
        payout = round(sale - commission, 2)
        payout_source = "Calcolato: totale venduto - commissione"
    elif gross is not None:
        payout = round(gross + (shipping or 0), 2)
        payout_source = "Calcolato: revenue_gross + spedizione (API Kaufland)"
    else:
        payout, payout_source = None, ""
    received_at, received_source = _received_timestamp(raw, status)
    shipped_at, shipped_source = _shipped_timestamp(raw, status)
    released_at, released_source = _payment_release_timestamp(raw, status)
    carrier, tracking = extract_tracking(raw)
    return {
        "external_line_id": _safe_text(raw.get("id_order_unit"), 200),
        "order_id": _safe_text(raw.get("id_order"), 200),
        "created_at": _iso_seconds(raw.get("ts_created_iso")) or None,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status.replace("_", " ").capitalize()),
        "storefront": storefront,
        "currency": currency,
        "product_name": _safe_text(product.get("title")),
        "ean": _first_ean(product),
        "sku": _safe_text(raw.get("id_offer")),
        "quantity": 1,
        "sale_amount": sale,
        "shipping_amount": shipping,
        "commission_amount": commission,
        "commission_rate": (
            round(commission / sale * 100, 4) if sale and commission is not None else None
        ),
        "payout_amount": payout,
        "details": {
            "product_amount": _decimal(price),
            "revenue_gross": _decimal(gross),
            "revenue_net": _decimal(_minor_money(raw.get("revenue_net"))),
            "commission_source": source,
            "payout_source": payout_source,
            "carrier": carrier,
            "tracking": tracking,
            "carrier_source": "api" if carrier else "",
            "tracking_source": "api" if tracking else "",
            "received_at": received_at or None,
            "received_source": received_source,
            "shipped_at": shipped_at or None,
            "shipped_source": shipped_source,
            "released_at": released_at or None,
            "released_source": released_source,
            "updated_at": _iso_seconds(raw.get("ts_updated_iso")) or None,
            "excluded_from_totals": status in {"cancelled", "canceled"},
            "financial_source": "API Kaufland: importi in unità minime / 100",
        },
    }


def _worten_values(raw: dict) -> dict:
    order = raw.get("_order") if isinstance(raw.get("_order"), dict) else {}
    quantity = max(1, int(_number(_value(raw, "quantity"), 1) or 1))
    sku = _safe_text(
        _value(raw, "offer_sku", "seller_sku", "shop_sku", "offer_id", "sku", "product_sku")
    )
    if len(sku.rsplit("_", 3)) != 4:
        # Recover only this line's API SKU. Scanning sibling order lines could
        # substitute another product's publication cost (legacy raw recovery).
        for key in ("offer_sku", "seller_sku", "shop_sku", "marketplace_sku", "sku"):
            candidate = _safe_text(_value(raw, key))
            parts = candidate.rsplit("_", 3)
            if len(parts) == 4 and parts[0] and parts[1]:
                sku = candidate
                break
    status = _safe_text(
        _value(raw, "order_line_state", "state", "status")
        or _value(order, "order_state", "state", "status"),
        100,
    ).upper()
    label = WORTEN_ORDER_STATUS_LABELS.get(status, status.replace("_", " ").capitalize())
    gross = _mirakl_sale_amount(raw, quantity)
    shipping = _number(_value(raw, "shipping_price", "shipping_amount"))
    commission = _mirakl_commission_amount(raw)
    rate = commission_rate_from_order_line(raw)
    refund = _refund_amount(raw)
    direct = _mirakl_direct_payout(raw)
    status_detail = _safe_text(
        _value(raw, "cancellation_reason", "cancel_reason", "reason", "message")
        or _value(order, "cancellation_reason", "cancel_reason", "reason", "message")
    )
    zero_reason = _zero_economics_reason(status, label, status_detail)
    refund_source = "API Mirakl/Worten" if refund > 0 else ""
    source = "API Mirakl/Worten: importi in valuta principale"
    if zero_reason:
        sale = gross = refund = commission = payout = shipping = 0.0
        source = f"Calcolato dalla regola originale per stato: {zero_reason}"
        payout_source = source
    elif gross is None:
        sale, payout = None, direct
        payout_source = "API Mirakl/Worten: netto esplicito" if direct is not None else ""
    else:
        gross = max(0.0, gross)
        if ("refund" in status.lower() or "return" in status.lower()) and refund <= 0:
            refund = gross
            refund_source = "Calcolato dalla regola originale per stato: reso integrale"
        refund = min(gross, max(0.0, refund))
        sale = round(max(0.0, gross - refund), 2)
        if direct is not None:
            payout, payout_source = direct, "API Mirakl/Worten: netto esplicito"
        elif sale <= 0:
            payout, commission = 0.0, 0.0
            payout_source = "Calcolato dalla regola originale: vendita netta nulla"
        elif commission is not None:
            payout = round(sale - commission, 2)
            payout_source = "Calcolato: vendita netta - commissione"
        else:
            payout, payout_source = None, ""
    address = order.get("shipping_address")
    if not isinstance(address, dict):
        customer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
        address = customer.get("shipping_address", {})
    address = address if isinstance(address, dict) else {}
    country = _safe_text(
        _value(address, "country_code", "country")
        or _value(order, "shipping_country_code", "country_code", "country"),
        20,
    )
    ean = _safe_text(_value(raw, "ean", "ean13", "barcode", "gtin"), 100).removesuffix(".0")
    # An opaque product code is retained separately, never presented as an EAN.
    if not ean:
        code = sku.rsplit("_", 3)[1] if len(sku.rsplit("_", 3)) == 4 else ""
        for candidate in (code, _safe_text(_value(raw, "product_sku"))):
            if candidate.isdigit() and len(candidate) in {8, 12, 13, 14}:
                ean = candidate
                break
    carrier, tracking = extract_tracking(
        {key: value for key, value in raw.items() if not key.startswith("_")}
    )
    order_carrier, order_tracking = extract_tracking(
        {key: value for key, value in order.items() if key not in {"order_lines", "lines"}}
    )
    return {
        "external_line_id": _safe_text(_value(raw, "order_line_id", "id"), 200),
        "order_id": _safe_text(
            _value(order, "order_id", "commercial_id", "id") or _value(raw, "order_id"), 200
        ),
        "created_at": _iso_seconds(
            _value(order, "created_date", "date_created", "creation_date", "order_date")
        )
        or None,
        "status": status,
        "status_label": label,
        "storefront": country.lower() or "pt",
        "currency": _currency_code(raw, order),
        "product_name": _safe_text(
            _value(raw, "product_title", "product_name", "title", "product_label")
        ),
        "ean": ean,
        "sku": sku,
        "quantity": quantity,
        "sale_amount": sale,
        "shipping_amount": shipping,
        "commission_amount": commission,
        "commission_rate": rate.get("rate") if rate else None,
        "payout_amount": payout,
        "details": {
            "product_amount": _decimal(None if gross is None else gross - (shipping or 0)),
            "sale_original_amount": _decimal(gross),
            "refund_amount": _decimal(refund),
            "refund_source": refund_source,
            "commission_source": "API Mirakl/Worten" if commission is not None else "",
            "commission_rate_source": rate.get("method", "") if rate else "",
            "payout_source": payout_source,
            "financial_source": source,
            "zero_economic_reason": zero_reason,
            "carrier": carrier or order_carrier,
            "tracking": tracking or order_tracking,
            "updated_at": _iso_seconds(_value(order, "last_updated_date", "updated_date")) or None,
            "excluded_from_totals": bool(zero_reason),
        },
    }


def normalize_order_line(
    marketplace: str,
    raw: dict,
    *,
    fx_rates: dict | None = None,
) -> dict:
    """One physical Kaufland unit or one Mirakl line (including its quantity).

    Unqualified API amounts retain ``currency``. Purchase cost and profit are
    explicitly EUR, with *_eur aliases. All numeric monetary values are decimal
    strings; missing amounts stay None. ``raw`` is private persistence data.
    """
    if not isinstance(raw, dict):
        raise ValueError("invalid_order_line")
    if marketplace == "kaufland":
        output = _kaufland_values(raw)
    elif marketplace == "worten":
        output = _worten_values(raw)
    else:
        raise ValueError("unsupported_marketplace")
    if not output["external_line_id"] or not output["order_id"]:
        raise ValueError("missing_order_identifier")
    output["marketplace"] = marketplace
    details = output["details"]
    cost, sku_details = _sku_cost(
        output["sku"],
        output["ean"],
        output["quantity"],
        marketplace,
    )
    details.update(sku_details)
    warnings: list[str] = []
    if cost is None:
        warnings.append(
            "Costo non calcolabile: SKU non valido; ricerca nei listini non disponibile."
        )
    elif marketplace == "worten":
        warnings.append("Costo da SKU; confronto prioritario con i listini non ancora disponibile.")
    if details.get("zero_economic_reason"):
        cost = 0.0
        warnings = []
    snapshot = raw.get("_fx") if isinstance(raw.get("_fx"), dict) else {}
    rates = fx_rates if isinstance(fx_rates, dict) else snapshot.get("rates", {})
    rates = rates if isinstance(rates, dict) else {}
    currency = output["currency"]
    rate = 1.0 if currency == "EUR" else _finite(rates.get(currency))
    if rate is None or rate <= 0:
        rate = None
        warnings.append(f"Cambio {currency or 'valuta sconosciuta'}/EUR non disponibile.")
    details["fx"] = {
        "rate": _decimal(rate, 8),
        "date": _safe_text(snapshot.get("date"), 30),
        "source": _safe_text(snapshot.get("source"), 120),
        "online": snapshot.get("online") if isinstance(snapshot.get("online"), bool) else None,
    }
    for field in ("sale_amount", "shipping_amount", "commission_amount", "payout_amount"):
        value = output[field]
        output[field] = _decimal(value)
        output[f"{field}_eur"] = _decimal(value / rate) if value is not None and rate else None
    for field in ("product_amount", "sale_original_amount", "refund_amount"):
        if field in details:
            value = _finite(details[field])
            details[f"{field}_eur"] = _decimal(value / rate) if value is not None and rate else None
    if output["sale_amount"] is None:
        warnings.append("Prezzo di vendita non disponibile dall'API.")
    if output["commission_amount"] is None:
        warnings.append("Commissione non disponibile dall'API.")
    if output["payout_amount"] is None:
        warnings.append("Netto da ricevere non determinabile dai dati disponibili.")
    payout_eur = _finite(output["payout_amount_eur"])
    profit = round(payout_eur - cost, 2) if payout_eur is not None and cost is not None else None
    output["purchase_cost"] = output["purchase_cost_eur"] = _decimal(cost)
    output["purchase_cost_source"] = (
        f"Non dovuto: {details['zero_economic_reason']}"
        if details.get("zero_economic_reason")
        else "SKU composto: terzo valore, EUR"
        if cost is not None
        else "Costo non calcolabile"
    )
    output["profit_amount"] = output["profit_amount_eur"] = _decimal(profit)
    output["profit_pct"] = _decimal(profit / cost * 100) if profit is not None and cost else None
    output["commission_rate"] = _decimal(output["commission_rate"], 4)
    details["economic_currency"] = "EUR"
    if raw.get("_detail_warning") == "details_unavailable":
        details["detail_warning"] = "details_unavailable"
        warnings.append("Dettagli aggiuntivi non disponibili: conservati i dati dell'elenco API.")
    output["monetary_warnings"] = warnings
    output["raw"] = raw
    return output
