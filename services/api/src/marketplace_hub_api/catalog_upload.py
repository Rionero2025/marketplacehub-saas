from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Request
from marketplace_hub_core.catalogs.schema import MAX_CATALOG_ARTIFACT_BYTES
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

MAX_MULTIPART_OVERHEAD = 64 * 1024
MAX_CATALOG_MULTIPART_BYTES = MAX_CATALOG_ARTIFACT_BYTES + MAX_MULTIPART_OVERHEAD
MAX_TEXT_FIELD_BYTES = 4 * 1024


class CatalogMultipartError(ValueError):
    pass


class CatalogMultipartLimitError(CatalogMultipartError):
    pass


class _BodyLimitExceeded(MultiPartException):
    pass


@dataclass(frozen=True)
class CatalogUpload:
    supplier_id: UUID
    name: str
    provider: str
    feed_role: str
    file_name: str
    media_type: str
    content: bytes


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise CatalogMultipartError("La dimensione della richiesta non è valida.") from exc
    if value < 0:
        raise CatalogMultipartError("La dimensione della richiesta non è valida.")
    return value


async def read_catalog_multipart(request: Request) -> CatalogUpload:
    content_type = request.headers.get("content-type", "")
    if not content_type.casefold().startswith("multipart/form-data"):
        raise CatalogMultipartError("Invia fornitore, nome e file come multipart/form-data.")
    length = _content_length(request)
    if length is not None and length > MAX_CATALOG_MULTIPART_BYTES:
        raise CatalogMultipartLimitError("Il file supera il limite di 20 MiB.")

    async def bounded_stream():
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_CATALOG_MULTIPART_BYTES:
                raise _BodyLimitExceeded("body limit")
            yield chunk

    parser = MultiPartParser(
        request.headers,
        bounded_stream(),
        max_files=1,
        max_fields=4,
        max_part_size=MAX_TEXT_FIELD_BYTES,
    )
    form = None
    try:
        try:
            form = await parser.parse()
        except _BodyLimitExceeded as exc:
            raise CatalogMultipartLimitError("Il file supera il limite di 20 MiB.") from exc
        except MultiPartException as exc:
            raise CatalogMultipartError("La richiesta multipart non è valida.") from exc

        values: dict[str, object] = {}
        allowed = {"supplier_id", "name", "provider", "feed_role", "file"}
        for key, value in form.multi_items():
            if key not in allowed or key in values:
                raise CatalogMultipartError("La richiesta multipart non è valida.")
            values[key] = value
        if not {"supplier_id", "name", "file"}.issubset(values):
            raise CatalogMultipartError("Indica fornitore, nome del listino e file.")
        if any(
            isinstance(values[key], UploadFile)
            for key in ("supplier_id", "name", "provider", "feed_role")
            if key in values
        ):
            raise CatalogMultipartError("I campi del listino non sono validi.")
        file = values["file"]
        if not isinstance(file, UploadFile):
            raise CatalogMultipartError("Il file del listino non è valido.")
        try:
            supplier_id = UUID(str(values["supplier_id"]))
        except (TypeError, ValueError) as exc:
            raise CatalogMultipartError("Il fornitore indicato non è valido.") from exc
        name = str(values["name"])
        if file.size is not None and file.size > MAX_CATALOG_ARTIFACT_BYTES:
            raise CatalogMultipartLimitError("Il file supera il limite di 20 MiB.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CATALOG_ARTIFACT_BYTES:
                raise CatalogMultipartLimitError("Il file supera il limite di 20 MiB.")
            chunks.append(chunk)
        return CatalogUpload(
            supplier_id=supplier_id,
            name=name,
            provider=str(values.get("provider", "generic")),
            feed_role=str(values.get("feed_role", "standard")),
            file_name=file.filename or "",
            media_type=file.content_type or "application/octet-stream",
            content=b"".join(chunks),
        )
    finally:
        if form is not None:
            await form.close()
