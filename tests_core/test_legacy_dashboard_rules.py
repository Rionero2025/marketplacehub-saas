from __future__ import annotations

from datetime import date

from services.dashboard import order_local_date, period_totals, seller_dashboard_summary


def line(created: str, sale: float, purchase: float | None, commission: float, *, status: str = "Spedito"):
    payout = sale - commission
    return {
        "order_created": created,
        "sale_eur": sale,
        "purchase_cost_eur": purchase,
        "commission_eur": commission,
        "payout_eur": payout,
        "extra_cost_eur": 0,
        "raw_status": status,
        "status_label": status,
        "supplier_order_number": "",
        "note": "",
    }


def test_order_date_uses_rome_timezone_for_utc_timestamp():
    assert order_local_date("2026-07-30T22:30:00Z") == date(2026, 7, 31)


def test_period_totals_use_accounting_profit_formula():
    result = period_totals([line("2026-07-30T10:00:00Z", 100, 60, 15)])
    assert result["sales"] == 100
    assert result["profit"] == 25
    assert result["missing_profit_rows"] == 0


def test_period_totals_zero_cancelled_rows_even_if_database_contains_values():
    result = period_totals([
        line("2026-07-30T10:00:00Z", 99.64, 60, 15, status="Cancellato")
    ])
    assert result["sales"] == 0
    assert result["profit"] == 0


def test_missing_cost_is_not_counted_as_profit_and_is_reported():
    result = period_totals([line("2026-07-30T10:00:00Z", 100, None, 15)])
    assert result["sales"] == 100
    assert result["profit"] == 0
    assert result["missing_profit_rows"] == 1


def test_seller_summary_groups_today_week_month_and_all():
    records = [
        line("2026-07-30T10:00:00+02:00", 100, 60, 10),  # today
        line("2026-07-27T10:00:00+02:00", 200, 140, 20),  # same Monday week
        line("2026-07-01", 300, 200, 30),                 # month
        line("2026-06-30", 400, 300, 40),                # all only
        line("data non valida", 50, 25, 5),              # all only
    ]
    result = seller_dashboard_summary(
        {"id": 1, "name": "Ginevra Srl"}, records, today=date(2026, 7, 30)
    )
    assert result["periods"]["today"]["sales"] == 100
    assert result["periods"]["week"]["sales"] == 300
    assert result["periods"]["month"]["sales"] == 600
    assert result["periods"]["all"]["sales"] == 1050
    assert result["periods"]["today"]["profit"] == 30
    assert result["periods"]["week"]["profit"] == 70
    assert result["periods"]["month"]["profit"] == 140
    assert result["periods"]["all"]["profit"] == 220


def test_period_totals_counts_each_order_once_with_multiple_product_lines():
    first = line("2026-07-30T10:00:00Z", 100, 60, 15)
    first.update({"marketplace_account_id": 7, "marketplace": "worten", "order_id": "ABC-1", "row_key": "r1"})
    second = line("2026-07-30T10:00:00Z", 50, 20, 5)
    second.update({"marketplace_account_id": 7, "marketplace": "worten", "order_id": "ABC-1", "row_key": "r2"})
    third = line("2026-07-30T11:00:00Z", 75, 40, 10)
    third.update({"marketplace_account_id": 7, "marketplace": "worten", "order_id": "ABC-2", "row_key": "r3"})
    result = period_totals([first, second, third])
    assert result["orders"] == 2
    assert result["rows"] == 3


def test_cancelled_order_stays_in_order_count_with_zero_economics():
    cancelled = line("2026-07-30T10:00:00Z", 99.64, 60, 15, status="Cancellato")
    cancelled.update({"marketplace_account_id": 7, "marketplace": "worten", "order_id": "CANCEL-1"})
    result = period_totals([cancelled])
    assert result["orders"] == 1
    assert result["sales"] == 0
    assert result["profit"] == 0


def test_dashboard_sync_in_progress_ignores_stale_v131_lease():
    from datetime import datetime, timedelta, timezone
    from services.dashboard import dashboard_sync_in_progress

    now = datetime(2026, 7, 30, 16, 0, tzinfo=timezone.utc)
    state = {
        "last_started_at": (now - timedelta(hours=2)).isoformat(),
        "last_completed_at": "",
    }
    assert dashboard_sync_in_progress(state, now=now) is False


def test_dashboard_sync_in_progress_detects_recent_background_run():
    from datetime import datetime, timedelta, timezone
    from services.dashboard import dashboard_sync_in_progress

    now = datetime(2026, 7, 30, 16, 0, tzinfo=timezone.utc)
    state = {
        "last_started_at": (now - timedelta(minutes=2)).isoformat(),
        "last_completed_at": (now - timedelta(minutes=10)).isoformat(),
    }
    assert dashboard_sync_in_progress(state, now=now) is True


def test_date_range_totals_is_inclusive_and_excludes_invalid_dates():
    from services.dashboard import date_range_totals

    records = [
        line("2026-08-01T10:00:00+02:00", 100, 60, 10),
        line("2026-08-02T10:00:00+02:00", 200, 120, 20),
        line("2026-08-03T10:00:00+02:00", 300, 180, 30),
        line("data non valida", 400, 200, 40),
    ]
    result = date_range_totals(records, date(2026, 8, 1), date(2026, 8, 2))
    assert result["sales"] == 300
    assert result["profit"] == 90
    assert result["orders"] == 2


def test_seller_summary_adds_selected_interval_and_profit_split():
    records = [
        line("2026-08-01T10:00:00+02:00", 100, 60, 10),
        line("2026-08-02T10:00:00+02:00", 200, 120, 20),
        line("2026-08-03T10:00:00+02:00", 300, 180, 30),
    ]
    result = seller_dashboard_summary(
        {
            "id": 1,
            "name": "Ginevra",
            "our_profit_pct": 35,
            "partner_profit_pct": 65,
        },
        records,
        today=date(2026, 8, 3),
        selected_from=date(2026, 8, 1),
        selected_to=date(2026, 8, 2),
    )
    selected = result["periods"]["selected"]
    assert selected["sales"] == 300
    assert selected["profit"] == 90
    assert selected["our_amount"] == 31.5
    assert selected["partner_amount"] == 58.5


def test_combined_selected_period_returns_total_bebol_share():
    from services.dashboard import combined_dashboard_period

    summaries = [
        {"periods": {"selected": {"sales": 100, "profit": 30, "our_amount": 12, "partner_amount": 18, "orders": 1, "missing_profit_rows": 0}}},
        {"periods": {"selected": {"sales": 200, "profit": 50, "our_amount": 17.5, "partner_amount": 32.5, "orders": 2, "missing_profit_rows": 1}}},
    ]
    result = combined_dashboard_period(summaries, "selected")
    assert result["our_amount"] == 29.5
    assert result["partner_amount"] == 50.5
    assert result["orders"] == 3
