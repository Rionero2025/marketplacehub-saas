from marketplace_core.contracts import JobRequest
from marketplace_core.orders import OrderScope, OrdersCore


def test_orders_build_background_job():
    request = OrdersCore().build_sync_job(
        OrderScope(3, 7, "kaufland", "live"),
        maximum=500,
        include_tracking_details=False,
    )
    assert request.kind == "orders.kaufland.sync"
    assert request.seller_id == 3
    assert request.payload["account_id"] == 7
    assert request.payload["maximum"] == 500


def test_job_request_is_ui_independent():
    request = JobRequest(kind="x", seller_id=1, payload={"a": 1})
    assert request.kind == "x"
    assert request.payload == {"a": 1}
