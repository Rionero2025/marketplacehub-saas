from __future__ import annotations

import hashlib
from decimal import Decimal
from uuid import UUID

import pytest
import test_tenancy_api
from marketplace_hub_core.catalogs import artifacts
from marketplace_hub_core.catalogs.artifacts import CatalogArtifactEncodingError
from marketplace_hub_core.catalogs.repository import SqlCatalogsRepository
from marketplace_hub_core.catalogs.schema import (
    seller_price_list_versions,
    seller_price_lists,
)
from sqlalchemy import select

workspace = test_tenancy_api.workspace


def _product(source_row: int, *, sku: str = "SKU-1") -> dict:
    return {
        "source_row": source_row,
        "ean": f"805000000{source_row:04d}",
        "sku": sku,
        "name": f"Prodotto {sku}",
        "cost": Decimal("10"),
        "shipping_cost": Decimal("1"),
        "total_cost": Decimal("11"),
        "quantity": Decimal("3"),
        "canonical_json": f'{{"sku":"{sku}"}}',
    }


def _scope(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    return organization_id, seller_id


def test_upload_repository_compresses_and_roundtrips_large_source(workspace, monkeypatch):
    monkeypatch.setattr(artifacts, "COMPRESSION_THRESHOLD_BYTES", 32)
    repository = SqlCatalogsRepository(workspace.engine)
    organization_id, seller_id = _scope(workspace)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    raw = b"ean,sku,cost\n" + b"8050000000001,SKU-1,10\n" * 20
    list_id = repository.add_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="InnPro FULL",
        provider="innpro",
        feed_role="full",
        original_filename="full.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=raw,
        normalized_products=[_product(1)],
    )

    with workspace.engine.connect() as connection:
        saved = connection.execute(select(seller_price_lists).where(
            seller_price_lists.c.id == list_id,
        )).mappings().one()
        version = connection.execute(select(seller_price_list_versions).where(
            seller_price_list_versions.c.price_list_id == list_id,
        )).mappings().one()
    assert saved["artifact_encoding"] == "gzip"
    assert saved["artifact_size"] == len(raw)
    assert saved["artifact_stored_size"] == len(saved["artifact_bytes"])
    assert saved["artifact_stored_size"] < saved["artifact_size"]
    assert saved["artifact_sha256"] == hashlib.sha256(raw).hexdigest()
    assert version["artifact_encoding"] == "gzip"
    assert repository.raw_artifact(organization_id, seller_id, list_id) == raw

    public = repository.dashboard(organization_id, seller_id)["price_lists"][0]
    assert public["artifact_encoding"] == "gzip"
    assert public["artifact_size"] == len(raw)
    assert public["artifact_stored_size"] == saved["artifact_stored_size"]
    metadata = repository.version_metadata(organization_id, seller_id, list_id)[0]
    assert metadata["artifact_encoding"] == "gzip"
    assert metadata["artifact_stored_size"] == saved["artifact_stored_size"]
    assert "artifact_bytes" not in public and "artifact_bytes" not in metadata


def test_remote_activation_accepts_preencoded_metadata_and_reports_raw_progress(
    workspace, monkeypatch,
):
    monkeypatch.setattr(artifacts, "COMPRESSION_THRESHOLD_BYTES", 32)
    repository = SqlCatalogsRepository(workspace.engine)
    organization_id, seller_id = _scope(workspace)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    created = repository.create_url_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="InnPro LIGHT",
        provider="innpro",
        feed_role="light",
        source_config_encrypted="opaque",
        source_host="feed.example.test",
        requested_by=workspace.user().id,
        realm="seller",
    )
    job_id = UUID(created["job_id"])
    raw = b"<offer>" + b"<product id='1'/>" * 50 + b"</offer>"
    encoded = artifacts.encode_catalog_artifact(raw)
    assert encoded.encoding == "gzip"
    assert repository.claim_job(organization_id, seller_id, job_id)

    result = repository.activate_remote_version(
        job_id,
        expected_config_revision=1,
        original_filename="light.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=encoded.content,
        artifact_encoding=encoded.encoding,
        artifact_size=encoded.raw_size,
        artifact_sha256=encoded.raw_sha256,
        normalized_products=[_product(1, sku="LIGHT")],
    )
    assert result == {"duplicate": False, "version_number": 1}
    assert repository.raw_artifact(
        organization_id, seller_id, UUID(created["price_list_id"]),
    ) == raw
    job = repository.job(organization_id, seller_id, job_id)
    assert job["processed_bytes"] == len(raw)
    assert job["total_bytes"] == len(raw)


def test_repository_rejects_partial_or_false_identity_metadata(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    organization_id, seller_id = _scope(workspace)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="Fornitore", notes="",
    )
    common = {
        "name": "Non valido",
        "original_filename": "bad.csv",
        "media_type": "text/csv",
        "file_format": "csv",
        "artifact": b"abc",
        "normalized_products": [_product(1)],
    }
    with pytest.raises(CatalogArtifactEncodingError, match="completi"):
        repository.add_price_list(
            organization_id,
            seller_id,
            supplier_id,
            artifact_encoding="identity",
            **common,
        )
    with pytest.raises(CatalogArtifactEncodingError, match="non corrisponde"):
        repository.add_price_list(
            organization_id,
            seller_id,
            supplier_id,
            artifact_encoding="identity",
            artifact_size=4,
            artifact_sha256=hashlib.sha256(b"abc").hexdigest(),
            **common,
        )


def test_repository_rejects_corrupted_preencoded_gzip(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    organization_id, seller_id = _scope(workspace)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    invalid = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\xff\xff\xff\xff"
    with pytest.raises(CatalogArtifactEncodingError, match="non è leggibile"):
        repository.add_price_list(
            organization_id,
            seller_id,
            supplier_id,
            name="Gzip corrotto",
            provider="innpro",
            feed_role="full",
            original_filename="full.xml",
            media_type="application/xml",
            file_format="iof",
            artifact=invalid,
            artifact_encoding="gzip",
            artifact_size=100,
            artifact_sha256="0" * 64,
            normalized_products=[_product(1)],
        )


def test_raw_artifact_detects_corrupted_compressed_payload(workspace, monkeypatch):
    monkeypatch.setattr(artifacts, "COMPRESSION_THRESHOLD_BYTES", 1)
    repository = SqlCatalogsRepository(workspace.engine)
    organization_id, seller_id = _scope(workspace)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    list_id = repository.add_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="Corrotto",
        provider="innpro",
        feed_role="light",
        original_filename="light.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer><product /></offer>",
        normalized_products=[_product(1)],
    )
    with workspace.engine.begin() as connection:
        stored_size = connection.scalar(select(
            seller_price_lists.c.artifact_stored_size,
        ).where(seller_price_lists.c.id == list_id))
        connection.execute(seller_price_lists.update().where(
            seller_price_lists.c.id == list_id,
        ).values(artifact_bytes=b"\x1f\x8b" + b"\x00" * (stored_size - 2)))

    with pytest.raises(CatalogArtifactEncodingError, match="non è leggibile"):
        repository.raw_artifact(organization_id, seller_id, list_id)
