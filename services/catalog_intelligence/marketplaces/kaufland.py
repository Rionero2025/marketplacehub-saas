from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from services.catalog_intelligence.models import (
    Capability,
    TaxonomyAttribute,
    TaxonomyBundle,
    TaxonomyCategory,
)
from services.catalog_intelligence.utils import (
    as_list,
    bool_value,
    clean_text,
    first_value,
    int_value,
    slug,
)
from services.kaufland import KauflandClient


def _items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in keys + ("data", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            nested = value.get("data")
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, Mapping)]
    return []


def _category_id(raw: Mapping[str, Any]) -> str:
    return clean_text(first_value(raw, "id_category", "category_id", "id", "code"))


def _category_parent(raw: Mapping[str, Any]) -> str:
    return clean_text(
        first_value(raw, "id_parent_category", "id_parent", "parent_id", "parent_code")
    )


def _category_label(raw: Mapping[str, Any]) -> str:
    return clean_text(
        first_value(
            raw,
            "title_singular",
            "title_plural",
            "label",
            "display_name",
            "name",
            "code",
        )
    )


def parse_categories(raw_categories: Iterable[Mapping[str, Any]]) -> list[TaxonomyCategory]:
    raw_by_id: dict[str, dict[str, Any]] = {}
    for source in raw_categories:
        raw = dict(source)
        external_id = _category_id(raw)
        if external_id:
            raw_by_id[external_id] = raw

    if "1" not in raw_by_id:
        raw_by_id["1"] = {
            "id_category": 1,
            "id_parent_category": 0,
            "name": "root",
            "title_singular": "Root",
            "level": 0,
        }

    parents_with_children = {
        _category_parent(raw)
        for raw in raw_by_id.values()
        if _category_parent(raw)
    }

    path_cache: dict[str, str] = {}

    def path_for(external_id: str, stack: set[str] | None = None) -> str:
        if external_id in path_cache:
            return path_cache[external_id]
        stack = set(stack or ())
        if external_id in stack:
            return _category_label(raw_by_id.get(external_id, {}))
        stack.add(external_id)
        raw = raw_by_id.get(external_id, {})
        label = _category_label(raw) or external_id
        parent = _category_parent(raw)
        if not parent or parent in {"0", external_id} or parent not in raw_by_id:
            result = label
        else:
            prefix = path_for(parent, stack)
            result = f"{prefix} > {label}" if prefix else label
        path_cache[external_id] = result
        return result

    result: list[TaxonomyCategory] = []
    for external_id, raw in raw_by_id.items():
        explicit_leaf = first_value(raw, "is_leaf", "leaf", default=None)
        if explicit_leaf is None or explicit_leaf == "":
            is_leaf = external_id not in parents_with_children
        else:
            is_leaf = bool_value(explicit_leaf)
        required = []
        for item in as_list(raw.get("required_attributes")):
            if isinstance(item, Mapping):
                token = clean_text(
                    first_value(item, "id_attribute", "attribute_id", "code", "name", "id")
                )
            else:
                token = clean_text(item)
            if token:
                required.append(token)
        result.append(
            TaxonomyCategory(
                external_id=external_id,
                parent_external_id=_category_parent(raw),
                code=clean_text(first_value(raw, "name", "code", default=external_id)),
                label=_category_label(raw) or external_id,
                path=path_for(external_id),
                level=int_value(first_value(raw, "level", "depth", default=0)),
                is_leaf=is_leaf,
                product_type=clean_text(first_value(raw, "product_type", "name")),
                required_attributes=list(dict.fromkeys(required)),
                raw=raw,
            )
        )
    return sorted(result, key=lambda item: (item.path.lower(), item.external_id))


def _attribute_id(raw: Mapping[str, Any]) -> str:
    return clean_text(
        first_value(raw, "id_attribute", "attribute_id", "id", "code", "name")
    )


def _attribute_label(raw: Mapping[str, Any]) -> str:
    return clean_text(
        first_value(raw, "title", "label", "display_name", "name", "code", "id_attribute")
    )


def _attribute_values(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in (
        "values",
        "allowed_values",
        "shared_set_values",
        "value_list",
        "options",
    ):
        value = raw.get(key)
        if isinstance(value, list):
            normalized: list[dict[str, Any]] = []
            for item in value:
                if isinstance(item, Mapping):
                    normalized.append(dict(item))
                elif clean_text(item):
                    normalized.append({"code": clean_text(item), "label": clean_text(item)})
            return normalized
        if isinstance(value, Mapping):
            normalized = []
            for code, label in value.items():
                if isinstance(label, Mapping):
                    record = dict(label)
                    record.setdefault("code", clean_text(code))
                else:
                    record = {"code": clean_text(code), "label": clean_text(label)}
                normalized.append(record)
            return normalized
    return []


def parse_attribute(
    raw: Mapping[str, Any],
    *,
    category_external_id: str = "",
    requirement_level: str = "OPTIONAL",
    condition: Mapping[str, Any] | None = None,
) -> TaxonomyAttribute | None:
    source = dict(raw)
    external_id = _attribute_id(source)
    if not external_id:
        return None
    requirement = clean_text(
        first_value(source, "requirement_level", "requirement", "required", default=requirement_level)
    ).upper()
    required = requirement in {"REQUIRED", "MANDATORY", "TRUE", "1"} or bool_value(
        source.get("required"), default=requirement_level.upper() == "REQUIRED"
    )
    if required:
        requirement = "REQUIRED"
    elif requirement_level.upper() == "CONDITIONAL" or condition:
        requirement = "CONDITIONAL"
    else:
        requirement = "OPTIONAL"
    constraints: dict[str, Any] = {}
    for key in (
        "min_length",
        "max_length",
        "minimum",
        "maximum",
        "min",
        "max",
        "regex",
        "pattern",
        "precision",
        "scale",
    ):
        if source.get(key) not in (None, ""):
            constraints[key] = source.get(key)
    conditions = [dict(condition)] if isinstance(condition, Mapping) else []
    nested_conditions = source.get("conditions") or source.get("condition")
    if isinstance(nested_conditions, Mapping):
        conditions.append(dict(nested_conditions))
    elif isinstance(nested_conditions, list):
        conditions.extend(dict(item) for item in nested_conditions if isinstance(item, Mapping))
    return TaxonomyAttribute(
        external_id=external_id,
        category_external_id=clean_text(category_external_id),
        code=clean_text(first_value(source, "name", "code", default=external_id)),
        label=_attribute_label(source) or external_id,
        data_type=clean_text(
            first_value(source, "data_type", "type", "value_type", default="TEXT")
        ).upper(),
        requirement_level=requirement,
        required=required,
        multiple=bool_value(
            first_value(
                source,
                "multiple",
                "is_multiple",
                "is_multiple_allowed",
                "multi_value",
                default=False,
            )
        ),
        variant=bool_value(first_value(source, "variant", "is_variant", default=False)),
        unit=clean_text(first_value(source, "unit", "unit_code", "measurement_unit")),
        locale=clean_text(first_value(source, "locale", "language")),
        value_list_code=clean_text(
            first_value(source, "shared_set", "value_list_code", "value_list", "set_id")
        ),
        constraints=constraints,
        values=_attribute_values(source),
        conditions=conditions,
        raw=source,
    )


def parse_general_attributes(raw_attributes: Iterable[Mapping[str, Any]]) -> list[TaxonomyAttribute]:
    result: list[TaxonomyAttribute] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_attributes:
        attribute = parse_attribute(raw)
        if not attribute:
            continue
        key = (attribute.category_external_id, attribute.external_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(attribute)
    return result


def parse_category_attributes(
    category_external_id: str,
    detail: Mapping[str, Any],
) -> list[TaxonomyAttribute]:
    result: list[TaxonomyAttribute] = []
    seen: set[tuple[str, str]] = set()
    groups = (
        ("required_attributes", "REQUIRED"),
        ("optional_attributes", "OPTIONAL"),
        ("conditional_attributes", "CONDITIONAL"),
    )
    for field, requirement in groups:
        for item in as_list(detail.get(field)):
            if not isinstance(item, Mapping):
                continue
            condition = item.get("condition") if requirement == "CONDITIONAL" else None
            attribute = parse_attribute(
                item,
                category_external_id=category_external_id,
                requirement_level=requirement,
                condition=condition if isinstance(condition, Mapping) else None,
            )
            if not attribute:
                continue
            key = (attribute.category_external_id, attribute.external_id)
            if key in seen:
                continue
            seen.add(key)
            result.append(attribute)
    return result


def discover_capabilities(client: KauflandClient) -> list[Capability]:
    capabilities: list[Capability] = []

    def check(key: str, callback) -> Any:
        try:
            value = callback()
            details: dict[str, Any] = {}
            if isinstance(value, list):
                details["count"] = len(value)
                details["sample"] = value[:10]
            elif isinstance(value, Mapping):
                details["keys"] = sorted(str(k) for k in value.keys())[:30]
            capabilities.append(Capability(key=key, supported=True, details=details))
            return value
        except Exception as exc:  # capability discovery must never alter remote data
            status = None
            message = clean_text(exc)
            if message.startswith("HTTP "):
                try:
                    status = int(message.split()[1].rstrip(":"))
                except (TypeError, ValueError, IndexError):
                    status = None
            capabilities.append(
                Capability(key=key, supported=False, status_code=status, message=message[:1000])
            )
            return None

    check("connection", client.ping)
    storefronts = check("storefronts", client.storefronts) or []
    check("locales", client.locales)
    storefront = clean_text(storefronts[0] if storefronts else "de").lower()
    check(
        "categories",
        lambda: _items(client.categories_page(storefront, parent_id=1, limit=1), "categories"),
    )
    check(
        "attributes",
        lambda: _items(client.attributes(storefront, limit=1), "attributes"),
    )
    check("warehouses", lambda: client.warehouses(storefront))
    check("shipping_groups", lambda: client.shipping_groups(storefront))
    # Write capabilities are deliberately not probed: discovery must be read-only.
    capabilities.extend(
        [
            Capability(
                key="product_data_write",
                supported=True,
                message="Disponibile tramite PUT /product-data; non provato per evitare modifiche remote.",
                details={"probe": "documentation_only", "safe_read_only_discovery": True},
            ),
            Capability(
                key="unit_write",
                supported=True,
                message="Disponibile tramite POST /units; non provato per evitare modifiche remote.",
                details={"probe": "documentation_only", "safe_read_only_discovery": True},
            ),
        ]
    )
    return capabilities


def sync_taxonomy(
    client: KauflandClient,
    *,
    storefront: str,
    locale: str = "",
    category_attribute_ids: Iterable[str] = (),
    progress=None,
) -> TaxonomyBundle:
    storefront = clean_text(storefront).lower()
    if not storefront:
        raise ValueError("Seleziona uno storefront Kaufland.")
    raw_categories = client.all_categories(storefront, locale=locale, progress=progress)
    categories = parse_categories(raw_categories)
    attributes = parse_general_attributes(client.all_attributes(storefront, locale=locale))

    requested = list(dict.fromkeys(clean_text(value) for value in category_attribute_ids if clean_text(value)))
    for category_id in requested:
        try:
            detail = client.category(
                int(category_id), storefront, locale=locale, include_attributes=True
            )
        except (TypeError, ValueError):
            continue
        attributes.extend(parse_category_attributes(category_id, detail if isinstance(detail, Mapping) else {}))

    deduplicated: dict[tuple[str, str], TaxonomyAttribute] = {}
    for item in attributes:
        key = (item.category_external_id, item.external_id)
        previous = deduplicated.get(key)
        if previous is None or (item.required and not previous.required):
            deduplicated[key] = item

    locales = client.locales()
    return TaxonomyBundle(
        marketplace="kaufland",
        scope_key=f"{storefront}:{clean_text(locale) or '*'}",
        storefront=storefront,
        locale=clean_text(locale),
        categories=categories,
        attributes=list(deduplicated.values()),
        locales=locales,
        metadata={
            "source": "Kaufland Seller API v2",
            "root_category_id": "1",
            "category_attributes_loaded_for": requested,
            "category_count": len(categories),
            "attribute_count": len(deduplicated),
        },
    )


__all__ = [
    "discover_capabilities",
    "parse_attribute",
    "parse_categories",
    "parse_category_attributes",
    "parse_general_attributes",
    "sync_taxonomy",
]
