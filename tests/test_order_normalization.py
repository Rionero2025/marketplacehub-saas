"""Captured v271 examples plus currency/missing-data boundaries for the SaaS port."""

from copy import deepcopy

import pytest
from marketplace_hub_core.orders.normalization import (
    extract_tracking,
    find_order_unit,
    merge_order_unit,
    normalize_order_line,
)


def kaufland(**changes):
    return {
        "id_order_unit": 123,
        "id_order": "ORDER-1",
        "storefront": "de",
        "status": "need_to_be_sent",
        "ts_created_iso": "2026-09-06T23:30:00Z",
        "id_offer": "Innpro_6974662350503_335.07_452.34",
        "price": 45000,
        "shipping_rate": 0,
        "revenue_gross": 40000,
        "product": {"title": "Prodotto originale", "eans": ["6974662350503"]},
        **changes,
    }


def worten(**changes):
    return {
        "order_line_id": "WORDER-1-A",
        "offer_sku": "fornitore_codice_20_30",
        "product_title": "Prodotto Worten",
        "quantity": 2,
        "price": 100,
        "shipping_price": 5,
        "commission_fee": 10,
        "commission_vat": 2.3,
        "order_line_state": "SHIPPING",
        "_order": {
            "order_id": "WORDER-1",
            "created_date": "2026-09-07T12:00:00Z",
            "currency_iso_code": "EUR",
        },
        **changes,
    }


def test_original_kaufland_pln_minor_units_and_fx_are_separate():
    row = normalize_order_line(
        "kaufland",
        kaufland(
            storefront="pl",
            price=10000,
            shipping_rate=500,
            revenue_gross=8500,
            revenue_net=6900,
        ),
        fx_rates={"PLN": 4},
    )
    assert row["currency"] == "PLN"
    assert row["sale_amount"] == "105.00"
    assert row["shipping_amount"] == "5.00"
    assert row["commission_amount"] == "15.00"
    assert row["commission_rate"] == "14.2857"
    assert row["payout_amount"] == "90.00"
    assert row["sale_amount_eur"] == "26.25"
    assert row["shipping_amount_eur"] == "1.25"
    assert row["commission_amount_eur"] == "3.75"
    assert row["payout_amount_eur"] == "22.50"
    assert row["details"]["product_amount_eur"] == "25.00"
    assert row["purchase_cost_eur"] == "335.07"  # SKU is EUR, not PLN.


@pytest.mark.parametrize(
    ("sku", "ean", "payout", "cost", "profit", "percentage"),
    [
        ("Innpro_6974662350503_335.07_452.34", "6974662350503", 400, "335.07", "64.93", "19.38"),
        ("AB_Online_8690842106835_44.80_58.24", "8690842106835", 52, "44.80", "7.20", "16.07"),
        ("AB-Online_8690842106835_44.80_58.24", "111100000606", 54.16, "44.80", "9.36", "20.89"),
        ("in_C01030002_199_273", "6971362040475", 250, "199.00", "51.00", "25.63"),
        ("ceco_A01_EU01_110320_249_340", "110320", 300, "249.00", "51.00", "20.48"),
    ],
)
def test_original_composite_sku_fixtures(sku, ean, payout, cost, profit, percentage):
    row = normalize_order_line(
        "kaufland",
        kaufland(
            id_offer=sku,
            product={"title": "Prodotto", "ean": ean},
            price=int(payout * 100),
            revenue_gross=int(payout * 100),
        ),
    )
    assert row["purchase_cost_eur"] == cost
    assert row["profit_amount_eur"] == profit
    assert row["profit_pct"] == percentage
    assert row["details"]["sku_product_code"] == sku.rsplit("_", 3)[1]
    assert row["details"]["sku_supplier"] == sku.rsplit("_", 3)[0]
    assert row["details"]["sku_ean_matches_order"] == (sku.rsplit("_", 3)[1] == ean)
    assert row["details"]["purchase_cost_method"] == "SKU composto"


@pytest.mark.parametrize(
    "sku",
    [
        "SKU-ORIGINALE",
        "force_KLCS14SAKHPCG",
        "a_code_NaN_10",
        "a_code_10_inf",
        "a_code_0_10",
        "a_code_10_0",
        "a_code_-1_10",
    ],
)
def test_unrecognized_or_invalid_sku_does_not_invent_cost(sku):
    row = normalize_order_line("kaufland", kaufland(id_offer=sku))
    assert row["purchase_cost"] is None
    assert row["profit_amount"] is None
    assert row["details"]["purchase_cost_method"] == "Costo non calcolabile"
    assert "listini non disponibile" in row["monetary_warnings"][0]


def test_explicit_commission_wins_over_revenue_difference_and_handles_object():
    row = normalize_order_line("kaufland", kaufland(commission={"amount": -1234}))
    assert row["commission_amount"] == "12.34"
    assert row["payout_amount"] == "437.66"
    assert row["details"]["commission_source"] == "API Kaufland: commission"


def test_fee_list_commission_and_product_ean_dict():
    row = normalize_order_line(
        "kaufland",
        kaufland(
            fees=[{"type": "commission", "amount": 1200}, {"name": "commission_tax", "gross": 300}],
            product={"title": "Titolo", "eans": {"first": "0123456789012"}},
        ),
    )
    assert row["commission_amount"] == "15.00"
    assert row["ean"] == "0123456789012"
    assert row["product_name"] == "Titolo"


def test_missing_currency_rate_does_not_turn_pln_into_eur():
    row = normalize_order_line("kaufland", kaufland(storefront="pl"))
    assert row["sale_amount"] == "450.00"
    assert row["sale_amount_eur"] is None
    assert row["profit_amount_eur"] is None
    assert any("Cambio PLN/EUR" in warning for warning in row["monetary_warnings"])


def test_fx_snapshot_and_details_warning_are_allowlisted():
    row = normalize_order_line(
        "kaufland",
        kaufland(
            storefront="cz",
            _fx={
                "rates": {"CZK": 25},
                "date": "2026-09-04",
                "source": "BCE",
                "online": True,
                "api_key": "private",
            },
            _detail_warning="details_unavailable",
        ),
    )
    assert row["sale_amount_eur"] == "18.00"
    assert row["details"]["fx"] == {
        "rate": "25.00000000",
        "date": "2026-09-04",
        "source": "BCE",
        "online": True,
    }
    assert row["details"]["detail_warning"] == "details_unavailable"
    bad = normalize_order_line("kaufland", kaufland(_detail_warning="api_key=private"))
    assert "private" not in str(bad["details"])
    assert "private" not in str(bad["monetary_warnings"])


@pytest.mark.parametrize("value", ["NaN", "Infinity", float("inf"), True, {}, []])
def test_invalid_money_is_not_serialized_as_nan_or_infinity(value):
    row = normalize_order_line(
        "kaufland",
        kaufland(
            price=value,
            shipping_rate=None,
            revenue_gross=None,
        ),
    )
    assert row["sale_amount"] is None
    assert row["commission_amount"] is None
    assert row["payout_amount"] is None


def test_tracking_detail_merge_and_matching_unit_preserve_list_fields():
    base = kaufland(status="sent")
    details = {
        "data": {
            "units": [
                {
                    "id_order_unit": 123,
                    "product": {"url": "https://example.test/p"},
                    "shipments": [
                        {"carrier_code": "DHL", "tracking_numbers": ["TRACK-A", "TRACK-B"]}
                    ],
                }
            ]
        }
    }
    matching = find_order_unit(details, "123")
    assert find_order_unit(details, "999") == {}
    merged = merge_order_unit(base, matching)
    assert merged["product"]["title"] == "Prodotto originale"
    assert extract_tracking(merged) == ("DHL", "TRACK-A, TRACK-B")
    row = normalize_order_line("kaufland", merged)
    assert row["details"]["carrier"] == "DHL"


def test_status_dates_do_not_treat_updated_as_delivery():
    row = normalize_order_line(
        "kaufland",
        kaufland(
            status="received",
            ts_updated_iso="2026-09-07T10:00:00Z",
        ),
    )
    assert row["details"]["received_at"] is None
    assert row["details"]["released_at"] is None
    paid = normalize_order_line(
        "kaufland",
        kaufland(
            status="sent_and_autopaid",
            ts_updated_iso="2026-09-07T10:00:00Z",
        ),
    )
    assert paid["details"]["received_at"] is None
    assert paid["details"]["released_at"] == "2026-09-07T10:00:00+00:00"
    assert paid["details"]["released_source"] == (
        "API Kaufland: stato sent_and_autopaid (ts_updated_iso)"
    )


def test_actual_release_and_received_timestamps_have_priority():
    row = normalize_order_line(
        "kaufland",
        kaufland(
            status="sent_and_autopaid",
            ts_updated_iso="2026-09-07T10:00:00Z",
            revenue_released_timestamp_iso="2026-09-06T10:00:00Z",
            order_received_timestamp_iso="2026-08-23T10:00:00Z",
        ),
    )
    assert row["details"]["received_at"] == "2026-08-23T10:00:00+00:00"
    assert row["details"]["released_at"] == "2026-09-06T10:00:00+00:00"
    assert row["details"]["received_source"] == (
        "API Kaufland: order_received_timestamp_iso"
    )
    assert row["details"]["released_source"] == (
        "API Kaufland: revenue_released_timestamp_iso"
    )


def test_kaufland_cancelled_keeps_api_amounts_but_excludes_them_from_totals():
    row = normalize_order_line("kaufland", kaufland(status="cancelled"))
    assert row["sale_amount"] == "450.00"
    assert row["details"]["excluded_from_totals"] is True


def test_worten_major_units_line_price_and_sku_quantity_cost():
    row = normalize_order_line("worten", worten())
    assert row["sale_amount"] == "105.00"  # price is already the line amount.
    assert row["quantity"] == 2
    assert row["shipping_amount"] == "5.00"
    assert row["commission_amount"] == "12.30"
    assert row["commission_rate"] == "9.5238"  # rate uses commission fee excluding VAT.
    assert row["payout_amount"] == "92.70"
    assert row["purchase_cost_eur"] == "40.00"
    assert row["profit_amount_eur"] == "52.70"
    assert row["profit_pct"] == "131.75"
    assert row["status_label"] == "In attesa di spedizione"
    assert row["ean"] == ""  # opaque SKU code must not become an invented EAN.


def test_worten_dedicated_unit_price_is_multiplied_and_total_has_priority():
    row = normalize_order_line("worten", worten(unit_price=30))
    assert row["sale_amount"] == "65.00"
    row = normalize_order_line("worten", worten(unit_price=30, total_price=55))
    assert row["sale_amount"] == "55.00"


def test_worten_uses_positive_sku_cost_without_requiring_a_numeric_minimum():
    row = normalize_order_line("worten", worten(offer_sku="supplier_code_20.123_invalid"))
    assert row["purchase_cost_eur"] == "40.25"
    assert row["details"]["minimum_price_sku_eur"] is None
    assert row["details"]["purchase_cost_method"] == "SKU composto"


def test_worten_recovers_only_current_line_sku_and_tracking():
    row = normalize_order_line(
        "worten",
        worten(
            offer_sku=None,
            offer_id=12345,
            sku="supplier_code_20_30",
            shipping_carrier_code="CTT",
            shipping_tracking="WORDER-TRACK",
        ),
    )
    assert row["sku"] == "supplier_code_20_30"
    assert row["details"]["tracking"] == "WORDER-TRACK"
    assert row["details"]["carrier"] == "CTT"


def test_worten_commission_breakdown_rate_and_total_fee_priority():
    row = normalize_order_line(
        "worten",
        worten(
            total_commission=15,
            commission_fee=10,
            commission_vat=5,
            price_amount_breakdown={
                "parts": [
                    {"commissionable": True, "amount": 80},
                    {"commissionable": False, "amount": 20},
                ]
            },
        ),
    )
    assert row["commission_amount"] == "15.00"
    assert row["commission_rate"] == "12.5000"


def test_worten_direct_payout_is_authoritative_and_unknown_fee_not_zero():
    raw = worten(commission_fee=None, commission_vat=None)
    row = normalize_order_line("worten", raw)
    assert row["commission_amount"] is None
    assert row["payout_amount"] is None
    raw["payout_amount"] = 91
    row = normalize_order_line("worten", raw)
    assert row["payout_amount"] == "91.00"
    assert row["commission_amount"] is None


def test_worten_explicit_partial_refund_and_returned_inference():
    row = normalize_order_line("worten", worten(refund_amount=20))
    assert row["sale_amount"] == "85.00"
    assert row["payout_amount"] == "72.70"
    assert row["details"]["refund_amount"] == "20.00"
    returned = normalize_order_line("worten", worten(order_line_state="RETURNED"))
    assert returned["sale_amount"] == "0.00"
    assert returned["payout_amount"] == "0.00"
    assert returned["purchase_cost_eur"] == "40.00"
    assert returned["profit_amount_eur"] == "-40.00"
    assert "regola originale" in returned["details"]["refund_source"]


@pytest.mark.parametrize("status", ["CANCELED", "REFUSED", "REFUNDED", "PARTIALLY_REFUNDED"])
def test_worten_exact_original_zero_economics_rules_preserve_raw_evidence(status):
    raw = worten(order_line_state=status, refund_amount=20)
    original = deepcopy(raw)
    row = normalize_order_line("worten", raw)
    assert row["sale_amount"] == "0.00"
    assert row["commission_amount"] == "0.00"
    assert row["purchase_cost_eur"] == "0.00"
    assert row["profit_amount_eur"] == "0.00"
    assert "regola originale" in row["details"]["financial_source"]
    assert row["raw"] == raw == original


def test_unknown_marketplace_and_missing_identifiers_raise_safely():
    with pytest.raises(ValueError, match="unsupported_marketplace"):
        normalize_order_line("unknown", {})
    with pytest.raises(ValueError, match="missing_order_identifier"):
        normalize_order_line("kaufland", {"price": 1000})
    with pytest.raises(ValueError, match="missing_order_identifier"):
        normalize_order_line("worten", worten(order_line_id=None))


def test_input_is_not_mutated_and_created_timestamp_preserves_original_instant():
    raw = kaufland()
    original = deepcopy(raw)
    row = normalize_order_line("kaufland", raw)
    assert raw == original
    assert row["created_at"] == "2026-09-06T23:30:00+00:00"
    assert row["quantity"] == 1
