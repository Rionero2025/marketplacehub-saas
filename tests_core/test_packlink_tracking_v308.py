from datetime import date

from marketplace_core.accounting import AccountingPeriod
from marketplace_core.packlink import PacklinkCore, PacklinkScope
from marketplace_core.tracking import TrackingCore, TrackingScope


def test_packlink_sync_job_contains_no_credentials():
    request = PacklinkCore().build_sync_shipments_job(PacklinkScope(7))
    assert request.kind == "packlink.shipments.sync"
    assert request.seller_id == 7
    assert request.payload == {}


def test_tracking_analysis_job_is_small_and_reference_based():
    request = TrackingCore().build_analysis_job(
        TrackingScope(3, 9, "worten"),
        AccountingPeriod(date(2026, 8, 1), date(2026, 8, 31)),
        file_ids=[12, 12, 15],
        urls=["https://example.test/tracking.csv", "https://example.test/tracking.csv"],
        supplier_choice="Cecotec",
    )
    assert request.kind == "tracking.documents.analyze"
    assert request.seller_id == 3
    assert request.payload["file_ids"] == [12, 15]
    assert request.payload["urls"] == ["https://example.test/tracking.csv"]
    assert "content" not in request.payload
    assert "credentials" not in request.payload


def test_tracking_orders_sync_reuses_accounting_worker_contract():
    request = TrackingCore().build_orders_sync_job(
        TrackingScope(2, 5, "kaufland"),
        AccountingPeriod(date(2026, 8, 10), date(2026, 8, 20)),
        full=True,
    )
    assert request.kind == "accounting.orders.sync"
    assert request.payload["account_id"] == 5
    assert request.payload["marketplace"] == "kaufland"
    assert request.payload["full"] is True
