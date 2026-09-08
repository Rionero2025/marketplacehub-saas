"""Bounded in-memory multipart reader for Seller tracking uploads.

FastAPI's ``UploadFile`` dependency parses the request before the endpoint is
entered and may spool large parts to disk.  Tracking imports authenticate first
and call this reader explicitly, so unauthenticated bodies are never parsed and
accepted file bytes never leave memory.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from marketplace_hub_core.orders.tracking import MAX_FILE_BYTES
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.requests import ClientDisconnect, Request

MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_MULTIPART_BODY_BYTES = MAX_FILE_BYTES + MAX_MULTIPART_OVERHEAD_BYTES
MAX_PART_HEADER_BYTES = 8 * 1024
MAX_MAPPING_BYTES = 8 * 1024
MAX_MANUAL_JSON_BYTES = 8 * 1024
TRACKING_BODY_TIMEOUT_SECONDS = 60
BOUNDARY_PATTERN = re.compile(rb"^[0-9A-Za-z'()+_,./:=?-]{1,70}$")


class TrackingMultipartError(ValueError):
    pass


class TrackingMultipartLimitError(TrackingMultipartError):
    pass


class TrackingMultipartTimeoutError(TrackingMultipartError):
    pass


@dataclass(frozen=True)
class TrackingMultipartUpload:
    file_name: str
    content: bytes
    mapping: str | None


def _decode_parameter(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1")


class _TrackingMultipartCollector:
    def __init__(self, *, require_mapping: bool):
        self.require_mapping = require_mapping
        self.file_name = ""
        self.file_content = bytearray()
        self.mapping_content = bytearray()
        self.file_seen = False
        self.mapping_seen = False
        self.complete = False
        self.part_count = 0
        self.current_kind = ""
        self.current_headers: dict[bytes, bytes] = {}
        self.header_field = bytearray()
        self.header_value = bytearray()
        self.header_bytes = 0

    @property
    def callbacks(self):
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_begin": self.on_header_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

    def _add_header_bytes(self, length: int) -> None:
        self.header_bytes += length
        if self.header_bytes > MAX_PART_HEADER_BYTES:
            raise TrackingMultipartLimitError("Le intestazioni multipart sono troppo grandi.")

    def on_part_begin(self) -> None:
        self.part_count += 1
        expected_parts = 2 if self.require_mapping else 1
        if self.part_count > expected_parts:
            raise TrackingMultipartError("Il caricamento contiene parti inattese.")
        self.current_kind = ""
        self.current_headers = {}
        self.header_bytes = 0

    def on_header_begin(self) -> None:
        self.header_field = bytearray()
        self.header_value = bytearray()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._add_header_bytes(end - start)
        self.header_field.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._add_header_bytes(end - start)
        self.header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        name = bytes(self.header_field).strip().lower()
        if not name or name in self.current_headers:
            raise TrackingMultipartError("Le intestazioni multipart non sono valide.")
        self.current_headers[name] = bytes(self.header_value).strip()

    def on_headers_finished(self) -> None:
        disposition = self.current_headers.get(b"content-disposition")
        if disposition is None or b"content-transfer-encoding" in self.current_headers:
            raise TrackingMultipartError("La parte multipart non è valida.")
        kind, options = parse_options_header(disposition)
        if kind.lower() != b"form-data" or b"name" not in options:
            raise TrackingMultipartError("La parte multipart non è valida.")
        name = options[b"name"]
        filename = options.get(b"filename")
        if name == b"file":
            if self.file_seen or filename is None:
                raise TrackingMultipartError("È richiesto un solo file.")
            decoded_filename = _decode_parameter(filename)
            if (not decoded_filename or len(decoded_filename) > 255
                    or "\x00" in decoded_filename or "/" in decoded_filename
                    or "\\" in decoded_filename):
                raise TrackingMultipartError("Il nome del file non è valido.")
            self.file_seen = True
            self.file_name = decoded_filename
            self.current_kind = "file"
            return
        if name == b"mapping" and self.require_mapping:
            if self.mapping_seen or filename is not None:
                raise TrackingMultipartError("La mappatura multipart non è valida.")
            self.mapping_seen = True
            self.current_kind = "mapping"
            return
        raise TrackingMultipartError("Il caricamento contiene campi inattesi.")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self.current_kind == "file":
            if len(self.file_content) + len(chunk) > MAX_FILE_BYTES:
                raise TrackingMultipartLimitError("Il file supera il limite di 5 MB.")
            self.file_content.extend(chunk)
        elif self.current_kind == "mapping":
            if len(self.mapping_content) + len(chunk) > MAX_MAPPING_BYTES:
                raise TrackingMultipartError("La mappatura colonne è troppo grande.")
            self.mapping_content.extend(chunk)
        else:  # pragma: no cover - parser orders headers before part data
            raise TrackingMultipartError("La parte multipart non è valida.")

    def on_part_end(self) -> None:
        self.current_kind = ""

    def on_end(self) -> None:
        self.complete = True

    def result(self) -> TrackingMultipartUpload:
        if not self.complete:
            raise TrackingMultipartError("Il corpo multipart è incompleto.")
        if not self.file_seen:
            raise TrackingMultipartError("È richiesto un file.")
        if self.require_mapping and not self.mapping_seen:
            raise TrackingMultipartError("È richiesta la mappatura colonne.")
        try:
            mapping = self.mapping_content.decode("utf-8") if self.require_mapping else None
        except UnicodeDecodeError as exc:
            raise TrackingMultipartError("La mappatura colonne non è valida.") from exc
        return TrackingMultipartUpload(
            file_name=self.file_name,
            content=bytes(self.file_content),
            mapping=mapping,
        )


def _multipart_boundary(request: Request) -> bytes:
    content_type = request.headers.get("content-type")
    if content_type is None or len(content_type) > 1_024:
        raise TrackingMultipartError("Content-Type multipart non valido.")
    try:
        media_type, options = parse_options_header(content_type)
    except (ValueError, IndexError, UnicodeError) as exc:
        raise TrackingMultipartError("Content-Type multipart non valido.") from exc
    boundary = options.get(b"boundary")
    if media_type.lower() != b"multipart/form-data" or boundary is None:
        raise TrackingMultipartError("È richiesto multipart/form-data.")
    if BOUNDARY_PATTERN.fullmatch(boundary) is None:
        raise TrackingMultipartError("Boundary multipart non valido.")
    return boundary


def _declared_length(request: Request, maximum: int) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal():
        raise TrackingMultipartError("Content-Length non valido.")
    if len(value) > 20:
        raise TrackingMultipartLimitError("Il caricamento supera il limite consentito.")
    length = int(value)
    if length > maximum:
        raise TrackingMultipartLimitError("Il caricamento supera il limite consentito.")
    return length


async def read_tracking_multipart(
    request: Request, *, require_mapping: bool,
) -> TrackingMultipartUpload:
    """Read one authenticated multipart body without Starlette's form spooler."""
    boundary = _multipart_boundary(request)
    declared_length = _declared_length(request, MAX_MULTIPART_BODY_BYTES)
    collector = _TrackingMultipartCollector(require_mapping=require_mapping)
    parser = MultipartParser(
        boundary, callbacks=collector.callbacks, max_size=MAX_MULTIPART_BODY_BYTES,
    )
    received = 0
    try:
        async with asyncio.timeout(TRACKING_BODY_TIMEOUT_SECONDS):
            async for request_chunk in request.stream():
                received += len(request_chunk)
                if received > MAX_MULTIPART_BODY_BYTES:
                    raise TrackingMultipartLimitError(
                        "Il caricamento supera il limite consentito."
                    )
                # ASGI servers may expose a complete body as one chunk. Small parser
                # slices prevent that input from monopolizing the event loop.
                for offset in range(0, len(request_chunk), 64 * 1024):
                    chunk = request_chunk[offset:offset + 64 * 1024]
                    if parser.write(chunk) != len(chunk):  # pragma: no cover - guarded above
                        raise TrackingMultipartLimitError(
                            "Il caricamento supera il limite consentito."
                        )
                    await asyncio.sleep(0)
            parser.finalize()
    except TimeoutError as exc:
        raise TrackingMultipartTimeoutError(
            "Tempo massimo di caricamento superato. Riprova."
        ) from exc
    except TrackingMultipartError:
        raise
    except (ClientDisconnect, MultipartParseError, ValueError) as exc:
        raise TrackingMultipartError("Il corpo multipart non è valido.") from exc
    if declared_length is not None and received != declared_length:
        raise TrackingMultipartError("Il corpo multipart è incompleto.")
    return collector.result()


async def read_tracking_json(request: Request) -> bytes:
    """Read a small authenticated JSON body without pre-handler deserialization."""
    content_type = request.headers.get("content-type")
    if content_type is None or len(content_type) > 1_024:
        raise TrackingMultipartError("Content-Type JSON non valido.")
    try:
        media_type, _ = parse_options_header(content_type)
    except (ValueError, IndexError, UnicodeError) as exc:
        raise TrackingMultipartError("Content-Type JSON non valido.") from exc
    if media_type.lower() != b"application/json":
        raise TrackingMultipartError("È richiesto application/json.")
    declared_length = _declared_length(request, MAX_MANUAL_JSON_BYTES)
    content = bytearray()
    try:
        async with asyncio.timeout(TRACKING_BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_MANUAL_JSON_BYTES:
                    raise TrackingMultipartLimitError(
                        "Il corpo JSON supera il limite consentito."
                    )
                content.extend(chunk)
    except TimeoutError as exc:
        raise TrackingMultipartTimeoutError(
            "Tempo massimo di caricamento superato. Riprova."
        ) from exc
    except TrackingMultipartError:
        raise
    except ClientDisconnect as exc:
        raise TrackingMultipartError("Il corpo JSON è incompleto.") from exc
    if declared_length is not None and len(content) != declared_length:
        raise TrackingMultipartError("Il corpo JSON è incompleto.")
    if not content:
        raise TrackingMultipartError("Il corpo JSON è vuoto.")
    return bytes(content)
