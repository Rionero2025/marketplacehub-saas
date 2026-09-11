from __future__ import annotations

import gzip
import hashlib
import io
import re
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

ARTIFACT_CHUNK_BYTES = 1024 * 1024
MAX_REMOTE_CATALOG_SOURCE_BYTES = 200 * 1024 * 1024
MAX_STORED_CATALOG_ARTIFACT_BYTES = 64 * 1024 * 1024
COMPRESSION_THRESHOLD_BYTES = 20 * 1024 * 1024


class CatalogArtifactEncodingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EncodedCatalogArtifact:
    content: bytes
    encoding: str
    raw_size: int
    raw_sha256: str

    @property
    def stored_size(self) -> int:
        return len(self.content)


def _copy_and_hash(source: BinaryIO, output: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    while chunk := source.read(ARTIFACT_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_REMOTE_CATALOG_SOURCE_BYTES:
            raise CatalogArtifactEncodingError(
                "Il listino supera il limite di 200 MiB previsto per i feed InnPro."
            )
        digest.update(chunk)
        output.write(chunk)
    if total == 0:
        raise CatalogArtifactEncodingError("Il listino è vuoto.")
    return total, digest.hexdigest()


def encode_catalog_artifact(source: bytes | Path) -> EncodedCatalogArtifact:
    """Create a durable artifact while keeping large source files off the heap.

    Small feeds retain their exact byte representation. Large feeds are gzip
    encoded deterministically; the size and SHA-256 always describe the original
    supplier response so version deduplication is independent of storage format.
    """

    if isinstance(source, bytes):
        raw_size = len(source)
        if raw_size == 0:
            raise CatalogArtifactEncodingError("Il listino è vuoto.")
        if raw_size > MAX_REMOTE_CATALOG_SOURCE_BYTES:
            raise CatalogArtifactEncodingError(
                "Il listino supera il limite di 200 MiB previsto per i feed InnPro."
            )
        raw_sha256 = hashlib.sha256(source).hexdigest()
        if raw_size <= COMPRESSION_THRESHOLD_BYTES:
            return EncodedCatalogArtifact(source, "identity", raw_size, raw_sha256)
        input_stream: BinaryIO = io.BytesIO(source)
    else:
        try:
            raw_size = source.stat().st_size
        except OSError as exc:
            raise CatalogArtifactEncodingError(
                "Il file temporaneo del listino non è leggibile."
            ) from exc
        if raw_size <= 0:
            raise CatalogArtifactEncodingError("Il listino è vuoto.")
        if raw_size > MAX_REMOTE_CATALOG_SOURCE_BYTES:
            raise CatalogArtifactEncodingError(
                "Il listino supera il limite di 200 MiB previsto per i feed InnPro."
            )
        input_stream = source.open("rb")
        if raw_size <= COMPRESSION_THRESHOLD_BYTES:
            try:
                content = input_stream.read(COMPRESSION_THRESHOLD_BYTES + 1)
            finally:
                input_stream.close()
            if len(content) != raw_size:
                raise CatalogArtifactEncodingError("Il file temporaneo del listino è incompleto.")
            return EncodedCatalogArtifact(
                content, "identity", raw_size, hashlib.sha256(content).hexdigest(),
            )

    compressed = tempfile.TemporaryFile(mode="w+b", prefix="marketplace-hub-artifact-")
    try:
        with input_stream:
            with gzip.GzipFile(
                filename="", fileobj=compressed, mode="wb", compresslevel=6, mtime=0,
            ) as output:
                measured_size, raw_sha256 = _copy_and_hash(input_stream, output)
        if measured_size != raw_size:
            raise CatalogArtifactEncodingError("Il file temporaneo del listino è incompleto.")
        stored_size = compressed.tell()
        if stored_size > MAX_STORED_CATALOG_ARTIFACT_BYTES:
            raise CatalogArtifactEncodingError(
                "Il listino compresso supera il limite di archiviazione consentito."
            )
        compressed.seek(0)
        content = compressed.read(MAX_STORED_CATALOG_ARTIFACT_BYTES + 1)
        if len(content) != stored_size:
            raise CatalogArtifactEncodingError("Il listino compresso è incompleto.")
        return EncodedCatalogArtifact(content, "gzip", raw_size, raw_sha256)
    finally:
        compressed.close()


def decode_catalog_artifact(content: bytes, encoding: str) -> bytes:
    if encoding == "identity":
        return content
    if encoding != "gzip":
        raise CatalogArtifactEncodingError("Codifica del listino non supportata.")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content), mode="rb") as source:
            output = source.read(MAX_REMOTE_CATALOG_SOURCE_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise CatalogArtifactEncodingError("Il listino archiviato non è leggibile.") from exc
    if len(output) > MAX_REMOTE_CATALOG_SOURCE_BYTES:
        raise CatalogArtifactEncodingError("Il listino archiviato supera il limite consentito.")
    return output


def verify_encoded_catalog_artifact(artifact: EncodedCatalogArtifact) -> None:
    """Verify stored bytes against raw metadata without materializing the source."""

    if artifact.encoding not in {"identity", "gzip"}:
        raise CatalogArtifactEncodingError("Codifica del listino non supportata.")
    if artifact.raw_size <= 0 or artifact.raw_size > MAX_REMOTE_CATALOG_SOURCE_BYTES:
        raise CatalogArtifactEncodingError("La dimensione originale del listino non è valida.")
    if not re.fullmatch(r"[0-9a-f]{64}", artifact.raw_sha256):
        raise CatalogArtifactEncodingError("L'impronta del listino non è valida.")
    if not artifact.content or artifact.stored_size > MAX_STORED_CATALOG_ARTIFACT_BYTES:
        raise CatalogArtifactEncodingError("La dimensione archiviata del listino non è valida.")

    if artifact.encoding == "identity":
        if (
            artifact.stored_size != artifact.raw_size
            or hashlib.sha256(artifact.content).hexdigest() != artifact.raw_sha256
        ):
            raise CatalogArtifactEncodingError(
                "Il listino non corrisponde ai metadati originali."
            )
        return

    digest = hashlib.sha256()
    measured_size = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(artifact.content), mode="rb") as source:
            while chunk := source.read(ARTIFACT_CHUNK_BYTES):
                measured_size += len(chunk)
                if measured_size > MAX_REMOTE_CATALOG_SOURCE_BYTES:
                    raise CatalogArtifactEncodingError(
                        "Il listino archiviato supera il limite consentito."
                    )
                digest.update(chunk)
    except CatalogArtifactEncodingError:
        raise
    except (OSError, EOFError, zlib.error) as exc:
        raise CatalogArtifactEncodingError("Il listino compresso non è leggibile.") from exc
    if measured_size != artifact.raw_size or digest.hexdigest() != artifact.raw_sha256:
        raise CatalogArtifactEncodingError("Il listino non corrisponde ai metadati originali.")


__all__ = [
    "CatalogArtifactEncodingError",
    "EncodedCatalogArtifact",
    "MAX_REMOTE_CATALOG_SOURCE_BYTES",
    "MAX_STORED_CATALOG_ARTIFACT_BYTES",
    "decode_catalog_artifact",
    "encode_catalog_artifact",
    "verify_encoded_catalog_artifact",
]
