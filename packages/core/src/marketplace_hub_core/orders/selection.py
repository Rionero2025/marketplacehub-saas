import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from tempfile import TemporaryFile
from uuid import uuid4

from sqlalchemy import and_, case, exists, func, literal, or_, select, tuple_
from sqlalchemy.exc import DBAPIError

from marketplace_hub_core.auth.schema import auth_sessions
from marketplace_hub_core.orders.repository import OrdersNotFoundError
from marketplace_hub_core.orders.schema import order_lines as lines
from marketplace_hub_core.orders.schema import order_selection_members as members
from marketplace_hub_core.orders.schema import order_selections as selections


class _ExportRows:
    """Materialize one authorized snapshot to a private disk-backed stream."""

    def __init__(self, owner, session_id, scope, criteria, selection_id, kind, current_time):
        self.owner = owner
        self.selected_count = 0
        self._file = TemporaryFile(mode="w+b", prefix="marketplace-hub-orders-")
        try:
            self._prepare(
                session_id, scope, criteria, selection_id, kind, current_time,
            )
        except BaseException:
            self.close()
            raise

    def _prepare(self, session_id, scope, criteria, selection_id, kind, current_time):
        with self.owner.engine.connect() as connection:
            if self.owner.engine.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                predicates = self.owner._predicates(
                    scope, criteria, current_time=current_time,
                )
                selection = connection.execute(select(
                    selections.c.default_selected,
                ).where(
                    selections.c.id == selection_id,
                    selections.c.session_id == session_id,
                    *self.owner.repository._scope(selections, *scope),
                    selections.c.filter_hash == criteria.fingerprint(),
                )).mappings().first()
                if selection is None:
                    raise OrdersNotFoundError("Selezione non disponibile.")
                selected = self.owner._selected_predicate(
                    selection_id, bool(selection["default_selected"]),
                    alias_name="export_count_members",
                )
                self.selected_count = int(connection.scalar(
                    select(func.count()).select_from(lines).where(*predicates, selected)
                ) or 0)
                if self.selected_count == 0:
                    return

                export_predicates = list(predicates)
                if kind == "selected":
                    export_predicates.append(self.owner._selected_predicate(
                        selection_id, bool(selection["default_selected"]),
                        alias_name="export_members",
                    ))
                cursor = connection.execution_options(
                    stream_results=True, yield_per=100,
                ).execute(select(lines).where(*export_predicates).order_by(
                    lines.c.order_created_at.desc().nulls_last(), lines.c.order_id,
                    lines.c.external_line_id,
                )).mappings()
                context = None
                try:
                    for batch in cursor.partitions(100):
                        if context is None and any(
                            row["marketplace"] == "kaufland" for row in batch
                        ):
                            context = self.owner.repository.payment_context(
                                connection, scope, current_time=current_time,
                            )
                        enriched = self.owner.repository.enrich_payment_rows(
                            connection, scope, batch, context=context,
                        )
                        for row in enriched:
                            payload = row["canonical_json"].encode("utf-8")
                            self._file.write(len(payload).to_bytes(8, "big"))
                            self._file.write(payload)
                finally:
                    cursor.close()
        # No database connection remains checked out while authentication guards
        # run. Reading later is bounded to one response batch.
        self._file.seek(0)

    def __iter__(self):
        return self

    def __next__(self):
        if self._file.closed:
            raise StopIteration
        batch = []
        for _ in range(100):
            size = self._file.read(8)
            if not size:
                break
            if len(size) != 8:
                self.close()
                raise RuntimeError("Export temporaneo non valido.")
            length = int.from_bytes(size, "big")
            payload = self._file.read(length)
            if len(payload) != length:
                self.close()
                raise RuntimeError("Export temporaneo incompleto.")
            batch.append((payload.decode("utf-8"),))
        if batch:
            return batch
        self.close()
        raise StopIteration

    def close(self):
        self._file.close()


class SqlOrderSelections:
    def __init__(self, repository):
        self.repository, self.engine = repository, repository.engine

    def _transaction(self, operation):
        # A worker may upsert between list/count/summary SQL statements. PostgreSQL
        # must expose one snapshot; row locks also serialize checkbox mutations.
        for attempt in range(3):
            try:
                with self.engine.connect() as connection:
                    if self.engine.dialect.name == "postgresql":
                        connection = connection.execution_options(isolation_level="REPEATABLE READ")
                    with connection.begin():
                        return operation(connection)
            except DBAPIError as exc:
                code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
                if code not in {"40001", "40P01"} or attempt == 2:
                    raise

    def purge_stale(
        self, *, current_time=None, limit=50, member_limit=1_000, exclude_session_id=None,
    ):
        """Delete a bounded batch of stale UI state while retaining auth audit rows."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Il limite di pulizia deve essere un intero positivo.")
        if isinstance(member_limit, bool) or not isinstance(member_limit, int) or member_limit < 1:
            raise ValueError("Il limite membri deve essere un intero positivo.")

        def operation(connection):
            now = current_time or datetime.now(UTC)
            stale_condition = or_(
                auth_sessions.c.expires_at <= now,
                auth_sessions.c.revoked_at.is_not(None),
            )
            stale_members = select(
                members.c.selection_id, members.c.line_id,
            ).select_from(members.join(
                selections, selections.c.id == members.c.selection_id,
            ).join(
                auth_sessions, auth_sessions.c.id == selections.c.session_id,
            )).where(stale_condition).order_by(
                members.c.selection_id, members.c.line_id,
            ).limit(member_limit)
            if exclude_session_id is not None:
                stale_members = stale_members.where(
                    selections.c.session_id != exclude_session_id,
                )
            if self.engine.dialect.name == "postgresql":
                stale_members = stale_members.with_for_update(of=members, skip_locked=True)
            connection.execute(members.delete().where(tuple_(
                members.c.selection_id, members.c.line_id,
            ).in_(stale_members)))

            stale = select(selections.c.id).join(
                auth_sessions, auth_sessions.c.id == selections.c.session_id,
            ).where(
                stale_condition,
                ~exists(select(1).where(members.c.selection_id == selections.c.id)),
            ).order_by(selections.c.created_at, selections.c.id).limit(limit)
            if exclude_session_id is not None:
                stale = stale.where(selections.c.session_id != exclude_session_id)
            if self.engine.dialect.name == "postgresql":
                stale = stale.with_for_update(of=selections, skip_locked=True)
            identities = list(connection.scalars(stale))
            if identities:
                connection.execute(selections.delete().where(selections.c.id.in_(identities)))
            return len(identities)

        return self._transaction(operation)

    def list(self, session_id, scope, criteria, page, page_size, *, payments=False):
        def operation(connection):
            current_time = datetime.now(UTC)
            identity, predicates, default_selected = self._load_orders(
                connection, session_id, scope, criteria, current_time=current_time,
            )
            rows, total, options = self.repository.list(
                *scope, criteria=criteria, page=page, page_size=page_size, connection=connection,
                current_time=current_time,
            )
            page_ids = [row["id"] for row in rows]
            payload = self._payload(
                connection, identity, predicates, default_selected, page_ids,
                purpose="orders", current_time=current_time,
            )
            payment_payload = None
            if payments:
                payment_id, payment_predicates, payment_default = self._load_payments(
                    connection, session_id, scope, criteria, identity, predicates,
                    default_selected, current_time=current_time,
                )
                payment_payload = self._payload(
                    connection, payment_id, payment_predicates, payment_default, page_ids,
                    purpose="payments", current_time=current_time,
                )
            return rows, total, options, payload, payment_payload

        return self._transaction(operation)

    def _insert(self, table):
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        return insert(table)

    def _predicates(self, scope, criteria, *, current_time):
        return [
            *self.repository._scope(lines, *scope),
            *self.repository.filters(criteria, current_time=current_time),
        ]

    @staticmethod
    def _payments_hash(criteria, orders_selection_id):
        raw = f"payments:{orders_selection_id}:{criteria.fingerprint()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _selection(self, connection, session_id, scope, filter_hash, predicates, *,
                   current_time, selection_id=None, default_initial=False):
        conditions = [
            selections.c.session_id == session_id,
            *self.repository._scope(selections, *scope),
            selections.c.filter_hash == filter_hash,
        ]
        if selection_id:
            conditions.append(selections.c.id == selection_id)
        row = connection.execute(select(selections).where(*conditions).with_for_update()
                                 ).mappings().first()
        if row is None and selection_id is None:
            connection.execute(self._insert(selections).values(
                id=uuid4(), session_id=session_id, organization_id=scope[0], seller_id=scope[1],
                account_id=scope[2], environment=scope[3], filter_hash=filter_hash,
                default_selected=default_initial,
                created_at=current_time, updated_at=current_time,
            ).on_conflict_do_nothing())
            row = connection.execute(select(selections).where(*conditions).with_for_update()
                                     ).mappings().first()
        if row is None:
            raise OrdersNotFoundError("Selezione non disponibile. Aggiorna l'elenco ordini.")
        return row["id"], predicates, bool(row["default_selected"])

    def _load_orders(self, connection, session_id, scope, criteria, selection_id=None, *,
                     current_time):
        return self._selection(
            connection, session_id, scope, criteria.fingerprint(),
            self._predicates(scope, criteria, current_time=current_time),
            current_time=current_time, selection_id=selection_id, default_initial=True,
        )

    @staticmethod
    def _selected_predicate(selection_id, default_selected, *, alias_name="selection_members"):
        selected_members = members.alias(alias_name)
        exception = exists(select(1).select_from(selected_members).where(
            selected_members.c.selection_id == selection_id,
            selected_members.c.line_id == lines.c.id,
        ))
        return ~exception if default_selected else exception

    def _payment_predicates(self, predicates, orders_selection_id, orders_default_selected):
        return [
            *predicates,
            self._selected_predicate(
                orders_selection_id, orders_default_selected,
                alias_name="orders_selection_members",
            ),
        ]

    def _load_payments(self, connection, session_id, scope, criteria, orders_selection_id,
                       order_predicates=None, orders_default_selected=None, selection_id=None, *,
                       current_time):
        if order_predicates is None:
            orders_selection_id, order_predicates, orders_default_selected = self._load_orders(
                connection, session_id, scope, criteria, orders_selection_id,
                current_time=current_time,
            )
        predicates = self._payment_predicates(
            order_predicates, orders_selection_id, bool(orders_default_selected),
        )
        return self._selection(
            connection, session_id, scope,
            self._payments_hash(criteria, orders_selection_id), predicates,
            current_time=current_time, selection_id=selection_id, default_initial=False,
        )

    def _clean_payment_after_orders_change(
        self, connection, session_id, scope, criteria, orders_selection_id, *,
        action, line_id, selected, current_time,
    ):
        payment = connection.execute(select(
            selections.c.id, selections.c.default_selected,
        ).where(
            selections.c.session_id == session_id,
            *self.repository._scope(selections, *scope),
            selections.c.filter_hash == self._payments_hash(criteria, orders_selection_id),
        ).with_for_update()).mappings().first()
        if payment is None:
            return
        payment_id = payment["id"]
        if action == "clear":
            connection.execute(members.delete().where(members.c.selection_id == payment_id))
            connection.execute(selections.update().where(selections.c.id == payment_id).values(
                default_selected=False, updated_at=current_time,
            ))
        elif action == "set" and selected is False and line_id is not None:
            if payment["default_selected"]:
                connection.execute(self._insert(members).values(
                    selection_id=payment_id, line_id=line_id,
                ).on_conflict_do_nothing())
            else:
                connection.execute(members.delete().where(
                    members.c.selection_id == payment_id, members.c.line_id == line_id,
                ))

    def _summary(
        self, connection, identity, predicates, default_selected, *,
        include_payment=False, current_time,
    ):
        where = [
            *predicates,
            self._selected_predicate(identity, default_selected, alias_name="summary_members"),
        ]
        active = lines.c.excluded.is_(False)
        complete = and_(lines.c.sale_eur.is_not(None), lines.c.commission_eur.is_not(None),
                        lines.c.payout_eur.is_not(None))
        known = and_(active, lines.c.purchase_eur.is_not(None), lines.c.profit_eur.is_not(None))

        def count(condition, name):
            return func.coalesce(func.sum(case((condition, 1), else_=0)), 0).label(name)

        def total(condition, column, name):
            return func.coalesce(func.sum(case((condition, column), else_=0)), 0).label(name)

        payout_total_condition = (
            active & lines.c.payout_eur.is_not(None)
            if include_payment
            else active & complete
        )
        columns = [
            func.count().label("selected_rows"),
            func.count(func.distinct(lines.c.order_id)).label("distinct_orders"),
            func.coalesce(func.sum(lines.c.quantity), 0).label("quantity"),
            count(~active, "cancelled_rows"),
            count(complete | ~active, "complete_economic_rows"),
            count(active & ~complete, "missing_economic_rows"),
            count(known, "known_cost_rows"), count(active & ~known, "missing_cost_rows"),
            count(known & (lines.c.profit_eur < 0), "loss_rows"),
            count(known & lines.c.catalog_cost.is_(False), "sku_cost_rows"),
            count(known & lines.c.catalog_cost.is_(True), "catalog_cost_rows"),
            total(active & complete, lines.c.sale_eur, "sale_amount_eur"),
            total(active & complete, lines.c.commission_eur, "commission_amount_eur"),
            total(payout_total_condition, lines.c.payout_eur, "payout_amount_eur"),
            total(known, lines.c.purchase_eur, "purchase_cost_eur"),
            total(known, lines.c.profit_eur, "profit_amount_eur"),
        ]
        if include_payment:
            available_expression = self.repository.payment_available_expression(
                current_time=current_time,
            )
            payment_scheduled = and_(
                active, lines.c.payment_date_final.is_(True),
                lines.c.payment_due_at.is_not(None),
            )
            payment_available = and_(active, available_expression)
            payment_waiting = and_(active, ~available_expression)
            columns.extend([
                count(active, "payment_payable_rows"),
                count(payment_scheduled, "payment_scheduled_rows"),
                count(and_(active, ~payment_scheduled), "payment_unscheduled_rows"),
                count(payment_available, "payment_available_rows"),
                count(payment_waiting, "payment_waiting_rows"),
                func.max(case((payment_scheduled, lines.c.payment_due_at), else_=None)).label(
                    "latest_payment_due_at"
                ),
                total(payment_available, lines.c.payout_eur, "available_payout_eur"),
                total(payment_waiting, lines.c.payout_eur, "waiting_payout_eur"),
            ])
        result = dict(connection.execute(select(*columns).select_from(lines).where(*where)
                                         ).mappings().one())
        cost, profit = result["purchase_cost_eur"], result["profit_amount_eur"]
        result["profit_pct"] = f"{profit / cost * 100:.2f}" if cost > 0 else None
        if include_payment:
            unscheduled_ids = connection.scalars(select(lines.c.id).select_from(lines).where(
                *where, active, ~payment_scheduled,
            ).order_by(lines.c.id)).all()
            result["payment_unscheduled_ids"] = [str(value) for value in unscheduled_ids]
            result["payment_all_dates_known"] = bool(result["payment_payable_rows"]) and not bool(
                result["payment_unscheduled_rows"]
            )
            result["payment_all_available"] = bool(result["payment_payable_rows"]) and not bool(
                result["payment_waiting_rows"]
            )
            if result["latest_payment_due_at"]:
                latest = result["latest_payment_due_at"]
                latest = (
                    latest.replace(tzinfo=UTC) if latest.tzinfo is None
                    else latest.astimezone(UTC)
                )
                result["latest_payment_due_at"] = latest.isoformat()
        for key in tuple(result):
            if key.endswith("_eur"):
                result[key] = f"{Decimal(result[key]):.2f}"
        missing = connection.scalars(select(lines.c.currency).select_from(lines).where(
            *where, active, ~complete,
        ).distinct().order_by(lines.c.currency)).all()
        result["missing_currencies"] = [value or "Sconosciuta" for value in missing]
        return result

    def _payload(
        self, connection, identity, predicates, default_selected, page_ids, *,
        purpose, current_time,
    ):
        selected_ids = connection.scalars(select(lines.c.id).where(
            *predicates,
            lines.c.id.in_(page_ids),
            self._selected_predicate(identity, default_selected, alias_name="page_members"),
        )).all() if page_ids else []
        summary = self._summary(
            connection, identity, predicates, default_selected,
            include_payment=purpose == "payments", current_time=current_time,
        )
        return {
            "id": str(identity), "purpose": purpose,
            "selected_ids": [str(value) for value in selected_ids],
            "selected_count": summary["selected_rows"], "summary": summary,
            "filtered_count": connection.scalar(
                select(func.count()).select_from(lines).where(*predicates)
            ),
        }

    def get(self, session_id, scope, criteria, page_ids=(), *, current_time=None):
        def operation(connection):
            now = current_time or datetime.now(UTC)
            identity, predicates, default_selected = self._load_orders(
                connection, session_id, scope, criteria, current_time=now,
            )
            return self._payload(
                connection, identity, predicates, default_selected, page_ids,
                purpose="orders", current_time=now,
            )
        return self._transaction(operation)

    def change(self, session_id, scope, criteria, selection_id, action,
               line_id=None, selected=None, *, purpose="orders", orders_selection_id=None,
               payments_enabled=False):
        def operation(connection):
            current_time = datetime.now(UTC)
            if purpose == "payments":
                if not payments_enabled or orders_selection_id is None:
                    raise OrdersNotFoundError("Selezione pagamenti non disponibile.")
                identity, predicates, default_selected = self._load_payments(
                    connection, session_id, scope, criteria, orders_selection_id,
                    selection_id=selection_id, current_time=current_time,
                )
            else:
                identity, predicates, default_selected = self._load_orders(
                    connection, session_id, scope, criteria, selection_id,
                    current_time=current_time,
                )
            if action == "select_all":
                connection.execute(members.delete().where(members.c.selection_id == identity))
                if purpose == "payments":
                    connection.execute(self._insert(members).from_select(
                        ["selection_id", "line_id"],
                        select(
                            literal(identity, type_=members.c.selection_id.type), lines.c.id,
                        ).where(*predicates),
                    ).on_conflict_do_nothing())
                    # Payment membership is an explicit snapshot. Future main
                    # expansion and newly imported rows therefore stay unselected.
                    default_selected = False
                else:
                    default_selected = True
            elif action == "clear":
                connection.execute(members.delete().where(members.c.selection_id == identity))
                default_selected = False
            elif action == "set":
                visible = connection.scalar(select(lines.c.id).where(
                    lines.c.id == line_id, *predicates,
                ))
                if visible is None:
                    raise OrdersNotFoundError("Riga non presente nel filtro corrente.")
                if bool(selected) != default_selected:
                    connection.execute(self._insert(members).values(
                        selection_id=identity, line_id=line_id,
                    ).on_conflict_do_nothing())
                else:
                    connection.execute(members.delete().where(
                        members.c.selection_id == identity, members.c.line_id == line_id,
                    ))
            connection.execute(selections.update().where(selections.c.id == identity).values(
                default_selected=default_selected, updated_at=current_time,
            ))
            if purpose == "orders":
                self._clean_payment_after_orders_change(
                    connection, session_id, scope, criteria, identity,
                    action=action, line_id=line_id, selected=selected,
                    current_time=current_time,
                )
            return self._payload(
                connection, identity, predicates, default_selected, (),
                purpose=purpose, current_time=current_time,
            )
        return self._transaction(operation)

    def validate(self, session_id, scope, criteria, selection_id, *, current_time=None):
        with self.engine.begin() as connection:
            self._load_orders(
                connection, session_id, scope, criteria, selection_id,
                current_time=current_time or datetime.now(UTC),
            )

    def export_rows(
        self, session_id, scope, criteria, selection_id, kind, *, current_time=None,
    ):
        if kind not in {"selected", "filtered"}:
            raise ValueError("Tipo di esportazione non valido.")
        return _ExportRows(
            self, session_id, scope, criteria, selection_id, kind,
            current_time or datetime.now(UTC),
        )
