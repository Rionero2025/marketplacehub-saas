from datetime import date

import pytest

from marketplace_core.orders import OrderQuery, OrderScope, OrdersCore


def test_order_query_rejects_invalid_ranges():
    with pytest.raises(ValueError):
        OrderQuery(date_from=date(2026, 9, 2), date_to=date(2026, 9, 1))
    with pytest.raises(ValueError):
        OrderQuery(limit=0)
    with pytest.raises(ValueError):
        OrderQuery(offset=-1)


def test_orders_core_dispatches_kaufland_page(monkeypatch):
    import services.kaufland_orders as svc

    calls = []

    def fake_page(seller_id, account_id, environment, **kwargs):
        calls.append((seller_id, account_id, environment, kwargs))
        return {
            "items": [{"id_order": "M1"}],
            "total": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
            "has_more": False,
        }

    monkeypatch.setattr(svc, "saved_orders_page", fake_page)
    result = OrdersCore().page(
        OrderScope(3, 7, "kaufland", "live"),
        OrderQuery(search="m1", limit=50, offset=0),
    )
    assert result.total == 1
    assert result.items[0]["id_order"] == "M1"
    assert calls[0][0:3] == (3, 7, "live")
    assert calls[0][3]["search"] == "m1"
    assert calls[0][3]["limit"] == 50


def test_orders_core_dispatches_worten_cache(monkeypatch):
    import services.cecotec_orders as svc

    calls = []

    def fake_page(seller_id, account_id, marketplace, **kwargs):
        calls.append((seller_id, account_id, marketplace, kwargs))
        return {
            "items": [{"order_id": "83570000-A"}],
            "total": 301,
            "limit": 100,
            "offset": 200,
            "has_more": True,
        }

    monkeypatch.setattr(svc, "cached_orders_page", fake_page)
    result = OrdersCore().page(
        OrderScope(3, 9, "worten"),
        OrderQuery(limit=100, offset=200, suppliers=("Cecotec",)),
    )
    assert result.page_number == 3
    assert result.page_count == 4
    assert result.has_more is True
    assert calls[0][2] == "worten"
    assert calls[0][3]["suppliers"] == ("Cecotec",)
