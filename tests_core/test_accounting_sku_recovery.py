"""Preserve publication-time SKU costs from the original Streamlit service."""
from contextlib import contextmanager
import json
import sqlite3

import pytest

from services import accounting, db, shared_cache

SKU = "Cecotec_8435484015312_63.59_80.00"


@pytest.mark.parametrize("stored,payload,expected", [
    ("8435484015312", {"line": {"offer_sku": SKU}}, SKU),
    ("Cecotec_8435484015312_unknown_80", {"offer_sku": SKU}, SKU),
    ("Cecotec_8435484015312_42_80", {"offer_sku": SKU}, "Cecotec_8435484015312_42_80"),
    ("8435484015312", '{broken-json', "8435484015312"),
    ("8435484015312", {"offer_id": "opaque-id", "sku": "product-only"}, "8435484015312"),
    ("8435484015312", {"items": [{"Offer-SKU": SKU}]}, SKU),
    ("8435484015312", json.dumps({"line": {"shop_sku": SKU}}), SKU),
    ("", None, ""),
])
def test_composite_sku_recovery_precedence(stored, payload, expected):
    assert accounting._best_composite_sku(stored, payload) == expected


def normalized_line():
    return accounting._normalize_worten_line(
        {"order_id": "TEST-ORDER", "created_date": "2026-08-10T10:00:00Z",
         "order_state": "SHIPPED", "shipping_address": {"country_code": "PT"}},
        {"order_line_id": "1", "offer_id": "opaque-id", "quantity": 2,
         "price": 100, "commission_fee": 10, "metadata": {"offer_sku": SKU}},
        account_id=1, catalogs=[], fx_rates={}, index=0,
    )


def test_worten_normalization_recovers_nested_offer_cost():
    item = normalized_line()
    assert item["composite_sku"] == SKU
    assert item["purchase_cost_eur"] == pytest.approx(127.18)


@pytest.mark.parametrize("line,expected", [
    ({"metadata": {"offer_sku": SKU}}, SKU),
    ({"offer_id": "opaque-id"}, "product-only"),
])
def test_cached_order_never_borrows_another_lines_sku(line, expected):
    payload = {"order": {"order_lines": [{"offer_sku": "Other_999_1_2"}]}, "line": line}
    assert accounting._best_composite_sku("product-only", payload) == expected


@pytest.fixture
def accounting_database(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.row_factory = lambda cursor, values: dict(zip((col[0] for col in cursor.description), values))
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE marketplace_accounts (id INTEGER PRIMARY KEY);
        CREATE TABLE price_lists (id INTEGER PRIMARY KEY);
        INSERT INTO sellers VALUES(1);
        INSERT INTO marketplace_accounts VALUES(1);
    """)

    @contextmanager
    def connect():
        with con:
            yield con

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(accounting, "connect", connect)
    monkeypatch.setenv("MARKETPLACE_HUB_DB_ENGINE", "sqlite")
    monkeypatch.setattr(shared_cache, "_BACKEND", shared_cache.LocalSharedCache())
    monkeypatch.setattr(accounting, "load_supplier_catalogs", lambda seller_id: [])
    monkeypatch.setattr(accounting, "accounting_catalog_selection", lambda seller_id: {"configured": False})
    accounting.ensure_schema()
    try:
        yield con
    finally:
        con.close()


@pytest.mark.parametrize("manual", [False, True])
def test_refresh_persists_recovered_sku_and_preserves_manual_cost(accounting_database, manual):
    item = normalized_line()
    item.update(composite_sku="8435484015312", supplier="", purchase_cost_eur=None)
    accounting.upsert_accounting_rows(1, 1, "worten", [item])
    if manual:
        saved = accounting.save_accounting_inline_edits([{
            "marketplace_account_id": 1, "marketplace": "worten", "row_key": item["row_key"],
            "fields": {"purchase_cost_eur": 40.0},
        }])
        assert saved["updated_rows"] == 1
    stats = accounting.refresh_accounting_costs(1, 1, "worten")
    assert stats["examined"] == 1
    first = accounting.accounting_rows(1, 1, "worten")[0]
    assert first["composite_sku"] == SKU
    assert first["purchase_cost_eur"] == pytest.approx(40 if manual else 127.18)
    if manual:
        assert first["cost_source"] == "Modifica manuale persistente"
        assert accounting.computed_values(first)["gross_margin_eur"] == pytest.approx(
            first["payout_eur"] - 40
        )
    accounting.refresh_accounting_costs(1, 1, "worten")
    second = accounting.accounting_rows(1, 1, "worten")[0]
    assert second["purchase_cost_eur"] == first["purchase_cost_eur"]
    assert second["composite_sku"] == first["composite_sku"]
