from __future__ import annotations

from datetime import date

from services.product_stats import (
    aggregate_dimension,
    aggregate_product_orders,
    aggregate_product_stats,
    filter_product_rows,
    merge_previous_period,
    previous_period_range,
    product_identity,
    product_rows,
    product_totals,
    sort_product_stats,
)


def row(
    *,
    seller_id: int = 1,
    seller_name: str = "Ginevra",
    marketplace: str = "Kaufland",
    country: str = "DE",
    supplier: str = "Innpro",
    order_id: str = "O-1",
    row_key: str = "R-1",
    title: str = "Prodotto Test",
    ean: str = "5900000000001",
    sku: str = "innpro_5900000000001_10_20",
    quantity: int = 1,
    sale: float = 100.0,
    purchase: float | None = 60.0,
    commission: float = 10.0,
    margin: float | None = 30.0,
    order_date: date = date(2026, 8, 23),
):
    payout = None if sale is None else sale - commission
    return {
        "seller_id": seller_id,
        "seller_name": seller_name,
        "marketplace_account_id": seller_id * 10,
        "marketplace": marketplace,
        "country_code": country,
        "supplier": supplier,
        "order_id": order_id,
        "row_key": row_key,
        "order_date": order_date,
        "product_title": title,
        "ean": ean,
        "composite_sku": sku,
        "quantity": quantity,
        "sale_eur": sale,
        "purchase_cost_eur": purchase,
        "commission_eur": commission,
        "refund_eur": 0.0,
        "payout_eur": payout,
        "extra_cost_eur": 0.0,
        "net_revenue_eur": margin,
        "our_share_eur": None if margin is None else margin * 0.35,
        "partner_share_eur": None if margin is None else margin * 0.65,
    }


def test_v265_product_identity_prefers_ean_then_sku_then_title():
    assert product_identity(row()) == "ean:5900000000001"
    without_ean = row(ean="")
    assert product_identity(without_ean).startswith("sku:innpro_")
    without_sku = row(ean="", sku="", title="  Nome   Prodotto ")
    assert product_identity(without_sku) == "title:nome prodotto"


def test_v265_top_products_sum_quantity_not_order_rows():
    rows = [
        row(quantity=3, sale=300, purchase=180, commission=30, margin=90, order_id="A"),
        row(seller_id=2, seller_name="Bebol", quantity=2, sale=200, purchase=120, commission=20, margin=60, order_id="B"),
    ]
    stats = aggregate_product_stats(rows)
    assert len(stats) == 1
    product = stats[0]
    assert product["quantity"] == 5
    assert product["orders"] == 2
    assert product["sales_eur"] == 500
    assert product["margin_eur"] == 150
    assert product["average_price_eur"] == 100
    assert product["seller_count"] == 2


def test_v265_cancelled_or_fully_refunded_zero_sale_does_not_inflate_units():
    sold = row(quantity=2, sale=200, order_id="OK")
    cancelled = row(quantity=9, sale=0, purchase=0, commission=0, margin=0, order_id="CANCEL")
    stats = aggregate_product_stats([sold, cancelled])
    assert stats[0]["quantity"] == 2
    assert stats[0]["orders"] == 1


def test_v265_incomplete_margin_keeps_known_profit_visible_and_flags_missing_rows():
    stats = aggregate_product_stats([
        row(order_id="OK", margin=30),
        row(order_id="MISS", margin=None, purchase=None),
    ])
    product = stats[0]
    assert product["margin_eur"] == 30
    # Percentage waits for complete data because using total sales would be misleading.
    assert product["margin_pct"] is None
    assert product["margin_missing_rows"] == 1
    totals = product_totals(stats)
    assert totals["margin_eur"] == 30
    assert totals["margin_missing_rows"] == 1


def test_v265_filters_cover_seller_marketplace_country_supplier_and_search():
    rows = [
        row(seller_id=1, seller_name="Ginevra", marketplace="Kaufland", country="DE", supplier="Innpro", title="Cuffie Bluetooth"),
        row(seller_id=2, seller_name="Fintrade", marketplace="Worten", country="PT", supplier="Cecotec", title="Friggitrice"),
    ]
    filtered = filter_product_rows(
        rows,
        seller_ids=[2],
        marketplaces=["Worten"],
        countries=["PT"],
        suppliers=["Cecotec"],
        search="frigg",
    )
    assert len(filtered) == 1
    assert filtered[0]["seller_name"] == "Fintrade"


def test_v265_previous_period_has_same_length_and_comparison_by_product():
    assert previous_period_range(date(2026, 8, 20), date(2026, 8, 23)) == (
        date(2026, 8, 16),
        date(2026, 8, 19),
    )
    current = aggregate_product_stats([row(quantity=4, sale=400, margin=120)])
    previous = aggregate_product_stats([row(quantity=2, sale=200, margin=60, order_date=date(2026, 8, 22))])
    merged = merge_previous_period(current, previous)[0]
    assert merged["previous_quantity"] == 2
    assert merged["quantity_delta_pct"] == 100
    assert merged["sales_delta_pct"] == 100
    assert merged["margin_delta_pct"] == 100


def test_v265_sort_modes_quantity_sales_and_margin():
    a = aggregate_product_stats([row(ean="1", title="A", quantity=5, sale=50, margin=5)])[0]
    b = aggregate_product_stats([row(ean="2", title="B", quantity=2, sale=200, margin=80)])[0]
    assert sort_product_stats([a, b], "Più venduti (quantità)")[0]["product_title"] == "A"
    assert sort_product_stats([a, b], "Maggior fatturato")[0]["product_title"] == "B"
    assert sort_product_stats([a, b], "Maggior margine")[0]["product_title"] == "B"


def test_v265_product_detail_breakdowns_and_orders_use_selected_product_only():
    rows = [
        row(ean="1", title="A", quantity=2, sale=200, order_id="A1", country="DE"),
        row(ean="1", title="A", quantity=1, sale=100, order_id="A2", country="FR"),
        row(ean="2", title="B", quantity=7, sale=700, order_id="B1", country="DE"),
    ]
    key = product_identity(rows[0])
    selected = product_rows(rows, key)
    assert len(selected) == 2
    by_country = aggregate_dimension(selected, "country_code")
    assert {item["country_code"] for item in by_country} == {"DE", "FR"}
    orders = aggregate_product_orders(selected)
    assert len(orders) == 2
    assert sum(item["quantity"] for item in orders) == 3
