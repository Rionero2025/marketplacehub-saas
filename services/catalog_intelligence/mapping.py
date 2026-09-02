from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from services.catalog_intelligence.models import TaxonomyAttribute
from services.catalog_intelligence.utils import clean_text, slug


CANONICAL_ATTRIBUTE_ALIASES: dict[str, tuple[str, ...]] = {
    "ean": ("ean", "gtin", "barcode", "product_id"),
    "title": ("title", "name", "product_name", "designation"),
    "description": ("description", "long_description", "product_description"),
    "short_description": ("short_description", "summary", "subtitle"),
    "brand": ("brand", "manufacturer", "maker", "mark", "marca", "producer", "producent"),
    "model": ("model", "mpn", "manufacturer_part_number", "part_number"),
    "images": ("picture", "pictures", "image", "images", "media"),
    "weight_kg": ("weight", "weight_kg", "package_weight"),
    "length_cm": ("length", "length_cm", "package_length"),
    "width_cm": ("width", "width_cm", "package_width"),
    "height_cm": ("height", "height_cm", "package_height"),
    "power_w": ("power", "power_w", "wattage"),
    "capacity_l": ("capacity", "capacity_l", "volume", "volume_l"),
    "color": ("color", "colour", "farbe", "cor"),
    "material": ("material", "materials"),
}

_ALIAS_TO_CANONICAL = {
    slug(alias): canonical
    for canonical, aliases in CANONICAL_ATTRIBUTE_ALIASES.items()
    for alias in (canonical, *aliases)
}


def canonical_field_for_attribute(attribute: TaxonomyAttribute | Mapping[str, Any]) -> str:
    if isinstance(attribute, TaxonomyAttribute):
        candidates = (attribute.code, attribute.external_id, attribute.label)
    else:
        candidates = (
            attribute.get("code"),
            attribute.get("external_id"),
            attribute.get("label"),
        )
    for candidate in candidates:
        token = slug(candidate)
        if token in _ALIAS_TO_CANONICAL:
            return _ALIAS_TO_CANONICAL[token]
        for alias, canonical in _ALIAS_TO_CANONICAL.items():
            if alias and (token == alias or token.endswith(f"_{alias}") or alias.endswith(f"_{token}")):
                return canonical
    return ""


def mapped_value(
    normalized: Mapping[str, Any],
    attribute: TaxonomyAttribute | Mapping[str, Any],
) -> tuple[str, Any]:
    canonical = canonical_field_for_attribute(attribute)
    if canonical and normalized.get(canonical) not in (None, "", [], {}):
        return canonical, normalized.get(canonical)
    # Exact source-field fallback remains deterministic and does not invent data.
    source_attributes = normalized.get("source_attributes")
    if isinstance(source_attributes, Mapping):
        tokens = {
            slug(key): (str(key), value)
            for key, value in source_attributes.items()
            if value not in (None, "", [], {})
        }
        if isinstance(attribute, TaxonomyAttribute):
            candidates = (attribute.code, attribute.external_id, attribute.label)
        else:
            candidates = (
                attribute.get("code"),
                attribute.get("external_id"),
                attribute.get("label"),
            )
        for candidate in candidates:
            token = slug(candidate)
            if token and token in tokens:
                original_key, value = tokens[token]
                return f"source_attributes.{original_key}", value

        # IOF full feeds keep the complete technical parameter dictionary.  It
        # is intentionally searched by the official marketplace attribute code
        # or label so product characteristics are reused deterministically and
        # are never invented merely because they are nested in the supplier XML.
        for container_name in ("parameters", "technical_attributes", "attributes", "specifications"):
            nested = source_attributes.get(container_name)
            if not isinstance(nested, Mapping):
                continue
            nested_tokens = {
                slug(key): (str(key), value)
                for key, value in nested.items()
                if value not in (None, "", [], {})
            }
            for candidate in candidates:
                token = slug(candidate)
                found = nested_tokens.get(token)
                if token and found:
                    original_key, value = found
                    return f"source_attributes.{container_name}.{original_key}", value
    return "", None


def auto_map_attributes(
    normalized: Mapping[str, Any],
    attributes: Iterable[TaxonomyAttribute | Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for attribute in attributes:
        external_id = clean_text(
            attribute.external_id if isinstance(attribute, TaxonomyAttribute) else attribute.get("external_id")
        )
        if not external_id:
            continue
        source_field, value = mapped_value(normalized, attribute)
        if value in (None, "", [], {}):
            continue
        result[external_id] = {
            "value": value,
            "source": source_field,
            "source_kind": "DETERMINISTIC",
            "confidence": 1.0,
        }
    return result


__all__ = [
    "CANONICAL_ATTRIBUTE_ALIASES",
    "auto_map_attributes",
    "canonical_field_for_attribute",
    "mapped_value",
]
