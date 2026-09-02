from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from services.catalog_intelligence.models import CanonicalProduct, Evidence
from services.catalog_intelligence.repository import (
    create_source_snapshot,
    mark_source_snapshot_normalized,
    save_canonical_products,
    source_snapshot_normalization_cache,
)
from services.catalog_intelligence.utils import (
    clean_text,
    json_hash,
    normalized_ean,
    slug,
    stable_row_key,
)
from services.lists import normalize as normalize_price_list, read_list


NORMALIZATION_ENGINE_VERSION = 252
ProgressCallback = Callable[[dict[str, Any]], None]

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_UNIT_RE = re.compile(r"(?i)(kg|mg|g|mm|cm|m|ml|cl|dl|l|w|kw|°?c|celsius)\b")
_GRAM_SOURCE_RE = re.compile(r"(?:^|_)g(?:$|_)")
_URL_SPLIT_RE = re.compile(r"[\n\r;,|]+")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _notify_progress(callback: ProgressCallback | None, payload: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:
        # A Streamlit placeholder can disappear during a rerender.  Progress is
        # informative and must never invalidate the catalogue transaction.
        return


TITLE_ALIASES: tuple[str, ...] = (
    "name", "title", "nome", "product_name", "product_title", "item_name",
    "article_name", "article_title", "designation", "denomination",
    "denominazione", "bezeichnung", "nazwa", "label", "short_name",
)


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "brand": (
        "brand", "marca", "manufacturer", "produttore", "fabricante", "marka",
        "brand_name", "manufacturer_name",
        # Innpro and several B2B supplier feeds expose the commercial brand in
        # a column called ``producer``.  Keep it after the explicit brand aliases
        # so a populated ``brand`` column wins, while a blank brand transparently
        # falls back to the supplier producer value.
        "producer", "producer_name", "producent", "producer_brand", "manufacturer_brand",
    ),
    "model": (
        "model", "modello", "modelo", "model_name", "mpn", "manufacturer_part_number",
        "part_number", "reference_model",
    ),
    "description": (
        "description", "descrizione", "descripcion", "descricao", "long_description",
        "long_desc", "longdesc", "description_long", "description_html",
        "product_description", "full_description", "complete_description", "detailed_description",
        "opis", "opis_produktu",
    ),
    "short_description": (
        "short_description", "short_desc", "shortdesc", "descrizione_breve",
        "descripcion_corta", "resumo", "summary", "bullet_description",
    ),
    "color": ("color", "colour", "colore", "cor", "couleur", "farbe", "kolor"),
    "material": ("material", "materiale", "materiaal", "materiau", "stoff", "material_type"),
    "power_w": (
        "power", "power_w", "wattage", "potenza", "potencia", "puissance", "leistung",
        "moc", "rated_power",
    ),
    "capacity_l": (
        "capacity", "capacity_l", "capacita", "capacidad", "capacidade", "volume",
        "volume_l", "tank_capacity",
    ),
    "temperature_min_c": (
        "temperature_min", "min_temperature", "temperatura_minima", "temperatura_min",
        "minimum_temperature",
    ),
    "temperature_max_c": (
        "temperature_max", "max_temperature", "temperatura_massima", "temperatura_max",
        "maximum_temperature",
    ),
    "length_cm": (
        "length", "length_cm", "package_length", "lunghezza", "comprimento", "longitud",
        "depth", "depth_cm", "profondita",
    ),
    "width_cm": (
        "width", "width_cm", "package_width", "larghezza", "largura", "ancho",
    ),
    "height_cm": (
        "height", "height_cm", "package_height", "altezza", "altura", "wysokosc",
    ),
    "weight_kg": (
        "weight_kg", "weight", "package_weight", "peso", "poids", "gewicht", "waga",
    ),
    "purchase_price": (
        "cost", "purchase_price", "costo", "prezzo_acquisto", "net_price", "wholesale_price",
    ),
    "stock": ("quantity", "stock", "qty", "available_quantity", "availability"),
}

_FIELD_ALIAS_TOKENS: dict[str, tuple[str, ...]] = {
    field: tuple(dict.fromkeys(token for token in (slug(alias) for alias in aliases) if token))
    for field, aliases in FIELD_ALIASES.items()
}

IMAGE_TOKENS = (
    "image", "images", "picture", "photo", "foto", "immagine", "img", "media_url",
    "image_url", "picture_url", "photo_url", "gallery", "large_images", "image_gallery",
)
DOCUMENT_TOKENS = (
    "document", "documents", "manual", "pdf", "datasheet", "scheda_tecnica", "fiche",
    "attachment", "attachments", "attachment_url", "attachment_urls", "technical_documents",
)


def _python_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set, dict)):
        return value
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _record_from_series(series: pd.Series) -> dict[str, Any]:
    """Compatibility helper retained for callers outside the fast dataframe path."""
    return {str(key): _python_value(value) for key, value in series.items()}


def _source_lookup(record: Mapping[str, Any]) -> dict[str, tuple[str, Any]]:
    result: dict[str, tuple[str, Any]] = {}
    for key, value in record.items():
        token = slug(key)
        if token and token not in result:
            result[token] = (str(key), value)
    return result


def _find_source(
    record: Mapping[str, Any],
    aliases: Iterable[str],
    *,
    lookup: Mapping[str, tuple[str, Any]] | None = None,
) -> tuple[str, Any]:
    source_lookup = lookup if lookup is not None else _source_lookup(record)
    for alias in aliases:
        found = source_lookup.get(slug(alias))
        if found and clean_text(found[1]):
            return found
    return "", None


def _candidate_columns(
    token_columns: Mapping[str, Sequence[str]], aliases: Iterable[str]
) -> tuple[str, ...]:
    result: list[str] = []
    for alias in aliases:
        for column in token_columns.get(slug(alias), ()):
            if column not in result:
                result.append(column)
    return tuple(result)


def _normalization_plan(columns: Sequence[str]) -> dict[str, Any]:
    """Compile column matching once instead of rebuilding it for every row."""
    token_columns: dict[str, list[str]] = {}
    clean_columns = [str(column) for column in columns]
    token_by_column: dict[str, str] = {}
    for column in clean_columns:
        token = slug(column)
        token_by_column[column] = token
        if token:
            token_columns.setdefault(token, []).append(column)
    fields: dict[str, tuple[str, ...]] = {
        "ean": _candidate_columns(token_columns, ("ean", "gtin", "barcode", "ean13", "ean_13")),
        "supplier_sku": _candidate_columns(
            token_columns, ("sku", "supplier_sku", "reference", "codice", "product_code")
        ),
        "title": _candidate_columns(token_columns, TITLE_ALIASES),
    }
    for field_name, aliases in FIELD_ALIASES.items():
        fields[field_name] = _candidate_columns(token_columns, aliases)
    return {
        "fields": fields,
        "image_columns": tuple(
            column
            for column in clean_columns
            if any(token in token_by_column[column] for token in IMAGE_TOKENS)
        ),
        "document_columns": tuple(
            column
            for column in clean_columns
            if any(token in token_by_column[column] for token in DOCUMENT_TOKENS)
        ),
    }


def _find_planned_source(
    record: Mapping[str, Any], columns: Sequence[str]
) -> tuple[str, Any]:
    for column in columns:
        value = record.get(column)
        if clean_text(value):
            return str(column), value
    return "", None


def _number_and_unit(value: Any) -> tuple[float | None, str]:
    text = clean_text(value)
    if not text:
        return None, ""
    normalized = text.replace("\u00a0", " ").replace(",", ".")
    match = _NUMBER_RE.search(normalized)
    if not match:
        return None, ""
    try:
        number = float(match.group(0))
    except ValueError:
        return None, ""
    unit_match = _UNIT_RE.search(normalized)
    return number, (unit_match.group(1).lower() if unit_match else "")


def _normalize_measurement(field: str, value: Any, source_field: str = "") -> float | None:
    number, unit = _number_and_unit(value)
    if number is None:
        return None
    source_token = slug(source_field)
    if field == "weight_kg":
        if unit == "g" or (not unit and _GRAM_SOURCE_RE.search(source_token)):
            return number / 1000.0
        if unit == "mg":
            return number / 1_000_000.0
        return number
    if field in {"length_cm", "width_cm", "height_cm"}:
        if unit == "mm":
            return number / 10.0
        if unit == "m":
            return number * 100.0
        return number
    if field == "power_w":
        return number * 1000.0 if unit == "kw" else number
    if field == "capacity_l":
        if unit == "ml":
            return number / 1000.0
        if unit == "cl":
            return number / 100.0
        if unit == "dl":
            return number / 10.0
        return number
    return number


def _split_urls(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        candidates = [clean_text(item) for item in value]
    else:
        candidates = _URL_SPLIT_RE.split(clean_text(value))
    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        item = clean_text(item)
        if not item:
            continue
        urls = _URL_RE.findall(item)
        for url in urls or [item]:
            url = url.strip().rstrip(".,;)")
            if url.lower().startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                result.append(url)
    return result


def _media(
    record: Mapping[str, Any],
    tokens: tuple[str, ...],
    *,
    planned_columns: Sequence[str] | None = None,
) -> tuple[list[str], list[tuple[str, Any]]]:
    urls: list[str] = []
    seen: set[str] = set()
    evidence: list[tuple[str, Any]] = []
    if planned_columns is None:
        candidates = list(record.items())
    else:
        candidates = [(column, record.get(column)) for column in planned_columns]
    for key, value in candidates:
        if planned_columns is None:
            token = slug(key)
            if not any(part in token for part in tokens):
                continue
        found = _split_urls(value)
        if not found:
            continue
        evidence.append((str(key), value))
        for url in found:
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls, evidence


def _nested_source(
    record: Mapping[str, Any],
    aliases: Iterable[str],
    *,
    containers: tuple[str, ...] = (
        "parameters", "technical_attributes", "technical_data", "attributes", "specifications"
    ),
) -> tuple[str, Any]:
    """Find an exact supplier field inside a nested technical-attribute map.

    IOF full feeds intentionally keep all technical characteristics in the
    ``parameters`` dictionary.  Promoting known fields here improves category
    classification and marketplace mapping while preserving the original value
    and its exact source path as evidence.
    """
    alias_tokens = [slug(alias) for alias in aliases if slug(alias)]
    for container_name in containers:
        # Keep this explicit lookup in the release contract: it protects the
        # Innpro/IdoSell IOF ``parameters`` mapping from future regressions.
        nested = record.get("parameters") if container_name == "parameters" else record.get(container_name)
        if not isinstance(nested, Mapping):
            continue
        lookup = {
            slug(key): (str(key), value)
            for key, value in nested.items()
            if value not in (None, "", [], {})
        }
        for token in alias_tokens:
            found = lookup.get(token)
            if found:
                original_key, value = found
                return f"{container_name}.{original_key}", value
        # Supplier labels often include the unit or a short prefix/suffix
        # (for example ``Rated power [W]``).  A conservative token-boundary
        # match keeps this deterministic and avoids semantic invention.
        for nested_token, (original_key, value) in lookup.items():
            for token in alias_tokens:
                if not token:
                    continue
                if (
                    nested_token == token
                    or nested_token.startswith(f"{token}_")
                    or nested_token.endswith(f"_{token}")
                ):
                    return f"{container_name}.{original_key}", value
    return "", None


def _text_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        parts = [clean_text(item) for item in value if clean_text(item)]
        return " | ".join(dict.fromkeys(parts))
    return clean_text(value)


def _evidence(
    *, canonical_field: str, source_field: str, source_value: Any,
    source_path: str, source_file: str, source_row: int,
) -> Evidence:
    python_value = _python_value(source_value)
    return Evidence(
        canonical_field=canonical_field,
        source_field=source_field,
        source_value=python_value,
        source_path=source_path,
        source_file=source_file,
        source_row=int(source_row),
        source_hash=json_hash({"field": source_field, "value": python_value}),
    )


def normalize_record(
    record: Mapping[str, Any],
    *,
    row_number: int,
    source_path: str = "",
    source_file: str = "",
    plan: Mapping[str, Any] | None = None,
) -> CanonicalProduct:
    raw = {str(key): _python_value(value) for key, value in record.items()}
    normalized: dict[str, Any] = {}
    evidences: list[Evidence] = []
    lookup = None if plan is not None else _source_lookup(raw)
    planned_fields = dict(plan.get("fields") or {}) if plan is not None else {}

    # Build direct and token-boundary indexes only once per product.  Innpro
    # full feeds often expose dozens of technical parameters.  Older versions
    # rebuilt the same slug map for every canonical field, causing millions of
    # repeated Unicode/regex operations on a 7k-product catalogue.
    nested_exact: dict[str, tuple[str, Any]] = {}
    nested_boundary: dict[str, tuple[str, Any]] = {}
    for container_name in (
        "parameters", "technical_attributes", "technical_data", "attributes", "specifications"
    ):
        nested = raw.get(container_name)
        if not isinstance(nested, Mapping):
            continue
        for key, value in nested.items():
            if value in (None, "", [], {}):
                continue
            token = slug(key)
            if not token:
                continue
            entry = (f"{container_name}.{key}", value)
            nested_exact.setdefault(token, entry)
            parts = [part for part in token.split("_") if part]
            # Preserve the old conservative prefix/suffix matching semantics,
            # but pre-index every boundary once instead of scanning all keys for
            # every canonical field.
            for cut in range(1, len(parts)):
                nested_boundary.setdefault("_".join(parts[:cut]), entry)
                nested_boundary.setdefault("_".join(parts[cut:]), entry)

    def find_nested(field: str) -> tuple[str, Any]:
        for token in _FIELD_ALIAS_TOKENS.get(field, ()):
            found = nested_exact.get(token)
            if found is not None:
                return found
        for token in _FIELD_ALIAS_TOKENS.get(field, ()):
            found = nested_boundary.get(token)
            if found is not None:
                return found
        return "", None

    def find(field: str, aliases: Iterable[str]) -> tuple[str, Any]:
        if plan is not None:
            return _find_planned_source(raw, planned_fields.get(field, ()))
        return _find_source(raw, aliases, lookup=lookup)

    def add(field: str, value: Any, source_field: str, source_value: Any) -> None:
        if value is None or value == "" or value == [] or value == {}:
            return
        normalized[field] = value
        evidences.append(
            _evidence(
                canonical_field=field,
                source_field=source_field,
                source_value=source_value,
                source_path=source_path,
                source_file=source_file,
                source_row=row_number,
            )
        )

    ean_field, ean_raw = find("ean", ("ean", "gtin", "barcode", "ean13", "ean_13"))
    sku_field, sku_raw = find(
        "supplier_sku", ("sku", "supplier_sku", "reference", "codice", "product_code")
    )
    title_field, title_raw = find("title", TITLE_ALIASES)
    ean = normalized_ean(ean_raw)
    supplier_sku = clean_text(sku_raw).removesuffix(".0")
    title = clean_text(title_raw)
    if ean:
        add("ean", ean, ean_field, ean_raw)
    if supplier_sku:
        add("supplier_sku", supplier_sku, sku_field, sku_raw)
    if title:
        add("title", title, title_field, title_raw)

    for field in ("brand", "model", "description", "short_description", "color", "material"):
        source_field, source_value = find(field, FIELD_ALIASES[field])
        value = clean_text(source_value)
        if value:
            add(field, value, source_field, source_value)

    for field in (
        "power_w", "capacity_l", "temperature_min_c", "temperature_max_c",
        "length_cm", "width_cm", "height_cm", "weight_kg",
    ):
        source_field, source_value = find(field, FIELD_ALIASES[field])
        value = _normalize_measurement(field, source_value, source_field)
        if value is not None and value >= 0:
            add(field, round(float(value), 6), source_field, source_value)

    # Promote known characteristics from the complete supplier parameter map
    # only when no direct column already supplied the value.  Unknown parameters
    # remain untouched under source_attributes.parameters for later deterministic
    # mapping against the official marketplace taxonomy.
    for field in ("brand", "model", "description", "short_description", "color", "material"):
        if normalized.get(field) not in (None, "", [], {}):
            continue
        source_field, source_value = find_nested(field)
        value = _text_value(source_value)
        if value:
            add(field, value, source_field, source_value)

    for field in (
        "power_w", "capacity_l", "temperature_min_c", "temperature_max_c",
        "length_cm", "width_cm", "height_cm", "weight_kg",
    ):
        if normalized.get(field) not in (None, "", [], {}):
            continue
        source_field, source_value = find_nested(field)
        measurement_input = source_value[0] if isinstance(source_value, (list, tuple)) and source_value else source_value
        value = _normalize_measurement(field, measurement_input, source_field)
        if value is not None and value >= 0:
            add(field, round(float(value), 6), source_field, source_value)

    source_field, source_value = find("purchase_price", FIELD_ALIASES["purchase_price"])
    purchase_price = _normalize_measurement("purchase_price", source_value, source_field)
    if purchase_price is not None and purchase_price >= 0:
        add("purchase_price", round(float(purchase_price), 6), source_field, source_value)

    source_field, source_value = find("stock", FIELD_ALIASES["stock"])
    stock = _normalize_measurement("stock", source_value, source_field)
    if stock is not None and stock >= 0:
        add("stock", int(stock), source_field, source_value)

    images, image_sources = _media(
        raw,
        IMAGE_TOKENS,
        planned_columns=plan.get("image_columns") if plan is not None else None,
    )
    if images:
        normalized["images"] = images
        for source_field, source_value in image_sources:
            evidences.append(
                _evidence(
                    canonical_field="images",
                    source_field=source_field,
                    source_value=source_value,
                    source_path=source_path,
                    source_file=source_file,
                    source_row=row_number,
                )
            )
    documents, document_sources = _media(
        raw,
        DOCUMENT_TOKENS,
        planned_columns=plan.get("document_columns") if plan is not None else None,
    )
    if documents:
        normalized["documents"] = documents
        for source_field, source_value in document_sources:
            evidences.append(
                _evidence(
                    canonical_field="documents",
                    source_field=source_field,
                    source_value=source_value,
                    source_path=source_path,
                    source_file=source_file,
                    source_row=row_number,
                )
            )

    # Preserve every supplier field. Unrecognized source attributes must remain
    # available for category and attribute mappings in later phases.
    normalized["source_attributes"] = raw
    normalized["_provenance"] = [
        {
            "canonical_field": item.canonical_field,
            "source_field": item.source_field,
            "source_path": item.source_path,
            "source_file": item.source_file,
            "source_row": item.source_row,
            "source_hash": item.source_hash,
        }
        for item in evidences
    ]

    essential = (
        "ean", "supplier_sku", "title", "brand", "description", "images",
        "weight_kg", "purchase_price",
    )
    present = sum(1 for field in essential if normalized.get(field) not in (None, "", [], {}))
    completeness = round((present / len(essential)) * 100.0, 2)
    normalized_for_hash = {
        key: value for key, value in normalized.items() if key != "source_attributes"
    }
    content_hash = json_hash({"normalized": normalized_for_hash, "raw": raw})
    source_row_key = (
        json_hash({"ean": ean, "sku": supplier_sku})[:32]
        if ean or supplier_sku
        else stable_row_key(raw, row_number)
    )
    return CanonicalProduct(
        source_row_key=source_row_key,
        source_row_number=int(row_number),
        ean=ean,
        supplier_sku=supplier_sku,
        brand=clean_text(normalized.get("brand")),
        model=clean_text(normalized.get("model")),
        title=title,
        description=clean_text(normalized.get("description")),
        normalized=normalized,
        raw=raw,
        evidence=evidences,
        completeness_score=completeness,
        content_hash=content_hash,
    )


def normalize_dataframe(
    frame: pd.DataFrame,
    *,
    source_path: str = "",
    source_file: str = "",
    progress_callback: ProgressCallback | None = None,
    progress_every: int | None = None,
) -> list[CanonicalProduct]:
    """Normalize a dataframe with a precompiled column plan and fast row iteration."""
    if frame is None or frame.empty:
        _notify_progress(
            progress_callback,
            {"phase": "NORMALIZE", "completed": 0, "total": 0, "phase_percent": 100.0},
        )
        return []

    raw_frame = frame.copy(deep=False)
    canonical = normalize_price_list(frame)
    # Make canonical aliases available only when the supplier did not already
    # provide an equivalent source field. The generic list normalizer uses zero
    # defaults for missing measurements; those defaults must not shadow a real
    # source field such as ``Peso = 4200 g``.
    source_tokens = {slug(column) for column in raw_frame.columns}
    canonical_aliases = {
        "ean": ("ean", "gtin", "barcode", "ean13", "ean_13"),
        "sku": ("sku", "supplier_sku", "reference", "codice", "product_code"),
        "name": ("name", "title", "nome", "product_name", "nazwa"),
        "cost": FIELD_ALIASES["purchase_price"],
        "quantity": FIELD_ALIASES["stock"],
        "weight_kg": FIELD_ALIASES["weight_kg"],
    }
    for column in canonical.columns:
        aliases = canonical_aliases.get(str(column), (str(column),))
        has_source = any(slug(alias) in source_tokens for alias in aliases)
        if column not in raw_frame.columns and not has_source:
            raw_frame[column] = canonical[column]

    columns = [str(value) for value in raw_frame.columns]
    plan = _normalization_plan(columns)
    total = len(raw_frame)
    every = max(1, int(progress_every or max(1, total // 100)))
    products: list[CanonicalProduct] = []
    _notify_progress(
        progress_callback,
        {"phase": "NORMALIZE", "completed": 0, "total": total, "phase_percent": 0.0},
    )
    for position, values in enumerate(raw_frame.itertuples(index=False, name=None), start=1):
        record = {
            column: _python_value(value)
            for column, value in zip(columns, values)
        }
        products.append(
            normalize_record(
                record,
                row_number=position,
                source_path=source_path,
                source_file=source_file,
                plan=plan,
            )
        )
        if position == total or position % every == 0:
            _notify_progress(
                progress_callback,
                {
                    "phase": "NORMALIZE",
                    "completed": position,
                    "total": total,
                    "phase_percent": round((position / total) * 100.0, 2),
                },
            )
    return products



def _is_empty_supplier_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return not clean_text(value)


def _rich_feed_columns(frame: pd.DataFrame) -> set[str]:
    return {slug(column) for column in frame.columns}


def _needs_full_feed_enrichment(frame: pd.DataFrame) -> bool:
    """Return True when a legacy saved view lost rich supplier fields.

    Saved views created before Catalog Intelligence often contain only the offer
    columns (EAN/SKU/name/cost/stock).  Innpro's current IOF full feed already
    contains producer, descriptions, gallery URLs, documents and parameters;
    reusing the old pickle without those fields would make Brand/Description/
    Images appear empty even though the supplier feed is complete.
    """
    if frame is None or frame.empty:
        return False
    tokens = _rich_feed_columns(frame)
    brand_tokens = {slug(v) for v in FIELD_ALIASES["brand"]}
    desc_tokens = {slug(v) for v in FIELD_ALIASES["description"]}
    has_brand_column = bool(tokens & brand_tokens)
    has_desc_column = bool(tokens & desc_tokens)
    has_image_column = any(any(marker in token for marker in IMAGE_TOKENS) for token in tokens)

    def any_populated(candidates: set[str]) -> bool:
        for column in frame.columns:
            if slug(column) not in candidates:
                continue
            series = frame[column]
            for value in series.head(min(len(series), 250)):
                if not _is_empty_supplier_value(value):
                    return True
        return False

    has_brand = has_brand_column and any_populated(brand_tokens)
    has_desc = has_desc_column and any_populated(desc_tokens)
    has_images = False
    if has_image_column:
        for column in frame.columns:
            token = slug(column)
            if not any(marker in token for marker in IMAGE_TOKENS):
                continue
            for value in frame[column].head(min(len(frame), 250)):
                if _split_urls(value):
                    has_images = True
                    break
            if has_images:
                break
    return not (has_brand and has_desc and has_images)


def enrich_saved_view_from_price_list(
    frame: pd.DataFrame,
    *,
    price_list_id: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fill a saved view with the complete current supplier row when needed.

    The selection/order of the saved view is preserved.  Enrichment is matched
    by EAN first and supplier SKU second and only fills missing values.  This
    makes old Innpro views immediately benefit from producer/description/images
    without forcing the user to recreate the view in another page.
    """
    info: dict[str, Any] = {"enriched": False, "matched": 0, "source_path": ""}
    if frame is None or frame.empty or not _needs_full_feed_enrichment(frame):
        return frame, info
    from services.db import row as db_row

    price_list = db_row("SELECT local_path FROM price_lists WHERE id=?", (int(price_list_id),))
    source_path = Path(clean_text((price_list or {}).get("local_path")))
    if not source_path.is_file():
        return frame, info
    try:
        full = read_list(source_path)
    except Exception:
        return frame, info
    if full is None or full.empty:
        return frame, info

    source_columns = [str(column) for column in full.columns]
    source_plan = _normalization_plan(source_columns)
    view_columns = [str(column) for column in frame.columns]
    view_plan = _normalization_plan(view_columns)

    def key_for(record: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[str, str]:
        ean_field, ean_value = _find_planned_source(record, plan["fields"].get("ean", ()))
        sku_field, sku_value = _find_planned_source(record, plan["fields"].get("supplier_sku", ()))
        return normalized_ean(ean_value), clean_text(sku_value).removesuffix(".0").upper()

    by_ean: dict[str, dict[str, Any]] = {}
    by_sku: dict[str, dict[str, Any]] = {}
    for values in full.itertuples(index=False, name=None):
        record = {column: _python_value(value) for column, value in zip(source_columns, values)}
        ean, sku = key_for(record, source_plan)
        if ean and ean not in by_ean:
            by_ean[ean] = record
        if sku and sku not in by_sku:
            by_sku[sku] = record

    enriched = frame.copy()
    # Object dtype is required for dictionaries/lists coming from IOF parameters
    # and galleries. Existing columns keep their original dtype where possible.
    for column in source_columns:
        if column not in enriched.columns:
            enriched[column] = pd.Series([None] * len(enriched), index=enriched.index, dtype=object)

    matched = 0
    for index, values in zip(enriched.index, frame.itertuples(index=False, name=None)):
        record = {column: _python_value(value) for column, value in zip(view_columns, values)}
        ean, sku = key_for(record, view_plan)
        source = by_ean.get(ean) if ean else None
        if source is None and sku:
            source = by_sku.get(sku)
        if source is None:
            continue
        matched += 1
        for column, value in source.items():
            if _is_empty_supplier_value(value):
                continue
            current = enriched.at[index, column]
            if _is_empty_supplier_value(current):
                enriched.at[index, column] = value

    info.update({"enriched": matched > 0, "matched": matched, "source_path": str(source_path)})
    return enriched, info

def dataframe_content_hash(frame: pd.DataFrame) -> str:
    """Preserve the historical content-hash contract used by saved snapshots."""
    if frame is None:
        return json_hash({"columns": [], "rows": []})
    safe = frame.copy()
    safe = safe.where(pd.notna(safe), None)
    records = [
        {str(key): _python_value(value) for key, value in item.items()}
        for item in safe.to_dict(orient="records")
    ]
    return json_hash({"columns": [str(value) for value in safe.columns], "rows": records})


def load_saved_view(path: str | Path) -> pd.DataFrame:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Vista salvata non disponibile: {target}")
    try:
        frame = pd.read_pickle(target)
    except Exception as error:
        raise ValueError(f"La vista salvata non è leggibile: {error}") from error
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("La vista salvata non contiene una tabella prodotti valida.")
    return frame


_PHASE_RANGES: dict[str, tuple[float, float]] = {
    "LOAD": (0.0, 4.0),
    "HASH": (4.0, 8.0),
    "NORMALIZE": (8.0, 58.0),
    "PERSIST": (58.0, 99.0),
    "COMPLETED": (100.0, 100.0),
}


def persist_saved_view(
    *,
    seller_id: int,
    supplier_id: int,
    price_list_id: int,
    saved_view_id: int | None,
    snapshot_path: str,
    metadata: Mapping[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
    force: bool = False,
    batch_size: int = 400,
) -> dict[str, Any]:
    """Normalize and persist a saved view with measurable, cached progress.

    The first run performs batched persistence in one transaction. Reopening the
    same immutable view reuses its verified normalization cache and completes
    immediately unless ``force=True``.
    """
    started = time.perf_counter()
    total_rows = 0
    read_count = 0
    normalized_count = 0
    saved_count = 0

    phase_labels = {
        "LOAD": "Lettura del listino",
        "HASH": "Verifica dello snapshot",
        "NORMALIZE": "Normalizzazione prodotti",
        "PERSIST": "Salvataggio prodotti e provenienza",
        "COMPLETED": "Normalizzazione completata",
    }

    def emit(
        phase: str,
        phase_percent: float,
        *,
        message: str = "",
        cache_hit: bool = False,
    ) -> None:
        start_percent, end_percent = _PHASE_RANGES[phase]
        fraction = max(0.0, min(100.0, float(phase_percent))) / 100.0
        overall = 100.0 if phase == "COMPLETED" else start_percent + (
            end_percent - start_percent
        ) * fraction
        elapsed = max(0.0, time.perf_counter() - started)
        processed = max(read_count, normalized_count, saved_count)
        throughput = (processed / elapsed) if processed and elapsed > 0 else 0.0
        eta = 0.0
        if 0.5 < overall < 100.0 and elapsed > 0:
            eta = max(0.0, elapsed * ((100.0 - overall) / overall))
        _notify_progress(
            progress_callback,
            {
                "phase": phase,
                "phase_label": phase_labels[phase],
                "phase_percent": round(float(phase_percent), 2),
                "overall_percent": round(overall, 2),
                "total_products": int(total_rows),
                "products_read": int(read_count),
                "products_normalized": int(normalized_count),
                "products_saved": int(saved_count),
                "elapsed_seconds": round(elapsed, 3),
                "products_per_second": round(throughput, 2),
                "eta_seconds": round(eta, 2),
                "cache_hit": bool(cache_hit),
                "message": message or phase_labels[phase],
            },
        )

    emit("LOAD", 0.0, message="Apertura della vista salvata...")
    frame = load_saved_view(snapshot_path)
    frame, enrichment = enrich_saved_view_from_price_list(frame, price_list_id=price_list_id)
    total_rows = len(frame)
    enrichment_note = (
        f" · feed completo recuperato per {int(enrichment.get('matched') or 0)} righe"
        if enrichment.get("enriched") else ""
    )
    emit("LOAD", 100.0, message=f"{total_rows} prodotti individuati nel listino{enrichment_note}.")

    emit("HASH", 0.0, message="Calcolo dell'identità dello snapshot...")
    content_hash = dataframe_content_hash(frame)
    source_snapshot_id, created = create_source_snapshot(
        seller_id=seller_id,
        supplier_id=supplier_id,
        price_list_id=price_list_id,
        saved_view_id=saved_view_id,
        source_path=snapshot_path,
        content_hash=content_hash,
        row_count=total_rows,
        columns=[str(value) for value in frame.columns],
        metadata={**dict(metadata or {}), "full_feed_enrichment": enrichment},
    )
    emit("HASH", 100.0, message=f"Snapshot {source_snapshot_id} verificato.")

    if not force:
        cached = source_snapshot_normalization_cache(
            source_snapshot_id,
            engine_version=NORMALIZATION_ENGINE_VERSION,
        )
        if cached is not None:
            read_count = total_rows
            normalized_count = int(cached["normalized_count"])
            saved_count = int(cached["unique_product_count"])
            duration = max(0.0, time.perf_counter() - started)
            emit(
                "COMPLETED",
                100.0,
                cache_hit=True,
                message="Prodotti già normalizzati: riutilizzata la memoria persistente.",
            )
            return {
                "source_snapshot_id": source_snapshot_id,
                "snapshot_created": created,
                "row_count": total_rows,
                "normalized_count": normalized_count,
                "unique_product_count": saved_count,
                "product_ids": list(cached["product_ids"]),
                "content_hash": content_hash,
                "average_completeness": float(cached["average_completeness"]),
                "cache_hit": True,
                "elapsed_seconds": round(duration, 3),
                "products_per_second": round(total_rows / duration, 2) if duration > 0 else 0.0,
            }

    def normalization_progress(event: dict[str, Any]) -> None:
        nonlocal read_count, normalized_count
        current = int(event.get("completed") or 0)
        read_count = min(total_rows, current)
        normalized_count = min(total_rows, current)
        emit(
            "NORMALIZE",
            float(event.get("phase_percent") or 0),
            message=f"Letti e normalizzati {current} prodotti su {total_rows}.",
        )

    products = normalize_dataframe(
        frame,
        source_path=snapshot_path,
        source_file=Path(snapshot_path).name,
        progress_callback=normalization_progress,
    )
    read_count = total_rows
    normalized_count = len(products)
    average_completeness = (
        round(sum(item.completeness_score for item in products) / len(products), 2)
        if products else 0.0
    )
    unique_product_count = len({item.source_row_key for item in products})

    def persistence_progress(event: dict[str, Any]) -> None:
        nonlocal saved_count
        saved_count = int(event.get("completed") or 0)
        total_unique = int(event.get("total") or unique_product_count)
        emit(
            "PERSIST",
            float(event.get("phase_percent") or 0),
            message=f"Salvati {saved_count} prodotti univoci su {total_unique}.",
        )

    emit(
        "PERSIST",
        0.0,
        message=f"Avvio salvataggio di {unique_product_count} prodotti univoci...",
    )
    product_ids = save_canonical_products(
        seller_id=seller_id,
        supplier_id=supplier_id,
        price_list_id=price_list_id,
        source_snapshot_id=source_snapshot_id,
        products=products,
        progress_callback=persistence_progress,
        batch_size=batch_size,
    )
    saved_count = unique_product_count
    duration = max(0.0, time.perf_counter() - started)
    mark_source_snapshot_normalized(
        source_snapshot_id,
        engine_version=NORMALIZATION_ENGINE_VERSION,
        input_row_count=total_rows,
        normalized_count=len(products),
        unique_product_count=unique_product_count,
        average_completeness=average_completeness,
        duration_seconds=duration,
    )
    emit(
        "COMPLETED",
        100.0,
        message=f"{len(products)} prodotti normalizzati e memorizzati.",
    )
    return {
        "source_snapshot_id": source_snapshot_id,
        "snapshot_created": created,
        "row_count": total_rows,
        "normalized_count": len(products),
        "unique_product_count": unique_product_count,
        "product_ids": product_ids,
        "content_hash": content_hash,
        "average_completeness": average_completeness,
        "cache_hit": False,
        "elapsed_seconds": round(duration, 3),
        "products_per_second": round(len(products) / duration, 2) if duration > 0 else 0.0,
    }


__all__ = [
    "FIELD_ALIASES",
    "enrich_saved_view_from_price_list",
    "NORMALIZATION_ENGINE_VERSION",
    "dataframe_content_hash",
    "load_saved_view",
    "normalize_dataframe",
    "normalize_record",
    "persist_saved_view",
]
