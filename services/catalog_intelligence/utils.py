from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from functools import lru_cache
from collections.abc import Mapping, Sequence
from typing import Any


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list, tuple, int, float, bool)):
        return value
    try:
        return json.loads(clean_text(value) or "null")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {} if default is None else default


@lru_cache(maxsize=65536)
def _slug_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")


def slug(value: Any) -> str:
    # Column names, taxonomy labels and supplier parameter names repeat across
    # thousands of products.  Caching their normalized token avoids repeating
    # Unicode normalization and regular expressions millions of times.
    return _slug_text(clean_text(value))


def first_value(mapping: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    normalized = {slug(key): value for key, value in mapping.items()}
    for name in names:
        key = slug(name)
        if key in normalized and clean_text(normalized[key]):
            return normalized[key]
    return default


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if isinstance(value, Mapping):
        for key in ("data", "items", "values", "results", "categories", "attributes", "hierarchies"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
        return list(value.values())
    return [value]


def bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = clean_text(value).lower()
    if text in {"1", "true", "yes", "y", "si", "sì", "required", "mandatory"}:
        return True
    if text in {"0", "false", "no", "n", "optional"}:
        return False
    return default


def int_value(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def float_value(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def normalized_ean(value: Any) -> str:
    text = re.sub(r"\D+", "", clean_text(value).replace(".0", ""))
    return text if 8 <= len(text) <= 14 else ""


def stable_row_key(record: Mapping[str, Any], row_number: int = 0) -> str:
    ean = normalized_ean(first_value(record, "ean", "gtin", "barcode"))
    sku = clean_text(first_value(record, "sku", "supplier_sku", "reference", "codice"))
    if ean or sku:
        return json_hash({"ean": ean, "sku": sku})[:32]
    return json_hash({"row": int(row_number), "record": dict(record)})[:32]


def trim_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            nested = trim_dict(item)
            if nested:
                result[str(key)] = nested
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            cleaned = [x for x in item if x not in (None, "", [], {})]
            if cleaned:
                result[str(key)] = cleaned
        elif clean_text(item):
            result[str(key)] = item
    return result


__all__ = [
    "as_list",
    "bool_value",
    "bytes_hash",
    "canonical_json",
    "clean_text",
    "first_value",
    "float_value",
    "int_value",
    "json_hash",
    "load_json",
    "normalized_ean",
    "slug",
    "stable_row_key",
    "trim_dict",
]
