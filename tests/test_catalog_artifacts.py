from __future__ import annotations

import hashlib

import pytest
from marketplace_hub_core.catalogs import artifacts
from marketplace_hub_core.catalogs.artifacts import (
    CatalogArtifactEncodingError,
    EncodedCatalogArtifact,
    decode_catalog_artifact,
    encode_catalog_artifact,
    verify_encoded_catalog_artifact,
)


def test_small_artifact_retains_original_bytes_and_identity_metadata():
    raw = b"sku;cost\nA;12.34\n"

    encoded = encode_catalog_artifact(raw)

    assert encoded.content == raw
    assert encoded.encoding == "identity"
    assert encoded.raw_size == len(raw)
    assert encoded.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert encoded.stored_size == len(raw)
    assert decode_catalog_artifact(encoded.content, encoded.encoding) == raw


def test_large_path_is_compressed_deterministically_and_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "COMPRESSION_THRESHOLD_BYTES", 32)
    raw = (b"<product id='123'><description>caffe</description></product>" * 100)
    source = tmp_path / "full.xml"
    source.write_bytes(raw)

    first = encode_catalog_artifact(source)
    second = encode_catalog_artifact(source)

    assert first.encoding == "gzip"
    assert first.content == second.content
    assert first.raw_size == len(raw)
    assert first.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert first.stored_size < first.raw_size
    assert decode_catalog_artifact(first.content, first.encoding) == raw


def test_source_and_stored_limits_fail_closed(tmp_path, monkeypatch):
    source = tmp_path / "too-large.xml"
    source.write_bytes(b"x" * 65)
    monkeypatch.setattr(artifacts, "MAX_REMOTE_CATALOG_SOURCE_BYTES", 64)
    with pytest.raises(CatalogArtifactEncodingError, match="200 MiB"):
        encode_catalog_artifact(source)

    monkeypatch.setattr(artifacts, "MAX_REMOTE_CATALOG_SOURCE_BYTES", 1_000)
    monkeypatch.setattr(artifacts, "COMPRESSION_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(artifacts, "MAX_STORED_CATALOG_ARTIFACT_BYTES", 8)
    with pytest.raises(CatalogArtifactEncodingError, match="archiviazione"):
        encode_catalog_artifact(b"0123456789" * 5)


@pytest.mark.parametrize("encoding", ["brotli", "", "GZIP"])
def test_unknown_storage_encoding_is_rejected(encoding):
    with pytest.raises(CatalogArtifactEncodingError, match="non supportata"):
        decode_catalog_artifact(b"payload", encoding)


def test_corrupted_gzip_is_rejected_without_exposing_payload():
    with pytest.raises(CatalogArtifactEncodingError, match="non è leggibile"):
        decode_catalog_artifact(b"not-gzip", "gzip")


def test_invalid_deflate_stream_is_wrapped_as_catalog_error():
    invalid = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\xff\xff\xff\xff"
    with pytest.raises(CatalogArtifactEncodingError, match="non è leggibile"):
        decode_catalog_artifact(invalid, "gzip")


def test_encoded_artifact_verification_streams_and_checks_raw_metadata(monkeypatch):
    monkeypatch.setattr(artifacts, "COMPRESSION_THRESHOLD_BYTES", 1)
    raw = b"catalog-row" * 100
    encoded = encode_catalog_artifact(raw)
    verify_encoded_catalog_artifact(encoded)

    corrupt = EncodedCatalogArtifact(
        content=encoded.content,
        encoding="gzip",
        raw_size=encoded.raw_size,
        raw_sha256="0" * 64,
    )
    with pytest.raises(CatalogArtifactEncodingError, match="non corrisponde"):
        verify_encoded_catalog_artifact(corrupt)
