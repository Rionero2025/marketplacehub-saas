from datetime import date

import services.dashboard as dashboard


def test_dashboard_snapshot_uses_one_accounting_read(monkeypatch):
    calls = []
    sellers = [
        {
            "id": 3,
            "name": "Fintrade",
            "legal_name": "",
            "our_profit_pct": 35,
            "partner_profit_pct": 65,
        }
    ]
    accounting = [
        {
            "seller_id": 3,
            "marketplace_account_id": 7,
            "marketplace": "kaufland",
            "row_key": "r1",
            "order_id": "M1",
            "order_created": "2026-08-31T10:00:00+00:00",
            "country_code": "IT",
            "market_label": "IT",
            "raw_status": "sent",
            "status_label": "Spedito",
            "supplier": "Innpro",
            "composite_sku": "innpro_123_10_15",
            "product_title": "Prodotto",
            "ean": "1234567890123",
            "quantity": 1,
            "sale_eur": 20.0,
            "purchase_cost_eur": 10.0,
            "commission_eur": 2.0,
            "refund_eur": 0.0,
            "payout_eur": 18.0,
            "extra_cost_eur": 0.0,
            "note": "",
            "supplier_order_number": "",
            "synced_at": "2026-08-31T10:05:00+00:00",
        }
    ]

    monkeypatch.setattr(dashboard, "sellers", lambda: sellers)

    def fake_rows(sql, params=()):
        calls.append((sql, params))
        return accounting

    monkeypatch.setattr(dashboard, "rows", fake_rows)
    monkeypatch.setattr(dashboard, "apply_accounting_manual_overrides", lambda rows: list(rows))

    snap = dashboard.dashboard_snapshot(
        today=date(2026, 8, 31),
        selected_from=date(2026, 8, 31),
        selected_to=date(2026, 8, 31),
    )
    assert len(calls) == 1
    assert snap["rows_loaded"] == 1
    assert snap["summaries"][0]["periods"]["selected"]["sales"] == 20.0
    assert len(snap["detail_rows"]) == 1
    assert "raw_json" not in calls[0][0]
