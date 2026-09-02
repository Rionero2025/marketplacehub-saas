from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from services.deletion_scope import detect_supplier_from_sku

CURRENCY_BY_STOREFRONT = {"pl": "PLN", "cz": "CZK"}


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_units_embedded(value: Any) -> str:
    """Return a Kaufland-valid ``embedded`` value for ``GET /units``.

    The current API accepts ``products``, ``eco_participation`` and
    ``battery_participation``. Older Marketplace Hub builds used the obsolete
    singular alias ``product``; keep accepting it locally and translate it to
    the official plural value before sending the request.
    """
    if isinstance(value, (list, tuple, set)):
        raw_values = [clean_text(item) for item in value]
    else:
        raw_values = [part.strip() for part in clean_text(value).split(",")]
    aliases = {"product": "products", "products": "products"}
    allowed = {"products", "eco_participation", "battery_participation"}
    normalized: list[str] = []
    for raw in raw_values:
        if not raw:
            continue
        item = aliases.get(raw.lower(), raw.lower())
        if item in allowed and item not in normalized:
            normalized.append(item)
    return ",".join(normalized)


def _product_rows_from_response(response: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Extract products embedded at response level by Kaufland ``GET /units``.

    Depending on the generated client/API representation, ``products`` may be
    returned as a list, a ``data`` collection or a mapping keyed by product ID.
    """
    if not isinstance(response, Mapping):
        return []
    candidates: list[Any] = []
    embedded = response.get("embedded")
    if isinstance(embedded, Mapping):
        candidates.extend([embedded.get("products"), embedded.get("product")])
    candidates.extend([response.get("products"), response.get("product")])

    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, list):
            result.extend(dict(item) for item in candidate if isinstance(item, Mapping))
        elif isinstance(candidate, Mapping):
            data = candidate.get("data")
            if isinstance(data, list):
                result.extend(dict(item) for item in data if isinstance(item, Mapping))
            elif candidate.get("id_product") not in (None, ""):
                result.append(dict(candidate))
            else:
                result.extend(
                    dict(item) for item in candidate.values() if isinstance(item, Mapping)
                )
    return result


def merge_response_products(
    rows: Iterable[Mapping[str, Any]], response: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Attach response-level embedded products to their corresponding units."""
    product_rows = _product_rows_from_response(response)
    by_id = {
        clean_text(item.get("id_product")): item
        for item in product_rows
        if clean_text(item.get("id_product"))
    }
    only_product = product_rows[0] if len(product_rows) == 1 else None
    merged: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        if not isinstance(item.get("product"), Mapping):
            product = by_id.get(clean_text(item.get("id_product"))) or only_product
            if isinstance(product, Mapping):
                item["product"] = dict(product)
        merged.append(item)
    return merged


def fetch_all_catalog_units(
    client: Any,
    storefront: str,
    *,
    embedded: str = "products",
    progress=None,
    page_limit: int = 100,
) -> list[dict[str, Any]]:
    """Return every seller unit for a storefront across client versions.

    Marketplace Hub installations can keep an older ``KauflandClient`` class in
    memory while a page is updated.  This compatibility layer therefore uses the
    newest ``all_units`` method when available, then falls back to ``units_page``
    and finally to the public ``request`` method with official limit/offset
    pagination.
    """
    storefront_code = clean_text(storefront).lower()
    if not storefront_code:
        raise ValueError("Lo storefront Kaufland è obbligatorio.")
    embedded_value = normalize_units_embedded(embedded)

    direct = getattr(client, "all_units", None)
    if callable(direct):
        values = direct(storefront_code, embedded=embedded_value, progress=progress)
        return [dict(item) for item in values or [] if isinstance(item, Mapping)]

    page_method = getattr(client, "units_page", None)
    request_method = getattr(client, "request", None)
    if not callable(page_method) and not callable(request_method):
        raise AttributeError(
            "Il client Kaufland installato non espone né all_units, né units_page, "
            "né il metodo request necessario per leggere GET /units/."
        )

    items: list[dict[str, Any]] = []
    offset = 0
    limit = max(1, min(100, int(page_limit or 100)))
    total: int | None = None
    while True:
        if callable(page_method):
            response = page_method(
                storefront_code, limit=limit, offset=offset, embedded=embedded_value
            ) or {}
        else:
            params: dict[str, Any] = {
                "storefront": storefront_code,
                "limit": limit,
                "offset": offset,
            }
            if embedded_value:
                params["embedded"] = embedded_value
            response = request_method("GET", "/units/", params=params) or {}

        page = response.get("data", []) if isinstance(response, Mapping) else []
        if not isinstance(page, list):
            page = []
        rows = merge_response_products(
            [dict(item) for item in page if isinstance(item, Mapping)],
            response if isinstance(response, Mapping) else None,
        )
        items.extend(rows)

        pagination = response.get("pagination", {}) if isinstance(response, Mapping) else {}
        try:
            total = int(pagination.get("total"))
        except (TypeError, ValueError, AttributeError):
            total = None
        if progress:
            progress(len(items), total)

        if not rows:
            break
        if total is not None and len(items) >= total:
            break
        if total is None and len(rows) < limit:
            break
        offset += len(rows)

    return items


def storefront_currency(storefront: Any) -> str:
    return CURRENCY_BY_STOREFRONT.get(clean_text(storefront).lower(), "EUR")


def price_to_eur(value: float, currency: str, rates: Mapping[str, Any]) -> float:
    code = clean_text(currency).upper() or "EUR"
    amount = float(value or 0.0)
    if code == "EUR":
        return amount
    rate = float((rates.get("rates") or {}).get(code) or 0.0)
    return amount / rate if rate > 0 else amount


def normalize_inventory_unit(
    unit: Mapping[str, Any],
    storefront: str,
    *,
    rates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = dict(unit)
    embedded = source.get("embedded") if isinstance(source.get("embedded"), Mapping) else {}
    product = source.get("product")
    if not isinstance(product, Mapping):
        product = embedded.get("product") if isinstance(embedded, Mapping) else {}
    if not isinstance(product, Mapping):
        products = source.get("products")
        if not isinstance(products, list) and isinstance(embedded, Mapping):
            products = embedded.get("products")
        if isinstance(products, list):
            product = next(
                (item for item in products if isinstance(item, Mapping)), {}
            )
        elif isinstance(products, Mapping):
            data = products.get("data")
            if isinstance(data, list):
                product = next(
                    (item for item in data if isinstance(item, Mapping)), {}
                )
            elif products.get("id_product") not in (None, ""):
                product = products
            else:
                product = next(
                    (item for item in products.values() if isinstance(item, Mapping)), {}
                )
    if not isinstance(product, Mapping):
        product = {}

    eans = product.get("eans") if isinstance(product.get("eans"), list) else []
    ean = clean_text(source.get("ean") or (eans[0] if eans else ""))
    sf = clean_text(source.get("storefront") or storefront).lower()
    currency = clean_text(source.get("currency") or storefront_currency(sf)).upper()
    raw_price = source.get("listing_price")
    if raw_price in (None, ""):
        raw_price = source.get("price")
    try:
        native_price = float(raw_price or 0.0) / 100.0
    except (TypeError, ValueError):
        native_price = 0.0
    rates = rates or {"rates": {}}
    eur_price = price_to_eur(native_price, currency, rates)
    status = clean_text(source.get("status") or source.get("unit_status") or "UNKNOWN").upper()
    id_offer = clean_text(source.get("id_offer"))
    return {
        "id_unit": clean_text(source.get("id_unit")),
        "id_product": clean_text(source.get("id_product") or product.get("id_product")),
        "id_offer": id_offer,
        "sku": id_offer,
        "ean": ean,
        "storefront": sf,
        "currency": currency,
        "price_native": round(native_price, 2),
        "price_eur": round(eur_price, 2),
        "amount": int(float(source.get("amount") or 0)),
        "status": status,
        "condition": clean_text(source.get("condition")),
        "title": clean_text(product.get("title") or source.get("title")),
        "manufacturer": clean_text(product.get("manufacturer") or source.get("manufacturer")),
        "date_inserted": clean_text(source.get("date_inserted_iso") or source.get("date_inserted")),
        "date_updated": clean_text(source.get("date_lastchange_iso") or source.get("date_lastchange")),
        "raw": source,
    }


def annotate_catalog_units(
    units: Iterable[Mapping[str, Any]],
    *,
    history_offers: Iterable[Mapping[str, Any]],
    suppliers: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    history_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    history_by_sku: dict[str, dict[str, Any]] = {}
    for source in history_offers:
        item = dict(source)
        sku = clean_text(item.get("sku_inviato") or item.get("id_offer") or item.get("sku"))
        sf = clean_text(item.get("paese") or item.get("storefront")).lower()
        if not sku:
            continue
        history_by_sku[sku] = item
        if sf:
            history_by_pair[(sf, sku)] = item

    supplier_items = [dict(item) for item in suppliers]
    result: list[dict[str, Any]] = []
    for source in units:
        item = dict(source)
        sku = clean_text(item.get("id_offer") or item.get("sku"))
        sf = clean_text(item.get("storefront")).lower()
        history = history_by_pair.get((sf, sku)) or history_by_sku.get(sku) or {}
        supplier_id = int(history.get("supplier_id") or 0)
        supplier_name = clean_text(history.get("supplier_name"))
        if not supplier_name:
            detected = detect_supplier_from_sku(sku, supplier_items)
            supplier_id = int(detected.get("id") or 0)
            supplier_name = clean_text(detected.get("name")) or "Sconosciuto"
        item.update(
            {
                "supplier_id": supplier_id,
                "supplier_name": supplier_name or "Sconosciuto",
                "price_list_id": int(history.get("price_list_id") or 0),
                "price_list_name": clean_text(history.get("price_list_name") or history.get("name"))
                or "Non associato a un listino",
                "operation_id": int(history.get("operation_id") or 0),
            }
        )
        result.append(item)
    return result


def filter_catalog_units(
    units: Iterable[Mapping[str, Any]],
    *,
    storefronts: Iterable[str] = (),
    supplier_ids: Iterable[int] = (),
    price_list_ids: Iterable[int] = (),
    minimum_price_eur: float | None = None,
    maximum_price_eur: float | None = None,
    minimum_amount: int | None = None,
    maximum_amount: int | None = None,
    statuses: Iterable[str] = (),
    conditions: Iterable[str] = (),
    search: str = "",
) -> list[dict[str, Any]]:
    sf_allowed = {clean_text(value).lower() for value in storefronts if clean_text(value)}
    supplier_allowed = {int(value) for value in supplier_ids}
    list_allowed = {int(value) for value in price_list_ids}
    status_allowed = {clean_text(value).upper() for value in statuses if clean_text(value)}
    condition_allowed = {clean_text(value).upper() for value in conditions if clean_text(value)}
    needle = clean_text(search).lower()
    result: list[dict[str, Any]] = []
    for source in units:
        item = dict(source)
        if sf_allowed and clean_text(item.get("storefront")).lower() not in sf_allowed:
            continue
        if supplier_allowed and int(item.get("supplier_id") or 0) not in supplier_allowed:
            continue
        if list_allowed and int(item.get("price_list_id") or 0) not in list_allowed:
            continue
        price = float(item.get("price_eur") or 0.0)
        if minimum_price_eur is not None and price < float(minimum_price_eur):
            continue
        if maximum_price_eur is not None and price > float(maximum_price_eur):
            continue
        amount = int(item.get("amount") or 0)
        if minimum_amount is not None and amount < int(minimum_amount):
            continue
        if maximum_amount is not None and amount > int(maximum_amount):
            continue
        if status_allowed and clean_text(item.get("status")).upper() not in status_allowed:
            continue
        if condition_allowed and clean_text(item.get("condition")).upper() not in condition_allowed:
            continue
        if needle:
            haystack = " ".join(
                clean_text(item.get(key)).lower()
                for key in (
                    "id_offer", "ean", "title", "manufacturer", "supplier_name",
                    "price_list_name", "storefront", "id_unit", "id_product",
                )
            )
            if needle not in haystack:
                continue
        result.append(item)
    return result
