from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.exc import DBAPIError

from marketplace_hub_core.auth.schema import auth_sessions
from marketplace_hub_core.orders.repository import OrdersNotFoundError
from marketplace_hub_core.orders.schema import order_lines as lines
from marketplace_hub_core.orders.schema import order_selection_members as members
from marketplace_hub_core.orders.schema import order_selections as selections


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

    def list(self, session_id, scope, criteria, page, page_size):
        def operation(connection):
            identity, predicates = self._load(connection, session_id, scope, criteria)
            rows, total, options = self.repository.list(
                *scope, criteria=criteria, page=page, page_size=page_size, connection=connection,
            )
            payload = self._payload(connection, identity, predicates, [row["id"] for row in rows])
            return rows, total, options, payload
        return self._transaction(operation)

    def _insert(self, table):
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        return insert(table)

    def _predicates(self, scope, criteria):
        return [*self.repository._scope(lines, *scope), *self.repository.filters(criteria)]

    def _load(self, connection, session_id, scope, criteria, selection_id=None):
        user = select(auth_sessions.c.user_id).where(auth_sessions.c.id == session_id)
        stale_sessions = select(auth_sessions.c.id).where(
            auth_sessions.c.user_id == user.scalar_subquery(), or_(
                auth_sessions.c.expires_at <= datetime.now(UTC),
                auth_sessions.c.revoked_at.is_not(None),
            ),
        )
        connection.execute(selections.delete().where(selections.c.session_id.in_(stale_sessions)))
        self.repository.refresh_projections(connection, scope)
        conditions = [selections.c.session_id == session_id,
                      *self.repository._scope(selections, *scope),
                      selections.c.filter_hash == criteria.fingerprint()]
        if selection_id:
            conditions.append(selections.c.id == selection_id)
        created = False
        if selection_id is None:
            now = datetime.now(UTC)
            inserted = connection.execute(self._insert(selections).values(
                id=uuid4(), session_id=session_id, organization_id=scope[0], seller_id=scope[1],
                account_id=scope[2], environment=scope[3], filter_hash=criteria.fingerprint(),
                created_at=now, updated_at=now,
            ).on_conflict_do_nothing())
            created = inserted.rowcount == 1
        # Serialize actions for this session/filter. No mutation of order data.
        row = connection.execute(select(selections).where(*conditions).with_for_update()
                                 ).mappings().first()
        if row is None:
            raise OrdersNotFoundError("Selezione non disponibile. Aggiorna l'elenco ordini.")
        predicates = self._predicates(scope, criteria)
        identity = row["id"]
        if created:
            self._select_all(connection, identity, predicates)
        connection.execute(members.delete().where(
            members.c.selection_id == identity,
            ~exists(select(1).where(lines.c.id == members.c.line_id, *predicates)),
        ))
        return identity, predicates

    def _select_all(self, connection, identity, predicates):
        connection.execute(self._insert(members).from_select(
            ["selection_id", "line_id"], select(literal(identity, type_=selections.c.id.type),
                                                lines.c.id).where(*predicates),
        ).on_conflict_do_nothing())

    def _summary(self, connection, identity, predicates):
        query = lines.join(members, lines.c.id == members.c.line_id)
        where = [members.c.selection_id == identity, *predicates]
        active = lines.c.excluded.is_(False)
        complete = and_(lines.c.sale_eur.is_not(None), lines.c.commission_eur.is_not(None),
                        lines.c.payout_eur.is_not(None))
        known = and_(active, lines.c.purchase_eur.is_not(None), lines.c.profit_eur.is_not(None))

        def count(condition, name):
            return func.coalesce(func.sum(case((condition, 1), else_=0)), 0).label(name)

        def total(condition, column, name):
            return func.coalesce(func.sum(case((condition, column), else_=0)), 0).label(name)

        statement = select(
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
            total(active & complete, lines.c.payout_eur, "payout_amount_eur"),
            total(known, lines.c.purchase_eur, "purchase_cost_eur"),
            total(known, lines.c.profit_eur, "profit_amount_eur"),
        ).select_from(query).where(*where)
        result = dict(connection.execute(statement).mappings().one())
        cost, profit = result["purchase_cost_eur"], result["profit_amount_eur"]
        result["profit_pct"] = f"{profit / cost * 100:.2f}" if cost > 0 else None
        for key in tuple(result):
            if key.endswith("_eur"):
                result[key] = f"{Decimal(result[key]):.2f}"
        missing = connection.scalars(select(lines.c.currency).select_from(query).where(
            *where, active, ~complete,
        ).distinct().order_by(lines.c.currency)).all()
        result["missing_currencies"] = [value or "Sconosciuta" for value in missing]
        return result

    def _payload(self, connection, identity, predicates, page_ids):
        selected_ids = connection.scalars(select(members.c.line_id).where(
            members.c.selection_id == identity, members.c.line_id.in_(page_ids),
        )).all() if page_ids else []
        summary = self._summary(connection, identity, predicates)
        return {"id": str(identity), "selected_ids": [str(value) for value in selected_ids],
                "selected_count": summary["selected_rows"], "summary": summary,
                "filtered_count": connection.scalar(select(func.count()).select_from(lines)
                                                     .where(*predicates))}

    def get(self, session_id, scope, criteria, page_ids=()):
        def operation(connection):
            identity, predicates = self._load(connection, session_id, scope, criteria)
            return self._payload(connection, identity, predicates, page_ids)
        return self._transaction(operation)

    def change(self, session_id, scope, criteria, selection_id, action,
               line_id=None, selected=None):
        def operation(connection):
            identity, predicates = self._load(connection, session_id, scope, criteria, selection_id)
            if action == "select_all":
                self._select_all(connection, identity, predicates)
            elif action == "clear":
                connection.execute(members.delete().where(members.c.selection_id == identity))
            elif action == "set":
                visible = connection.scalar(select(lines.c.id).where(lines.c.id == line_id,
                                                                      *predicates))
                if visible is None:
                    raise OrdersNotFoundError("Riga non presente nel filtro corrente.")
                if selected:
                    connection.execute(self._insert(members).values(
                        selection_id=identity, line_id=line_id,
                    ).on_conflict_do_nothing())
                else:
                    connection.execute(members.delete().where(members.c.selection_id == identity,
                                                              members.c.line_id == line_id))
            connection.execute(selections.update().where(selections.c.id == identity)
                               .values(updated_at=datetime.now(UTC)))
            return self._payload(connection, identity, predicates, ())
        return self._transaction(operation)

    def validate(self, session_id, scope, criteria, selection_id):
        with self.engine.begin() as connection:
            self._load(connection, session_id, scope, criteria, selection_id)

    def export_rows(self, scope, criteria, selection_id, kind):
        predicates = self._predicates(scope, criteria)
        if kind == "selected":
            predicates.append(exists(select(1).where(members.c.selection_id == selection_id,
                                                      members.c.line_id == lines.c.id)))
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                self.repository.refresh_projections(connection, scope)
                cursor = connection.execution_options(stream_results=True, yield_per=100).execute(
                    select(lines.c.canonical_json).where(*predicates).order_by(
                        lines.c.order_created_at.desc().nulls_last(), lines.c.order_id,
                        lines.c.external_line_id,
                    ),
                )
                try:
                    yield from cursor.partitions(100)
                finally:
                    cursor.close()
