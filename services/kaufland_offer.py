from __future__ import annotations

import re
from collections.abc import Mapping


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalized_ean(product: Mapping) -> str:
    value=str(product.get("ean","")).strip().removesuffix(".0")
    if not value or value.lower() in ("nan","none","null","<na>"):
        return ""
    return value


def has_valid_ean(product: Mapping) -> bool:
    return bool(normalized_ean(product))


def composed_sku(supplier_name: str, product: Mapping) -> str:
    """Build the Kaufland SKU from the exact commercial values shown in the grid."""
    supplier=re.sub(r"[^A-Za-z0-9-]+","-",str(supplier_name or "").strip()).strip("-") or "FORNITORE"
    ean=normalized_ean(product)
    if not ean:
        raise ValueError("EAN mancante: impossibile creare lo SKU composto.")
    purchase=_number(product.get("cost"))
    minimum=_number(product.get("minimum_price"))
    return f"{supplier}_{ean}_{purchase:.2f}_{minimum:.2f}"


def price_fields(product: Mapping, currency_multiplier: float = 1.0) -> dict[str,int]:
    """Return Kaufland price fields without recalculating or adding shipping again.

    ``price`` and ``minimum_price`` are already calculated from the total cost in
    the publication grid.  They are the source of truth for both the API payload
    and the composed SKU.  Only the storefront currency conversion is applied.
    """
    multiplier=_number(currency_multiplier)
    if multiplier <= 0:
        raise ValueError("Il moltiplicatore valuta deve essere maggiore di zero.")
    listing=_number(product.get("price"))
    minimum=_number(product.get("minimum_price"))
    if listing <= 0:
        raise ValueError("Prezzo di vendita non valido.")
    if minimum <= 0:
        raise ValueError("Prezzo minimo non valido.")
    return {
        "listing_price":int(round(listing*multiplier*100)),
        "minimum_price":int(round(minimum*multiplier*100)),
    }


def commercial_values(purchase_cost, shipping_cost, margin_pct,
                      minimum_margin_pct, commission_pct) -> dict[str,float]:
    """Calculate the commercial values shown and sent for one storefront."""
    purchase=max(0.0,_number(purchase_cost))
    shipping=max(0.0,_number(shipping_cost))
    total=purchase+shipping
    price=total*(1+_number(margin_pct)/100)
    minimum=total*(1+_number(minimum_margin_pct)/100)
    commission=price*_number(commission_pct)/100
    return {
        "cost":round(total,2),
        "price":round(price,2),
        "minimum_price":round(minimum,2),
        "commission_eur":round(commission,2),
        "profit":round(price-commission-total,2),
    }
