import csv
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import test_orders_api
from fastapi.testclient import TestClient
from marketplace_hub_core.auth.schema import auth_sessions
from marketplace_hub_core.orders.export import csv_cell, export_csv, safe_text
from marketplace_hub_core.orders.filters import OrderFilters
from marketplace_hub_core.orders.schema import (
    order_lines,
    order_selection_members,
    order_selections,
)
from marketplace_hub_core.tenancy.schema import memberships
from sqlalchemy import create_engine, func, select
from test_orders_api import account, owner, path, read, row, run, start

workspace = test_orders_api.workspace
configured = test_orders_api.configured


def money(identifier, **values):
    return row(identifier, **{
        "order_id": identifier, "sale_amount_eur": "20.00", "commission_amount_eur": "2.00",
        "payout_amount_eur": "18.00", "purchase_cost_eur": "10.00", "profit_amount_eur": "8.00",
        "purchase_cost_source": "SKU composto", **values,
    })


def setup(configured, items):
    client, seller, organization, member, user = owner(configured)
    account_id = account(configured, seller, organization)
    configured.fetcher.items = items
    job = run(configured, start(client, seller, account_id))
    return client, seller, account_id, member, user, configured.order_repository.job(job)


def change(client, seller, account_id, selection, action, filters=None, **values):
    return client.post(path(seller, "/selection"), json={
        "account_id": str(account_id), "selection_id": selection["id"],
        "filters": filters or {}, "action": action, **values,
    })


def export(client, seller, account_id, selection, kind="selected", filters=None, **values):
    return client.post(path(seller, "/export"), json={
        "account_id": str(account_id), "selection_id": selection["id"], "filters": filters or {},
        "kind": kind, **values,
    })


def test_presence_ranges_and_empty_selection_filters(configured):
    client, seller, aid, *_ = setup(configured, [
        money("zero", commission_amount="0", commission_amount_eur="0",
              details={"tracking": "T1", "carrier": "DHL"}),
        money("missing", commission_amount=None, commission_amount_eur=None,
              storefront="at", details={}),
        money("usd", currency="USD", sale_amount_eur=None, commission_amount_eur=None,
              payout_amount_eur=None, details={"carrier": "UPS"}),
        money("unknown", currency="", sale_amount_eur="30"),
    ])
    def total(**kwargs):
        return read(client, seller, aid, **kwargs).json()["total"]
    assert total(commission="present") == 3
    assert total(commission="missing") == 1
    assert total(tracking="present") == 2
    assert total(tracking="missing") == 2
    assert total(carrier="DHL") == 1
    assert total(amount_min="20", amount_max="20") == 2
    assert total(currency="") == 1
    for marker in ("status_selection", "storefront_selection", "currency_selection"):
        assert total(**{marker: "selected"}) == 0
    assert total(status="sent", status_selection="selected") == 4
    options = read(client, seller, aid).json()["filters"]
    assert options["currencies"] == ["", "EUR", "USD"]
    assert options["carriers"] == ["DHL", "UPS"]


@pytest.mark.parametrize("query", [
    {"amount_min": "-1"}, {"amount_min": "nan"}, {"amount_min": "Infinity"},
    {"amount_max": "1e999"}, {"amount_min": "2", "amount_max": "1"},
    {"tracking": "other"}, {"commission": "other"}, {"status_selection": "other"},
])
def test_invalid_filters_are_safe_422(configured, query):
    client, seller, organization, *_ = owner(configured)
    aid = account(configured, seller, organization)
    response = read(client, seller, aid, **query)
    assert response.status_code == 422


def test_selection_persists_per_filter_and_page_not_first_page_cap(configured):
    client, seller, aid, _, user, _ = setup(configured, [money(f"r{i}") for i in range(125)])
    first = read(client, seller, aid).json()
    selection = first["selection"]
    assert selection["filtered_count"] == selection["selected_count"] == 125
    assert len(selection["selected_ids"]) == len(first["items"]) == 50
    assert set(selection["selected_ids"]) == {item["id"] for item in first["items"]}
    cleared = change(client, seller, aid, selection, "clear").json()["selection"]
    assert cleared["selected_count"] == 0
    assert read(client, seller, aid, page=2).json()["selection"]["selected_count"] == 0
    other_filter = read(client, seller, aid, search="r1").json()["selection"]
    assert other_filter["id"] != selection["id"] and other_filter["selected_count"] == 36
    restored = read(client, seller, aid).json()["selection"]
    assert restored["id"] == selection["id"] and restored["selected_count"] == 0
    again = change(client, seller, aid, restored, "select_all").json()["selection"]
    assert again["selected_count"] == 125 and again["selected_ids"] == []
    same_user_new_session, _ = configured.session(user)
    independent = read(same_user_new_session, seller, aid).json()["selection"]
    assert independent["id"] != selection["id"] and independent["selected_count"] == 125


def test_prunes_no_longer_visible_and_never_reselects_new_rows(configured):
    client, seller, aid, _, _, job = setup(configured, [money("first"), money("second")])
    selected = read(client, seller, aid, status="sent").json()["selection"]
    configured.order_repository.upsert_batch(job, [money("first", status="returned"), money("new")])
    selection = read(client, seller, aid, status="sent").json()["selection"]
    assert selection["id"] == selected["id"]
    assert selection["filtered_count"] == 2 and selection["selected_count"] == 1
    all_items = read(client, seller, aid).json()["items"]
    hidden = next(item["id"] for item in all_items if item["external_line_id"] == "first")
    result = change(client, seller, aid, selection, "set", {"statuses": ["sent"]},
                    line_id=hidden, selected=True)
    assert result.status_code == 404


def test_summary_respects_complete_triples_independent_cost_profit_and_cancelled(configured):
    client, seller, aid, *_ = setup(configured, [
        money("regular"),
        money("fee-zero", commission_amount="0", commission_amount_eur="0",
              payout_amount_eur="20", profit_amount_eur="10"),
        money("missing-fee", commission_amount=None, commission_amount_eur=None,
              profit_amount_eur="5"),
        money("missing-cost", purchase_cost_eur=None, profit_amount_eur=None),
        money("loss", purchase_cost_eur="30", profit_amount_eur="-12",
              purchase_cost_source="Listino pubblicato: prova"),
        money("cancelled", status="cancelled"),
        money("usd", currency="USD", sale_amount_eur=None, commission_amount_eur=None,
              payout_amount_eur=None, purchase_cost_eur=None, profit_amount_eur=None),
    ])
    summary = read(client, seller, aid).json()["selection"]["summary"]
    assert summary == {
        "selected_rows": 7, "distinct_orders": 7, "quantity": 7, "cancelled_rows": 1,
        "sale_amount_eur": "80.00", "commission_amount_eur": "6.00", "payout_amount_eur": "74.00",
        "purchase_cost_eur": "60.00", "profit_amount_eur": "11.00", "profit_pct": "18.33",
        "complete_economic_rows": 5, "missing_economic_rows": 2, "known_cost_rows": 4,
        "missing_cost_rows": 2, "loss_rows": 1, "sku_cost_rows": 3, "catalog_cost_rows": 1,
        "missing_currencies": ["EUR", "USD"],
    }


def test_selection_scope_filter_session_and_read_permissions(configured):
    client, seller, aid, member, user, _ = setup(configured, [money("one")])
    state = read(client, seller, aid).json()["selection"]
    another, _ = configured.session(user)
    assert change(another, seller, aid, state, "clear").status_code == 404
    assert export(another, seller, aid, state).status_code == 404
    assert change(client, seller, aid, state, "clear", {"statuses": []}).status_code == 404
    assert export(client, seller, aid, state, environment="playground").status_code == 404
    foreign, fseller, forg, *_ = owner(configured)
    faid = account(configured, fseller, forg)
    assert change(foreign, fseller, faid, state, "clear").status_code == 404
    assert export(foreign, fseller, faid, state).status_code == 404
    with configured.engine.begin() as connection:
        connection.execute(memberships.update().where(memberships.c.id == member)
                           .values(read_only=True))
    assert change(client, seller, aid, state, "select_all").status_code == 200
    assert export(client, seller, aid, state).status_code == 200
    with configured.engine.begin() as connection:
        connection.execute(memberships.update().where(memberships.c.id == member)
                           .values(active=False))
    assert change(client, seller, aid, state, "clear").status_code == 404
    assert export(client, seller, aid, state).status_code == 404


def test_invalid_selection_actions_require_exact_boolean_and_row(configured):
    client, seller, aid, *_ = setup(configured, [money("one")])
    initial = read(client, seller, aid).json()
    state, line_id = initial["selection"], initial["items"][0]["id"]
    assert change(client, seller, aid, state, "set", selected=True).status_code == 422
    assert change(client, seller, aid, state, "set", line_id=line_id).status_code == 422
    assert change(client, seller, aid, state, "clear", selected=True).status_code == 422
    assert change(client, seller, aid, state, "set", line_id=line_id,
                  selected="false").status_code == 422
    assert change(client, seller, aid, state, "set", line_id=str(uuid4()), selected=True
                  ).status_code == 404
    result = change(client, seller, aid, state, "set", line_id=line_id, selected=False)
    assert result.json()["selection"]["selected_count"] == 0
    assert export(client, seller, aid, state).status_code == 422
    assert export(client, seller, aid, state, "filtered").status_code == 422


def test_csv_streams_full_filter_or_selection_without_private_data(configured):
    client, seller, aid, *_ = setup(configured, [
        money(f"r{i:03}", product_name="\ufeff =HYPERLINK(\"evil\")", profit_amount_eur="-2.50")
        for i in range(125)
    ])
    page = read(client, seller, aid).json()
    state = page["selection"]
    response = export(client, seller, aid, state, "filtered")
    assert response.status_code == 200 and response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["cache-control"] == "no-store"
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 125
    assert rows[0]["Prodotto"].startswith("'") and rows[0]["Utile EUR"] == "-2.50"
    assert rows[0]["Data ordine (Italia)"] == "07/09/2026 12:30:00"
    for private in ("private-raw", "must-not-escape", "api_key", "customer", "secret-test"):
        assert private not in response.text
    change(client, seller, aid, state, "clear")
    change(client, seller, aid, state, "set", line_id=page["items"][0]["id"], selected=True)
    selected = export(client, seller, aid, state)
    assert len(list(csv.DictReader(io.StringIO(selected.content.decode("utf-8-sig"))))) == 1
    assert len(list(csv.DictReader(io.StringIO(export(client, seller, aid, state, "filtered")
                                            .content.decode("utf-8-sig"))))) == 125


@pytest.mark.parametrize("value", ["=1", "+1", "-1", "@x", " \t=1", "\ufeff@x", "\r1", "\u200b+1",
                                   " \t1", "\ufeff\rfoo"])
def test_csv_formula_injection_text(value):
    assert safe_text(value) == "'" + value


@pytest.mark.parametrize("value", ["2026-09-07T10:30:00", "2026-09-07T10:30:00Z",
                                   "2026-09-07T12:30:00+02:00"])
def test_csv_dates_have_explicit_utc_fallback(value):
    assert csv_cell({"created_at": value}, "created_at", "date") == "07/09/2026 12:30:00"


def test_stream_rechecks_guard_and_closes_database_iterator_on_revocation():
    calls, closed = [], []
    def source():
        try:
            for _ in range(3):
                yield [(json.dumps(money("x")),)]
        finally:
            closed.append(True)
    def guard():
        calls.append(True)
        if len(calls) == 3:
            raise PermissionError("revoked")
    output = export_csv(source(), guard)
    assert next(output).startswith(b"\xef\xbb\xbf")
    assert b"x" in next(output)
    with pytest.raises(PermissionError):
        next(output)
    assert closed == [True]


def test_rolling_old_worker_write_refreshes_stale_projections(configured):
    client, seller, aid, *_ = setup(configured, [money("one")])
    initial = read(client, seller, aid).json()
    line_id = UUID(initial["items"][0]["id"])
    newer = money("one", sale_amount_eur="500", commission_amount_eur="0", commission_amount="0",
                  payout_amount_eur="500", details={"carrier": "UPS", "tracking": ""})
    with configured.engine.begin() as connection:
        connection.execute(order_lines.update().where(order_lines.c.id == line_id).values(
            canonical_json=json.dumps(newer), updated_at=datetime.now(UTC) + timedelta(seconds=1),
        ))
    data = read(client, seller, aid, amount_min="400", tracking="missing").json()
    assert data["total"] == data["selection"]["filtered_count"] == 1
    assert data["selection"]["summary"]["sale_amount_eur"] == "500.00"
    assert data["filters"]["carriers"] == ["UPS"]
    with configured.engine.connect() as connection:
        saved = connection.execute(select(order_lines).where(order_lines.c.id == line_id)
                                   ).mappings().one()
    assert saved["projection_updated_at"] == saved["updated_at"]
    assert json.loads(saved["canonical_json"]) == newer


def test_selection_members_cascade_when_auth_session_deleted(configured):
    client, seller, aid, *_ = setup(configured, [money("one")])
    read(client, seller, aid)
    with configured.engine.begin() as connection:
        session_id = connection.scalar(select(order_selections.c.session_id))
        connection.execute(auth_sessions.delete().where(auth_sessions.c.id == session_id))
        assert connection.scalar(select(func.count()).select_from(order_selections)) == 0
        assert connection.scalar(select(func.count()).select_from(order_selection_members)) == 0


def test_expired_session_ui_selections_purged_without_deleting_auth_audit(configured):
    client, seller, aid, _, user, _ = setup(configured, [money("one")])
    first = read(client, seller, aid).json()["selection"]
    with configured.engine.begin() as connection:
        session_id = connection.scalar(select(order_selections.c.session_id))
        connection.execute(auth_sessions.update().where(auth_sessions.c.id == session_id)
                           .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    fresh, _ = configured.session(user)
    new = read(fresh, seller, aid).json()["selection"]
    assert new["id"] != first["id"] and new["selected_count"] == 1
    with configured.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(order_selections)) == 1
        assert connection.scalar(select(auth_sessions.c.id).where(
            auth_sessions.c.id == session_id)) == session_id


def test_list_and_summary_share_snapshot_when_worker_updates_between_queries(
    configured, tmp_path, monkeypatch,
):
    client, seller, aid, _, _, job = setup(configured, [money("one")])
    # Independent file-backed connections model the real worker/API boundary.
    destination = tmp_path / "orders-concurrency.sqlite3"
    with sqlite3.connect(destination) as target:
        source = configured.engine.raw_connection()
        try:
            source.driver_connection.backup(target)
        finally:
            source.close()
    engine = create_engine(f"sqlite:///{destination}")
    monkeypatch.setattr(configured.order_repository, "engine", engine)
    monkeypatch.setattr(configured.orders.selections, "engine", engine)
    original_list = configured.order_repository.list
    attempted = []
    with ThreadPoolExecutor(max_workers=1) as worker:
        def interleave(*args, **kwargs):
            result = original_list(*args, **kwargs)
            assert kwargs["connection"].in_transaction()
            if not attempted:
                attempted.append(worker.submit(configured.order_repository.upsert_batch,
                                               job, [money("new")]))
            return result
        monkeypatch.setattr(configured.order_repository, "list", interleave)
        initial = read(client, seller, aid).json()
        assert initial["total"] == initial["selection"]["filtered_count"] == 1
        assert initial["selection"]["selected_count"] == 1
        # This test also runs alongside the desktop frontend build on Windows;
        # allow filesystem/worker scheduling latency after the reader commits.
        attempted[0].result(timeout=30)
        refreshed = read(client, seller, aid).json()
        assert refreshed["total"] == refreshed["selection"]["filtered_count"] == 2
        assert refreshed["selection"]["selected_count"] == 1
    engine.dispose()


def test_new_routes_reject_unauthenticated_requests(configured):
    client = TestClient(configured.app)
    request = {"account_id": str(uuid4()), "selection_id": str(uuid4()), "filters": {}}
    assert client.post(path(uuid4(), "/selection"), json={**request, "action": "clear"}
                       ).status_code == 401
    assert client.post(path(uuid4(), "/export"), json={**request, "kind": "selected"}
                       ).status_code == 401


def test_equivalent_filter_values_reuse_fingerprint():
    first = OrderFilters(statuses=["sent", "returned", "sent"], amount_min="10.00")
    second = OrderFilters(statuses=["returned", "sent"], amount_min="10")
    assert first.fingerprint() == second.fingerprint()
    assert OrderFilters(statuses=[]).fingerprint() != OrderFilters().fingerprint()
