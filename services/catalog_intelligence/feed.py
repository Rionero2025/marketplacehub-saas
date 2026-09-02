from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from services.catalog_intelligence.mapping import canonical_field_for_attribute
from services.catalog_intelligence.models import (
    ProductFeedPreparation,
    TaxonomyAttribute,
    ValidationIssue,
)
from services.catalog_intelligence.utils import clean_text, json_hash, load_json, normalized_ean, slug
from services.catalog_intelligence.validation import validate_canonical_product


_EMPTY = (None, "", [], {})


def _normalized(product: Mapping[str, Any]) -> dict[str, Any]:
    value = load_json(product.get("normalized_json"), {})
    result = dict(value) if isinstance(value, Mapping) else {}
    if not isinstance(result.get("source_attributes"), Mapping):
        raw = load_json(product.get("raw_json"), {})
        if isinstance(raw, Mapping):
            result["source_attributes"] = dict(raw)
    return result


def _raw(product: Mapping[str, Any]) -> dict[str, Any]:
    value = load_json(product.get("raw_json"), {})
    return dict(value) if isinstance(value, Mapping) else {}


def _first_raw(raw: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    values = {slug(key): value for key, value in raw.items()}
    for alias in aliases:
        value = values.get(slug(alias))
        if value not in _EMPTY:
            return value
    return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value).replace("\u00a0", " ")
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _compact_number(value: Any, *, decimal_comma: bool = False) -> str:
    number = _float(value)
    if number is None:
        return clean_text(value)
    if math.isclose(number, round(number), abs_tol=1e-9):
        text = str(int(round(number)))
    else:
        text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if decimal_comma else text


def _allowed_value(attribute: TaxonomyAttribute, value: Any) -> Any:
    if not attribute.values:
        return value
    index: dict[str, Any] = {}
    for item in attribute.values:
        if not isinstance(item, Mapping):
            index[slug(item)] = item
            continue
        canonical = (
            item.get("value")
            if item.get("value") not in _EMPTY
            else item.get("code")
            if item.get("code") not in _EMPTY
            else item.get("id")
            if item.get("id") not in _EMPTY
            else item.get("key")
        )
        for key in ("value", "code", "id", "key", "label", "name", "description"):
            token = slug(item.get(key))
            if token:
                index[token] = canonical if canonical not in _EMPTY else item.get(key)
    return index.get(slug(value), value)


def _kaufland_si_value(attribute: TaxonomyAttribute, value: Any, canonical_field: str) -> str:
    data_type = clean_text(attribute.data_type).upper()
    unit = clean_text(attribute.unit).lower()
    if canonical_field == "power_w" or "WATT" in data_type or unit in {"w", "watt"}:
        return f"{_compact_number(value, decimal_comma=True)}W"
    if canonical_field == "capacity_l" or "LITER" in data_type or unit in {"l", "liter", "litre"}:
        return f"{_compact_number(value, decimal_comma=True)}l"
    if canonical_field == "weight_kg" or "KILOGRAM" in data_type or unit in {"kg", "kilogram"}:
        return f"{_compact_number(value, decimal_comma=True)}kg"
    if canonical_field in {"length_cm", "width_cm", "height_cm"} or "METER" in data_type:
        # Canonical dimensions are stored in centimetres; sending cm avoids a
        # hidden conversion and is accepted by Kaufland Si_Meter fields.
        return f"{_compact_number(value, decimal_comma=True)}cm"
    suffix = clean_text(attribute.unit)
    return f"{_compact_number(value, decimal_comma=True)}{suffix}"


def format_kaufland_value(
    attribute: TaxonomyAttribute,
    value: Any,
    *,
    canonical_field: str = "",
) -> Any:
    if isinstance(value, (list, tuple, set)):
        return [
            format_kaufland_value(attribute, item, canonical_field=canonical_field)
            for item in value
            if item not in _EMPTY
        ]
    value = _allowed_value(attribute, value)
    data_type = clean_text(attribute.data_type).upper().replace("-", "_")
    if data_type.startswith("SI_"):
        return _kaufland_si_value(attribute, value, canonical_field)
    if data_type in {"FLOAT", "NUMBER", "DECIMAL", "DOUBLE"}:
        return _compact_number(value, decimal_comma=True)
    if data_type in {"INT", "INTEGER"}:
        number = _float(value)
        return str(int(round(number))) if number is not None else clean_text(value)
    if data_type in {"BOOL", "BOOLEAN"}:
        if isinstance(value, bool):
            return "true" if value else "false"
        return "true" if clean_text(value).lower() in {"1", "true", "yes", "y", "si", "sì", "ja"} else "false"
    return clean_text(value)


def format_mirakl_value(attribute: TaxonomyAttribute, value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return [_allowed_value(attribute, item) for item in value if item not in _EMPTY]
    return _allowed_value(attribute, value)


def _general_values(
    product: Mapping[str, Any],
    category: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = _normalized(product)
    category_name = clean_text(category.get("code") or category.get("label") or category.get("external_id"))
    return {
        "ean": normalized_ean(product.get("ean") or normalized.get("ean")),
        "title": clean_text(product.get("title") or normalized.get("title")),
        "description": clean_text(product.get("description") or normalized.get("description")),
        "short_description": clean_text(normalized.get("short_description")),
        "manufacturer": clean_text(product.get("brand") or normalized.get("brand")),
        "brand": clean_text(product.get("brand") or normalized.get("brand")),
        "mpn": clean_text(product.get("model") or normalized.get("model") or product.get("supplier_sku")),
        "model": clean_text(product.get("model") or normalized.get("model")),
        "category": category_name,
        "picture": list(normalized.get("images") or []),
        "images": list(normalized.get("images") or []),
        "documents": list(normalized.get("documents") or []),
        "product_url": clean_text(
            normalized.get("product_url")
            or _first_raw(_raw(product), ("product_url", "card_url", "url", "product_page_url"))
        ),
        # The entire source feed remains available for deterministic category
        # and attribute mapping. It is not sent as an unknown marketplace field.
        "source_attributes": dict(normalized.get("source_attributes") or _raw(product) or {}),
    }


def _attribute_manual_values(
    product: Mapping[str, Any],
    category: Mapping[str, Any],
    attributes: Iterable[TaxonomyAttribute],
) -> dict[str, Any]:
    general = _general_values(product, category)
    result: dict[str, Any] = {}
    for attribute in attributes:
        code = slug(attribute.code or attribute.external_id)
        value = None
        if code in {"ean", "gtin", "product_id"}:
            value = general["ean"]
        elif code in {"title", "name", "product_name", "designation"}:
            value = general["title"]
        elif code in {"description", "long_description", "product_description"}:
            value = general["description"]
        elif code in {"short_description", "summary", "subtitle"}:
            value = general["short_description"]
        elif code in {"manufacturer", "brand", "maker", "marca"}:
            value = general["manufacturer"]
        elif code in {"mpn", "model", "manufacturer_part_number", "part_number"}:
            value = general["mpn"]
        elif code in {"category", "category_code", "hierarchy", "hierarchy_code"}:
            value = general["category"]
        elif code in {"picture", "pictures", "image", "images", "media", "image_url", "image_urls"}:
            value = general["picture"]
        elif code in {
            "document", "documents", "manual", "manuals", "datasheet",
            "data_sheet", "technical_document", "technical_documents",
            "attachment", "attachments",
        }:
            value = general["documents"]
        elif code in {"product_url", "product_page_url", "card_url", "product_link"}:
            value = general["product_url"]
        if value not in _EMPTY:
            result[attribute.external_id] = value
            result[attribute.code] = value
    return result


def _mapped_by_attribute(
    summary: Mapping[str, Any],
    attributes: Iterable[TaxonomyAttribute],
    *,
    marketplace: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    by_id = {item.external_id: item for item in attributes}
    fields: dict[str, Any] = {}
    records: dict[str, dict[str, Any]] = {}
    for external_id, mapping in dict(summary.get("mapped_attributes") or {}).items():
        attribute = by_id.get(external_id)
        if not attribute:
            continue
        value = mapping.get("value") if isinstance(mapping, Mapping) else None
        if value in _EMPTY:
            continue
        canonical_field = canonical_field_for_attribute(attribute)
        formatted = (
            format_kaufland_value(attribute, value, canonical_field=canonical_field)
            if marketplace == "kaufland"
            else format_mirakl_value(attribute, value)
        )
        field_key = clean_text(attribute.code or attribute.external_id)
        if not field_key:
            continue
        fields[field_key] = formatted
        records[external_id] = {
            "target": field_key,
            "value": formatted,
            "source_value": value,
            "source": mapping.get("source") if isinstance(mapping, Mapping) else "",
            "source_kind": mapping.get("source_kind") if isinstance(mapping, Mapping) else "",
            "confidence": mapping.get("confidence") if isinstance(mapping, Mapping) else 1.0,
            "canonical_field": canonical_field,
        }
    return fields, records


def build_kaufland_product_payload(
    product: Mapping[str, Any],
    *,
    category: Mapping[str, Any],
    attributes: Iterable[TaxonomyAttribute],
    validation_summary: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    attributes = list(attributes)
    general = _general_values(product, category)
    mapped_fields, mapped_records = _mapped_by_attribute(
        validation_summary, attributes, marketplace="kaufland"
    )
    product_attributes: dict[str, list[Any]] = {}

    # General product data are included even when an API taxonomy response does
    # not repeat the general attributes for the selected category.
    general_order = (
        "title", "description", "short_description", "manufacturer", "mpn", "category", "picture"
    )
    for key in general_order:
        value = general.get(key)
        if value in _EMPTY:
            continue
        if isinstance(value, list):
            product_attributes[key] = [item for item in value if item not in _EMPTY]
        else:
            product_attributes[key] = [value]

    for key, value in mapped_fields.items():
        if slug(key) in {"ean", "gtin"}:
            continue
        if value in _EMPTY:
            continue
        formatted_values = list(value) if isinstance(value, list) else [value]
        existing = product_attributes.setdefault(key, [])
        for item in formatted_values:
            if item not in _EMPTY and item not in existing:
                existing.append(item)

    payload = {
        "ean": [general["ean"]] if general["ean"] else [],
        "attributes": product_attributes,
    }
    return payload, mapped_records


def build_mirakl_product_payload(
    product: Mapping[str, Any],
    *,
    category: Mapping[str, Any],
    attributes: Iterable[TaxonomyAttribute],
    validation_summary: Mapping[str, Any],
    locale: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    attributes = list(attributes)
    normalized = _normalized(product)
    general = _general_values(product, category)
    mapped_fields, mapped_records = _mapped_by_attribute(
        validation_summary, attributes, marketplace="worten"
    )
    sku = clean_text(product.get("supplier_sku") or normalized.get("supplier_sku") or general["ean"])
    category_code = clean_text(category.get("external_id"))

    fields: dict[str, Any] = {
        "product-sku": sku,
        "product-id": general["ean"],
        "product-id-type": "EAN",
        "category-code": category_code,
        "title": general["title"],
        "description": general["description"],
        "brand": general["brand"],
        "manufacturer-part-number": general["mpn"],
    }
    if general["short_description"]:
        fields["short-description"] = general["short_description"]
    for index, url in enumerate(general["images"], start=1):
        fields[f"image-{index}"] = url
    for key, value in mapped_fields.items():
        if value not in _EMPTY:
            fields[key] = value
    fields = {key: value for key, value in fields.items() if value not in _EMPTY}
    payload = {
        "format": "MIRAKL_DYNAMIC_PRODUCT_RECORD",
        "category_code": category_code,
        "locale": clean_text(locale),
        "product_sku": sku,
        "product_id": general["ean"],
        "product_id_type": "EAN",
        "attributes": fields,
        # This flat record can be written directly to the tenant-specific P41
        # template once its headers are confirmed from the connected account.
        "csv_row": fields,
    }
    return payload, mapped_records


def build_offer_payload(
    product: Mapping[str, Any],
    *,
    marketplace: str,
    storefront: str,
) -> dict[str, Any]:
    normalized = _normalized(product)
    raw = _raw(product)
    ean = normalized_ean(product.get("ean") or normalized.get("ean"))
    sku = clean_text(product.get("supplier_sku") or normalized.get("supplier_sku") or ean)
    stock = normalized.get("stock")
    selling_price = _first_raw(
        raw,
        (
            "selling_price", "sale_price", "retail_price", "marketplace_price", "prezzo_vendita",
            "preco_venda", "precio_venta", "recommended_selling_price",
        ),
    )
    price = _float(selling_price)
    if marketplace == "kaufland":
        payload: dict[str, Any] = {
            "ean": ean,
            "id_offer": sku,
            "condition": "NEW",
            "storefront": clean_text(storefront).lower(),
        }
        if price is not None:
            payload["listing_price"] = int(round(price * 100))
        if stock not in _EMPTY:
            payload["amount"] = max(0, int(float(stock)))
        return payload
    payload = {
        "shop_sku": sku,
        "product_id": ean,
        "product_id_type": "EAN",
        "state_code": "11",  # conventional Mirakl code for new; tenant validation remains authoritative
    }
    if price is not None:
        payload["price"] = round(price, 2)
    if stock not in _EMPTY:
        payload["quantity"] = max(0, int(float(stock)))
    return payload


def prepare_product_feed(
    product: Mapping[str, Any],
    *,
    marketplace: str,
    category: Mapping[str, Any],
    attributes: Iterable[TaxonomyAttribute],
    storefront: str = "",
    locale: str = "",
    supplemental_values: Mapping[str, Any] | None = None,
    supplemental_mapping_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> ProductFeedPreparation:
    marketplace = clean_text(marketplace).lower()
    attributes = list(attributes)
    manual_values = _attribute_manual_values(product, category, attributes)
    for key, value in dict(supplemental_values or {}).items():
        if value not in _EMPTY:
            manual_values[clean_text(key)] = value
    issues, summary = validate_canonical_product(
        product,
        marketplace=marketplace,
        category=category,
        attributes=attributes,
        manual_values=manual_values,
        strict_product_feed=True,
    )
    # Validation remains authoritative.  Supplemental records only restore the
    # provenance metadata (for example AI_EVIDENCE) after the validator has
    # accepted the supplied value.
    mapped_summary = dict(summary.get("mapped_attributes") or {})
    for external_id, metadata in dict(supplemental_mapping_records or {}).items():
        key = clean_text(external_id)
        if key not in mapped_summary or not isinstance(metadata, Mapping):
            continue
        current = dict(mapped_summary[key])
        current.update({
            "source": clean_text(metadata.get("source")) or current.get("source", ""),
            "source_kind": clean_text(metadata.get("source_kind")) or current.get("source_kind", ""),
            "confidence": float(metadata.get("confidence") or current.get("confidence") or 0.0),
        })
        if clean_text(metadata.get("reason")):
            current["reason"] = clean_text(metadata.get("reason"))
        mapped_summary[key] = current
    summary["mapped_attributes"] = mapped_summary
    if marketplace == "kaufland":
        product_payload, mapped_records = build_kaufland_product_payload(
            product,
            category=category,
            attributes=attributes,
            validation_summary=summary,
        )
    elif marketplace == "worten":
        product_payload, mapped_records = build_mirakl_product_payload(
            product,
            category=category,
            attributes=attributes,
            validation_summary=summary,
            locale=locale,
        )
    else:
        raise ValueError(f"Marketplace non supportato per il feed prodotto: {marketplace}")

    offer_payload = build_offer_payload(product, marketplace=marketplace, storefront=storefront)
    missing_fields = list(
        dict.fromkeys(
            item.field_name or item.code
            for item in issues
            if item.code.startswith("MISSING_") or item.code == "MISSING_REQUIRED_ATTRIBUTE"
        )
    )
    validator_status = clean_text(summary.get("status")).upper()
    validation_status = (
        "READY"
        if validator_status == "VALID"
        else "VALID_WITH_WARNINGS"
        if validator_status == "VALID_WITH_WARNINGS"
        else "BLOCKED"
    )
    payload_hash = json_hash(
        {
            "marketplace": marketplace,
            "category": clean_text(category.get("external_id")),
            "product": product_payload,
            "offer": offer_payload,
        }
    )
    return ProductFeedPreparation(
        marketplace=marketplace,
        category_external_id=clean_text(category.get("external_id")),
        product_payload=product_payload,
        offer_payload=offer_payload,
        mapped_attributes=mapped_records,
        missing_fields=missing_fields,
        issues=issues,
        validation_status=validation_status,
        readiness_score=float(summary.get("readiness_score") or 0.0),
        payload_hash=payload_hash,
    )


def preparation_as_record(preparation: ProductFeedPreparation) -> dict[str, Any]:
    return {
        "marketplace": preparation.marketplace,
        "category_external_id": preparation.category_external_id,
        "product_payload": preparation.product_payload,
        "offer_payload": preparation.offer_payload,
        "mapped_attributes": preparation.mapped_attributes,
        "missing_fields": preparation.missing_fields,
        "issues": [
            {
                "severity": item.severity,
                "code": item.code,
                "message": item.message,
                "field_name": item.field_name,
                "details": item.details,
            }
            for item in preparation.issues
        ],
        "validation_status": preparation.validation_status,
        "readiness_score": preparation.readiness_score,
        "payload_hash": preparation.payload_hash,
    }


__all__ = [
    "build_kaufland_product_payload",
    "build_mirakl_product_payload",
    "build_offer_payload",
    "format_kaufland_value",
    "format_mirakl_value",
    "preparation_as_record",
    "prepare_product_feed",
]
