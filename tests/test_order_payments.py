from datetime import UTC, datetime

from marketplace_hub_core.orders.normalization import normalize_order_line
from marketplace_hub_core.orders.payments import (
    apply_payment_details,
    payment_schedule,
    repair_payment_event_details,
    selected_order_financial_summary,
    selected_payment_deadline,
    ticket_holds,
)

NOW = datetime(2026, 7, 20, 9, tzinfo=UTC)


def test_release_autopaid_and_estimated_payment_rules_match_original():
    released = payment_schedule(
        "received", received_at="2026-07-10T12:00:00Z",
        released_at="2026-07-23T09:30:00Z", current_time=NOW,
    )
    assert released["payment_due_at"] == "2026-07-23T09:30:00+00:00"
    assert released["payment_date_final"] is True
    assert released["payment_rule"] == "Data effettiva comunicata da Kaufland"

    autopaid = payment_schedule("sent_and_autopaid", current_time=NOW)
    assert autopaid["payment_available"] is True
    assert autopaid["payment_due_at"] == ""

    tracked = payment_schedule(
        "received", received_at="2026-07-10T12:00:00Z", has_tracking=True,
        current_time=NOW,
    )
    assert tracked["payment_due_at"] == "2026-07-24T12:00:00+00:00"
    assert tracked["payment_days_remaining"] == 4

    tracking_without_delivery = payment_schedule(
        "sent", shipped_at="2026-07-10T12:00:00Z", has_tracking=True,
        current_time=NOW,
    )
    assert tracking_without_delivery["payment_due_at"] == ""
    assert tracking_without_delivery["payment_status"] == (
        "Tracking presente · consegna non ancora rilevata"
    )
    assert tracking_without_delivery["payment_date_final"] is False

    untracked = payment_schedule(
        "sent", shipped_at="2026-07-10T12:00:00Z", current_time=NOW,
    )
    assert untracked["payment_due_at"] == "2026-07-31T12:00:00+00:00"
    assert untracked["payment_rule"] == "Senza tracking: spedizione + 21 giorni"
    assert payment_schedule("open", current_time=NOW)["payment_status"] == "Non ancora spedito"


def test_archived_raw_payload_repairs_legacy_received_fallback_and_release_events():
    legacy = {
        "marketplace": "kaufland", "status": "received",
        "details": {
            "tracking": "TRACK-1", "received_at": "2026-07-21T08:15:00+00:00",
            "received_source": "API Kaufland: aggiornamento allo stato Ricevuto",
        },
    }
    repaired = repair_payment_event_details(legacy, {
        "status": "received", "order_received_timestamp_iso": "2026-07-07T08:15:00Z",
        "ts_updated_iso": "2026-07-21T08:15:00Z",
    })
    projected = apply_payment_details(repaired, current_time=NOW)
    assert projected["details"]["received_at"] == "2026-07-07T08:15:00+00:00"
    assert projected["details"]["received_source"] == (
        "API Kaufland: order_received_timestamp_iso"
    )
    assert projected["details"]["payment_due_at"] == "2026-07-21T08:15:00+00:00"

    unsafe = repair_payment_event_details(legacy, {
        "status": "received", "ts_updated_iso": "2026-07-21T08:15:00Z",
    })
    assert unsafe["details"]["received_at"] is None
    assert unsafe["details"]["received_source"] == ""
    assert apply_payment_details(unsafe, current_time=NOW)["details"]["payment_due_at"] == ""


def test_estimated_availability_uses_current_utc_calendar_date_on_every_projection():
    before = payment_schedule(
        "received", received_at="2026-07-10T23:59:59Z", has_tracking=True,
        current_time=datetime(2026, 7, 23, 23, 59, 59, tzinfo=UTC),
    )
    on_due_date = payment_schedule(
        "received", received_at="2026-07-10T23:59:59Z", has_tracking=True,
        current_time=datetime(2026, 7, 24, 0, 0, 0, tzinfo=UTC),
    )
    after = payment_schedule(
        "received", received_at="2026-07-10T23:59:59Z", has_tracking=True,
        current_time=datetime(2026, 7, 29, 0, 0, 0, tzinfo=UTC),
    )
    assert before["payment_days_remaining"] == 1
    assert before["payment_available"] is False
    assert on_due_date["payment_days_remaining"] == 0
    assert on_due_date["payment_available"] is True
    assert after["payment_days_remaining"] == -5
    assert after["payment_status"] == "Disponibile da 5 giorni"
    assert after["payment_available"] is True


def test_ticket_holds_merge_overlaps_and_open_ticket_keeps_date_provisional():
    holds = ticket_holds([
        {
            "id_ticket": "closed", "ids_order_units": ["UNIT-1"],
            "ts_created_iso": "2026-07-12T00:00:00Z",
            "ts_updated_iso": "2026-07-16T00:00:00Z", "status": "both_closed",
        },
        {
            "id_ticket": "overlap", "ids_order_units": ["UNIT-1"],
            "ts_created_iso": "2026-07-14T00:00:00Z",
            "ts_updated_iso": "2026-07-18T00:00:00Z", "status": "seller_closed",
        },
        {
            "id_ticket": "open", "ids_order_units": ["UNIT-1"],
            "ts_created_iso": "2026-07-19T00:00:00Z",
            "ts_updated_iso": "2026-07-19T00:00:00Z", "status": "opened",
        },
    ], current_time=NOW)
    hold = holds["UNIT-1"]
    assert hold["delay_seconds"] == 7 * 86400 + 9 * 3600
    assert hold["ticket_count"] == 3 and hold["open_ticket_count"] == 1
    assert hold["ticket_ids"] == ["closed", "overlap", "open"]
    payment = payment_schedule(
        "received", received_at="2026-07-10T12:00:00Z", has_tracking=True,
        ticket_delay_seconds=hold["delay_seconds"], ticket_open=True, current_time=NOW,
    )
    assert payment["payment_status"] == "Ticket aperto · data in aggiornamento"
    assert payment["payment_date_final"] is False
    assert payment["payment_available"] is False

    closed = payment_schedule(
        "received", received_at="2026-07-10T12:00:00Z", has_tracking=True,
        ticket_delay_seconds=3 * 86400, current_time=NOW,
    )
    assert closed["payment_due_at"] == "2026-07-27T12:00:00+00:00"
    assert closed["ticket_delay_days"] == 3.0
    assert closed["payment_date_final"] is True


def test_selected_deadline_and_financial_summary_ignore_cancelled_rows():
    items = [
        {
            "external_line_id": "paid", "status": "sent_and_autopaid",
            "payout_amount_eur": "80", "purchase_cost_eur": "50",
            "profit_amount_eur": "30", "details": {
                "payment_available": True, "payment_date_final": False,
                "payment_due_at": "",
            },
        },
        {
            "external_line_id": "waiting", "status": "received",
            "payout_amount_eur": "120", "purchase_cost_eur": "90",
            "profit_amount_eur": "30", "details": {
                "payment_available": False, "payment_date_final": True,
                "payment_due_at": "2026-08-01T00:00:00Z",
            },
        },
        {
            "external_line_id": "cancelled", "status": "cancelled",
            "payout_amount_eur": "500", "purchase_cost_eur": "400",
            "profit_amount_eur": "100", "details": {},
        },
    ]
    deadline = selected_payment_deadline(items)
    assert deadline["payable_units"] == 2 and deadline["ignored_cancelled_units"] == 1
    assert deadline["scheduled_units"] == 1 and deadline["unscheduled_ids"] == ["paid"]
    assert deadline["latest_payment_due_at"] == "2026-08-01T00:00:00+00:00"
    summary = selected_order_financial_summary(items)
    assert summary["payable_units"] == 2 and summary["cancelled_units"] == 1
    assert summary["available_eur"] == 80 and summary["waiting_eur"] == 120


def test_selected_deadline_uses_latest_date_and_accepts_legacy_flat_rows():
    deadline = selected_payment_deadline([
        {
            "id_order_unit": "1", "status": "received", "payment_date_final": True,
            "payment_due_at": "2026-08-02T12:00:00Z", "payment_available": True,
        },
        {
            "id_order_unit": "2", "status": "sent_and_autopaid",
            "payment_date_final": True, "payment_due_at": "2026-08-07T08:00:00Z",
            "payment_available": True,
        },
        {"id_order_unit": "3", "status": "cancelled", "payment_due_at": ""},
    ])
    assert deadline["payable_units"] == 2
    assert deadline["ignored_cancelled_units"] == 1
    assert deadline["all_dates_known"] is True
    assert deadline["all_available"] is True
    assert deadline["latest_payment_due_at"] == "2026-08-07T08:00:00+00:00"


def test_selected_financial_summary_marks_unknown_cost_and_ignores_cancelled_amounts():
    summary = selected_order_financial_summary([
        {
            "status": "cancelled", "payout_eur": 500,
            "purchase_cost_eur": 400, "order_profit_eur": 100,
        },
        {
            "status": "received", "payout_eur": 75,
            "purchase_cost_eur": None, "order_profit_eur": None,
            "payment_available": False,
        },
    ])
    assert summary["cancelled_units"] == 1
    assert summary["payable_units"] == 1
    assert summary["payout_eur"] == 75
    assert summary["purchase_cost_eur"] == 0
    assert summary["unknown_cost_units"] == 1


def test_apply_payment_details_exposes_ticket_projection_without_mutating_input():
    item = {
        "marketplace": "kaufland", "status": "received",
        "details": {"received_at": "2026-07-10T12:00:00Z", "tracking": "TRACK"},
    }
    projected = apply_payment_details(item, {
        "delay_seconds": 86400, "ticket_count": 1, "open_ticket_count": 0,
        "ticket_ids": ["T-1"],
    }, current_time=NOW)
    assert projected is not item and item["details"].get("payment_due_at") is None
    assert projected["details"]["payment_due_at"] == "2026-07-25T12:00:00+00:00"
    assert projected["details"]["payment_source"] == "Con tracking: consegna + 14 giorni"
    assert projected["details"]["ticket_ids"] == ["T-1"]


def test_legacy_saved_row_keeps_commission_enrichment_and_adds_payment_without_sql_columns():
    legacy = normalize_order_line("kaufland", {
        "id_order_unit": "LEGACY", "id_order": "ORDER-LEGACY", "storefront": "de",
        "status": "received", "ts_updated_iso": "2026-07-10T12:00:00Z",
        "price": 10000, "shipping_rate": 500, "revenue_gross": 8425,
    })
    legacy["details"]["commission_source"] = "API Kaufland: archivio ordine esistente"
    assert legacy["sale_amount"] == "105.00"
    assert legacy["commission_amount"] == "15.75"
    assert legacy["commission_rate"] == "15.0000"
    assert "payment_status" not in legacy["details"]

    enriched = apply_payment_details(legacy, current_time=NOW)
    assert enriched["commission_amount"] == "15.75"
    assert enriched["commission_rate"] == "15.0000"
    assert enriched["details"]["commission_source"] == (
        "API Kaufland: archivio ordine esistente"
    )
    assert enriched["details"]["payment_due_at"] == ""
    assert enriched["details"]["payment_status"] == "Data di spedizione non disponibile"
