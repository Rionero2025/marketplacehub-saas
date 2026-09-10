import json
from datetime import UTC, datetime
from uuid import uuid4

import test_order_normalization
import test_orders_api
from marketplace_hub_core.orders.normalization import normalize_order_line
from marketplace_hub_core.orders.payments import LEGACY_RECEIVED_FALLBACK_SOURCE
from marketplace_hub_core.orders.projections import project_order
from marketplace_hub_core.orders.schema import order_lines
from sqlalchemy import select

workspace = test_orders_api.workspace
configured = test_orders_api.configured

PAYMENT_PROJECTION_COLUMNS = {
    "payment_due_at",
    "payment_available",
    "payment_date_final",
    "payment_ticket_open",
    "payment_ticket_delay_days",
    "payment_event_backup_json",
}


def normalized_kaufland(identifier, **changes):
    raw = test_order_normalization.kaufland(
        id_order_unit=identifier,
        id_order=f"ORDER-{identifier}",
        **changes,
    )
    return normalize_order_line("kaufland", raw)


def sync_and_read(configured, items):
    client, seller, organization, _, _ = test_orders_api.owner(configured)
    account_id = test_orders_api.account(configured, seller, organization)
    configured.fetcher.items = items
    response = test_orders_api.start(client, seller, account_id)
    assert response.status_code == 202
    test_orders_api.run(configured, response)
    page = test_orders_api.read(client, seller, account_id)
    assert page.status_code == 200
    return client, seller, account_id, page.json()


def test_legacy_0348_received_tracking_adds_14_days_without_changing_economics(configured):
    item = normalized_kaufland(
        "LEGACY-0348",
        status="received",
        order_received_timestamp_iso="2026-07-10T12:00:00Z",
        ts_updated_iso="2026-07-10T12:00:00Z",
        tracking_numbers="TRACK-0348",
        price=10000,
        shipping_rate=500,
        revenue_gross=8425,
        id_offer="",
    )
    expected_economics = {
        "sale_amount": "105.00",
        "shipping_amount": "5.00",
        "commission_amount": "15.75",
        "commission_rate": "15.0000",
        "payout_amount": "89.25",
        "purchase_cost": None,
        "profit_amount": None,
        "sale_amount_eur": "105.00",
        "shipping_amount_eur": "5.00",
        "commission_amount_eur": "15.75",
        "payout_amount_eur": "89.25",
        "purchase_cost_eur": None,
        "profit_amount_eur": None,
    }
    assert {field: item[field] for field in expected_economics} == expected_economics

    _, _, _, page = sync_and_read(configured, [item])
    saved = page["items"][0]

    assert {field: saved[field] for field in expected_economics} == expected_economics
    assert saved["details"]["tracking"] == "TRACK-0348"
    assert saved["details"]["received_at"] == "2026-07-10T12:00:00+00:00"
    assert saved["details"]["payment_due_at"] == "2026-07-24T12:00:00+00:00"
    assert saved["details"]["payment_rule"] == "Con tracking: consegna + 14 giorni"


def test_legacy_0350_autopaid_uses_actual_release_source_and_is_available(configured):
    item = normalized_kaufland(
        "LEGACY-0350",
        status="sent_and_autopaid",
        ts_updated_iso="2026-09-01T10:00:00Z",
        revenue_released_timestamp_iso="2026-08-31T12:00:00Z",
    )

    _, _, _, page = sync_and_read(configured, [item])
    details = page["items"][0]["details"]

    assert details["released_at"] == "2026-08-31T12:00:00+00:00"
    assert details["released_source"] == "API Kaufland: revenue_released_timestamp_iso"
    assert details["payment_due_at"] == details["released_at"]
    assert details["payment_source"] == details["released_source"]
    assert details["payment_rule"] == "Data effettiva comunicata da Kaufland"
    assert details["payment_date_final"] is True
    assert details["payment_available"] is True


def test_legacy_0361_repository_row_without_new_projections_is_enriched_by_api(configured):
    client, seller, organization, _, _ = test_orders_api.owner(configured)
    account_id = test_orders_api.account(configured, seller, organization)

    legacy = normalized_kaufland(
        "LEGACY-0361",
        status="received",
        order_received_timestamp_iso="2026-07-07T08:15:00Z",
        ts_updated_iso="2026-07-21T08:15:00Z",
        shipments=[{"carrier_code": "DHL", "tracking_numbers": ["TRACK-0361"]}],
    )
    legacy["details"].update(
        {
            "received_at": "2026-07-21T08:15:00+00:00",
            "received_source": LEGACY_RECEIVED_FALLBACK_SOURCE,
            "commission_source": "API Kaufland: archivio ordine esistente",
        }
    )
    assert not PAYMENT_PROJECTION_COLUMNS.intersection(legacy["details"])

    public = {key: value for key, value in legacy.items() if key != "raw"}
    projections = {
        key: value
        for key, value in project_order(public).items()
        if key not in PAYMENT_PROJECTION_COLUMNS
    }
    now = datetime.now(UTC)
    with configured.engine.begin() as connection:
        connection.execute(order_lines.insert().values(
            **projections,
            id=uuid4(),
            organization_id=organization,
            seller_id=seller,
            account_id=account_id,
            environment="live",
            external_line_id=public["external_line_id"],
            order_id=public["order_id"],
            marketplace="kaufland",
            order_created_at=now,
            status=public["status"],
            storefront=public["storefront"],
            search_text="legacy-0361",
            canonical_json=json.dumps(public, ensure_ascii=False),
            raw_json=json.dumps(legacy["raw"], ensure_ascii=False),
            updated_at=now,
            projection_updated_at=now,
            payment_projection_updated_at=now,
        ))

    with configured.engine.connect() as connection:
        stored = connection.execute(select(order_lines)).mappings().one()
    stored_public = json.loads(stored["canonical_json"])
    assert not PAYMENT_PROJECTION_COLUMNS.intersection(stored_public["details"])
    assert stored["payment_due_at"] is None
    assert stored["payment_available"] is False
    assert stored["payment_date_final"] is False
    assert stored["payment_ticket_open"] is False
    assert stored["payment_ticket_delay_days"] == 0

    response = test_orders_api.read(client, seller, account_id)
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["commission_amount"] == legacy["commission_amount"] == "50.00"
    assert item["commission_rate"] == legacy["commission_rate"]
    assert item["details"]["commission_source"] == ("API Kaufland: archivio ordine esistente")
    assert item["details"]["received_at"] == "2026-07-07T08:15:00+00:00"
    assert item["details"]["received_source"] == ("API Kaufland: order_received_timestamp_iso")
    assert item["details"]["payment_due_at"] == "2026-07-21T08:15:00+00:00"
    assert item["details"]["payment_available"] is True

    with configured.engine.connect() as connection:
        after_get = connection.execute(select(order_lines)).mappings().one()
    assert dict(after_get) == dict(stored)


def test_legacy_0372_autopaid_block_with_actual_dates_is_fully_available(configured):
    first = normalized_kaufland(
        "LEGACY-0372-A",
        status="sent_and_autopaid",
        revenue_released_timestamp_iso="2026-09-07T08:00:00Z",
    )
    second = normalized_kaufland(
        "LEGACY-0372-B",
        status="sent_and_autopaid",
        revenue_released_timestamp_iso="2026-09-08T09:30:00Z",
    )
    client, seller, account_id, page = sync_and_read(configured, [first, second])

    selected = test_orders_api.change_payment(
        client,
        seller,
        account_id,
        page,
        action="select_all",
    )
    assert selected.status_code == 200
    summary = selected.json()["selection"]["summary"]

    assert summary["selected_rows"] == 2
    assert summary["payment_payable_rows"] == 2
    assert summary["payment_scheduled_rows"] == 2
    assert summary["payment_unscheduled_rows"] == 0
    assert summary["payment_all_dates_known"] is True
    assert summary["payment_available_rows"] == 2
    assert summary["payment_waiting_rows"] == 0
    assert summary["payment_all_available"] is True
    assert summary["latest_payment_due_at"] == "2026-09-08T09:30:00+00:00"
    assert summary["available_payout_eur"] == "800.00"
    assert summary["waiting_payout_eur"] == "0.00"


def test_legacy_0373_payment_totals_only_include_selected_available_and_waiting_rows(
    configured,
):
    available = normalized_kaufland(
        "LEGACY-0373-AVAILABLE",
        status="sent_and_autopaid",
        revenue_released_timestamp_iso="2026-01-01T08:00:00Z",
        price=10000,
        revenue_gross=8000,
        id_offer="Supplier_1234567890123_10_20",
    )
    waiting = normalized_kaufland(
        "LEGACY-0373-WAITING",
        status="received",
        order_received_timestamp_iso="2099-01-01T00:00:00Z",
        tracking_numbers="TRACK-0373",
        price=15000,
        revenue_gross=12000,
        id_offer="Supplier_1234567890123_10_20",
    )
    excluded = normalized_kaufland(
        "LEGACY-0373-NOT-SELECTED",
        status="sent_and_autopaid",
        revenue_released_timestamp_iso="2026-01-02T08:00:00Z",
        price=100000,
        revenue_gross=99900,
        id_offer="Supplier_1234567890123_500_600",
    )
    client, seller, account_id, page = sync_and_read(
        configured,
        [available, waiting, excluded],
    )
    ids_by_external = {item["external_line_id"]: item["id"] for item in page["items"]}

    first = test_orders_api.change_payment(
        client,
        seller,
        account_id,
        page,
        line_id=ids_by_external["LEGACY-0373-AVAILABLE"],
    )
    assert first.status_code == 200
    selected = test_orders_api.change_payment(
        client,
        seller,
        account_id,
        page,
        line_id=ids_by_external["LEGACY-0373-WAITING"],
    )
    assert selected.status_code == 200
    selection = selected.json()["selection"]
    summary = selection["summary"]

    assert selection["selected_count"] == 2
    assert ids_by_external["LEGACY-0373-NOT-SELECTED"] not in selection["selected_ids"]
    expected_totals = {
        "selected_rows": 2,
        "distinct_orders": 2,
        "quantity": 2,
        "cancelled_rows": 0,
        "sale_amount_eur": "250.00",
        "commission_amount_eur": "50.00",
        "payout_amount_eur": "200.00",
        "purchase_cost_eur": "20.00",
        "profit_amount_eur": "180.00",
        "profit_pct": "900.00",
        "complete_economic_rows": 2,
        "missing_economic_rows": 0,
        "known_cost_rows": 2,
        "missing_cost_rows": 0,
        "loss_rows": 0,
        "sku_cost_rows": 2,
        "catalog_cost_rows": 0,
        "payment_payable_rows": 2,
        "payment_scheduled_rows": 2,
        "payment_unscheduled_rows": 0,
        "payment_available_rows": 1,
        "payment_waiting_rows": 1,
        "payment_all_dates_known": True,
        "payment_all_available": False,
        "latest_payment_due_at": "2099-01-15T00:00:00+00:00",
        "available_payout_eur": "80.00",
        "waiting_payout_eur": "120.00",
    }
    assert {key: summary[key] for key in expected_totals} == expected_totals


def test_master_0821_worten_events_never_receive_kaufland_payment_rules(configured):
    client, seller, organization, _, _ = test_orders_api.owner(configured)
    account_id = test_orders_api.account(
        configured,
        seller,
        organization,
        marketplace="worten",
    )
    item = normalize_order_line(
        "worten",
        test_order_normalization.worten(
            order_line_id="WORTEN-0821",
            order_line_state="RECEIVED",
            tracking_number="WORTEN-TRACK-0821",
            received_at="2026-08-20T08:15:00Z",
            shipped_at="2026-08-18T07:00:00Z",
        ),
    )
    item["details"].update(
        {
            "received_at": "2026-08-20T08:15:00+00:00",
            "received_source": "API Worten: received_at",
            "shipped_at": "2026-08-18T07:00:00+00:00",
            "shipped_source": "API Worten: shipped_at",
        }
    )
    configured.fetcher.items = [item]

    started = test_orders_api.start(client, seller, account_id)
    assert started.status_code == 202
    test_orders_api.run(configured, started)
    response = test_orders_api.read(client, seller, account_id)
    assert response.status_code == 200
    page = response.json()
    assert "payment_selection" not in page
    returned = page["items"][0]
    assert returned["marketplace"] == "worten"
    assert returned["details"]["tracking"] == "WORTEN-TRACK-0821"
    assert returned["details"]["received_at"] == "2026-08-20T08:15:00+00:00"
    assert returned["details"]["shipped_at"] == "2026-08-18T07:00:00+00:00"

    forbidden_payment_fields = {
        "payment_due_at",
        "payment_available",
        "payment_date_final",
        "payment_days_remaining",
        "payment_rule",
        "payment_source",
        "payment_status",
    }
    assert not forbidden_payment_fields.intersection(returned["details"])
    assert "+ 14 giorni" not in response.text
    assert "+ 21 giorni" not in response.text

    with configured.engine.connect() as connection:
        stored = (
            connection.execute(select(order_lines).where(order_lines.c.account_id == account_id))
            .mappings()
            .one()
        )
    stored_details = json.loads(stored["canonical_json"])["details"]
    assert not forbidden_payment_fields.intersection(stored_details)
    assert stored["payment_due_at"] is None
    assert stored["payment_available"] is False
    assert stored["payment_date_final"] is False
    assert stored["payment_ticket_open"] is False
    assert stored["payment_ticket_delay_days"] == 0
