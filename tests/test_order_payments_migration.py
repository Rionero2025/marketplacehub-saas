import ast
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import test_tenancy_migration
from alembic import command
from marketplace_hub_core.orders.repository import SqlOrdersRepository
from sqlalchemy.dialects import postgresql
from test_seller_settings_migration import read_rows, seed_settings

legacy_database = test_tenancy_migration.legacy_database
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260910_0009_order_payments.py"
)
spec = importlib.util.spec_from_file_location("order_payments_migration", MIGRATION_PATH)
payment_migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payment_migration)


def sqlite_trigger_names(engine):
    with engine.connect() as connection:
        return set(connection.scalars(sa.text(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )))


def test_payment_migration_has_no_application_runtime_imports():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ]
    modules.extend(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(module.startswith("marketplace_hub_core") for module in modules)
    assert "marketplace_hub_core" not in source


def test_postgresql_freshness_check_is_not_valid_then_validated_after_backfill():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "postgresql_not_valid=(connection.dialect.name == \"postgresql\")" in source

    metadata = sa.MetaData()
    table = sa.Table(
        "seller_order_lines",
        metadata,
        sa.Column("marketplace", sa.String()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("payment_projection_updated_at", sa.DateTime(timezone=True)),
    )
    constraint = sa.CheckConstraint(
        payment_migration.PAYMENT_FRESHNESS_EXPRESSION,
        name=payment_migration.PAYMENT_FRESHNESS_CONSTRAINT,
        postgresql_not_valid=True,
    )
    table.append_constraint(constraint)
    ddl = str(sa.schema.AddConstraint(constraint).compile(dialect=postgresql.dialect()))
    assert "CHECK" in ddl and ddl.endswith("NOT VALID")

    upgrade_start = source.index("def upgrade()")
    upgrade_end = source.index("\ndef downgrade()", upgrade_start)
    upgrade_source = source[upgrade_start:upgrade_end]
    assert upgrade_source.index("_create_payment_freshness_constraint(connection)") < (
        upgrade_source.index("now, last = datetime.now(UTC), None")
    )
    assert upgrade_source.index("_validate_payment_freshness_constraint(connection)") > (
        upgrade_source.index("last = rows[-1][\"id\"]")
    )


def test_payment_projection_migration_backfills_and_is_reversible(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    original = {
        "external_line_id": "UNIT-1", "order_id": "ORDER-1", "marketplace": "kaufland",
        "created_at": "2026-08-01T00:00:00Z", "status": "received",
        "details": {
            "tracking": "TRACK-1", "received_at": "2026-08-02T00:00:00Z",
            "released_at": "2026-08-20T00:00:00Z",
            "released_source": "API Kaufland: ts_units_released_iso",
        },
    }
    with engine.begin() as connection:
        connection.execute(lines.insert().values(
            id=uuid4().hex, organization_id=account["organization_id"],
            seller_id=account["seller_id"], account_id=account["id"], environment="live",
            external_line_id="UNIT-1", order_id="ORDER-1", marketplace="kaufland",
            order_created_at=now, status="received", storefront="de", search_text="order-1",
            canonical_json=json.dumps(original), raw_json="{}", updated_at=now,
        ))

    command.upgrade(config, "20260910_0009")
    inspector = sa.inspect(engine)
    names = {column["name"] for column in inspector.get_columns("seller_order_lines")}
    assert {
        "payment_due_at", "payment_available", "payment_date_final",
        "payment_ticket_open", "payment_ticket_delay_days",
        "payment_projection_updated_at",
    } <= names
    assert {
        "trg_order_lines_kaufland_payment_fresh_insert",
        "trg_order_lines_kaufland_payment_fresh_update",
    } <= sqlite_trigger_names(engine)
    assert "seller_order_payment_tickets" in inspector.get_table_names()
    saved = read_rows(engine, "seller_order_lines")[0]
    details = json.loads(saved["canonical_json"])["details"]
    assert details["payment_due_at"] == "2026-08-20T00:00:00+00:00"
    assert details["payment_available"] is True
    assert details["payment_date_final"] is True
    assert details["payment_source"] == "API Kaufland: ts_units_released_iso"
    assert saved["payment_available"] is True
    assert saved["payment_projection_updated_at"] == saved["updated_at"]

    command.downgrade(config, "20260908_0008")
    inspector = sa.inspect(engine)
    assert "seller_order_payment_tickets" not in inspector.get_table_names()
    names = {column["name"] for column in inspector.get_columns("seller_order_lines")}
    assert "payment_due_at" not in names and "payment_available" not in names
    assert "payment_projection_updated_at" not in names
    assert not {
        "trg_order_lines_kaufland_payment_fresh_insert",
        "trg_order_lines_kaufland_payment_fresh_update",
    } & sqlite_trigger_names(engine)
    restored = json.loads(read_rows(engine, "seller_order_lines")[0]["canonical_json"])
    assert restored == original

    # A rollback during a rolling release must remain upgradeable.  The exact
    # projector restores both the public details and the query columns.
    command.upgrade(config, "20260910_0009")
    inspector = sa.inspect(engine)
    assert "seller_order_payment_tickets" in inspector.get_table_names()
    names = {column["name"] for column in inspector.get_columns("seller_order_lines")}
    assert "payment_due_at" in names and "payment_available" in names
    upgraded_again = read_rows(engine, "seller_order_lines")[0]
    repeated_details = json.loads(upgraded_again["canonical_json"])["details"]
    assert repeated_details["payment_due_at"] == "2026-08-20T00:00:00+00:00"
    assert repeated_details["payment_available"] is True
    assert upgraded_again["payment_available"] is True
    assert upgraded_again["payment_projection_updated_at"] == upgraded_again["updated_at"]


def test_payment_migration_rejects_old_worker_writes_atomically(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    original = {
        "external_line_id": "ROLLING-UNIT",
        "order_id": "ROLLING-ORDER",
        "marketplace": "kaufland",
        "status": "received",
        "details": {"received_at": "2026-09-01T10:00:00Z"},
    }
    with engine.begin() as connection:
        connection.execute(lines.insert().values(
            id=uuid4().hex,
            organization_id=account["organization_id"],
            seller_id=account["seller_id"],
            account_id=account["id"],
            environment="live",
            external_line_id="ROLLING-UNIT",
            order_id="ROLLING-ORDER",
            marketplace="kaufland",
            order_created_at=now,
            status="received",
            storefront="de",
            search_text="rolling",
            canonical_json=json.dumps(original),
            raw_json="{}",
            updated_at=now,
        ))

    command.upgrade(config, "20260910_0009")
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    before = read_rows(engine, "seller_order_lines")[0]
    stale = {**original, "status": "sent", "details": {"received_at": "2099-01-01"}}
    later = now + timedelta(minutes=1)

    # A pre-0009 worker updates canonical/general projections and updated_at,
    # but cannot name the new payment freshness column. The whole write fails.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(lines.update().where(
                lines.c.id == before["id"],
            ).values(
                canonical_json=json.dumps(stale),
                status="sent",
                updated_at=later,
                projection_updated_at=later,
            ))
    after_update = read_rows(engine, "seller_order_lines")[0]
    assert after_update["canonical_json"] == before["canonical_json"]
    assert after_update["status"] == before["status"]
    assert after_update["updated_at"] == before["updated_at"]
    assert after_update["payment_projection_updated_at"] == before[
        "payment_projection_updated_at"
    ]

    # A pre-0009 worker insert also omits the marker and cannot create a row
    # that SQL payment filters would later interpret from stale defaults.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(lines.insert().values(
                id=uuid4().hex,
                organization_id=account["organization_id"],
                seller_id=account["seller_id"],
                account_id=account["id"],
                environment="live",
                external_line_id="ROLLING-NEW-UNIT",
                order_id="ROLLING-NEW-ORDER",
                marketplace="kaufland",
                order_created_at=later,
                status="received",
                storefront="de",
                search_text="rolling new",
                canonical_json=json.dumps({
                    **original,
                    "external_line_id": "ROLLING-NEW-UNIT",
                    "order_id": "ROLLING-NEW-ORDER",
                }),
                raw_json="{}",
                updated_at=later,
                projection_updated_at=later,
            ))
    assert len(read_rows(engine, "seller_order_lines")) == 1


def test_sqlite_payment_upgrade_preserves_fk_children(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    user = read_rows(engine, "auth_users")[0]
    metadata = sa.MetaData()
    lines = sa.Table("seller_order_lines", metadata, autoload_with=engine)
    events = sa.Table("seller_order_tracking_events", metadata, autoload_with=engine)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    line_id = uuid4().hex
    with engine.begin() as connection:
        connection.execute(lines.insert().values(
            id=line_id,
            organization_id=account["organization_id"],
            seller_id=account["seller_id"],
            account_id=account["id"],
            environment="live",
            external_line_id="FK-UNIT",
            order_id="FK-ORDER",
            marketplace="kaufland",
            order_created_at=now,
            status="open",
            storefront="de",
            search_text="fk",
            canonical_json=json.dumps({
                "external_line_id": "FK-UNIT",
                "order_id": "FK-ORDER",
                "marketplace": "kaufland",
                "status": "open",
            }),
            raw_json="{}",
            updated_at=now,
        ))
        connection.execute(events.insert().values(
            id=uuid4().hex,
            organization_id=account["organization_id"],
            seller_id=account["seller_id"],
            account_id=account["id"],
            environment="live",
            line_id=line_id,
            actor_id=user["id"],
            source="manual",
            previous_carrier="",
            previous_tracking="",
            carrier="DHL",
            tracking="TRACK",
            created_at=now,
        ))

    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    sa.event.listen(sa.engine.Engine, "connect", enable_foreign_keys)
    try:
        command.upgrade(config, "20260910_0009")
    finally:
        sa.event.remove(sa.engine.Engine, "connect", enable_foreign_keys)

    assert len(read_rows(engine, "seller_order_lines")) == 1
    assert len(read_rows(engine, "seller_order_tracking_events")) == 1


def test_new_worker_upsert_is_compatible_with_pre_payment_schema(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    assert "payment_projection_updated_at" not in {
        column["name"]
        for column in sa.inspect(engine).get_columns("seller_order_lines")
    }
    accounts = sa.Table("seller_marketplace_accounts", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(accounts.update().where(
            accounts.c.id == account["id"],
        ).values(
            active=True,
            settings_json=json.dumps({
                "marketplace_hub_connection_v1": {"connection_status": "connected"},
            }),
        ))

    SqlOrdersRepository(engine).upsert_batch({
        "organization_id": UUID(str(account["organization_id"])),
        "seller_id": UUID(str(account["seller_id"])),
        "account_id": UUID(str(account["id"])),
        "environment": "live",
        "marketplace": "kaufland",
    }, [{
        "external_line_id": "PRE-0009-UNIT",
        "order_id": "PRE-0009-ORDER",
        "marketplace": "kaufland",
        "created_at": "2026-09-10T10:00:00Z",
        "status": "open",
        "storefront": "de",
        "currency": "EUR",
        "quantity": "1",
        "details": {},
        "raw": {},
    }])

    saved = read_rows(engine, "seller_order_lines")
    assert len(saved) == 1
    assert saved[0]["external_line_id"] == "PRE-0009-UNIT"


def test_payment_migration_repairs_archived_legacy_received_fallback(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    canonical = {
        "external_line_id": "UNIT-LEGACY", "order_id": "ORDER-LEGACY",
        "marketplace": "kaufland", "created_at": "2026-07-01T00:00:00Z",
        "status": "received", "details": {
            "tracking": "TRACK-1", "received_at": "2026-07-21T08:15:00+00:00",
            "received_source": "API Kaufland: aggiornamento allo stato Ricevuto",
        },
    }
    archived = {
        "status": "received", "order_received_timestamp_iso": "2026-07-07T08:15:00Z",
        "tracking_numbers": "TRACK-1", "ts_updated_iso": "2026-07-21T08:15:00Z",
    }
    with engine.begin() as connection:
        connection.execute(lines.insert().values(
            id=uuid4().hex, organization_id=account["organization_id"],
            seller_id=account["seller_id"], account_id=account["id"], environment="live",
            external_line_id="UNIT-LEGACY", order_id="ORDER-LEGACY", marketplace="kaufland",
            order_created_at=now, status="received", storefront="de", search_text="legacy",
            canonical_json=json.dumps(canonical), raw_json=json.dumps(archived), updated_at=now,
        ))

    command.upgrade(config, "20260910_0009")
    migrated = json.loads(read_rows(engine, "seller_order_lines")[0]["canonical_json"])
    assert migrated["details"]["received_at"] == "2026-07-07T08:15:00+00:00"
    assert migrated["details"]["received_source"] == (
        "API Kaufland: order_received_timestamp_iso"
    )
    assert migrated["details"]["payment_due_at"] == "2026-07-21T08:15:00+00:00"

    command.downgrade(config, "20260908_0008")
    restored = json.loads(read_rows(engine, "seller_order_lines")[0]["canonical_json"])
    assert restored == canonical

    command.upgrade(config, "20260910_0009")
    migrated_again = json.loads(read_rows(engine, "seller_order_lines")[0]["canonical_json"])
    assert migrated_again["details"]["received_at"] == "2026-07-07T08:15:00+00:00"
    assert migrated_again["details"]["payment_due_at"] == "2026-07-21T08:15:00+00:00"


def test_worker_upsert_invalidates_event_backup_and_downgrade_keeps_new_event(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    legacy = {
        "external_line_id": "UNIT-WORKER", "order_id": "ORDER-WORKER",
        "marketplace": "kaufland", "created_at": "2026-07-01T00:00:00Z",
        "status": "received", "details": {
            "tracking": "TRACK", "received_at": "2026-07-21T08:15:00+00:00",
            "received_source": "API Kaufland: aggiornamento allo stato Ricevuto",
        },
    }
    with engine.begin() as connection:
        connection.execute(lines.insert().values(
            id=uuid4().hex, organization_id=account["organization_id"],
            seller_id=account["seller_id"], account_id=account["id"], environment="live",
            external_line_id="UNIT-WORKER", order_id="ORDER-WORKER", marketplace="kaufland",
            order_created_at=now, status="received", storefront="de", search_text="worker",
            canonical_json=json.dumps(legacy), raw_json=json.dumps({
                "status": "received", "order_received_timestamp_iso": "2026-07-07T08:15:00Z",
            }), updated_at=now,
        ))

    command.upgrade(config, "20260910_0009")
    migrated = read_rows(engine, "seller_order_lines")[0]
    assert migrated["payment_event_backup_json"] is not None
    organization_id = UUID(str(account["organization_id"]))
    seller_id = UUID(str(account["seller_id"]))
    account_id = UUID(str(account["id"]))
    accounts = sa.Table("seller_marketplace_accounts", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(accounts.update().where(accounts.c.id == account["id"]).values(
            active=True,
            settings_json=json.dumps({
                "marketplace_hub_connection_v1": {"connection_status": "connected"},
            }),
        ))
    fresh_received = "2026-09-09T10:00:00Z"
    item = {
        "external_line_id": "UNIT-WORKER", "order_id": "ORDER-WORKER",
        "marketplace": "kaufland", "created_at": "2026-07-01T00:00:00Z",
        "status": "received", "storefront": "de", "currency": "EUR", "quantity": "1",
        "details": {"tracking": "TRACK", "received_at": fresh_received},
        "raw": {
            "status": "received", "order_received_timestamp_iso": fresh_received,
        },
    }
    SqlOrdersRepository(engine).upsert_batch({
        "organization_id": organization_id, "seller_id": seller_id,
        "account_id": account_id, "environment": "live", "marketplace": "kaufland",
    }, [item])
    updated = read_rows(engine, "seller_order_lines")[0]
    assert updated["payment_event_backup_json"] is None

    command.downgrade(config, "20260908_0008")
    downgraded = json.loads(read_rows(engine, "seller_order_lines")[0]["canonical_json"])
    assert downgraded["details"]["received_at"] == "2026-09-09T10:00:00+00:00"
    assert downgraded["details"]["received_source"] == (
        "API Kaufland: order_received_timestamp_iso"
    )
    assert "payment_due_at" not in downgraded["details"]

    command.upgrade(config, "20260910_0009")
    upgraded_again = json.loads(read_rows(engine, "seller_order_lines")[0]["canonical_json"])
    assert upgraded_again["details"]["received_at"] == "2026-09-09T10:00:00+00:00"
    assert upgraded_again["details"]["payment_due_at"] == "2026-09-23T10:00:00+00:00"


def test_payment_migration_roundtrip_preserves_absent_and_non_mapping_details(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    originals = {
        "ABSENT": {
            "external_line_id": "ABSENT",
            "order_id": "ORDER-ABSENT",
            "marketplace": "kaufland",
            "status": "open",
        },
        "NULL": {
            "external_line_id": "NULL",
            "order_id": "ORDER-NULL",
            "marketplace": "kaufland",
            "status": "open",
            "details": None,
        },
        "TEXT": {
            "external_line_id": "TEXT",
            "order_id": "ORDER-TEXT",
            "marketplace": "kaufland",
            "status": "open",
            "details": "legacy-details",
        },
        "LIST": {
            "external_line_id": "LIST",
            "order_id": "ORDER-LIST",
            "marketplace": "kaufland",
            "status": "open",
            "details": ["legacy", 7],
        },
    }
    with engine.begin() as connection:
        for external_line_id, canonical in originals.items():
            connection.execute(lines.insert().values(
                id=uuid4().hex,
                organization_id=account["organization_id"],
                seller_id=account["seller_id"],
                account_id=account["id"],
                environment="live",
                external_line_id=external_line_id,
                order_id=canonical["order_id"],
                marketplace="kaufland",
                order_created_at=now,
                status="open",
                storefront="de",
                search_text=external_line_id.lower(),
                canonical_json=json.dumps(canonical),
                raw_json="{}",
                updated_at=now,
            ))

    command.upgrade(config, "20260910_0009")
    for row in read_rows(engine, "seller_order_lines"):
        assert isinstance(json.loads(row["canonical_json"])["details"], dict)

    command.downgrade(config, "20260908_0008")
    restored = {
        row["external_line_id"]: json.loads(row["canonical_json"])
        for row in read_rows(engine, "seller_order_lines")
    }
    assert restored == originals


def test_payment_migration_keeps_existing_selection_members_as_inclusions(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    user = read_rows(engine, "auth_users")[0]
    metadata = sa.MetaData()
    sessions = sa.Table("auth_sessions", metadata, autoload_with=engine)
    lines = sa.Table("seller_order_lines", metadata, autoload_with=engine)
    selections = sa.Table("seller_order_selections", metadata, autoload_with=engine)
    members = sa.Table("seller_order_selection_members", metadata, autoload_with=engine)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    session_id, line_id, selection_id = uuid4().hex, uuid4().hex, uuid4().hex
    with engine.begin() as connection:
        connection.execute(sessions.insert().values(
            id=session_id,
            user_id=user["id"],
            realm="seller",
            token_hash="a" * 64,
            created_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
            revoked_at=None,
        ))
        connection.execute(lines.insert().values(
            id=line_id,
            organization_id=account["organization_id"],
            seller_id=account["seller_id"],
            account_id=account["id"],
            environment="live",
            external_line_id="SELECTED-UNIT",
            order_id="SELECTED-ORDER",
            marketplace="kaufland",
            order_created_at=now,
            status="open",
            storefront="de",
            search_text="selected",
            canonical_json=json.dumps({
                "external_line_id": "SELECTED-UNIT",
                "order_id": "SELECTED-ORDER",
                "marketplace": "kaufland",
                "status": "open",
            }),
            raw_json="{}",
            updated_at=now,
        ))
        connection.execute(selections.insert().values(
            id=selection_id,
            session_id=session_id,
            organization_id=account["organization_id"],
            seller_id=account["seller_id"],
            account_id=account["id"],
            environment="live",
            filter_hash="f" * 64,
            created_at=now,
            updated_at=now,
        ))
        connection.execute(members.insert().values(
            selection_id=selection_id,
            line_id=line_id,
        ))

    command.upgrade(config, "20260910_0009")
    selection = read_rows(engine, "seller_order_selections")[0]
    assert selection["id"] == selection_id
    assert selection["default_selected"] is False
    assert read_rows(engine, "seller_order_selection_members") == [{
        "selection_id": selection_id,
        "line_id": line_id,
    }]

    command.downgrade(config, "20260908_0008")
    column_names = {
        column["name"]
        for column in sa.inspect(engine).get_columns("seller_order_selections")
    }
    assert "default_selected" not in column_names
    assert read_rows(engine, "seller_order_selection_members") == [{
        "selection_id": selection_id,
        "line_id": line_id,
    }]


def test_payment_migration_downgrade_materializes_sparse_default_all_selection(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    user = read_rows(engine, "auth_users")[0]
    metadata = sa.MetaData()
    sessions = sa.Table("auth_sessions", metadata, autoload_with=engine)
    lines = sa.Table("seller_order_lines", metadata, autoload_with=engine)
    selections = sa.Table("seller_order_selections", metadata, autoload_with=engine)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    session_id, selection_id = uuid4().hex, uuid4().hex
    line_ids = [uuid4().hex for _ in range(3)]
    with engine.begin() as connection:
        connection.execute(sessions.insert().values(
            id=session_id,
            user_id=user["id"],
            realm="seller",
            token_hash="b" * 64,
            created_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
            revoked_at=None,
        ))
        for index, line_id in enumerate(line_ids):
            canonical = {
                "external_line_id": f"SPARSE-{index}",
                "order_id": f"SPARSE-ORDER-{index}",
                "marketplace": "kaufland",
                "status": "sent",
                "details": {},
            }
            connection.execute(lines.insert().values(
                id=line_id,
                organization_id=account["organization_id"],
                seller_id=account["seller_id"],
                account_id=account["id"],
                environment="live",
                external_line_id=canonical["external_line_id"],
                order_id=canonical["order_id"],
                marketplace="kaufland",
                order_created_at=now,
                status="sent",
                storefront="de",
                search_text=f"sparse {index}",
                canonical_json=json.dumps(canonical),
                raw_json="{}",
                updated_at=now,
            ))
        connection.execute(selections.insert().values(
            id=selection_id,
            session_id=session_id,
            organization_id=account["organization_id"],
            seller_id=account["seller_id"],
            account_id=account["id"],
            environment="live",
            filter_hash="e" * 64,
            created_at=now,
            updated_at=now,
        ))

    command.upgrade(config, "20260910_0009")
    metadata = sa.MetaData()
    selections = sa.Table("seller_order_selections", metadata, autoload_with=engine)
    members = sa.Table("seller_order_selection_members", metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(selections.update().where(
            selections.c.id == selection_id,
        ).values(default_selected=True))
        # With sparse semantics this row is the one deselected exception.
        connection.execute(members.insert().values(
            selection_id=selection_id,
            line_id=line_ids[1],
        ))

    command.downgrade(config, "20260908_0008")
    restored = read_rows(engine, "seller_order_selection_members")
    assert {
        (str(row["selection_id"]), str(row["line_id"]))
        for row in restored
    } == {
        (selection_id, line_ids[0]),
        (selection_id, line_ids[2]),
    }

    # A following upgrade must preserve these now-explicit inclusions.
    command.upgrade(config, "20260910_0009")
    selection = read_rows(engine, "seller_order_selections")[0]
    assert selection["default_selected"] is False
    assert {
        str(row["line_id"])
        for row in read_rows(engine, "seller_order_selection_members")
    } == {line_ids[0], line_ids[2]}


def test_payment_migration_downgrade_keeps_tracking_added_to_legacy_shape(
    legacy_database,
):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    user = read_rows(engine, "auth_users")[0]
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    line_id = uuid4().hex
    now = datetime(2026, 9, 10, tzinfo=UTC)
    original = {
        "external_line_id": "TRACK-AFTER-UPGRADE",
        "order_id": "TRACK-AFTER-UPGRADE",
        "marketplace": "kaufland",
        "status": "open",
    }
    with engine.begin() as connection:
        connection.execute(lines.insert().values(
            id=line_id,
            organization_id=account["organization_id"],
            seller_id=account["seller_id"],
            account_id=account["id"],
            environment="live",
            external_line_id=original["external_line_id"],
            order_id=original["order_id"],
            marketplace="kaufland",
            order_created_at=now,
            status="open",
            storefront="de",
            search_text="track after upgrade",
            canonical_json=json.dumps(original),
            raw_json="{}",
            updated_at=now,
        ))

    command.upgrade(config, "20260910_0009")
    accounts = sa.Table(
        "seller_marketplace_accounts", sa.MetaData(), autoload_with=engine,
    )
    with engine.begin() as connection:
        connection.execute(accounts.update().where(
            accounts.c.id == account["id"],
        ).values(
            active=True,
            settings_json=json.dumps({
                "marketplace_hub_connection_v1": {"connection_status": "connected"},
            }),
        ))
    SqlOrdersRepository(engine).update_tracking(
        UUID(str(account["organization_id"])),
        UUID(str(account["seller_id"])),
        UUID(str(account["id"])),
        "live",
        carrier="DHL",
        tracking="NEW-TRACK",
        source="manual",
        actor_id=UUID(str(user["id"])),
        line_id=UUID(line_id),
    )
    migrated = read_rows(engine, "seller_order_lines")[0]
    assert migrated["payment_event_backup_json"] is not None
    assert json.loads(migrated["canonical_json"])["details"]["tracking"] == "NEW-TRACK"

    command.downgrade(config, "20260908_0008")
    restored = json.loads(read_rows(engine, "seller_order_lines")[0]["canonical_json"])
    assert restored["details"]["tracking"] == "NEW-TRACK"
    assert restored["details"]["tracking_source"] == "manual"
    assert restored["details"]["carrier"] == "DHL"
    assert restored["details"]["carrier_source"] == "manual"
    assert not {
        "payment_due_at", "payment_available", "payment_date_final",
        "ticket_delay_days", "ticket_open", "ticket_count", "open_ticket_count",
        "ticket_ids",
    } & restored["details"].keys()


def test_payment_migration_upgrade_resumes_after_partial_sqlite_ddl(legacy_database):
    config, engine = legacy_database
    seed_settings(engine)
    command.upgrade(config, "20260908_0008")
    account = read_rows(engine, "seller_marketplace_accounts")[0]
    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    originals = {
        "GOOD": {
            "external_line_id": "GOOD",
            "order_id": "GOOD",
            "marketplace": "kaufland",
            "status": "open",
        },
        "REPAIRED": {
            "external_line_id": "REPAIRED",
            "order_id": "REPAIRED",
            "marketplace": "kaufland",
            "status": "open",
            "details": None,
        },
    }
    with engine.begin() as connection:
        for identity, external_line_id, canonical_json in (
            ("0" * 31 + "1", "GOOD", json.dumps(originals["GOOD"])),
            ("f" * 32, "REPAIRED", "{invalid-json"),
        ):
            connection.execute(lines.insert().values(
                id=identity,
                organization_id=account["organization_id"],
                seller_id=account["seller_id"],
                account_id=account["id"],
                environment="live",
                external_line_id=external_line_id,
                order_id=external_line_id,
                marketplace="kaufland",
                order_created_at=now,
                status="open",
                storefront="de",
                search_text=external_line_id.lower(),
                canonical_json=canonical_json,
                raw_json="{}",
                updated_at=now,
            ))

    with pytest.raises(json.JSONDecodeError):
        command.upgrade(config, "20260910_0009")
    inspector = sa.inspect(engine)
    assert "seller_order_payment_tickets" in inspector.get_table_names()
    assert "default_selected" in {
        column["name"] for column in inspector.get_columns("seller_order_selections")
    }
    assert read_rows(engine, "alembic_version") == [{"version_num": "20260908_0008"}]

    partial_lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(partial_lines.update().where(
            partial_lines.c.external_line_id == "REPAIRED",
        ).values(canonical_json=json.dumps(originals["REPAIRED"])))

    command.upgrade(config, "20260910_0009")
    assert all(
        row["payment_event_backup_json"] is not None
        for row in read_rows(engine, "seller_order_lines")
    )
    command.downgrade(config, "20260908_0008")
    restored = {
        row["external_line_id"]: json.loads(row["canonical_json"])
        for row in read_rows(engine, "seller_order_lines")
    }
    assert restored == originals


def test_order_schema_boolean_server_defaults_are_false_on_sqlite():
    from marketplace_hub_core.auth.schema import metadata
    from marketplace_hub_core.orders.schema import order_lines, order_selections

    engine = sa.create_engine("sqlite://")
    try:
        metadata.create_all(engine)
        now = datetime(2026, 9, 10, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(order_selections.insert().values(
                id=uuid4(),
                session_id=uuid4(),
                organization_id=uuid4(),
                seller_id=uuid4(),
                account_id=uuid4(),
                environment="live",
                filter_hash="d" * 64,
                created_at=now,
                updated_at=now,
            ))
            connection.execute(order_lines.insert().values(
                id=uuid4(),
                organization_id=uuid4(),
                seller_id=uuid4(),
                account_id=uuid4(),
                environment="live",
                external_line_id="DEFAULTS",
                order_id="DEFAULTS",
                marketplace="kaufland",
                order_created_at=now,
                status="open",
                storefront="de",
                search_text="defaults",
                canonical_json="{}",
                raw_json="{}",
                updated_at=now,
                payment_projection_updated_at=now,
            ))
            assert connection.scalar(sa.select(order_selections.c.default_selected)) is False
            assert connection.execute(sa.select(
                order_lines.c.payment_available,
                order_lines.c.payment_date_final,
                order_lines.c.payment_ticket_open,
            )).one() == (False, False, False)
    finally:
        engine.dispose()
