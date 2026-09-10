"""Persist Kaufland payment tickets and SQL payment projections."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op

revision = "20260910_0009"
down_revision = "20260908_0008"
branch_labels = None
depends_on = None

EVENT_KEYS = (
    "received_at",
    "received_source",
    "shipped_at",
    "shipped_source",
    "released_at",
    "released_source",
)
DETAILS_SHAPE_KEY = "__details_shape__"
SHIPPED_ORDER_UNIT_STATUSES = {
    "sent",
    "sent_and_autopaid",
    "received",
    "returned",
    "returned_paid",
}
RECEIVED_TIMESTAMP_KEYS = (
    "order_received_timestamp_iso",
    "ts_received_iso",
    "received_at",
    "received_at_iso",
    "ts_delivered_iso",
    "delivered_at",
    "delivery_date",
)
SHIPPED_TIMESTAMP_KEYS = (
    "order_sent_timestamp_iso",
    "ts_sent_iso",
    "sent_at_iso",
    "sent_at",
    "ts_shipped_iso",
    "shipped_at_iso",
    "shipped_at",
)
PAYMENT_RELEASE_TIMESTAMP_KEYS = (
    "revenue_released_timestamp_iso",
    "revenue_released_at",
    "payout_timestamp_iso",
    "payout_at",
    "payment_timestamp_iso",
    "paid_at_iso",
    "paid_at",
)
LEGACY_RECEIVED_FALLBACK_SOURCE = "API Kaufland: aggiornamento allo stato Ricevuto"
PAYMENT_FRESHNESS_CONSTRAINT = "ck_order_lines_kaufland_payment_projection_fresh"
PAYMENT_FRESHNESS_INSERT_TRIGGER = "trg_order_lines_kaufland_payment_fresh_insert"
PAYMENT_FRESHNESS_UPDATE_TRIGGER = "trg_order_lines_kaufland_payment_fresh_update"
PAYMENT_FRESHNESS_EXPRESSION = (
    "marketplace <> 'kaufland' OR "
    "(payment_projection_updated_at IS NOT NULL "
    "AND payment_projection_updated_at = updated_at)"
)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _parse_timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_seconds(value) -> str:
    parsed = _parse_timestamp(value)
    return parsed.isoformat(timespec="seconds") if parsed else ""


def _received_timestamp(raw: dict, status: str) -> tuple[str, str]:
    if status not in SHIPPED_ORDER_UNIT_STATUSES:
        return "", ""
    for key in RECEIVED_TIMESTAMP_KEYS:
        value = _iso_seconds(raw.get(key))
        if value:
            return value, f"API Kaufland: {key}"
    return "", ""


def _shipped_timestamp(raw: dict, status: str) -> tuple[str, str]:
    if status not in SHIPPED_ORDER_UNIT_STATUSES:
        return "", ""
    for key in SHIPPED_TIMESTAMP_KEYS:
        value = _iso_seconds(raw.get(key))
        if value:
            return value, f"API Kaufland: {key}"
    if status == "sent":
        value = _iso_seconds(raw.get("ts_updated_iso"))
        if value:
            return value, "API Kaufland: passaggio a sent (ts_updated_iso)"
    return "", ""


def _released_timestamp(raw: dict, status: str) -> tuple[str, str]:
    for key in PAYMENT_RELEASE_TIMESTAMP_KEYS:
        value = _iso_seconds(raw.get(key))
        if value:
            return value, f"API Kaufland: {key}"
    if status == "sent_and_autopaid":
        value = _iso_seconds(raw.get("ts_updated_iso"))
        if value:
            return value, "API Kaufland: stato sent_and_autopaid (ts_updated_iso)"
    return "", ""


def _payment_events(raw: dict, status: str) -> dict[str, str]:
    received_at, received_source = _received_timestamp(raw, status)
    shipped_at, shipped_source = _shipped_timestamp(raw, status)
    released_at, released_source = _released_timestamp(raw, status)
    return {
        "received_at": received_at,
        "received_source": received_source,
        "shipped_at": shipped_at,
        "shipped_source": shipped_source,
        "released_at": released_at,
        "released_source": released_source,
    }


def _details(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _repair_payment_event_details(item: dict, raw: dict | None) -> dict:
    result = dict(item)
    details = _details(result.get("details"))
    if _text(result.get("marketplace")).lower() != "kaufland" or not isinstance(raw, dict):
        result["details"] = details
        return result
    events = _payment_events(raw, _text(result.get("status")).lower())
    if events["received_at"]:
        details["received_at"] = events["received_at"]
        details["received_source"] = events["received_source"]
    elif _text(details.get("received_source")) == LEGACY_RECEIVED_FALLBACK_SOURCE:
        details["received_at"] = None
        details["received_source"] = ""
    for prefix in ("shipped", "released"):
        value = events[f"{prefix}_at"]
        if value:
            details[f"{prefix}_at"] = value
            details[f"{prefix}_source"] = events[f"{prefix}_source"]
    result["details"] = details
    return result


def _payment_schedule(
    status: str,
    received_at: str = "",
    released_at: str = "",
    shipped_at: str = "",
    has_tracking: bool = False,
    *,
    current_time: datetime,
) -> dict:
    normalized_status = _text(status).lower()
    now = (
        current_time.replace(tzinfo=UTC)
        if current_time.tzinfo is None
        else current_time.astimezone(UTC)
    )
    release = _parse_timestamp(released_at)
    if release is not None:
        days = (release.date() - now.date()).days
        label = (
            f"Disponibile tra {days} giorni"
            if days > 0
            else "Disponibile oggi"
            if days == 0
            else f"Disponibile da {abs(days)} giorni"
        )
        return {
            "payment_due_at": release.isoformat(timespec="seconds"),
            "payment_days_remaining": days,
            "payment_available": days <= 0 or normalized_status == "sent_and_autopaid",
            "payment_status": label,
            "payment_date_final": True,
            "payment_rule": "Data effettiva comunicata da Kaufland",
            "ticket_delay_days": 0.0,
            "ticket_open": False,
        }
    if normalized_status == "sent_and_autopaid":
        return {
            "payment_due_at": "",
            "payment_days_remaining": None,
            "payment_available": True,
            "payment_status": "Ricavato già disponibile · data non disponibile",
            "payment_date_final": False,
            "payment_rule": "Pagamento confermato da Kaufland",
            "ticket_delay_days": 0.0,
            "ticket_open": False,
        }

    delivery = _parse_timestamp(received_at)
    shipped = _parse_timestamp(shipped_at)
    if has_tracking:
        if delivery is None:
            return {
                "payment_due_at": "",
                "payment_days_remaining": None,
                "payment_available": False,
                "payment_status": "Tracking presente · consegna non ancora rilevata",
                "payment_date_final": False,
                "payment_rule": "Con tracking: consegna + 14 giorni",
                "ticket_delay_days": 0.0,
                "ticket_open": False,
            }
        due = delivery + timedelta(days=14)
        rule = "Con tracking: consegna + 14 giorni"
    elif normalized_status in SHIPPED_ORDER_UNIT_STATUSES:
        if shipped is None:
            return {
                "payment_due_at": "",
                "payment_days_remaining": None,
                "payment_available": False,
                "payment_status": "Data di spedizione non disponibile",
                "payment_date_final": False,
                "payment_rule": "Senza tracking: spedizione + 21 giorni",
                "ticket_delay_days": 0.0,
                "ticket_open": False,
            }
        due = shipped + timedelta(days=21)
        rule = "Senza tracking: spedizione + 21 giorni"
    else:
        return {
            "payment_due_at": "",
            "payment_days_remaining": None,
            "payment_available": False,
            "payment_status": "Non ancora spedito",
            "payment_date_final": False,
            "payment_rule": "In attesa della spedizione",
            "ticket_delay_days": 0.0,
            "ticket_open": False,
        }
    days = (due.date() - now.date()).days
    label = (
        f"Tra {days} giorni"
        if days > 0
        else "Disponibile oggi"
        if days == 0
        else f"Disponibile da {abs(days)} giorni"
    )
    return {
        "payment_due_at": due.isoformat(timespec="seconds"),
        "payment_days_remaining": days,
        "payment_available": days <= 0,
        "payment_status": label,
        "payment_date_final": True,
        "payment_rule": rule,
        "ticket_delay_days": 0.0,
        "ticket_open": False,
    }


def _apply_payment_details(item: dict, *, current_time: datetime) -> dict:
    result = dict(item)
    details = _details(result.get("details"))
    if _text(result.get("marketplace")).lower() != "kaufland":
        result["details"] = details
        return result
    payment = _payment_schedule(
        _text(result.get("status")),
        received_at=_text(details.get("received_at")),
        released_at=_text(details.get("released_at")),
        shipped_at=_text(details.get("shipped_at")),
        has_tracking=bool(_text(details.get("tracking"))),
        current_time=current_time,
    )
    details.update(payment)
    details["payment_source"] = _text(details.get("released_source")) or payment[
        "payment_rule"
    ]
    details["ticket_count"] = 0
    details["open_ticket_count"] = 0
    details["ticket_ids"] = []
    result["details"] = details
    return result


def _number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and abs(result) < Decimal("1e30") else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _project_order(item: dict) -> dict:
    details = _details(item.get("details"))
    return {
        "currency": str(item.get("currency") or "").strip(),
        "carrier": str(details.get("carrier") or "").strip(),
        "has_tracking": bool(str(details.get("tracking") or "").strip()),
        "has_commission": _number(item.get("commission_amount")) is not None,
        "sale_eur": _number(item.get("sale_amount_eur")),
        "commission_eur": _number(item.get("commission_amount_eur")),
        "payout_eur": _number(item.get("payout_amount_eur")),
        "purchase_eur": _number(item.get("purchase_cost_eur", item.get("purchase_cost"))),
        "profit_eur": _number(item.get("profit_amount_eur", item.get("profit_amount"))),
        "quantity": max(1, int(_number(item.get("quantity")) or 1)),
        "excluded": str(item.get("status") or "").casefold() in {"cancelled", "canceled"}
        or bool(details.get("excluded_from_totals")),
        "catalog_cost": str(item.get("purchase_cost_source") or "").startswith(
            "Listino pubblicato"
        ),
        "payment_due_at": _parse_timestamp(details.get("payment_due_at")),
        "payment_available": bool(details.get("payment_available")),
        "payment_date_final": bool(details.get("payment_date_final")),
        "payment_ticket_open": bool(details.get("ticket_open")),
        "payment_ticket_delay_days": _number(details.get("ticket_delay_days"))
        or Decimal("0"),
    }


def _backup_details(item: dict) -> dict:
    original = item.get("details")
    is_mapping = isinstance(original, dict)
    backup = {
        DETAILS_SHAPE_KEY: {
            "present": "details" in item,
            "mapping": is_mapping,
            "value": None if is_mapping else original,
        }
    }
    original_details = original if is_mapping else {}
    backup.update({
        key: {"present": key in original_details, "value": original_details.get(key)}
        for key in EVENT_KEYS
    })
    return backup


def _restore_event_details(details: dict, backup: dict) -> None:
    for key in EVENT_KEYS:
        entry = backup.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("present"), bool):
            continue
        if entry["present"]:
            details[key] = entry.get("value")
        else:
            details.pop(key, None)


def _payment_freshness_constraint_exists(connection) -> bool:
    return PAYMENT_FRESHNESS_CONSTRAINT in {
        constraint.get("name")
        for constraint in sa.inspect(connection).get_check_constraints(
            "seller_order_lines"
        )
    }


def _create_payment_freshness_constraint(connection) -> None:
    if connection.dialect.name == "sqlite":
        message = "stale Kaufland payment projection"
        connection.exec_driver_sql(f"""
            CREATE TRIGGER IF NOT EXISTS {PAYMENT_FRESHNESS_INSERT_TRIGGER}
            BEFORE INSERT ON seller_order_lines
            WHEN NEW.marketplace = 'kaufland'
             AND (
                NEW.payment_projection_updated_at IS NULL
                OR NEW.payment_projection_updated_at <> NEW.updated_at
             )
            BEGIN
                SELECT RAISE(ABORT, '{message}');
            END
        """)
        connection.exec_driver_sql(f"""
            CREATE TRIGGER IF NOT EXISTS {PAYMENT_FRESHNESS_UPDATE_TRIGGER}
            BEFORE UPDATE OF updated_at, payment_projection_updated_at
            ON seller_order_lines
            WHEN NEW.marketplace = 'kaufland'
             AND (
                NEW.payment_projection_updated_at IS NULL
                OR NEW.payment_projection_updated_at <> NEW.updated_at
             )
            BEGIN
                SELECT RAISE(ABORT, '{message}');
            END
        """)
        return
    if _payment_freshness_constraint_exists(connection):
        return
    op.create_check_constraint(
        PAYMENT_FRESHNESS_CONSTRAINT,
        "seller_order_lines",
        PAYMENT_FRESHNESS_EXPRESSION,
        postgresql_not_valid=(connection.dialect.name == "postgresql"),
    )


def _validate_payment_freshness_constraint(connection) -> None:
    if (
        connection.dialect.name == "postgresql"
        and _payment_freshness_constraint_exists(connection)
    ):
        connection.exec_driver_sql(
            "ALTER TABLE seller_order_lines VALIDATE CONSTRAINT "
            f"{PAYMENT_FRESHNESS_CONSTRAINT}"
        )


def _drop_payment_freshness_constraint(connection) -> None:
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql(
            f"DROP TRIGGER IF EXISTS {PAYMENT_FRESHNESS_UPDATE_TRIGGER}"
        )
        connection.exec_driver_sql(
            f"DROP TRIGGER IF EXISTS {PAYMENT_FRESHNESS_INSERT_TRIGGER}"
        )
        return
    if not _payment_freshness_constraint_exists(connection):
        return
    op.drop_constraint(
        PAYMENT_FRESHNESS_CONSTRAINT,
        "seller_order_lines",
        type_="check",
    )


def upgrade() -> None:
    connection = op.get_bind()
    # Alembic treats SQLite DDL as non-transactional. A failed data backfill can
    # therefore leave this schema unstamped but partly upgraded; every DDL step
    # and row backup below must be safe to resume.
    selection_columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("seller_order_selections")
    }
    if "default_selected" not in selection_columns:
        op.add_column(
            "seller_order_selections",
            sa.Column(
                "default_selected", sa.Boolean(), nullable=False, server_default=sa.false(),
            ),
        )

    existing_line_columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("seller_order_lines")
    }
    payment_columns = (
        sa.Column("payment_projection_updated_at", sa.DateTime(timezone=True)),
        sa.Column("payment_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "payment_available", sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "payment_date_final", sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "payment_ticket_open", sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "payment_ticket_delay_days",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("payment_event_backup_json", sa.Text(), nullable=True),
    )
    for column in payment_columns:
        if column.name not in existing_line_columns:
            op.add_column("seller_order_lines", column)
    if connection.dialect.name in {"sqlite", "postgresql"}:
        # SQLite cannot add a CHECK without recreating the parent table. A
        # recreate can cascade-delete tracking/selection children when foreign
        # keys are enabled, so equivalent idempotent triggers guard new writes.
        # PostgreSQL's NOT VALID check avoids scanning legacy rows while still
        # rejecting stale writes throughout the backfill.
        _create_payment_freshness_constraint(connection)

    ticket_table_name = "seller_order_payment_tickets"
    if not sa.inspect(connection).has_table(ticket_table_name):
        op.create_table(
            ticket_table_name,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "organization_id",
                sa.Uuid(),
                sa.ForeignKey("organizations.id"),
                nullable=False,
            ),
            sa.Column(
                "seller_id", sa.Uuid(), sa.ForeignKey("seller_profiles.id"), nullable=False,
            ),
            sa.Column("account_id", sa.Uuid(), nullable=False),
            sa.Column("environment", sa.String(16), nullable=False),
            sa.Column("external_ticket_id", sa.Text(), nullable=False),
            sa.Column("order_unit_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("marketplace_created_at", sa.DateTime(timezone=True)),
            sa.Column("marketplace_updated_at", sa.DateTime(timezone=True)),
            sa.Column("status", sa.String(64), nullable=False, server_default=""),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "seller_id",
                "account_id",
                "environment",
                "external_ticket_id",
                name="uq_order_payment_ticket_scope",
            ),
        )
    ticket_indexes = {
        index["name"]
        for index in sa.inspect(connection).get_indexes(ticket_table_name)
    }
    if "ix_order_payment_tickets_scope_status" not in ticket_indexes:
        op.create_index(
            "ix_order_payment_tickets_scope_status",
            ticket_table_name,
            ["organization_id", "seller_id", "account_id", "environment", "status"],
        )

    lines = sa.Table("seller_order_lines", sa.MetaData(), autoload_with=connection)
    now, last = datetime.now(UTC), None
    while True:
        query = sa.select(
            lines.c.id,
            lines.c.canonical_json,
            lines.c.raw_json,
            lines.c.marketplace,
            lines.c.updated_at,
            lines.c.payment_event_backup_json,
        )
        if last is not None:
            query = query.where(lines.c.id > last)
        rows = connection.execute(query.order_by(lines.c.id).limit(100)).mappings().all()
        if not rows:
            break
        for row in rows:
            original = json.loads(row["canonical_json"])
            backup_json = row["payment_event_backup_json"]
            # A non-NULL backup belongs to an earlier attempt. Re-project the
            # current row, but never replace the pre-0009 rollback state.
            if backup_json is None:
                backup_json = json.dumps(
                    _backup_details(original),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                raw = {}
            repaired = _repair_payment_event_details(original, raw)
            public = _apply_payment_details(repaired, current_time=now)
            connection.execute(lines.update().where(lines.c.id == row["id"]).values(
                **_project_order(public),
                canonical_json=json.dumps(public, ensure_ascii=False, allow_nan=False),
                payment_event_backup_json=backup_json,
                payment_projection_updated_at=(
                    row["updated_at"] if row["marketplace"] == "kaufland" else None
                ),
            ))
        last = rows[-1]["id"]
    if connection.dialect.name == "postgresql":
        _validate_payment_freshness_constraint(connection)
    elif connection.dialect.name != "sqlite":
        _create_payment_freshness_constraint(connection)


def downgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    lines = sa.Table("seller_order_lines", metadata, autoload_with=connection)
    selections = sa.Table("seller_order_selections", metadata, autoload_with=connection)
    members = sa.Table("seller_order_selection_members", metadata, autoload_with=connection)
    payment_keys = {
        "payment_due_at", "payment_days_remaining", "payment_available",
        "payment_date_final", "payment_status", "payment_rule", "payment_source",
        "ticket_delay_days", "ticket_open", "ticket_count", "open_ticket_count",
        "ticket_ids",
    }
    for row in connection.execute(sa.select(
        lines.c.id, lines.c.canonical_json, lines.c.payment_event_backup_json,
    )).mappings():
        public = json.loads(row["canonical_json"])
        details = _details(public.get("details"))
        for key in payment_keys:
            details.pop(key, None)
        try:
            backup = json.loads(row["payment_event_backup_json"] or "{}")
        except (TypeError, ValueError):
            backup = {}
        shape = backup.get(DETAILS_SHAPE_KEY) if isinstance(backup, dict) else None
        has_new_details = any(key not in EVENT_KEYS for key in details)
        if (
            isinstance(backup, dict)
            and isinstance(shape, dict)
            and (
                shape.get("mapping") is True
                or (shape.get("mapping") is False and has_new_details)
            )
        ):
            _restore_event_details(details, backup)
            public["details"] = details
        elif isinstance(shape, dict) and shape.get("present") is False:
            public.pop("details", None)
        elif isinstance(shape, dict) and shape.get("mapping") is False:
            public["details"] = shape.get("value")
        else:
            # Backups produced by an earlier 0009 revision did not record the
            # details shape. Retain its original event-only rollback behavior.
            if isinstance(backup, dict):
                _restore_event_details(details, backup)
            public["details"] = details
        connection.execute(lines.update().where(lines.c.id == row["id"]).values(
            canonical_json=json.dumps(public, ensure_ascii=False, allow_nan=False),
        ))

    # Before 0009 every member row meant "selected".  In 0009 a selection with
    # default_selected=true stores only its deselected exceptions.  Materialize
    # the inverse while all scope columns are still available so a rolling
    # rollback cannot silently export the deselected rows instead.
    rollback_table_name = "_b204_order_selection_rollback_members"
    op.create_table(
        rollback_table_name,
        sa.Column("selection_id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
    )
    rollback_members = sa.Table(rollback_table_name, sa.MetaData(), autoload_with=connection)
    exceptions = members.alias("sparse_selection_exceptions")
    same_scope = sa.and_(
        lines.c.organization_id == selections.c.organization_id,
        lines.c.seller_id == selections.c.seller_id,
        lines.c.account_id == selections.c.account_id,
        lines.c.environment == selections.c.environment,
    )
    selected_rows = sa.select(
        selections.c.id.label("selection_id"),
        lines.c.id.label("line_id"),
    ).select_from(selections.join(lines, same_scope)).where(
        selections.c.default_selected.is_(True),
        ~sa.exists(sa.select(1).select_from(exceptions).where(
            exceptions.c.selection_id == selections.c.id,
            exceptions.c.line_id == lines.c.id,
        )),
    )
    connection.execute(rollback_members.insert().from_select(
        ["selection_id", "line_id"], selected_rows,
    ))
    sparse_selection_ids = sa.select(selections.c.id).where(
        selections.c.default_selected.is_(True),
    )
    connection.execute(members.delete().where(
        members.c.selection_id.in_(sparse_selection_ids),
    ))
    connection.execute(members.insert().from_select(
        ["selection_id", "line_id"],
        sa.select(rollback_members.c.selection_id, rollback_members.c.line_id),
    ))
    op.drop_table(rollback_table_name)
    op.drop_index(
        "ix_order_payment_tickets_scope_status", table_name="seller_order_payment_tickets",
    )
    op.drop_table("seller_order_payment_tickets")
    op.drop_column("seller_order_selections", "default_selected")
    _drop_payment_freshness_constraint(connection)
    for column in (
        "payment_event_backup_json", "payment_ticket_delay_days", "payment_ticket_open",
        "payment_date_final", "payment_available", "payment_due_at",
        "payment_projection_updated_at",
    ):
        op.drop_column("seller_order_lines", column)
