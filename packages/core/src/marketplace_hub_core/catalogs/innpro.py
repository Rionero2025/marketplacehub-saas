"""Streaming parser for InnPro/IdoSell IOF FULL and LIGHT catalog feeds.

FULL carries product content while LIGHT carries authoritative wholesale
prices and stock. The role is derived from XML evidence, never from a name.
"""

from __future__ import annotations

import io
import json
import os
import pickle
import re
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO, Literal

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

FeedRole = Literal["full", "light"]

MAX_INNPRO_ARTIFACT_BYTES = 200 * 1024 * 1024
MAX_INNPRO_PRODUCTS = 200_000
MAX_INNPRO_ROWS = 200_000
MAX_INNPRO_XML_ELEMENTS = 2_000_000
MAX_INNPRO_XML_DEPTH = 64
MAX_INNPRO_ELEMENTS_PER_PRODUCT = 100_000
MAX_INNPRO_FIELD_CHARS = 2 * 1024 * 1024
MAX_INNPRO_URLS_PER_PRODUCT = 2_000
MAX_INNPRO_PARAMETERS_PER_PRODUCT = 5_000
INNPRO_ROW_SPOOL_THRESHOLD_BYTES = 8 * 1024 * 1024
MAX_IN_MEMORY_INNPRO_ROWS = 1_000
MAX_IN_MEMORY_INNPRO_NORMALIZED_BYTES = 8 * 1024 * 1024
MAX_INNPRO_NORMALIZED_BYTES = 256 * 1024 * 1024
MAX_INNPRO_VARIANTS_PER_PRODUCT = 500


class InnproParseError(ValueError):
    """Base error for invalid or unsafe InnPro feeds."""


class InnproValidationError(InnproParseError):
    """The input is not a valid, unambiguous InnPro IOF feed."""


class InnproLimitError(InnproParseError):
    """The input exceeds a configured resource limit."""


@dataclass(frozen=True, slots=True)
class InnproParseResult:
    file_format: str
    feed_role: FeedRole
    rows: Sequence[dict[str, Any]]
    product_count: int
    metadata: dict[str, str]

    @property
    def rows_spooled_to_disk(self) -> bool:
        return isinstance(self.rows, _DiskBackedInnproRows)

    def close(self) -> None:
        close = getattr(self.rows, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> InnproParseResult:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _DiskBackedInnproRows(Sequence[dict[str, Any]]):
    """Replay parsed rows from a private temporary file.

    The pickle stream is never accepted from a caller: it is written and read
    through the same private handle from already validated XML values.  Keeping
    the uncanonicalized rows on disk avoids retaining both the full source model
    and its (potentially larger) canonical JSON representation in worker memory.
    """

    def __init__(
        self,
        stream: BinaryIO,
        offsets: list[int],
        role: FeedRole,
        *,
        canonicalized: bool,
    ) -> None:
        self._stream: BinaryIO | None = stream
        self._offsets = tuple(offsets)
        self._role = role
        self._canonicalized = canonicalized
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._offsets)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for offset in self._offsets:
            with self._lock:
                stream = self._require_stream()
                stream.seek(offset)
                row = pickle.load(stream)
            yield row if self._canonicalized else _canonicalize_row(row, self._role)

    def __getitem__(self, index: int | slice) -> dict[str, Any] | list[dict[str, Any]]:
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        with self._lock:
            stream = self._require_stream()
            stream.seek(self._offsets[index])
            row = pickle.load(stream)
            return row if self._canonicalized else _canonicalize_row(row, self._role)

    def _require_stream(self) -> BinaryIO:
        if self._stream is None:
            raise ValueError("Le righe temporanee del listino sono state chiuse.")
        return self._stream

    def close(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                stream.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class _InnproRowCollector:
    def __init__(self, *, use_disk: bool, provisional_role: FeedRole | None) -> None:
        self._rows: list[dict[str, Any]] | None = [] if not use_disk else None
        self._stream: BinaryIO | None = (
            tempfile.TemporaryFile(mode="w+b", prefix="marketplace-hub-innpro-")
            if use_disk else None
        )
        self._offsets: list[int] = []
        self._count = 0
        self._normalized_bytes = 0
        self._provisional_role = provisional_role

    def __len__(self) -> int:
        return self._count

    def _promote_to_disk(self) -> None:
        if self._rows is None:
            return
        stream = tempfile.TemporaryFile(mode="w+b", prefix="marketplace-hub-innpro-")
        try:
            for row in self._rows:
                self._offsets.append(stream.tell())
                pickle.dump(row, stream, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            stream.close()
            raise
        self._rows = None
        self._stream = stream

    def append(self, row: dict[str, Any]) -> None:
        if self._provisional_role is not None:
            row = _canonicalize_row(row, self._provisional_role)
            canonical_json = row["canonical_json"]
        else:
            canonical_json = _canonical_json(row, "light")
        normalized_bytes = len(canonical_json.encode("utf-8"))
        if self._normalized_bytes + normalized_bytes > MAX_INNPRO_NORMALIZED_BYTES:
            raise InnproLimitError(
                "Il feed InnPro genera troppi dati prodotto normalizzati."
            )
        if self._rows is not None and (
            self._count >= MAX_IN_MEMORY_INNPRO_ROWS
            or self._normalized_bytes + normalized_bytes
            > MAX_IN_MEMORY_INNPRO_NORMALIZED_BYTES
        ):
            self._promote_to_disk()
        if self._rows is not None:
            self._rows.append(row)
        else:
            stream = self._stream
            if stream is None:
                raise RuntimeError("Raccolta temporanea InnPro non disponibile.")
            self._offsets.append(stream.tell())
            pickle.dump(row, stream, protocol=pickle.HIGHEST_PROTOCOL)
        self._count += 1
        self._normalized_bytes += normalized_bytes

    def finish(self, role: FeedRole) -> Sequence[dict[str, Any]]:
        if self._rows is not None:
            rows, self._rows = self._rows, None
            if self._provisional_role is None:
                _canonicalize(rows, role)
            return rows
        stream, self._stream = self._stream, None
        if stream is None:
            raise RuntimeError("Raccolta temporanea InnPro non disponibile.")
        stream.flush()
        return _DiskBackedInnproRows(
            stream,
            self._offsets,
            role,
            canonicalized=self._provisional_role is not None,
        )

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()


def _source_size(
    source: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO,
) -> int | None:
    if isinstance(source, bytes | bytearray | memoryview):
        return len(source)
    if isinstance(source, str | os.PathLike):
        try:
            return Path(source).stat().st_size
        except OSError:
            return None
    try:
        position = source.tell()
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(position)
        return size
    except (AttributeError, OSError, TypeError, ValueError):
        return None


_FULL_CHILDREN = {
    "attachments",
    "card",
    "category",
    "description",
    "images",
    "parameters",
    "producer",
    "series",
    "unit",
    "warranty",
}
_LIGHT_CHILDREN = {
    "price",
    "sizes",
    "srp",
    "strikethrough_retail_price",
    "strikethrough_wholesale_price",
}
_LANGUAGE_PRIORITY = ("en", "it", "de", "pt", "es", "fr", "pl")
_LANGUAGE_ALIASES = {
    "eng": "en", "en": "en", "en-us": "en", "en-gb": "en",
    "ita": "it", "it": "it", "it-it": "it",
    "deu": "de", "ger": "de", "de": "de", "de-de": "de", "de-at": "de",
    "pol": "pl", "pl": "pl", "pl-pl": "pl",
    "por": "pt", "pt": "pt", "pt-pt": "pt", "pt-br": "pt",
    "spa": "es", "es": "es", "es-es": "es",
    "fra": "fr", "fre": "fr", "fr": "fr", "fr-fr": "fr",
    "nld": "nl", "dut": "nl", "nl": "nl", "nl-nl": "nl",
    "ces": "cs", "cze": "cs", "cs": "cs", "cs-cz": "cs",
    "slk": "sk", "slo": "sk", "sk": "sk", "sk-sk": "sk",
    "hun": "hu", "hu": "hu", "hu-hu": "hu",
    "ron": "ro", "rum": "ro", "ro": "ro", "ro-ro": "ro",
    "bul": "bg", "bg": "bg", "bg-bg": "bg",
    "hrv": "hr", "hr": "hr", "hr-hr": "hr",
    "slv": "sl", "sl": "sl", "sl-si": "sl",
    "dan": "da", "da": "da", "da-dk": "da",
    "swe": "sv", "sv": "sv", "sv-se": "sv",
    "fin": "fi", "fi": "fi", "fi-fi": "fi",
    "ell": "el", "gre": "el", "el": "el", "el-gr": "el",
    "est": "et", "et": "et", "et-ee": "et",
    "lav": "lv", "lv": "lv", "lv-lv": "lv",
    "lit": "lt", "lt": "lt", "lt-lt": "lt",
}


class _LimitedReader:
    def __init__(self, raw: BinaryIO, maximum_bytes: int) -> None:
        self.raw = raw
        self.maximum_bytes = maximum_bytes
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.maximum_bytes - self.bytes_read
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        chunk = self.raw.read(requested)
        if not isinstance(chunk, bytes):
            raise InnproValidationError(
                "Il feed InnPro deve essere aperto in modalità binaria."
            )
        self.bytes_read += len(chunk)
        if self.bytes_read > self.maximum_bytes:
            raise InnproLimitError("Il feed InnPro supera il limite consentito.")
        return chunk


@contextmanager
def _open_binary_source(
    source: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO,
    maximum_bytes: int,
) -> Iterator[_LimitedReader]:
    if maximum_bytes <= 0:
        raise InnproLimitError("Il limite del feed InnPro non è valido.")

    owned: BinaryIO | None = None
    supplied: BinaryIO | None = None
    original_position: int | None = None
    try:
        if isinstance(source, bytes | bytearray | memoryview):
            payload = bytes(source)
            if len(payload) > maximum_bytes:
                raise InnproLimitError("Il feed InnPro supera il limite consentito.")
            owned = io.BytesIO(payload)
            raw = owned
        elif isinstance(source, str | os.PathLike):
            try:
                path = Path(source)
                size = path.stat().st_size
                if size > maximum_bytes:
                    raise InnproLimitError(
                        "Il feed InnPro supera il limite consentito."
                    )
                owned = path.open("rb")
                raw = owned
            except InnproLimitError:
                raise
            except OSError as exc:
                raise InnproValidationError(
                    "Il feed InnPro non può essere aperto."
                ) from exc
        else:
            supplied = source
            if not all(hasattr(supplied, method) for method in ("read", "seek", "tell")):
                raise InnproValidationError(
                    "La sorgente InnPro deve essere un file binario seekable."
                )
            try:
                if hasattr(supplied, "seekable") and not supplied.seekable():
                    raise InnproValidationError(
                        "La sorgente InnPro deve essere un file binario seekable."
                    )
                original_position = supplied.tell()
                supplied.seek(0, os.SEEK_END)
                size = supplied.tell()
                supplied.seek(0)
            except InnproValidationError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise InnproValidationError(
                    "La sorgente InnPro deve essere un file binario seekable."
                ) from exc
            if size > maximum_bytes:
                raise InnproLimitError("Il feed InnPro supera il limite consentito.")
            raw = supplied
        yield _LimitedReader(raw, maximum_bytes)
    finally:
        if owned is not None:
            owned.close()
        elif supplied is not None and original_position is not None:
            try:
                supplied.seek(original_position)
            except (OSError, ValueError):
                pass


def _local_name(value: Any) -> str:
    tag = getattr(value, "tag", value)
    return str(tag or "").rsplit("}", 1)[-1].split(":")[-1]


def _attr(element: Any, name: str, default: str = "") -> str:
    for key, value in element.attrib.items():
        if _local_name(key) == name:
            return _checked_text(value)
    return default


def _checked_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_INNPRO_FIELD_CHARS:
        raise InnproLimitError("Il feed InnPro contiene un valore troppo lungo.")
    return text


def _element_text(element: Any | None) -> str:
    if element is None:
        return ""
    parts: list[str] = []
    length = 0
    for piece in element.itertext():
        length += len(piece)
        if length > MAX_INNPRO_FIELD_CHARS:
            raise InnproLimitError("Il feed InnPro contiene un valore troppo lungo.")
        parts.append(piece)
    return "".join(parts).strip()


def _children(element: Any | None, name: str) -> list[Any]:
    if element is None:
        return []
    return [child for child in list(element) if _local_name(child) == name]


def _child(element: Any | None, name: str) -> Any | None:
    matches = _children(element, name)
    return matches[0] if matches else None


def _language(value: Any) -> str:
    token = _checked_text(value).casefold().replace("_", "-")
    if not token:
        return ""
    if token in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[token]
    first = token.split("-", 1)[0]
    if first in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[first]
    return first if re.fullmatch(r"[a-z]{2,3}", first) else ""


def _localized(parent: Any | None, child_name: str) -> tuple[str, dict[str, str]]:
    ordered: list[tuple[str, str]] = []
    localized: dict[str, str] = {}
    for node in _children(parent, child_name):
        value = _element_text(node)
        if not value:
            continue
        language = _language(_attr(node, "lang"))
        ordered.append((language, value))
        if language and language not in localized:
            localized[language] = value
    if not ordered:
        return "", localized
    ranks = {language: index for index, language in enumerate(_LANGUAGE_PRIORITY)}
    chosen = min(
        enumerate(ordered),
        key=lambda item: (ranks.get(item[1][0], len(ranks) + item[0]), item[0]),
    )[1][1]
    return chosen, localized


def _ordered_urls(parent: Any | None, node_name: str) -> list[str]:
    if parent is None:
        return []
    found: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, node in enumerate(parent.iter()):
        if _local_name(node) != node_name:
            continue
        url = _attr(node, "url")
        if not url or url in seen:
            continue
        if len(found) >= MAX_INNPRO_URLS_PER_PRODUCT:
            raise InnproLimitError("Un prodotto InnPro contiene troppi URL.")
        seen.add(url)
        try:
            priority = int(float(_attr(node, "priority")))
        except ValueError:
            priority = 10_000 + order
        found.append((priority, order, url))
    found.sort(key=lambda item: (item[0], item[1]))
    return [url for _, _, url in found]


def _images(product: Any) -> tuple[list[str], list[str], list[dict[str, str]]]:
    container = _child(product, "images")
    image_urls = _ordered_urls(container, "image")
    icon_urls: list[str] = []
    for node_name in ("icon", "auction_icon", "group_icon"):
        for url in _ordered_urls(container, node_name):
            if url not in icon_urls:
                icon_urls.append(url)
    metadata: list[dict[str, str]] = []
    if container is not None:
        for node in container.iter():
            if _local_name(node) == "image" and _attr(node, "url"):
                if len(metadata) >= MAX_INNPRO_URLS_PER_PRODUCT:
                    raise InnproLimitError("Un prodotto InnPro contiene troppe immagini.")
                metadata.append({
                    "url": _attr(node, "url"),
                    "priority": _attr(node, "priority"),
                    "width": _attr(node, "width"),
                    "height": _attr(node, "height"),
                    "hash": _attr(node, "hash"),
                    "date_changed": _attr(node, "date_changed"),
                })
    return image_urls, icon_urls, metadata


def _attachments(product: Any) -> tuple[list[str], list[dict[str, Any]]]:
    container = _child(product, "attachments")
    urls = _ordered_urls(container, "file")
    metadata: list[dict[str, Any]] = []
    if container is not None:
        for node in container.iter():
            if _local_name(node) != "file" or not _attr(node, "url"):
                continue
            if len(metadata) >= MAX_INNPRO_URLS_PER_PRODUCT:
                raise InnproLimitError("Un prodotto InnPro contiene troppi allegati.")
            name, name_i18n = _localized(node, "name")
            metadata.append({
                "url": _attr(node, "url"),
                "name": name,
                "name_i18n": name_i18n,
                "type": _attr(node, "attachment_file_type"),
                "extension": _attr(node, "attachment_file_extension"),
                "version": _attr(node, "version"),
                "priority": _attr(node, "priority"),
            })
    return urls, metadata


def _parameters(product: Any) -> dict[str, str | list[str]]:
    container = _child(product, "parameters")
    result: dict[str, str | list[str]] = {}
    count = 0
    for parameter in _children(container, "parameter"):
        if _attr(parameter, "type", "parameter").casefold() == "section":
            continue
        count += 1
        if count > MAX_INNPRO_PARAMETERS_PER_PRODUCT:
            raise InnproLimitError("Un prodotto InnPro contiene troppi parametri.")
        parameter_id = _attr(parameter, "id")
        name = _attr(parameter, "name") or _localized(parameter, "name")[0]
        name = name or (f"parameter_{parameter_id}" if parameter_id else "parameter")
        values: list[str] = []
        for value_node in _children(parameter, "value"):
            value = (
                _attr(value_node, "name")
                or _localized(value_node, "name")[0]
                or _element_text(value_node)
            )
            if value and value not in values:
                values.append(value)
        direct = _attr(parameter, "value")
        if not values and direct:
            values.append(direct)
        if not values:
            continue
        previous = result.get(name)
        combined = (
            list(previous)
            if isinstance(previous, list)
            else ([previous] if previous else [])
        )
        for value in values:
            if value not in combined:
                combined.append(value)
        result[name] = combined[0] if len(combined) == 1 else combined
    return result


def _decimal(value: Any) -> Decimal:
    text = _checked_text(value).replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return Decimal("0")
    try:
        result = Decimal(text)
    except InvalidOperation:
        return Decimal("0")
    if not result.is_finite() or abs(result) >= Decimal("1e30"):
        return Decimal("0")
    return result


def _quantity(stocks: list[Any]) -> int:
    quantities: list[int] = []
    for stock in stocks:
        raw = (
            _attr(stock, "available_stock_quantity")
            or _attr(stock, "quantity")
            or _attr(stock, "stock_quantity")
            or "0"
        )
        try:
            quantities.append(max(0, int(float(raw.replace(",", ".")))))
        except ValueError:
            continue
    return sum(quantities)


def _gtin(*values: Any) -> str:
    for value in values:
        match = re.fullmatch(r"(\d{8}|\d{12,14})(?:\.0+)?", _checked_text(value))
        if match:
            return match.group(1)
    return ""


def _weight(value: Any) -> Decimal:
    result = _decimal(value)
    return max(Decimal("0"), result)


def _product_rows(
    product: Any,
    *,
    products_attributes: dict[str, str],
    source_row_start: int,
) -> Iterator[dict[str, Any]]:
    product_id = _attr(product, "id")
    producer = _child(product, "producer")
    category = _child(product, "category")
    unit = _child(product, "unit")
    series = _child(product, "series")
    warranty = _child(product, "warranty")
    description = _child(product, "description")

    name, title_i18n = _localized(description, "name")
    long_description, description_i18n = _localized(description, "long_desc")
    short_description, short_description_i18n = _localized(description, "short_desc")
    version = _child(description, "version")
    version_name, version_i18n = _localized(version, "name")
    if not version_name and version is not None:
        version_name = _attr(version, "name")

    card = _child(product, "card")
    image_urls, icon_urls, image_metadata = _images(product)
    document_urls, attachment_metadata = _attachments(product)
    parameters = _parameters(product)
    direct_price = _child(product, "price")
    direct_srp = _child(product, "srp")
    strike_retail = _child(product, "strikethrough_retail_price")
    strike_wholesale = _child(product, "strikethrough_wholesale_price")
    product_cost = _attr(direct_price, "net") if direct_price is not None else ""
    product_srp = _attr(direct_srp, "net") if direct_srp is not None else ""
    sizes = _children(_child(product, "sizes"), "size") or [product]

    producer_name = _attr(producer, "name") if producer is not None else ""
    product_url = _attr(card, "url") if card is not None else ""
    base: dict[str, Any] = {
        "product_id": product_id,
        "name": name,
        "title": name,
        "title_i18n": title_i18n,
        "description": long_description or short_description,
        "long_description": long_description,
        "description_i18n": description_i18n,
        "short_description": short_description,
        "short_description_i18n": short_description_i18n,
        "version_name": version_name,
        "version_i18n": version_i18n,
        "producer": producer_name,
        "brand": producer_name,
        "producer_id": _attr(producer, "id") if producer is not None else "",
        "category": _attr(category, "name") if category is not None else "",
        "category_id": _attr(category, "id") if category is not None else "",
        "unit": _attr(unit, "name") if unit is not None else "",
        "series": _attr(series, "name") if series is not None else "",
        "warranty": _attr(warranty, "name") if warranty is not None else "",
        "product_url": product_url,
        "card_url": product_url,
        "images": image_urls,
        "image_urls": image_urls,
        "image_url": image_urls[0] if image_urls else "",
        "image_count": len(image_urls),
        "image_metadata": image_metadata,
        "icon_urls": icon_urls,
        "documents": document_urls,
        "document_urls": document_urls,
        "attachment_urls": document_urls,
        "attachment_metadata": attachment_metadata,
        "parameters": parameters,
        "currency": _attr(product, "currency") or products_attributes.get("currency", ""),
        "vat": _attr(product, "vat"),
        "product_type": _attr(product, "type"),
        "code_on_card": _attr(product, "code_on_card"),
        "producer_code_standard": _attr(product, "producer_code_standard"),
        "strikethrough_retail_price": (
            _attr(strike_retail, "net") if strike_retail is not None else ""
        ),
        "strikethrough_wholesale_price": (
            _attr(strike_wholesale, "net") if strike_wholesale is not None else ""
        ),
    }

    for offset, size in enumerate(sizes):
        size_price = _child(size, "price")
        size_srp = _child(size, "srp")
        raw_cost = _attr(size_price, "net") if size_price is not None else product_cost
        raw_srp = _attr(size_srp, "net") if size_srp is not None else product_srp
        cost = _decimal(raw_cost)
        srp = _decimal(raw_srp)
        code_producer = _attr(size, "code_producer")
        code_external = _attr(size, "code_external")
        size_code = _attr(size, "code")
        ean = _gtin(code_producer, code_external, size_code) or code_external
        sku = code_producer or size_code or product_id
        weight_g = _weight(_attr(size, "weight") or _attr(size, "weight_net"))
        quantity = _quantity(_children(size, "stock"))
        source = dict(base)
        source.update({
            "sku": sku,
            "ean": ean,
            "size_id": _attr(size, "id"),
            "size_text_id": _attr(size, "text_id"),
            "size_panel_name": _attr(size, "panel_name"),
            "code_producer": code_producer,
            "code_external": code_external,
            "size_code": size_code,
            "cost": format(cost, "f"),
            "srp": format(srp, "f"),
            "quantity": quantity,
            "weight_g": format(weight_g, "f"),
            "weight_kg": format(weight_g / Decimal("1000"), "f"),
            "variant": _attr(size, "name") if size is not product else version_name,
        })
        yield {
            "source_row": source_row_start + offset,
            "ean": ean,
            "sku": sku,
            "name": name,
            "cost": cost,
            "shipping_cost": Decimal("0"),
            "total_cost": cost,
            "quantity": Decimal(quantity),
            "_source": source,
        }


def _canonical_json(row: dict[str, Any], role: FeedRole) -> str:
    source = row["_source"]
    canonical = {
        "ean": row["ean"],
        "sku": row["sku"],
        "name": row["name"],
        "cost": format(row["cost"], "f"),
        "shipping_cost": format(row["shipping_cost"], "f"),
        "total_cost": format(row["total_cost"], "f"),
        "quantity": format(row["quantity"], "f"),
        "feed_role": role,
        "source": source,
    }
    return json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _canonicalize_row(row: dict[str, Any], role: FeedRole) -> dict[str, Any]:
    row["canonical_json"] = _canonical_json(row, role)
    row.pop("_source")
    return row


def _canonicalize(rows: list[dict[str, Any]], role: FeedRole) -> None:
    for row in rows:
        _canonicalize_row(row, role)


def parse_innpro_iof(
    source: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO,
    *,
    expected_role: FeedRole | None = None,
    maximum_bytes: int = MAX_INNPRO_ARTIFACT_BYTES,
    maximum_products: int = MAX_INNPRO_PRODUCTS,
    maximum_rows: int = MAX_INNPRO_ROWS,
    maximum_elements: int = MAX_INNPRO_XML_ELEMENTS,
    maximum_depth: int = MAX_INNPRO_XML_DEPTH,
    maximum_product_elements: int = MAX_INNPRO_ELEMENTS_PER_PRODUCT,
    maximum_variants_per_product: int = MAX_INNPRO_VARIANTS_PER_PRODUCT,
) -> InnproParseResult:
    """Parse IOF from bytes, a path, or a seekable binary file."""
    if expected_role not in (None, "full", "light"):
        raise InnproValidationError("Il ruolo atteso deve essere FULL o LIGHT.")
    limits = (
        maximum_products,
        maximum_rows,
        maximum_elements,
        maximum_depth,
        maximum_product_elements,
        maximum_variants_per_product,
    )
    if any(limit <= 0 for limit in limits):
        raise InnproLimitError("I limiti del parser InnPro non sono validi.")

    source_size = _source_size(source)
    row_collector = _InnproRowCollector(
        use_disk=(
            source_size is None
            or source_size > INNPRO_ROW_SPOOL_THRESHOLD_BYTES
        ),
        provisional_role=expected_role,
    )
    rows_transferred = False
    stack: list[Any] = []
    product_count = 0
    element_count = 0
    current_product_elements = 0
    root_seen = False
    products_seen = False
    full_evidence = False
    light_evidence = False
    ambiguous_evidence = False
    products_attributes: dict[str, str] = {}
    metadata: dict[str, str] = {}

    try:
        with _open_binary_source(source, maximum_bytes) as reader:
            events = SafeElementTree.iterparse(
                reader,
                events=("start", "end"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
            for event, element in events:
                name = _local_name(element)
                if event == "start":
                    stack.append(element)
                    element_count += 1
                    if current_product_elements:
                        current_product_elements += 1
                    elif name == "product":
                        parent = stack[-2] if len(stack) > 1 else None
                        if parent is not None and _local_name(parent) == "products":
                            current_product_elements = 1
                    if current_product_elements > maximum_product_elements:
                        raise InnproLimitError(
                            "Un prodotto InnPro contiene troppi elementi XML."
                        )
                    if element_count > maximum_elements:
                        raise InnproLimitError(
                            "Il feed InnPro contiene troppi elementi XML."
                        )
                    if len(stack) > maximum_depth:
                        raise InnproLimitError(
                            "Il feed InnPro supera la profondità XML consentita."
                        )
                    if not root_seen:
                        root_seen = True
                        is_iof = _attr(element, "file_format").casefold() == "iof"
                        if name != "offer" or not is_iof:
                            raise InnproValidationError(
                                "Il file non è un feed InnPro/IdoSell IOF."
                            )
                        metadata = {
                            key: _attr(element, key)
                            for key in ("version", "generated_by", "generated", "expires")
                            if _attr(element, key)
                        }
                    elif name == "products" and not products_seen:
                        products_seen = True
                        products_attributes = {
                            key: _attr(element, key)
                            for key in ("currency", "language")
                            if _attr(element, key)
                        }
                        metadata.update({
                            f"products_{key}": value
                            for key, value in products_attributes.items()
                        })
                    continue

                parent = stack[-2] if len(stack) > 1 else None
                direct_product = (
                    name == "product"
                    and parent is not None
                    and _local_name(parent) == "products"
                )
                if direct_product:
                    product_count += 1
                    if product_count > maximum_products:
                        raise InnproLimitError(
                            "Il feed InnPro contiene troppi prodotti."
                        )
                    child_names = {_local_name(child) for child in list(element)}
                    if child_names & _FULL_CHILDREN:
                        full_evidence = True
                    else:
                        unknown = child_names - _LIGHT_CHILDREN
                        sizes = _children(_child(element, "sizes"), "size")
                        direct_price = _child(element, "price") is not None
                        size_price = any(_child(size, "price") is not None for size in sizes)
                        if sizes and (direct_price or size_price) and not unknown:
                            light_evidence = True
                        else:
                            ambiguous_evidence = True

                    parsed_rows = _product_rows(
                        element,
                        products_attributes=products_attributes,
                        source_row_start=len(row_collector) + 2,
                    )
                    variant_count = 0
                    for parsed_row in parsed_rows:
                        variant_count += 1
                        if variant_count > maximum_variants_per_product:
                            raise InnproLimitError(
                                "Un prodotto InnPro contiene troppe varianti."
                            )
                        if len(row_collector) >= maximum_rows:
                            raise InnproLimitError(
                                "Il feed InnPro contiene troppe varianti."
                            )
                        row_collector.append(parsed_row)
                    parent.remove(element)
                    element.clear()
                    current_product_elements = 0
                elif not current_product_elements:
                    # iterparse otherwise retains every closed non-product node
                    # below the root. Metadata needed later was copied on each
                    # container's start event, so release unrelated subtrees as
                    # soon as they close. Product descendants stay intact until
                    # _product_rows consumes the complete direct product tree.
                    if parent is not None:
                        parent.remove(element)
                    element.clear()

                if not stack or stack[-1] is not element:
                    raise InnproValidationError("La struttura XML InnPro non è valida.")
                stack.pop()
        if not root_seen or not products_seen or not len(row_collector):
            raise InnproValidationError(
                "Il feed InnPro non contiene prodotti leggibili."
            )
        if full_evidence:
            detected_role: FeedRole = "full"
        elif light_evidence and not ambiguous_evidence:
            detected_role = "light"
        else:
            raise InnproValidationError(
                "Il feed IOF è ambiguo: non è possibile distinguere FULL e LIGHT."
            )
        if expected_role is not None and expected_role != detected_role:
            raise InnproValidationError(
                f"Il feed è {detected_role.upper()}, ma era atteso {expected_role.upper()}."
            )

        rows = row_collector.finish(detected_role)
        rows_transferred = True
        return InnproParseResult(
            file_format="iof",
            feed_role=detected_role,
            rows=rows,
            product_count=product_count,
            metadata=metadata,
        )
    except InnproParseError:
        raise
    except (
        DefusedXmlException,
        SafeElementTree.ParseError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise InnproValidationError("Il feed XML InnPro non è valido.") from exc
    finally:
        if not rows_transferred:
            row_collector.close()


__all__ = [
    "FeedRole",
    "InnproLimitError",
    "InnproParseError",
    "InnproParseResult",
    "InnproValidationError",
    "MAX_INNPRO_ARTIFACT_BYTES",
    "parse_innpro_iof",
]
