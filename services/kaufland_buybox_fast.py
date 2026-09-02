from __future__ import annotations

"""Fast Buy Box refresh using the persistent Kaufland inventory cache.

The complete account inventory and the expensive commercial/economic resolution are
stored elsewhere.  A fast refresh therefore performs only one live ``GET /buybox``
request per offer and reuses all static data from the cached unit / previous check.
"""

import json
from typing import Any

from services.kaufland_buybox import parse_buybox_response
from services.kaufland_buybox_account import effective_commission
from services.kaufland_profit import buybox_financials


class QuickBuyboxNeedsFullCheck(RuntimeError):
    """The cached offer does not contain enough IDs for a one-request refresh."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # pandas NA/nan without importing pandas
    return None if number != number else number


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _cached_details(cached: dict[str, Any]) -> dict[str, Any]:
    raw = cached.get("details_json")
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def quick_buybox_check(
    client: Any,
    item: dict[str, Any],
    *,
    cached: dict[str, Any] | None = None,
    own_seller_pseudonyms: Any = None,
    checked_at: str,
) -> dict[str, Any]:
    """Refresh one offer with exactly one Kaufland ``GET /buybox`` request.

    ``item`` is the persistent live-unit row prepared by the Buy Box page.  ``cached``
    is the previous Buy Box check, if any.  Product/list/supplier/commission/cost data
    are intentionally reused instead of being re-downloaded.
    """
    cached = dict(cached or {})
    country = _text(item.get("paese")).lower()
    sku = _text(item.get("SKU inviato"))
    ean = _text(item.get("EAN"))
    id_product = _integer(item.get("_id_product")) or _integer(cached.get("id_product"))
    id_unit = _integer(item.get("_id_unit")) or _integer(cached.get("id_unit"))
    if not country or not sku:
        raise QuickBuyboxNeedsFullCheck("Paese o SKU dell'offerta memorizzata mancante.")
    if not id_product:
        raise QuickBuyboxNeedsFullCheck(
            f"ID prodotto Kaufland non memorizzato per {sku}; esegui un controllo completo una sola volta."
        )

    # This is the only live API request in the quick path.
    response = client.buybox(id_product, country, condition="new", limit=10)
    normalized = parse_buybox_response(response, sku, own_seller_pseudonyms)

    publication_currency = _text(item.get("_currency") or cached.get("currency") or "EUR").upper()
    eur_multiplier = _number(item.get("_eur_multiplier")) or 1.0

    our_price = _number(normalized.get("our_price"))
    if our_price is None:
        our_price = _number(item.get("Prezzo API"))
    if our_price is None:
        our_price = _number(cached.get("our_price"))
    our_shipping = _number(normalized.get("our_shipping"))
    if our_shipping is None:
        our_shipping = _number(cached.get("our_shipping"))
    if our_shipping is None:
        our_shipping = 0.0 if our_price is not None else None
    our_total = _number(normalized.get("our_total"))
    if our_total is None and our_price is not None:
        our_total = float(our_price) + float(our_shipping or 0)

    normalized["our_price"] = our_price
    normalized["our_shipping"] = our_shipping
    normalized["our_total"] = our_total
    normalized["currency"] = _text(normalized.get("currency") or cached.get("currency") or publication_currency).upper()

    minimum_price = _number(item.get("Prezzo minimo API"))
    minimum_price_source = "Manifest API memorizzato"
    if minimum_price is None:
        minimum_price = _number(cached.get("minimum_price"))
        minimum_price_source = _text(cached.get("minimum_price_source")) or "Ultimo controllo salvato"

    total_cost_eur = _number(item.get("Costo totale €"))
    if total_cost_eur is None:
        total_cost_eur = _number(cached.get("total_cost_eur"))
    purchase_cost_eur = _number(item.get("Costo acquisto €"))
    if purchase_cost_eur is None:
        purchase_cost_eur = _number(cached.get("purchase_cost_eur"))
    shipping_cost_eur = _number(item.get("Spedizione fornitore €"))
    if shipping_cost_eur is None:
        shipping_cost_eur = _number(cached.get("shipping_cost_eur"))

    commission_pct = _number(cached.get("commission_pct"))
    if commission_pct is None:
        commission_pct = _number(item.get("Commissione di riserva %")) or 15.0
    commission_fixed_eur = _number(cached.get("commission_fixed_eur")) or 0.0
    commission_source = _text(cached.get("commission_source")) or "Commissione memorizzata / regola di riserva"

    financial = buybox_financials(
        total_cost_eur=total_cost_eur,
        commission_pct=commission_pct,
        commission_fixed_eur=commission_fixed_eur,
        currency=normalized["currency"],
        eur_multiplier=eur_multiplier,
        status=normalized.get("status") or "",
        target_price=normalized.get("target_price"),
        winner_price=normalized.get("winner_price"),
        winner_total=normalized.get("winner_total"),
        our_price=normalized.get("our_price"),
        our_shipping=normalized.get("our_shipping"),
    )

    current_total = _number(normalized.get("our_total"))
    current_total_eur = current_total
    if (
        current_total is not None
        and normalized["currency"] != "EUR"
        and eur_multiplier > 0
    ):
        current_total_eur = current_total / eur_multiplier
    current_commission = effective_commission(
        current_total_eur, commission_pct, commission_fixed_eur
    )

    target_total_eur = None
    if financial.get("target_sales_price") is not None:
        target_total = float(financial["target_sales_price"]) + float(normalized.get("our_shipping") or 0)
        target_total_eur = (
            target_total / eur_multiplier
            if normalized["currency"] != "EUR" and eur_multiplier > 0
            else target_total
        )
    target_commission = effective_commission(
        target_total_eur, commission_pct, commission_fixed_eur
    )

    old_details = _cached_details(cached)
    old_details["buybox"] = response
    old_details["quick_check"] = True
    old_details["quick_check_api_requests"] = 1

    return {
        "paese": country,
        "ean": ean,
        "sku": sku,
        "original_sku": _text(item.get("SKU originale") or cached.get("original_sku")),
        "product_title": _text(item.get("Prodotto") or cached.get("product_title")),
        "inventory_status": _text(item.get("Stato API") or cached.get("inventory_status")),
        "inventory_amount": _integer(item.get("Quantità")) if item.get("Quantità") not in (None, "") else _integer(cached.get("inventory_amount")),
        "matched_price_list_id": _integer(item.get("_matched_price_list_id")) or _integer(cached.get("matched_price_list_id")),
        "matched_saved_view_id": _integer(item.get("_matched_saved_view_id")) or _integer(cached.get("matched_saved_view_id")),
        "supplier_name": _text(item.get("Fornitore") or cached.get("supplier_name")),
        "price_list_name": _text(item.get("Listino abbinato") or cached.get("price_list_name")),
        "cost_match_source": _text(item.get("Origine costo") or cached.get("cost_match_source")),
        "cost_match_count": _integer(item.get("Corrispondenze")) or _integer(cached.get("cost_match_count")) or 0,
        "purchase_cost_eur": purchase_cost_eur,
        "shipping_cost_eur": shipping_cost_eur,
        "total_cost_eur": total_cost_eur,
        "commission_pct": commission_pct,
        "commission_fixed_eur": commission_fixed_eur,
        "commission_source": commission_source,
        "actual_order_commission_pct": _number(cached.get("actual_order_commission_pct")),
        "actual_order_commission_local": _number(cached.get("actual_order_commission_local")),
        "actual_order_commission_currency": _text(cached.get("actual_order_commission_currency")),
        "actual_order_id": _text(cached.get("actual_order_id")),
        "id_product": id_product,
        "id_unit": id_unit or _integer(normalized.get("id_unit")),
        **normalized,
        "minimum_price": minimum_price,
        "minimum_price_source": minimum_price_source,
        "own_delivery_min": normalized.get("own_delivery_min") if normalized.get("own_delivery_min") is not None else _integer(cached.get("own_delivery_min")),
        "own_delivery_max": normalized.get("own_delivery_max") if normalized.get("own_delivery_max") is not None else _integer(cached.get("own_delivery_max")),
        "own_handling_time": _integer(cached.get("own_handling_time")),
        "logistics_status": _text(cached.get("logistics_status")),
        "current_commission_eur": current_commission.get("commission_eur"),
        "current_commission_effective_pct": current_commission.get("effective_pct"),
        "target_commission_eur": target_commission.get("commission_eur"),
        "target_commission_effective_pct": target_commission.get("effective_pct"),
        **financial,
        "ok": True,
        "error_type": "",
        "error": "",
        "detected_pseudonyms": [],
        "details": old_details,
        "checked_at": checked_at,
    }
