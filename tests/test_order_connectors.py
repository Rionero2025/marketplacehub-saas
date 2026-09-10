import asyncio
import hashlib
import hmac
from urllib.parse import parse_qs

import httpx
import pytest
from marketplace_hub_core.orders.connectors import (
    ORDER_STATUSES,
    OrdersConnector,
    OrdersFetchError,
)

ECB = ('<Envelope><Cube time="2026-09-07"><Cube currency="PLN" rate="4"/>'
       '<Cube currency="CZK" rate="25"/></Cube></Envelope>')
KEYS = {"client_key": "synthetic-client", "secret_key": "synthetic-secret"}
WORTEN = {"api_key": "synthetic-worten", "shop_id": "123"}


def unit(identity, status="open", **extra):
    return {
        "id_order_unit": identity,
        "id_order": f"order-{identity}",
        "status": status,
        "ts_created_iso": f"2026-09-{int(identity) % 28 + 1:02}T12:00:00Z",
        "storefront": "de",
        "price": 10000,
        "shipping_rate": 500,
        "revenue_gross": 9000,
        "id_offer": "Innpro_123_20_30",
        "product": {"title": "Prodotto dimostrativo", "eans": ["1234567890123"]},
        **extra,
    }


def run(handler, *, marketplace="kaufland", credentials=None, **options):
    calls, batches, messages, pauses = [], [], [], []

    def transport(request):
        calls.append(request)
        if request.url.host == "www.ecb.europa.eu":
            return httpx.Response(200, text=ECB)
        return handler(request)

    async def batch(rows, processed, total):
        batches.append((rows, processed, total))

    async def progress(message):
        messages.append(message)

    async def pause(seconds):
        pauses.append(seconds)

    connector = OrdersConnector(
        transport=httpx.MockTransport(transport),
        pause=pause,
        timestamp=lambda: 1234567,
        max_attempts=2,
    )
    result = asyncio.run(
        connector.fetch_orders(
            marketplace,
            credentials or (KEYS if marketplace == "kaufland" else WORTEN),
            environment=options.pop("environment", "live"),
            maximum=options.pop("maximum", 1000),
            include_details=options.pop("include_details", False),
            on_batch=batch,
            on_progress=progress,
            **options,
        )
    )
    return result, calls, batches, messages, pauses


def test_kaufland_reads_every_status_then_applies_global_recent_cap_and_deduplicates():
    def handler(request):
        status = parse_qs(request.url.query.decode())["status"][0]
        rows = (
            [unit(1, status), unit(2, status)]
            if status == "cancelled"
            else ([unit(2, status), unit(27, status)] if status == "sent" else [])
        )
        return httpx.Response(200, json={"data": rows, "pagination": {"total": len(rows)}})

    _, calls, batches, _, _ = run(handler, maximum=2)
    requests = [call for call in calls if call.url.path == "/v2/order-units"]
    assert [parse_qs(call.url.query.decode())["status"][0] for call in requests] == list(
        ORDER_STATUSES
    )
    assert [row["external_line_id"] for row in batches[0][0]] == ["27", "2"]
    assert batches[-1][1:] == (2, 2)


def test_kaufland_paginates_100_rows_and_signs_the_exact_full_url():
    def handler(request):
        query = parse_qs(request.url.query.decode())
        offset = int(query["offset"][0])
        rows = (
            [unit(i) for i in range(offset + 1, min(102, offset + 101))]
            if query["status"] == ["open"]
            else []
        )
        expected = hmac.new(
            KEYS["secret_key"].encode(), f"GET\n{request.url}\n\n1234567".encode(), hashlib.sha256
        ).hexdigest()
        assert request.headers["shop-signature"] == expected
        assert request.headers["shop-client-key"] == KEYS["client_key"]
        return httpx.Response(200, json={"data": rows, "pagination": {"total": 101 if rows else 0}})

    _, calls, batches, _, _ = run(handler)
    offsets = [
        parse_qs(call.url.query.decode())["offset"][0]
        for call in calls
        if call.url.path == "/v2/order-units" and "status=open" in str(call.url)
    ]
    assert offsets == ["0", "100"]
    assert sum(len(batch[0]) for batch in batches) == 101


def test_empty_marketplace_is_successful_and_never_creates_placeholder_rows():
    result, _, batches, _, _ = run(lambda request: httpx.Response(200, json={"data": []}))
    assert result["warning_count"] == 0
    assert batches == [([], 0, 0)]


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": [None]}, {"data": [{}]}])
def test_malformed_pages_are_not_silently_reported_as_zero_orders(payload):
    with pytest.raises(OrdersFetchError, match="dati ordine non riconoscibili"):
        run(lambda request: httpx.Response(200, json=payload))


def test_duplicate_full_page_aborts_instead_of_looping_forever():
    page = [unit(i) for i in range(100)]
    with pytest.raises(OrdersFetchError):
        run(lambda request: httpx.Response(200, json={"data": page}), maximum=None)


def test_detail_merge_and_order_fallback_preserve_list_fields_and_exact_unit():
    def handler(request):
        if request.url.path == "/v2/tickets":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v2/order-units":
            status = parse_qs(request.url.query.decode())["status"][0]
            return httpx.Response(200, json={"data": [unit(1, "sent")] if status == "sent" else []})
        if request.url.path == "/v2/order-units/1":
            return httpx.Response(
                200, json={"data": {"id_order_unit": 1, "product": {"title": "Dettaglio"}}}
            )
        assert request.url.path == "/v2/orders/order-1"
        return httpx.Response(
            200,
            json={
                "data": {
                    "order_units": [
                        {"id_order_unit": 2, "tracking_numbers": ["wrong"]},
                        {
                            "id_order_unit": 1,
                            "tracking_numbers": ["track-demo"],
                            "carrier_code": "DHL",
                        },
                    ]
                }
            },
        )

    result, _, batches, _, _ = run(handler, include_details=True)
    row = batches[0][0][0]
    assert row["product_name"] == "Dettaglio"
    assert row["ean"] == "1234567890123"
    assert row["details"]["tracking"] == "track-demo"
    assert row["details"]["detail_checked_at"] == "1970-01-15T06:56:07+00:00"
    assert result["details_checked"] == 1


def test_optional_details_failure_keeps_base_with_visible_warning():
    def handler(request):
        if request.url.path == "/v2/order-units":
            status = parse_qs(request.url.query.decode())["status"][0]
            return httpx.Response(200, json={"data": [unit(1, "sent")] if status == "sent" else []})
        return httpx.Response(404, text="private upstream body")

    result, _, batches, _, _ = run(handler, include_details=True)
    assert result["warning_count"] == 1
    assert batches[0][0][0]["details"]["detail_warning"] == "details_unavailable"
    assert "private upstream" not in str(batches)
    assert "detail_checked_at" not in batches[0][0][0]["details"]


def test_auth_error_is_not_retried_or_exposed():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401, text="synthetic-secret-and-private-response")

    with pytest.raises(OrdersFetchError) as failure:
        run(handler)
    assert failure.value.code == "invalid_credentials"
    assert "synthetic-secret" not in str(failure.value)
    assert len(seen) == 1


def test_transient_read_retry_rechecks_authorization():
    attempts, checks = [], []

    async def guard():
        checks.append(True)

    def handler(request):
        attempts.append(request)
        return (
            httpx.Response(429, headers={"retry-after": "1"})
            if len(attempts) == 1
            else httpx.Response(200, json={"data": []})
        )

    _, _, _, _, pauses = run(handler, before_request=guard)
    assert len([call for call in attempts if call.url.path == "/v2/order-units"]) == 9
    assert len([call for call in attempts if call.url.path == "/v2/tickets"]) == 5
    assert len(checks) == 15  # one initial gate plus every authenticated attempt
    assert any(delay >= 1 for delay in pauses)


def test_revoked_permission_stops_before_sending_marketplace_request():
    calls = []

    async def guard():
        raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        run(lambda request: calls.append(request), before_request=guard)
    assert calls == []


def test_playground_uses_its_own_host_and_worten_rejects_playground():
    _, calls, _, _, _ = run(
        lambda request: httpx.Response(200, json={"data": []}), environment="playground"
    )
    assert {call.url.host for call in calls} == {
        "www.ecb.europa.eu",
        "sellerapi-playground.kaufland.com",
    }
    with pytest.raises(OrdersFetchError):
        run(lambda request: None, marketplace="worten", environment="playground")


def test_kaufland_reads_all_ticket_statuses_and_returns_complete_snapshot():
    seen = []

    def handler(request):
        query = parse_qs(request.url.query.decode())
        if request.url.path == "/v2/tickets":
            status = query["status"][0]
            seen.append(status)
            rows = [{
                "id_ticket": f"T-{status}", "ids_order_units": ["1"],
                "ts_created_iso": "2026-09-01T00:00:00Z",
                "ts_updated_iso": "2026-09-02T00:00:00Z", "status": status,
            }]
            return httpx.Response(200, json={"data": rows, "pagination": {"total": 1}})
        return httpx.Response(200, json={"data": []})

    result, _, _, _, _ = run(handler)
    assert seen == [
        "opened", "buyer_closed", "seller_closed", "both_closed",
        "customer_service_closed_final",
    ]
    assert {ticket["id_ticket"] for ticket in result["tickets_snapshot"]} == {
        f"T-{status}" for status in seen
    }


def test_kaufland_ticket_snapshot_paginates_thirty_rows_per_status():
    offsets = []

    def handler(request):
        if request.url.path != "/v2/tickets":
            return httpx.Response(200, json={"data": []})
        query = parse_qs(request.url.query.decode())
        status, offset = query["status"][0], int(query["offset"][0])
        offsets.append((status, offset))
        total = 31 if status == "opened" else 0
        rows = [{
            "id_ticket": f"T-{index}", "ids_order_units": [str(index)],
            "ts_created_iso": "2026-09-01T00:00:00Z",
            "ts_updated_iso": "2026-09-02T00:00:00Z", "status": status,
        } for index in range(offset, min(offset + 30, total))]
        return httpx.Response(200, json={"data": rows, "pagination": {"total": total}})

    result, _, _, _, _ = run(handler)
    assert offsets[:2] == [("opened", 0), ("opened", 30)]
    assert offsets[2:] == [
        ("buyer_closed", 0), ("seller_closed", 0), ("both_closed", 0),
        ("customer_service_closed_final", 0),
    ]
    assert len(result["tickets_snapshot"]) == 31


def test_ticket_endpoint_failure_is_best_effort_and_orders_still_complete():
    def handler(request):
        if request.url.path == "/v2/tickets":
            return httpx.Response(503)
        status = parse_qs(request.url.query.decode())["status"][0]
        rows = [unit(1, "sent")] if status == "sent" else []
        return httpx.Response(200, json={"data": rows})

    result, _, batches, _, _ = run(handler)
    assert result["tickets_warning"] is True
    assert batches[0][0][0]["external_line_id"] == "1"


def test_worten_reads_line_quantities_and_major_currency_without_offer_filter():
    def handler(request):
        query = parse_qs(request.url.query.decode())
        assert query["shop_id"] == ["123"]
        assert query["order"] == ["desc"] and "sort" not in query
        assert "offer_id" not in query
        assert request.headers["authorization"] == WORTEN["api_key"]
        return httpx.Response(
            200,
            json={
                "orders": [
                    {
                        "order_id": "W1",
                        "created_date": "2026-09-01T00:00:00Z",
                        "order_lines": [
                            {
                                "order_line_id": "W1-A",
                                "quantity": 2,
                                "price": 100,
                                "shipping_price": 0,
                                "commission_fee": 10,
                                "total_commission": 12,
                                "offer_sku": "Innpro_123_20_30",
                                "product_title": "Prodotto Worten",
                                "currency_iso_code": "EUR",
                                "order_line_state": "RECEIVED",
                            },
                        ],
                    }
                ],
                "total_count": 1,
            },
        )

    _, _, batches, _, _ = run(handler, marketplace="worten")
    row = batches[0][0][0]
    assert row["quantity"] == 2
    assert row["sale_amount"] == "100.00"
    assert row["purchase_cost"] == "40.00"
    assert row["raw"]["_order"]["order_id"] == "W1"
    assert batches[-1][1:] == (1, 1)


def test_worten_paginates_orders_and_deduplicates_lines_with_no_cap():
    def handler(request):
        offset = int(parse_qs(request.url.query.decode())["offset"][0])
        orders = [
            {
                "order_id": f"W{i}",
                "order_lines": [
                    {"order_line_id": f"W{i}-A", "quantity": 1, "price": 1},
                ],
            }
            for i in range(offset, min(offset + 100, 101))
        ]
        return httpx.Response(200, json={"orders": orders, "total_count": 101})

    _, _, batches, _, _ = run(handler, marketplace="worten", maximum=None)
    assert sum(len(batch[0]) for batch in batches) == 101
    assert batches[-1][1:] == (101, 101)


def test_arbitrary_credential_endpoint_is_rejected_before_transmission():
    with pytest.raises(OrdersFetchError) as failure:
        run(
            lambda request: None,
            marketplace="worten",
            credentials={**WORTEN, "api_url": "https://unexpected.test/api"},
        )
    assert failure.value.code == "invalid_configuration"


def test_worten_rejects_a_response_for_another_shop():
    with pytest.raises(OrdersFetchError) as failure:
        run(lambda request: httpx.Response(200, json={"orders": [
            {"shop_id": 456, "order_id": "W1", "order_lines": []},
        ]}), marketplace="worten")
    assert failure.value.code == "permission_denied"


def test_worten_commercial_id_and_missing_line_id_keep_original_fallback():
    _, _, batches, _, _ = run(lambda request: httpx.Response(200, json={"orders": [{
        "commercial_id": "COMMERCIAL-1", "shop_id": 123,
        "order_lines": [{"quantity": 1, "price": 1}],
    }]}), marketplace="worten")
    assert batches[0][0][0]["order_id"] == "COMMERCIAL-1"
    assert batches[0][0][0]["external_line_id"] == "COMMERCIAL-1-1"
