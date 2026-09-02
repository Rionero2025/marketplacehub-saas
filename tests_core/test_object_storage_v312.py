from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from services import object_storage


def test_local_object_storage_roundtrip(tmp_path: Path):
    store = object_storage.LocalObjectStorage(tmp_path, "mh-test")
    payload = b"marketplace-hub-v312"
    key = "saved_views/seller_3/view_9/test.pkl"
    assert store.put_bytes(key, payload) == key
    assert store.exists(key)
    assert store.get_bytes(key) == payload
    store.delete(key)
    assert not store.exists(key)


def test_storage_config_defaults_to_local(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_HUB_STORAGE_BACKEND", "local")
    monkeypatch.setenv("MARKETPLACE_HUB_STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("MARKETPLACE_HUB_STORAGE_PREFIX", "test")
    object_storage._reset_storage_for_tests()
    status = object_storage.storage_status()
    assert status["backend"] == "local"
    assert status["ready"] is True
    object_storage.object_store().put_bytes("a/b.bin", b"123")
    assert object_storage.object_store().get_bytes("a/b.bin") == b"123"
    object_storage._reset_storage_for_tests()


def test_saved_view_page_uses_storage_layer():
    source = (Path(__file__).resolve().parents[1] / "pages" / "3_Lavora_sui_Listini.py").read_text(encoding="utf-8")
    assert "save_saved_view_frame(" in source
    assert "load_saved_view_frame(" in source
    assert "Migra viste legacy" in source


def test_publication_pages_use_saved_view_loader():
    root = Path(__file__).resolve().parents[1]
    for name in ("3_Pubblicazione_Kaufland.py", "3_Pubblicazione_Worten.py"):
        source = (root / "pages" / name).read_text(encoding="utf-8")
        assert "load_saved_view_frame" in source
