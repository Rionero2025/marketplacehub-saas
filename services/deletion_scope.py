from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping


def _clean(value: object) -> str:
    return str(value or "").strip()


def normalize_supplier_token(value: object) -> str:
    """Normalize supplier names and SKU prefixes for stable matching."""
    text = unicodedata.normalize("NFKD", _clean(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def detect_supplier_from_sku(
    sku: object,
    suppliers: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Return the supplier whose name matches the composite-SKU prefix.

    Marketplace Hub normally publishes SKUs as
    ``fornitore_codice_costo_prezzominimo``.  Matching is deliberately strict:
    an exact normalized prefix is preferred and fuzzy guesses are avoided.
    """
    raw_sku = _clean(sku)
    first_token = re.split(r"[_|;]", raw_sku, maxsplit=1)[0]
    normalized_first = normalize_supplier_token(first_token)
    normalized_sku = normalize_supplier_token(raw_sku)

    candidates: list[tuple[int, int, dict[str, object]]] = []
    for supplier in suppliers:
        supplier_name = _clean(supplier.get("name"))
        supplier_token = normalize_supplier_token(supplier_name)
        if not supplier_token:
            continue
        score = 0
        if normalized_first == supplier_token:
            score = 100
        elif normalized_sku.startswith(supplier_token):
            score = 80
        if score:
            candidates.append((score, len(supplier_token), dict(supplier)))
    if not candidates:
        return {"id": 0, "name": "Sconosciuto", "matched": False}
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    result = candidates[0][2]
    result["matched"] = True
    return result


def selected_price_list_ids(
    mode: str,
    available_lists: Iterable[Mapping[str, object]],
    *,
    supplier_ids: Iterable[int] = (),
    price_list_ids: Iterable[int] = (),
) -> list[int]:
    """Resolve the list IDs included by a deletion scope."""
    items = [dict(item) for item in available_lists]
    normalized_mode = _clean(mode).lower()
    if normalized_mode == "all":
        return sorted({int(item["id"]) for item in items})
    if normalized_mode == "suppliers":
        chosen = {int(value) for value in supplier_ids}
        return sorted(
            {
                int(item["id"])
                for item in items
                if int(item.get("supplier_id") or 0) in chosen
            }
        )
    if normalized_mode == "lists":
        allowed = {int(item["id"]) for item in items}
        return sorted({int(value) for value in price_list_ids if int(value) in allowed})
    raise ValueError(f"Modalità di cancellazione sconosciuta: {mode}")


def annotate_history_offers(
    offers: Iterable[Mapping[str, object]],
    available_lists: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Attach supplier/list metadata to active Kaufland publication history."""
    list_lookup = {int(item["id"]): dict(item) for item in available_lists}
    annotated: list[dict[str, object]] = []
    for source in offers:
        item = dict(source)
        list_id = int(item.get("price_list_id") or 0)
        price_list = list_lookup.get(list_id, {})
        item["supplier_id"] = int(price_list.get("supplier_id") or 0)
        item["supplier_name"] = _clean(price_list.get("supplier_name")) or "Sconosciuto"
        item["price_list_name"] = _clean(price_list.get("name")) or "Listino non disponibile"
        annotated.append(item)
    return annotated


def filter_history_offers(
    offers: Iterable[Mapping[str, object]],
    price_list_ids: Iterable[int],
) -> list[dict[str, object]]:
    allowed = {int(value) for value in price_list_ids}
    return [
        dict(item)
        for item in offers
        if int(item.get("price_list_id") or 0) in allowed
    ]
