from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from services.catalog_intelligence.mapping import auto_map_attributes
from services.catalog_intelligence.models import TaxonomyAttribute, ValidationIssue
from services.catalog_intelligence.utils import clean_text, load_json, normalized_ean, slug


_EMPTY = (None, "", [], {})
_TEXT_LIMITS = {
    "TINYTEXT": 256,
    "SHORTTEXT": 512,
    "SMALLTEXT": 1024,
    "TEXT": 65535,
}


def valid_gtin_checksum(value: str) -> bool:
    digits = normalized_ean(value)
    if not digits or len(digits) not in {8, 12, 13, 14}:
        return False
    payload = [int(item) for item in digits]
    check = payload.pop()
    total = 0
    for index, digit in enumerate(reversed(payload)):
        total += digit * (3 if index % 2 == 0 else 1)
    expected = (10 - (total % 10)) % 10
    return check == expected


def _attribute_from_row(row: Mapping[str, Any]) -> TaxonomyAttribute:
    values = row.get("values") if isinstance(row.get("values"), list) else []
    conditions = row.get("conditions") if isinstance(row.get("conditions"), list) else []
    return TaxonomyAttribute(
        external_id=clean_text(row.get("external_id")),
        category_external_id=clean_text(row.get("category_external_id")),
        code=clean_text(row.get("code")),
        label=clean_text(row.get("label")),
        data_type=clean_text(row.get("data_type")) or "TEXT",
        requirement_level=clean_text(row.get("requirement_level")) or "OPTIONAL",
        required=bool(row.get("required")),
        multiple=bool(row.get("multiple")),
        variant=bool(row.get("variant")),
        unit=clean_text(row.get("unit")),
        locale=clean_text(row.get("locale")),
        value_list_code=clean_text(row.get("value_list_code")),
        constraints=(
            dict(row.get("constraints"))
            if isinstance(row.get("constraints"), Mapping)
            else load_json(row.get("constraints_json"), {})
        ),
        values=values,
        conditions=conditions,
        raw=(
            dict(row.get("raw"))
            if isinstance(row.get("raw"), Mapping)
            else load_json(row.get("raw_json"), {})
        ),
    )


def _values(value: Any) -> list[Any]:
    if value in _EMPTY:
        return []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item not in _EMPTY]
    return [value]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value).replace("\u00a0", " ")
    if not text:
        return None
    # Accept both marketplace-formatted decimal commas and normalized dots.
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", text.replace(".", "."))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _valid_url(value: Any) -> bool:
    try:
        parsed = urlparse(clean_text(value))
    except Exception:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _allowed_tokens(attribute: TaxonomyAttribute) -> set[str]:
    result: set[str] = set()
    for item in attribute.values:
        if isinstance(item, Mapping):
            for key in ("code", "id", "value", "key", "label", "name", "description"):
                token = slug(item.get(key))
                if token:
                    result.add(token)
        else:
            token = slug(item)
            if token:
                result.add(token)
    return result


def _constraint_number(constraints: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        if constraints.get(name) not in (None, ""):
            return _number(constraints.get(name))
    return None


def _validate_attribute_value(
    attribute: TaxonomyAttribute,
    value: Any,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    values = _values(value)
    label = attribute.label or attribute.code or attribute.external_id
    if not attribute.multiple and len(values) > 1:
        issues.append(
            ValidationIssue(
                "ERROR",
                "ATTRIBUTE_MULTIPLE_NOT_ALLOWED",
                f"L'attributo {label} accetta un solo valore.",
                attribute.external_id,
                {"value_count": len(values)},
            )
        )
    data_type = clean_text(attribute.data_type).upper().replace("-", "_")
    for item in values:
        text = clean_text(item)
        if data_type in {"INT", "INTEGER"}:
            number = _number(item)
            if number is None or not float(number).is_integer():
                issues.append(
                    ValidationIssue(
                        "ERROR", "ATTRIBUTE_TYPE_INT", f"{label} richiede un numero intero.",
                        attribute.external_id, {"value": item},
                    )
                )
        elif data_type in {"FLOAT", "NUMBER", "DECIMAL", "DOUBLE"} or data_type.startswith("SI_"):
            if _number(item) is None:
                issues.append(
                    ValidationIssue(
                        "ERROR", "ATTRIBUTE_TYPE_NUMBER", f"{label} richiede un valore numerico.",
                        attribute.external_id, {"value": item, "type": data_type},
                    )
                )
        elif data_type in {"BOOL", "BOOLEAN"}:
            if text.lower() not in {
                "true", "false", "0", "1", "t", "f", "yes", "no", "y", "n", "ja", "nein",
                "si", "sì",
            } and not isinstance(item, bool):
                issues.append(
                    ValidationIssue(
                        "ERROR", "ATTRIBUTE_TYPE_BOOL", f"{label} richiede un valore booleano.",
                        attribute.external_id, {"value": item},
                    )
                )
        elif data_type in {"EAN", "GTIN"}:
            if not normalized_ean(item):
                issues.append(
                    ValidationIssue(
                        "ERROR", "ATTRIBUTE_TYPE_EAN", f"{label} richiede un EAN/GTIN valido.",
                        attribute.external_id, {"value": item},
                    )
                )
        elif data_type in {"PICTURE", "IMAGE", "URL"}:
            if not _valid_url(item):
                issues.append(
                    ValidationIssue(
                        "ERROR", "ATTRIBUTE_TYPE_URL", f"{label} richiede un URL HTTP/HTTPS valido.",
                        attribute.external_id, {"value": item},
                    )
                )
        elif data_type == "DATE":
            accepted = False
            for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    datetime.strptime(text, fmt)
                    accepted = True
                    break
                except ValueError:
                    pass
            if not accepted:
                issues.append(
                    ValidationIssue(
                        "ERROR", "ATTRIBUTE_TYPE_DATE", f"{label} richiede una data valida.",
                        attribute.external_id, {"value": item},
                    )
                )

        maximum_length = _constraint_number(attribute.constraints, "max_length", "maxlength")
        if maximum_length is None:
            maximum_length = float(_TEXT_LIMITS.get(data_type, 0) or 0) or None
        minimum_length = _constraint_number(attribute.constraints, "min_length", "minlength")
        if maximum_length is not None and len(text) > int(maximum_length):
            issues.append(
                ValidationIssue(
                    "ERROR", "ATTRIBUTE_MAX_LENGTH",
                    f"{label} supera la lunghezza massima di {int(maximum_length)} caratteri.",
                    attribute.external_id, {"length": len(text), "maximum": int(maximum_length)},
                )
            )
        if minimum_length is not None and len(text) < int(minimum_length):
            issues.append(
                ValidationIssue(
                    "ERROR", "ATTRIBUTE_MIN_LENGTH",
                    f"{label} non raggiunge la lunghezza minima di {int(minimum_length)} caratteri.",
                    attribute.external_id, {"length": len(text), "minimum": int(minimum_length)},
                )
            )

        pattern = clean_text(attribute.constraints.get("regex") or attribute.constraints.get("pattern"))
        if pattern:
            try:
                matches = re.fullmatch(pattern, text) is not None
            except re.error:
                matches = True
            if not matches:
                issues.append(
                    ValidationIssue(
                        "ERROR", "ATTRIBUTE_PATTERN",
                        f"{label} non rispetta il formato richiesto dal marketplace.",
                        attribute.external_id, {"value": item, "pattern": pattern},
                    )
                )

        number = _number(item)
        minimum = _constraint_number(attribute.constraints, "minimum", "min")
        maximum = _constraint_number(attribute.constraints, "maximum", "max")
        if number is not None and minimum is not None and number < minimum:
            issues.append(
                ValidationIssue(
                    "ERROR", "ATTRIBUTE_MIN_VALUE", f"{label} è inferiore al minimo ammesso.",
                    attribute.external_id, {"value": number, "minimum": minimum},
                )
            )
        if number is not None and maximum is not None and number > maximum:
            issues.append(
                ValidationIssue(
                    "ERROR", "ATTRIBUTE_MAX_VALUE", f"{label} supera il massimo ammesso.",
                    attribute.external_id, {"value": number, "maximum": maximum},
                )
            )

    allowed = _allowed_tokens(attribute)
    if allowed:
        for item in values:
            if slug(item) not in allowed:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        "ATTRIBUTE_VALUE_NOT_ALLOWED",
                        f"Valore non ammesso per {label}: {clean_text(item)}.",
                        attribute.external_id,
                        {"value": item, "allowed_count": len(allowed)},
                    )
                )
    return issues


def validate_canonical_product(
    product: Mapping[str, Any],
    *,
    marketplace: str,
    category: Mapping[str, Any] | None = None,
    attributes: Iterable[TaxonomyAttribute | Mapping[str, Any]] = (),
    manual_values: Mapping[str, Any] | None = None,
    strict_product_feed: bool = False,
) -> tuple[list[ValidationIssue], dict[str, Any]]:
    normalized = load_json(product.get("normalized_json"), {})
    if not isinstance(normalized, Mapping):
        normalized = {}
    else:
        normalized = dict(normalized)
    if not isinstance(normalized.get("source_attributes"), Mapping):
        raw_source = load_json(product.get("raw_json"), {})
        if isinstance(raw_source, Mapping):
            normalized["source_attributes"] = dict(raw_source)
    issues: list[ValidationIssue] = []
    ean = normalized_ean(product.get("ean") or normalized.get("ean"))
    if not ean:
        issues.append(
            ValidationIssue("ERROR", "MISSING_EAN", "EAN/GTIN obbligatorio per creare il prodotto.", "ean")
        )
    elif not valid_gtin_checksum(ean):
        issues.append(
            ValidationIssue(
                "WARNING",
                "EAN_CHECKSUM",
                "L'EAN è formalmente lungo ma il checksum non risulta valido.",
                "ean",
                {"ean": ean},
            )
        )
    if not clean_text(product.get("title") or normalized.get("title")):
        issues.append(ValidationIssue("ERROR", "MISSING_TITLE", "Titolo prodotto mancante.", "title"))

    missing_level = "ERROR" if strict_product_feed else "WARNING"
    if not clean_text(product.get("brand") or normalized.get("brand")):
        issues.append(ValidationIssue(missing_level, "MISSING_BRAND", "Marca non rilevata nel listino.", "brand"))
    if not clean_text(product.get("description") or normalized.get("description")):
        issues.append(
            ValidationIssue(missing_level, "MISSING_DESCRIPTION", "Descrizione prodotto mancante.", "description")
        )
    images = normalized.get("images") or []
    if not images:
        issues.append(ValidationIssue(missing_level, "MISSING_IMAGES", "Nessuna immagine rilevata.", "images"))
    else:
        for url in images:
            if not _valid_url(url):
                issues.append(
                    ValidationIssue(
                        "ERROR", "INVALID_IMAGE_URL", "URL immagine non valido.", "images", {"url": url}
                    )
                )

    selected_category = clean_text((category or {}).get("external_id"))
    if not selected_category:
        issues.append(
            ValidationIssue(
                "ERROR",
                "MISSING_CATEGORY",
                f"Categoria {clean_text(marketplace).title()} non selezionata.",
                "category",
            )
        )
    elif not bool((category or {}).get("is_leaf")):
        issues.append(
            ValidationIssue(
                "ERROR",
                "CATEGORY_NOT_LEAF",
                "La categoria selezionata non è una categoria foglia pubblicabile.",
                "category",
                {"category": selected_category},
            )
        )

    typed_attributes: list[TaxonomyAttribute] = [
        item if isinstance(item, TaxonomyAttribute) else _attribute_from_row(item)
        for item in attributes
    ]
    mapped = auto_map_attributes(normalized, typed_attributes)
    manual = dict(manual_values or {})
    for attribute in typed_attributes:
        candidates = (
            attribute.external_id,
            attribute.code,
            slug(attribute.external_id),
            slug(attribute.code),
        )
        value = None
        found = False
        for key in candidates:
            if key in manual and manual[key] not in _EMPTY:
                value = manual[key]
                found = True
                break
        if found:
            mapped[attribute.external_id] = {
                "value": value,
                "source": "system_or_manual",
                "source_kind": "SYSTEM" if attribute.code in {"category"} else "MANUAL",
                "confidence": 1.0,
            }

    for key, value in manual.items():
        if value not in _EMPTY and clean_text(key) not in mapped:
            mapped[clean_text(key)] = {
                "value": value,
                "source": "system_or_manual",
                "source_kind": "MANUAL",
                "confidence": 1.0,
            }

    for attribute in typed_attributes:
        value_record = mapped.get(attribute.external_id)
        value = value_record.get("value") if value_record else None
        if attribute.required and value in _EMPTY:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    "MISSING_REQUIRED_ATTRIBUTE",
                    f"Attributo obbligatorio mancante: {attribute.label or attribute.code or attribute.external_id}.",
                    attribute.external_id,
                    {
                        "category": attribute.category_external_id,
                        "attribute": attribute.external_id,
                    },
                )
            )
            continue
        if value not in _EMPTY:
            issues.extend(_validate_attribute_value(attribute, value))
        elif clean_text(attribute.requirement_level).upper() == "CONDITIONAL" and attribute.conditions:
            # The rule is retained in the snapshot and shown to the user.  If it
            # cannot be evaluated deterministically yet, do not invent a value.
            issues.append(
                ValidationIssue(
                    "WARNING",
                    "CONDITIONAL_ATTRIBUTE_NOT_APPLIED",
                    f"Verificare la condizione per {attribute.label or attribute.code or attribute.external_id}.",
                    attribute.external_id,
                    {"conditions": attribute.conditions},
                )
            )

    severities = {item.severity.upper() for item in issues}
    status = (
        "BLOCKED"
        if {"ERROR", "BLOCKER"} & severities
        else "VALID_WITH_WARNINGS"
        if "WARNING" in severities
        else "VALID"
    )
    readiness = max(0.0, min(100.0, float(product.get("completeness_score") or 0.0)))
    required_total = sum(1 for item in typed_attributes if item.required)
    required_present = sum(
        1
        for item in typed_attributes
        if item.required and mapped.get(item.external_id, {}).get("value") not in _EMPTY
    )
    if required_total:
        schema_completeness = required_present / required_total * 100.0
        readiness = readiness * 0.45 + schema_completeness * 0.55
    if status == "BLOCKED":
        readiness = min(readiness, 59.0)
    elif status == "VALID_WITH_WARNINGS":
        readiness = min(94.0, max(readiness, 60.0))
    else:
        readiness = max(95.0, readiness)
    return issues, {
        "status": status,
        "readiness_score": round(readiness, 2),
        "mapped_attributes": mapped,
        "category_external_id": selected_category,
        "required_attributes": required_total,
        "required_attributes_present": required_present,
    }


__all__ = ["valid_gtin_checksum", "validate_canonical_product"]
