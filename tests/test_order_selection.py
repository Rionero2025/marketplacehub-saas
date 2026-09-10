import csv
import gc
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import marketplace_hub_core.orders.selection as selection_module
import pytest
import test_orders_api
from fastapi.testclient import TestClient
from marketplace_hub_core.auth.passwords import hash_password
from marketplace_hub_core.auth.rate_limit import MemoryLoginRateLimiter
from marketplace_hub_core.auth.schema import auth_sessions, auth_users
from marketplace_hub_core.auth.service import AuthService
from marketplace_hub_core.auth.sql_repository import SqlAuthRepository
from marketplace_hub_core.orders.export import csv_cell, export_csv, safe_text
from marketplace_hub_core.orders.filters import OrderFilters
from marketplace_hub_core.orders.schema import (
    order_lines,
    order_selection_members,
    order_selections,
)
from marketplace_hub_core.tenancy.schema import memberships
from sqlalchemy import create_engine, event, func, select
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


def test_payment_selection_starts_empty_is_main_subset_and_prunes_immediately(configured):
    client, seller, aid, _, user, _ = setup(
        configured, [money("first"), money("second"), money("third")],
    )
    page = read(client, seller, aid).json()
    payment = page["payment_selection"]
    first_id = page["items"][0]["id"]
    assert page["selection"]["purpose"] == "orders"
    assert payment["purpose"] == "payments"
    assert payment["selected_count"] == 0 and payment["filtered_count"] == 3

    selected = test_orders_api.change_payment(
        client, seller, aid, page, line_id=first_id,
    )
    assert selected.status_code == 200
    assert selected.json()["selection"]["selected_count"] == 1
    assert selected.json()["selection"]["summary"]["payment_payable_rows"] == 1

    main = change(
        client, seller, aid, page["selection"], "set", line_id=first_id, selected=False,
    )
    assert main.status_code == 200 and main.json()["selection"]["selected_count"] == 2
    refreshed = read(client, seller, aid).json()
    assert refreshed["payment_selection"]["id"] == payment["id"]
    assert refreshed["payment_selection"]["selected_count"] == 0
    assert refreshed["payment_selection"]["filtered_count"] == 2
    assert test_orders_api.change_payment(
        client, seller, aid, page, line_id=first_id,
    ).status_code == 404

    other_session, _ = configured.session(user)
    assert test_orders_api.change_payment(
        other_session, seller, aid, page, line_id=page["items"][1]["id"],
    ).status_code == 404
    assert test_orders_api.change_payment(
        client, seller, aid, page, line_id=page["items"][1]["id"],
        filters={"statuses": []},
    ).status_code == 404


def test_payment_select_all_is_an_explicit_snapshot_when_main_later_expands(configured):
    client, seller, aid, _, _, job = setup(
        configured, [money("first"), money("second"), money("third")],
    )
    page = read(client, seller, aid).json()
    by_external = {item["external_line_id"]: item["id"] for item in page["items"]}
    for external_id in ("second", "third"):
        result = change(
            client, seller, aid, page["selection"], "set",
            line_id=by_external[external_id], selected=False,
        )
        assert result.status_code == 200
    subset = read(client, seller, aid).json()
    assert subset["selection"]["selected_count"] == 1
    assert subset["payment_selection"]["filtered_count"] == 1

    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.casefold().split()))

    event.listen(configured.engine, "before_cursor_execute", capture)
    try:
        payment = test_orders_api.change_payment(
            client, seller, aid, subset, action="select_all",
        )
    finally:
        event.remove(configured.engine, "before_cursor_execute", capture)
    assert payment.status_code == 200
    assert payment.json()["selection"]["selected_count"] == 1
    assert any(
        statement.startswith("insert into seller_order_selection_members")
        and " select " in statement
        for statement in statements
    )
    with configured.engine.connect() as connection:
        stored = connection.execute(select(
            order_selections.c.default_selected,
        ).where(
            order_selections.c.id == UUID(subset["payment_selection"]["id"]),
        )).scalar_one()
    assert stored is False

    # Re-adding one row and then selecting the whole main population must not
    # implicitly opt either newly eligible row into the payment program.
    assert change(
        client, seller, aid, subset["selection"], "set",
        line_id=by_external["second"], selected=True,
    ).status_code == 200
    after_set = read(client, seller, aid).json()
    assert after_set["selection"]["selected_count"] == 2
    assert after_set["payment_selection"]["selected_count"] == 1
    assert after_set["payment_selection"]["selected_ids"] == [by_external["first"]]

    assert change(
        client, seller, aid, after_set["selection"], "select_all",
    ).status_code == 200
    after_all = read(client, seller, aid).json()
    assert after_all["selection"]["selected_count"] == 3
    assert after_all["payment_selection"]["selected_count"] == 1
    assert after_all["payment_selection"]["selected_ids"] == [by_external["first"]]

    configured.order_repository.upsert_batch(job, [money("new")])
    after_sync = read(client, seller, aid).json()
    assert after_sync["selection"]["selected_count"] == 4
    assert after_sync["payment_selection"]["filtered_count"] == 4
    assert after_sync["payment_selection"]["selected_count"] == 1
    assert after_sync["payment_selection"]["selected_ids"] == [by_external["first"]]


def test_list_page_enrichment_never_updates_or_full_scans_canonical_archive(configured):
    client, seller, aid, *_ = setup(
        configured, [money(f"row-{index:03}") for index in range(125)],
    )
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.casefold().split()))

    event.listen(configured.engine, "before_cursor_execute", capture)
    try:
        response = read(client, seller, aid, page=1, page_size=50)
        with configured.engine.connect() as connection:
            assert connection.scalar(
                select(func.count()).select_from(order_selection_members)
            ) == 0
        statements.clear()
        repeated = read(client, seller, aid, page=1, page_size=50)
    finally:
        event.remove(configured.engine, "before_cursor_execute", capture)
    assert response.status_code == 200 and len(response.json()["items"]) == 50
    assert repeated.status_code == 200 and len(repeated.json()["items"]) == 50
    # A default-selected main selection is sparse: both the first and repeated
    # GET create zero membership rows and never mutate the exception table.
    assert not any(
        "seller_order_selection_members" in statement
        and statement.startswith(("insert", "update", "delete"))
        for statement in statements
    )
    assert not any(
        statement.startswith("update seller_order_lines") for statement in statements
    )
    canonical_reads = [
        statement for statement in statements
        if statement.startswith("select") and "seller_order_lines.canonical_json" in statement
    ]
    assert len(canonical_reads) == 1
    assert " limit " in canonical_reads[0]


def test_default_selected_tracks_new_rows_matching_the_same_filter(configured):
    client, seller, aid, _, _, job = setup(configured, [money("first"), money("second")])
    selected = read(client, seller, aid, status="sent").json()["selection"]
    configured.order_repository.upsert_batch(job, [money("first", status="returned"), money("new")])
    selection = read(client, seller, aid, status="sent").json()["selection"]
    assert selection["id"] == selected["id"]
    # Main selections start as "all matching rows". Membership records are
    # exceptions, so rows synced later become selected when they match.
    assert selection["filtered_count"] == selection["selected_count"] == 2
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
    page = read(client, seller, aid).json()
    summary = page["selection"]["summary"]
    assert summary == {
        "selected_rows": 7, "distinct_orders": 7, "quantity": 7, "cancelled_rows": 1,
        "sale_amount_eur": "80.00", "commission_amount_eur": "6.00", "payout_amount_eur": "74.00",
        "purchase_cost_eur": "60.00", "profit_amount_eur": "11.00", "profit_pct": "18.33",
        "complete_economic_rows": 5, "missing_economic_rows": 2, "known_cost_rows": 4,
        "missing_cost_rows": 2, "loss_rows": 1, "sku_cost_rows": 3, "catalog_cost_rows": 1,
        "missing_currencies": ["EUR", "USD"],
    }
    payment = test_orders_api.change_payment(
        client, seller, aid, page, action="select_all",
    ).json()["selection"]["summary"]
    # Streamlit counts every known active payout in the payment program, even
    # when sale, commission, cost or profit is missing.
    assert payment["payout_amount_eur"] == "92.00"
    assert payment["available_payout_eur"] == "0.00"
    assert payment["waiting_payout_eur"] == "92.00"
    assert payment["payment_unscheduled_rows"] == 6
    assert len(payment["payment_unscheduled_ids"]) == 6


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


def test_get_never_repairs_stale_projection_and_next_sync_materializes_it(configured):
    client, seller, aid, *_, job = setup(configured, [money("one")])
    initial = read(client, seller, aid).json()
    line_id = UUID(initial["items"][0]["id"])
    newer = money("one", sale_amount_eur="500", commission_amount_eur="0", commission_amount="0",
                  payout_amount_eur="500", details={"carrier": "UPS", "tracking": ""})
    newer_updated_at = datetime.now(UTC) + timedelta(seconds=1)
    with configured.engine.begin() as connection:
        connection.execute(order_lines.update().where(order_lines.c.id == line_id).values(
            canonical_json=json.dumps(newer), updated_at=newer_updated_at,
            payment_projection_updated_at=newer_updated_at,
        ))
    data = read(client, seller, aid).json()
    assert data["items"][0]["sale_amount_eur"] == "500"
    with configured.engine.connect() as connection:
        saved = connection.execute(select(order_lines).where(order_lines.c.id == line_id)
                                   ).mappings().one()
    assert saved["projection_updated_at"] != saved["updated_at"]
    assert json.loads(saved["canonical_json"]) == newer

    configured.order_repository.upsert_batch(job, [newer])
    materialized = read(client, seller, aid, amount_min="400", tracking="missing").json()
    assert materialized["total"] == materialized["selection"]["filtered_count"] == 1
    assert materialized["selection"]["summary"]["sale_amount_eur"] == "500.00"
    assert materialized["filters"]["carriers"] == ["UPS"]


def test_selection_members_cascade_when_auth_session_deleted(configured):
    client, seller, aid, *_ = setup(configured, [money("one")])
    read(client, seller, aid)
    with configured.engine.begin() as connection:
        session_id = connection.scalar(select(order_selections.c.session_id))
        connection.execute(auth_sessions.delete().where(auth_sessions.c.id == session_id))
        assert connection.scalar(select(func.count()).select_from(order_selections)) == 0
        assert connection.scalar(select(func.count()).select_from(order_selection_members)) == 0


def test_normal_get_does_not_mass_purge_expired_session_selections(configured):
    client, seller, aid, _, user, _ = setup(configured, [money("one")])
    first = read(client, seller, aid).json()["selection"]
    with configured.engine.begin() as connection:
        session_id = connection.scalar(select(order_selections.c.session_id))
        connection.execute(auth_sessions.update().where(auth_sessions.c.id == session_id)
                           .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    fresh, _ = configured.session(user)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.casefold().split()))

    event.listen(configured.engine, "before_cursor_execute", capture)
    try:
        new = read(fresh, seller, aid).json()["selection"]
    finally:
        event.remove(configured.engine, "before_cursor_execute", capture)
    assert new["id"] != first["id"] and new["selected_count"] == 1
    assert not any(statement.startswith("delete") for statement in statements)
    with configured.engine.connect() as connection:
        # Every Kaufland session owns one main and one payment selection. Normal
        # reads retain the expired session's audit state instead of doing a
        # cross-session purge in the request path.
        assert connection.scalar(select(func.count()).select_from(order_selections)) == 4
        assert connection.scalar(select(auth_sessions.c.id).where(
            auth_sessions.c.id == session_id)) == session_id


def test_stale_selection_maintenance_is_bounded_idempotent_and_keeps_auth_audit(configured):
    client, seller, aid, _, user, _ = setup(configured, [money("one")])
    first = read(client, seller, aid).json()
    with configured.engine.connect() as connection:
        first_session = connection.scalar(select(order_selections.c.session_id).where(
            order_selections.c.id == UUID(first["selection"]["id"]),
        ))

    revoked_client, revoked_principal = configured.session(user)
    read(revoked_client, seller, aid)
    active_client, active_principal = configured.session(user)
    active = read(active_client, seller, aid).json()
    stale_sessions = {first_session, revoked_principal.session_id}
    now = datetime.now(UTC)
    with configured.engine.begin() as connection:
        connection.execute(auth_sessions.update().where(
            auth_sessions.c.id == first_session,
        ).values(expires_at=now - timedelta(seconds=1)))
        connection.execute(auth_sessions.update().where(
            auth_sessions.c.id == revoked_principal.session_id,
        ).values(revoked_at=now))
        stale_ids = list(connection.scalars(select(order_selections.c.id).where(
            order_selections.c.session_id.in_(stale_sessions),
        )))
        line_id = connection.scalar(select(order_lines.c.id))
        connection.execute(order_selection_members.insert(), [
            {"selection_id": selection_id, "line_id": line_id}
            for selection_id in stale_ids
        ])

    assert len(stale_ids) == 4
    assert configured.orders.selections.purge_stale(
        current_time=now, limit=3, member_limit=2,
        exclude_session_id=active_principal.session_id,
    ) == 2
    with configured.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(
            order_selection_members,
        ).where(order_selection_members.c.selection_id.in_(stale_ids))) == 2
        assert connection.scalar(select(func.count()).select_from(order_selections).where(
            order_selections.c.session_id.in_(stale_sessions),
        )) == 2
    # A normal selection POST performs the next bounded maintenance pass.
    changed = change(active_client, seller, aid, active["selection"], "clear")
    assert changed.status_code == 200
    assert configured.orders.selections.purge_stale(current_time=now) == 0
    with configured.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(order_selections).where(
            order_selections.c.session_id.in_(stale_sessions),
        )) == 0
        assert connection.scalar(select(func.count()).select_from(order_selection_members).where(
            order_selection_members.c.selection_id.in_(stale_ids),
        )) == 0
        assert connection.scalar(select(func.count()).select_from(auth_sessions).where(
            auth_sessions.c.id.in_({*stale_sessions, active_principal.session_id}),
        )) == 3


def test_successful_login_runs_best_effort_stale_cleanup_for_readonly_user(
    configured, monkeypatch,
):
    client, seller, aid, membership, user, _ = setup(configured, [money("one")])
    state = read(client, seller, aid).json()["selection"]
    with configured.engine.begin() as connection:
        stale_session = connection.scalar(select(order_selections.c.session_id).where(
            order_selections.c.id == UUID(state["id"]),
        ))
        connection.execute(auth_sessions.update().where(
            auth_sessions.c.id == stale_session,
        ).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        connection.execute(auth_users.update().where(
            auth_users.c.id == user.id,
        ).values(password_hash=hash_password("ReadonlyCleanup42")))
        connection.execute(memberships.update().where(
            memberships.c.id == membership,
        ).values(read_only=True))

    login_client = TestClient(configured.app)
    logged_in = login_client.post("/v1/auth/login", json={
        "login": user.login,
        "password": "ReadonlyCleanup42",
        "realm": "seller",
    })
    assert logged_in.status_code == 200
    with configured.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(order_selections).where(
            order_selections.c.session_id == stale_session,
        )) == 0
        assert connection.scalar(select(auth_sessions.c.id).where(
            auth_sessions.c.id == stale_session,
        )) == stale_session
    # The new session can use the read path even when the membership has no
    # write capability; cleanup happened in the preceding login POST.
    assert read(login_client, seller, aid).status_code == 200

    def fail_cleanup(**_kwargs):
        raise RuntimeError("maintenance unavailable")

    monkeypatch.setattr(configured.orders.selections, "purge_stale", fail_cleanup)
    retry = TestClient(configured.app).post("/v1/auth/login", json={
        "login": user.login,
        "password": "ReadonlyCleanup42",
        "realm": "seller",
    })
    assert retry.status_code == 200


def test_export_selection_is_fixed_before_concurrent_change(
    configured, tmp_path, monkeypatch,
):
    client, seller, aid, *_ = setup(
        configured, [money("first"), money("second")],
    )
    page = read(client, seller, aid).json()
    state = page["selection"]
    expected = page["items"][0]
    assert change(client, seller, aid, state, "clear").status_code == 200
    assert change(
        client, seller, aid, state, "set", line_id=expected["id"], selected=True,
    ).status_code == 200

    destination = tmp_path / "orders-export-snapshot.sqlite3"
    with sqlite3.connect(destination) as target:
        source = configured.engine.raw_connection()
        try:
            source.driver_connection.backup(target)
        finally:
            source.close()
    engine = create_engine(
        f"sqlite:///{destination}", connect_args={"check_same_thread": False},
        pool_size=1, max_overflow=0, pool_timeout=1,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")

    monkeypatch.setattr(configured.order_repository, "engine", engine)
    monkeypatch.setattr(configured.orders.selections, "engine", engine)
    token = client.cookies.get(configured.settings.session_cookie_name)
    principal = configured.auth.authenticate(token)
    assert principal is not None
    file_auth = AuthService(SqlAuthRepository(engine), MemoryLoginRateLimiter())
    guard_calls = []

    def authenticate():
        # This deliberately uses the same size-one pool as the export. Holding
        # the snapshot connection until the first guard would deadlock here.
        current = file_auth.authenticate(token)
        assert current is not None
        guard_calls.append(True)
        return current

    content = configured.orders.export(
        principal, seller, aid, "live", UUID(state["id"]), OrderFilters(), "selected",
        authenticate=authenticate,
    )
    temporary = content._rows._file
    assert engine.pool.checkedout() == 0
    # The selection changes after export authorization but before the response
    # iterator is consumed. The already-authorized snapshot must still win.
    assert change(client, seller, aid, state, "clear").status_code == 200
    payload = b"".join(content)
    exported = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
    assert [row["Unità / riga"] for row in exported] == [expected["external_line_id"]]
    assert len(guard_calls) == 2  # once before the header and once for its only row batch
    assert temporary.closed and engine.pool.checkedout() == 0

    assert change(
        client, seller, aid, state, "set", line_id=expected["id"], selected=True,
    ).status_code == 200
    interrupted = configured.orders.export(
        principal, seller, aid, "live", UUID(state["id"]), OrderFilters(), "selected",
        authenticate=authenticate,
    )
    interrupted_file = interrupted._rows._file
    assert next(interrupted).startswith(b"\xef\xbb\xbf")
    interrupted.close()
    assert interrupted_file.closed and engine.pool.checkedout() == 0

    abandoned = configured.orders.export(
        principal, seller, aid, "live", UUID(state["id"]), OrderFilters(), "selected",
        authenticate=authenticate,
    )
    abandoned_file = abandoned._rows._file
    del abandoned
    gc.collect()
    assert abandoned_file.closed and engine.pool.checkedout() == 0
    engine.dispose()


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
        assert refreshed["selection"]["selected_count"] == 2
    engine.dispose()


def test_payment_list_captures_one_utc_now_across_midnight(configured, monkeypatch):
    actual_now = datetime.now(UTC).replace(microsecond=0)
    due_at = datetime.combine(
        actual_now.date() + timedelta(days=1), datetime.min.time(), UTC,
    ) + timedelta(hours=12)
    received_at = due_at - timedelta(days=14)
    client, seller, aid, *_ = setup(configured, [money(
        "midnight-payment", status="received",
        details={"tracking": "TRACK", "received_at": received_at.isoformat()},
    )])
    before = due_at.replace(hour=23, minute=59, second=59) - timedelta(days=1)
    after = due_at.replace(hour=0, minute=0, second=1)

    class FixedClock:
        @classmethod
        def now(cls, _timezone):
            return before

    monkeypatch.setattr(selection_module, "datetime", FixedClock)
    page = read(client, seller, aid, payment="waiting").json()
    assert page["total"] == 1
    selected = test_orders_api.change_payment(
        client, seller, aid, page, filters={"payment": "waiting"},
    )
    assert selected.status_code == 200

    class RolloverClock:
        calls = 0

        @classmethod
        def now(cls, _timezone):
            cls.calls += 1
            return before if cls.calls == 1 else after

    monkeypatch.setattr(selection_module, "datetime", RolloverClock)
    result = read(client, seller, aid, payment="waiting").json()
    assert RolloverClock.calls == 1
    assert result["total"] == 1
    assert result["items"][0]["details"]["payment_days_remaining"] == 1
    assert result["items"][0]["details"]["payment_available"] is False
    summary = result["payment_selection"]["summary"]
    assert summary["payment_available_rows"] == 0
    assert summary["payment_waiting_rows"] == 1


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
