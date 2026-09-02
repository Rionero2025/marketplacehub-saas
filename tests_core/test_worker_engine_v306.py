from datetime import date

from marketplace_core.accounting import AccountingCore, AccountingPeriod, AccountingScope
from marketplace_core.orders import OrderScope, OrdersCore


def test_worten_orders_build_background_job():
    request = OrdersCore().build_sync_job(
        OrderScope(3, 11, "worten", "live"),
        date_from=date(2026, 8, 1),
        date_to=date(2026, 9, 2),
    )
    assert request.kind == "orders.worten.sync"
    assert request.seller_id == 3
    assert request.payload["account_id"] == 11
    assert request.payload["date_from"] == "2026-08-01"
    assert request.payload["date_to"] == "2026-09-02"


def test_worten_orders_job_requires_period():
    try:
        OrdersCore().build_sync_job(OrderScope(3, 11, "worten", "live"))
    except ValueError:
        return
    raise AssertionError("Worten background job accepted a missing period")


def test_accounting_sync_job_is_credential_free():
    request = AccountingCore().build_sync_job(
        AccountingScope(3, 11, "worten"),
        AccountingPeriod(date(2026, 8, 1), date(2026, 9, 2)),
        full=False,
    )
    assert request.kind == "accounting.orders.sync"
    assert request.seller_id == 3
    assert request.payload["marketplace"] == "worten"
    assert "credentials" not in request.payload


def test_accounting_cost_job():
    request = AccountingCore().build_refresh_costs_job(
        AccountingScope(3, 7, "kaufland"),
        AccountingPeriod(date(2026, 8, 1), date(2026, 9, 2)),
    )
    assert request.kind == "accounting.costs.refresh"
    assert request.payload["account_id"] == 7
    assert request.payload["marketplace"] == "kaufland"
