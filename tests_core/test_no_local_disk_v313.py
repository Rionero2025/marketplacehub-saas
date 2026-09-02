from __future__ import annotations

from pathlib import Path

from services import object_storage
from services.durable_files import materialize, put_bytes, read_bytes


def test_durable_file_roundtrip_and_materialization(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_HUB_STORAGE_BACKEND", "local")
    monkeypatch.setenv("MARKETPLACE_HUB_STORAGE_LOCAL_ROOT", str(tmp_path / "store"))
    monkeypatch.setenv("MARKETPLACE_HUB_STORAGE_PREFIX", "v313-test")
    object_storage._reset_storage_for_tests()
    payload = b"marketplace-hub-v313-no-local-disk"
    stored = put_bytes(
        namespace="price_lists", identity="list_33", filename="catalog.csv",
        content=payload, content_type="text/csv",
    )
    assert read_bytes(storage_key=stored["storage_key"], expected_sha256=stored["sha256"]) == payload
    local = materialize(
        namespace="price_lists", identity="list_33", filename="catalog.csv",
        storage_key=stored["storage_key"], expected_sha256=stored["sha256"],
    )
    assert local.read_bytes() == payload
    object_storage._reset_storage_for_tests()


def test_price_lists_are_storage_backed():
    root = Path(__file__).resolve().parents[1]
    source = (root / "services" / "lists.py").read_text(encoding="utf-8")
    assert "storage_key" in source
    assert "persist_price_list_path" in source
    assert "materialize_price_list" in source


def test_tracking_archives_leave_payload_out_of_database():
    root = Path(__file__).resolve().parents[1]
    source = (root / "services" / "order_tracking.py").read_text(encoding="utf-8")
    assert 'stored["storage_key"]' in source
    assert 'stored["storage_backend"]' in source
    assert 'stored["sha256"]' in source
    assert 'b"",' in source


def test_accounting_and_publication_artifacts_use_storage():
    root = Path(__file__).resolve().parents[1]
    accounting = (root / "services" / "accounting.py").read_text(encoding="utf-8")
    publication = (root / "services" / "catalog_intelligence" / "publication.py").read_text(encoding="utf-8")
    page = (root / "pages" / "3_Creazione_Prodotti.py").read_text(encoding="utf-8")
    assert "accounting_export_bytes" in accounting
    assert 'namespace="accounting_exports"' in accounting
    assert 'namespace="publication_artifacts"' in publication
    assert "publication_artifact_bytes" in page


def test_supplier_documents_are_archived_before_analysis():
    root = Path(__file__).resolve().parents[1]
    page = (root / "pages" / "4_Contabilita.py").read_text(encoding="utf-8")
    storage = (root / "services" / "supplier_document_storage.py").read_text(encoding="utf-8")
    assert "archive_supplier_documents" in page
    assert "supplier_document_files" in storage
    assert 'namespace="supplier_documents"' in storage
