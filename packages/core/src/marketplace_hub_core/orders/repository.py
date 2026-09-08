import json
from collections import defaultdict
from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, bindparam, func, or_, select, tuple_
from sqlalchemy.exc import OperationalError

from marketplace_hub_core.marketplace_connections.repository import (
    VERIFICATION_KEY,
    settings_object,
)
from marketplace_hub_core.orders.filters import OrderFilters
from marketplace_hub_core.orders.projections import project_order
from marketplace_hub_core.orders.schema import order_lines as lines
from marketplace_hub_core.orders.schema import order_sync_jobs as jobs
from marketplace_hub_core.orders.schema import order_tracking_events as tracking_events
from marketplace_hub_core.orders.tracking import MAX_TRACKING_TARGET_UNITS, TrackingLimitError
from marketplace_hub_core.seller_settings.schema import seller_marketplace_accounts as accounts


def utc(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def timestamp(value):
    try:
        return utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


class OrdersNotFoundError(ValueError):
    pass


class OrdersAmbiguousMatchError(ValueError):
    pass


class OrdersImportBusyError(RuntimeError):
    pass


class OrdersAccountUnavailableError(ValueError):
    pass


class SqlOrdersRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def _scope(table, organization_id, seller_id, account_id, environment):
        return (table.c.organization_id == organization_id, table.c.seller_id == seller_id,
                table.c.account_id == account_id, table.c.environment == environment)

    @staticmethod
    def _execute_tracking_locked(connection, statement):
        statement = statement.with_for_update(
            nowait=connection.dialect.name == "postgresql",
        )
        try:
            return connection.execute(statement)
        except OperationalError as exc:
            if (
                connection.dialect.name == "postgresql"
                and getattr(exc.orig, "sqlstate", None) == "55P03"
            ):
                raise OrdersImportBusyError(
                    "Un’altra importazione tracking è già in corso per questo account. "
                    "Attendi e riprova."
                ) from exc
            raise

    @staticmethod
    def _lock_tracking_import(
        connection, organization_id, seller_id, account_id, *, fail_fast=True,
    ):
        conditions = (
            accounts.c.id == account_id,
            accounts.c.organization_id == organization_id,
            accounts.c.seller_id == seller_id,
        )
        if connection.dialect.name == "postgresql":
            try:
                locked = connection.execute(select(
                    accounts.c.id, accounts.c.active, accounts.c.marketplace,
                    accounts.c.settings_json,
                ).where(
                    *conditions,
                ).with_for_update(nowait=fail_fast)).mappings().first()
            except OperationalError as exc:
                if getattr(exc.orig, "sqlstate", None) == "55P03":
                    raise OrdersImportBusyError(
                        "Un’altra importazione tracking è già in corso per questo account. "
                        "Attendi e riprova."
                    ) from exc
                raise
        else:
            previous_timeout = None
            if fail_fast:
                previous_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
                connection.exec_driver_sql("PRAGMA busy_timeout = 0")
            try:
                acquired = connection.execute(accounts.update().where(*conditions).values(
                    updated_at=accounts.c.updated_at,
                ))
            except OperationalError as exc:
                if "locked" in str(exc).casefold():
                    raise OrdersImportBusyError(
                        "Un’altra importazione tracking è già in corso per questo account. "
                        "Attendi e riprova."
                    ) from exc
                raise
            finally:
                if previous_timeout is not None:
                    connection.exec_driver_sql(f"PRAGMA busy_timeout = {int(previous_timeout)}")
            locked = None if acquired.rowcount != 1 else connection.execute(select(
                accounts.c.id, accounts.c.active, accounts.c.marketplace,
                accounts.c.settings_json,
            ).where(*conditions)).mappings().first()
        if locked is None:
            raise OrdersNotFoundError("Account marketplace non disponibile.")
        verification = settings_object(locked["settings_json"]).get(VERIFICATION_KEY, {})
        if (
            not bool(locked["active"])
            or locked["marketplace"] != "kaufland"
            or not isinstance(verification, dict)
            or verification.get("connection_status") != "connected"
        ):
            raise OrdersAccountUnavailableError(
                "Verifica il marketplace prima di sincronizzare."
            )

    def refresh_projections(self, connection, scope):
        # A previous worker version can still commit during a rolling deploy.
        # Only stale projections are rebuilt; canonical/raw records stay untouched.
        last_id = None
        while True:
            conditions = [*self._scope(lines, *scope), or_(
                lines.c.projection_updated_at.is_(None),
                lines.c.projection_updated_at != lines.c.updated_at,
            )]
            if last_id is not None:
                conditions.append(lines.c.id > last_id)
            rows = connection.execute(select(lines.c.id, lines.c.canonical_json,
                                             lines.c.updated_at).where(*conditions)
                                      .order_by(lines.c.id).limit(100)).mappings().all()
            if not rows:
                return
            for row in rows:
                connection.execute(lines.update().where(
                    lines.c.id == row["id"], lines.c.updated_at == row["updated_at"],
                ).values(**project_order(json.loads(row["canonical_json"])),
                         projection_updated_at=row["updated_at"]))
            last_id = rows[-1]["id"]

    def list(self, organization_id, seller_id, account_id, environment, *, page, page_size,
             search="", status="", storefront="", date_from=None, date_to=None, criteria=None,
             connection=None):
        scope = self._scope(lines, organization_id, seller_id, account_id, environment)
        if criteria is None:
            criteria = OrderFilters(search=search,
                                    statuses=([status] if isinstance(status, str) else status)
                                    or None, storefronts=([storefront]
                                    if isinstance(storefront, str) else storefront) or None)
        filters = [*scope, *self.filters(criteria)]
        if date_from:
            filters.append(lines.c.order_created_at >= date_from)
        if date_to:
            filters.append(lines.c.order_created_at < date_to)
        with (nullcontext(connection) if connection is not None
              else self.engine.connect()) as connection:
            total = connection.scalar(select(func.count()).select_from(lines).where(*filters))
            rows = connection.execute(select(lines).where(*filters).order_by(
                lines.c.order_created_at.desc().nulls_last(), lines.c.order_id,
                lines.c.external_line_id,
            ).limit(page_size).offset((page - 1) * page_size)).mappings().all()
            statuses = connection.scalars(select(lines.c.status).where(*scope).distinct()
                                          .order_by(lines.c.status)).all()
            storefronts = connection.scalars(select(lines.c.storefront).where(*scope).distinct()
                                             .order_by(lines.c.storefront)).all()
            currencies = connection.scalars(select(lines.c.currency).where(*scope).distinct()
                                             .order_by(lines.c.currency)).all()
            carriers = connection.scalars(select(lines.c.carrier).where(*scope).distinct()
                                           .order_by(lines.c.carrier)).all()
            bounds = connection.execute(select(
                func.min(lines.c.order_created_at), func.max(lines.c.order_created_at),
                func.min(lines.c.sale_eur), func.max(lines.c.sale_eur),
            ).where(*scope)).one()
            return [dict(row) for row in rows], total, {
                "statuses": [s for s in statuses if s],
                "storefronts": [s for s in storefronts if s],
                "currencies": list(currencies), "carriers": [s for s in carriers if s],
                "date_min": utc(bounds[0]).date().isoformat() if bounds[0] else None,
                "date_max": utc(bounds[1]).date().isoformat() if bounds[1] else None,
                "amount_min": f"{max(0, bounds[2]):.2f}" if bounds[2] is not None else None,
                "amount_max": f"{max(0, bounds[3]):.2f}" if bounds[3] is not None else None,
            }

    @staticmethod
    def filters(criteria):
        result = []
        if criteria.search:
            result.append(lines.c.search_text.contains(criteria.search, autoescape=True))
        for name, values in (("status", criteria.statuses), ("storefront", criteria.storefronts),
                             ("currency", criteria.currencies)):
            if values is not None:
                result.append(lines.c[name].in_(values))
        if criteria.carriers:
            result.append(lines.c.carrier.in_(criteria.carriers))
        for field, value in (("has_tracking", criteria.tracking),
                             ("has_commission", criteria.commission)):
            if value != "all":
                result.append(lines.c[field].is_(value == "present"))
        start, end = criteria.dates()
        if start:
            result.append(lines.c.order_created_at >= start)
        if end:
            result.append(lines.c.order_created_at < end)
        if criteria.amount_min is not None:
            result.append(lines.c.sale_eur >= criteria.amount_min)
        if criteria.amount_max is not None:
            result.append(lines.c.sale_eur <= criteria.amount_max)
        return result

    def item(self, organization_id, seller_id, account_id, environment, line_id):
        with self.engine.connect() as connection:
            row = connection.execute(select(lines).where(
                *self._scope(lines, organization_id, seller_id, account_id, environment),
                lines.c.id == line_id,
            )).mappings().first()
            if row is None:
                raise OrdersNotFoundError("Riga ordine non disponibile.")
            return dict(row)

    def update_tracking(
        self, organization_id, seller_id, account_id, environment, *, carrier, tracking,
        source, actor_id, line_id=None, external_line_id="", order_id="", connection=None,
    ):
        """Update one unit, or every unit in an order, under an exact tenant scope."""
        conditions = list(self._scope(
            lines, organization_id, seller_id, account_id, environment,
        ))
        if line_id is not None:
            conditions.append(lines.c.id == line_id)
        elif external_line_id:
            conditions.append(lines.c.external_line_id == external_line_id)
        elif order_id:
            conditions.append(lines.c.order_id == order_id)
        else:
            raise OrdersNotFoundError("Indica una riga o un ordine da aggiornare.")
        context = nullcontext(connection) if connection is not None else self.engine.begin()
        with context as active:
            self._lock_tracking_import(
                active, organization_id, seller_id, account_id,
            )
            found = self._execute_tracking_locked(
                active, select(lines).where(*conditions),
            ).mappings().all()
            if external_line_id and len(found) > 1:
                raise OrdersAmbiguousMatchError(
                    "L'ID unità ordine corrisponde a più righe. Nessun dato è stato modificato."
                )
            now = datetime.now(UTC)
            for row in found:
                public = json.loads(row["canonical_json"])
                details = dict(public.get("details") or {})
                previous_carrier = str(details.get("carrier") or "")
                previous_tracking = str(details.get("tracking") or "")
                changed = False
                if carrier and (
                    previous_carrier != carrier or details.get("carrier_source") != source
                ):
                    details["carrier"] = carrier
                    details["carrier_source"] = source
                    details["carrier_updated_at"] = now.isoformat()
                    details["carrier_updated_by"] = str(actor_id)
                    changed = True
                if tracking and (
                    previous_tracking != tracking or details.get("tracking_source") != source
                ):
                    details["tracking"] = tracking
                    details["tracking_source"] = source
                    details["tracking_updated_at"] = now.isoformat()
                    details["tracking_updated_by"] = str(actor_id)
                    changed = True
                if not changed:
                    continue
                public["details"] = details
                search_text = " ".join(str(public.get(key) or "") for key in (
                    "order_id", "external_line_id", "product_name", "ean", "sku",
                )).casefold() + " " + " ".join(str(details.get(key) or "") for key in (
                    "tracking", "carrier",
                )).casefold()
                active.execute(lines.update().where(lines.c.id == row["id"]).values(
                    **project_order(public), canonical_json=json.dumps(
                        public, ensure_ascii=False, allow_nan=False,
                    ), search_text=search_text, updated_at=now, projection_updated_at=now,
                ))
                active.execute(tracking_events.insert().values(
                    id=uuid4(), organization_id=organization_id, seller_id=seller_id,
                    account_id=account_id, environment=environment, line_id=row["id"],
                    actor_id=actor_id, source=source, previous_carrier=previous_carrier,
                    previous_tracking=previous_tracking,
                    carrier=str(details.get("carrier") or ""),
                    tracking=str(details.get("tracking") or ""), created_at=now,
                ))
            return [dict(row) for row in found]

    def import_tracking_batch(
        self, organization_id, seller_id, account_id, environment, *, records, source, actor_id,
    ):
        """Resolve an import once, then bulk-write each affected unit at most once.

        Records are evaluated in source order. Repeated rows therefore use
        last-non-empty-value-wins semantics while empty cells preserve prior values.
        """
        if not records:
            return {"updated": 0, "unmatched": []}
        unit_ids = {record["order_unit_id"] for record in records if record["order_unit_id"]}
        order_ids = {
            record["order_id"] for record in records
            if not record["order_unit_id"] and record["order_id"]
        }
        lookup = []
        if unit_ids:
            lookup.append(lines.c.external_line_id.in_(unit_ids))
        if order_ids:
            lookup.append(lines.c.order_id.in_(order_ids))
        if not lookup:
            return {"updated": 0, "unmatched": []}

        with self.engine.begin() as connection:
            self._lock_tracking_import(
                connection, organization_id, seller_id, account_id,
            )
            found = self._execute_tracking_locked(connection, select(
                lines.c.id, lines.c.external_line_id, lines.c.order_id,
                lines.c.canonical_json,
            ).where(
                *self._scope(lines, organization_id, seller_id, account_id, environment),
                or_(*lookup),
            ).order_by(lines.c.id).limit(
                MAX_TRACKING_TARGET_UNITS + 1
            )).mappings().all()
            if len(found) > MAX_TRACKING_TARGET_UNITS:
                raise TrackingLimitError(
                    "L'import supera il limite di 10.000 unità ordine."
                )

            by_unit = defaultdict(list)
            by_order = defaultdict(list)
            original = {}
            staged = {}
            for row in found:
                values = dict(row)
                by_unit[str(row["external_line_id"])].append(values)
                by_order[str(row["order_id"])].append(values)
                public = json.loads(row["canonical_json"])
                original[row["id"]] = public
                staged[row["id"]] = {
                    **public,
                    "details": dict(public.get("details") or {}),
                }

            matched_ids = set()
            unmatched = []
            for record in records:
                if record["order_unit_id"]:
                    targets = by_unit.get(record["order_unit_id"], [])
                    if len(targets) > 1:
                        raise OrdersAmbiguousMatchError(
                            "L'ID unità ordine corrisponde a più righe. "
                            "Nessun dato è stato modificato."
                        )
                else:
                    targets = by_order.get(record["order_id"], [])
                if not targets:
                    unmatched.append({
                        "row": record["row"],
                        "order_unit_id": record["order_unit_id"],
                        "order_id": record["order_id"],
                    })
                    continue
                for target in targets:
                    line_id = target["id"]
                    matched_ids.add(line_id)
                    details = staged[line_id]["details"]
                    if record["carrier"]:
                        details["carrier"] = record["carrier"]
                        details["carrier_source"] = source
                    if record["tracking"]:
                        details["tracking"] = record["tracking"]
                        details["tracking_source"] = source

            if len(matched_ids) > MAX_TRACKING_TARGET_UNITS:
                raise TrackingLimitError(
                    "L'import supera il limite di 10.000 unità ordine."
                )

            now = datetime.now(UTC)
            updates = []
            events = []
            projection_keys = None
            for line_id in matched_ids:
                public = staged[line_id]
                details = public["details"]
                previous = original[line_id]
                previous_details = previous.get("details") or {}
                changed = False
                for field in ("carrier", "tracking"):
                    before_value = str(previous_details.get(field) or "")
                    after_value = str(details.get(field) or "")
                    before_source = previous_details.get(f"{field}_source")
                    after_source = details.get(f"{field}_source")
                    if (after_value, after_source) != (before_value, before_source):
                        details[f"{field}_updated_at"] = now.isoformat()
                        details[f"{field}_updated_by"] = str(actor_id)
                        changed = True
                if not changed:
                    continue
                public["details"] = details
                projected = project_order(public)
                projection_keys = projection_keys or tuple(projected)
                updates.append({
                    "_tracking_line_id": line_id,
                    **{f"_tracking_{key}": value for key, value in projected.items()},
                    "_tracking_canonical_json": json.dumps(
                        public, ensure_ascii=False, allow_nan=False,
                    ),
                    "_tracking_search_text": " ".join(
                        str(public.get(key) or "") for key in (
                            "order_id", "external_line_id", "product_name", "ean", "sku",
                        )
                    ).casefold() + " " + " ".join(
                        str(details.get(key) or "") for key in ("tracking", "carrier")
                    ).casefold(),
                    "_tracking_updated_at": now,
                })
                events.append({
                    "id": uuid4(), "organization_id": organization_id,
                    "seller_id": seller_id, "account_id": account_id,
                    "environment": environment, "line_id": line_id,
                    "actor_id": actor_id, "source": source,
                    "previous_carrier": str(previous_details.get("carrier") or ""),
                    "previous_tracking": str(previous_details.get("tracking") or ""),
                    "carrier": str(details.get("carrier") or ""),
                    "tracking": str(details.get("tracking") or ""), "created_at": now,
                })

            if updates:
                values = {
                    key: bindparam(f"_tracking_{key}") for key in projection_keys or ()
                }
                values.update({
                    "canonical_json": bindparam("_tracking_canonical_json"),
                    "search_text": bindparam("_tracking_search_text"),
                    "updated_at": bindparam("_tracking_updated_at"),
                    "projection_updated_at": bindparam("_tracking_updated_at"),
                })
                statement = lines.update().where(
                    lines.c.id == bindparam("_tracking_line_id")
                ).values(**values)
                for offset in range(0, len(updates), 1_000):
                    connection.execute(statement, updates[offset:offset + 1_000])
                    connection.execute(
                        tracking_events.insert(), events[offset:offset + 1_000],
                    )

            return {
                "updated": len(updates), "unmatched": unmatched,
            }

    def latest_job(self, organization_id, seller_id, account_id, environment, *, active=False):
        conditions = list(self._scope(jobs, organization_id, seller_id, account_id, environment))
        if active:
            conditions.append(jobs.c.status.in_(["queued", "running"]))
        with self.engine.connect() as connection:
            row = connection.execute(select(jobs).where(*conditions).order_by(
                jobs.c.created_at.desc(), jobs.c.id.desc(),
            ).limit(1)).mappings().first()
            return dict(row) if row else None

    def job(self, job_id: UUID):
        with self.engine.connect() as connection:
            row = connection.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
            if row is None:
                raise OrdersNotFoundError("Sincronizzazione non disponibile.")
            return dict(row)

    def create_job(self, values):
        now = datetime.now(UTC)
        job = {"id": uuid4(), **values, "status": "queued", "processed": 0, "total": None,
               "message": "Sincronizzazione in coda.", "error_code": None, "created_at": now,
               "updated_at": now, "started_at": None, "finished_at": None}
        with self.engine.begin() as connection:
            connection.execute(jobs.insert().values(**job))
        return job

    def claim(self, job_id):
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(jobs.update().where(
                jobs.c.id == job_id, jobs.c.status == "queued",
            ).values(status="running", started_at=now, updated_at=now,
                     message="Download ordini in corso."))
            return result.rowcount == 1

    def progress(self, job_id, *, message=None, processed=None, total=None):
        values = {"updated_at": datetime.now(UTC)}
        if message is not None:
            values["message"] = message
        if processed is not None:
            values.update(processed=processed, total=total)
        with self.engine.begin() as connection:
            connection.execute(jobs.update().where(
                jobs.c.id == job_id, jobs.c.status == "running",
            ).values(**values))

    def finish(self, job_id, *, error_code=None, message=None):
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            connection.execute(jobs.update().where(
                jobs.c.id == job_id, jobs.c.status.in_(["queued", "running"]),
            ).values(status="error" if error_code else "done", error_code=error_code,
                     message=message or "Sincronizzazione completata.", updated_at=now,
                     finished_at=now))

    def upsert_batch(self, job, items):
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            if job["marketplace"] == "kaufland":
                self._lock_tracking_import(
                    connection, job["organization_id"], job["seller_id"], job["account_id"],
                    fail_fast=False,
                )
                incoming_units = defaultdict(set)
                for item in items:
                    incoming_units[str(item["external_line_id"])].add(str(item["order_id"]))
                if any(len(order_ids) > 1 for order_ids in incoming_units.values()):
                    raise OrdersAmbiguousMatchError(
                        "L'ID unità ordine corrisponde a più ordini. "
                        "Nessun dato è stato modificato."
                    )
                if incoming_units:
                    saved_units = defaultdict(set)
                    for saved in connection.execute(select(
                        lines.c.external_line_id, lines.c.order_id,
                    ).where(
                        *self._scope(
                            lines, job["organization_id"], job["seller_id"],
                            job["account_id"], job["environment"],
                        ),
                        lines.c.external_line_id.in_(tuple(incoming_units)),
                    ).order_by(lines.c.id).with_for_update()).mappings():
                        saved_units[str(saved["external_line_id"])].add(str(saved["order_id"]))
                    if any(
                        saved_units[unit_id] - order_ids
                        for unit_id, order_ids in incoming_units.items()
                    ):
                        raise OrdersAmbiguousMatchError(
                            "L'ID unità ordine corrisponde a più ordini. "
                            "Nessun dato è stato modificato."
                        )
            identities = {(str(item["order_id"]), str(item["external_line_id"])) for item in items}
            previous = {
                (row["order_id"], row["external_line_id"]): json.loads(row["canonical_json"])
                for row in connection.execute(select(
                    lines.c.order_id, lines.c.external_line_id, lines.c.canonical_json,
                ).where(
                    *self._scope(lines, job["organization_id"], job["seller_id"],
                                 job["account_id"], job["environment"]),
                    tuple_(lines.c.order_id, lines.c.external_line_id).in_(identities),
                ).with_for_update()).mappings()
            } if identities else {}
            for item in items:
                public = {key: value for key, value in item.items() if key != "raw"}
                identity = (str(item["order_id"]), str(item["external_line_id"]))
                details = dict(public.get("details") or {})
                saved_details = previous.get(identity, {}).get("details") or {}
                # Match the original cache: API omissions must not erase tracking,
                # carrier or last detail verification. All financial fields and
                # shipment/payment event dates still come from the current read.
                for key in ("tracking", "carrier", "detail_checked_at"):
                    if not details.get(key) and saved_details.get(key):
                        details[key] = saved_details[key]
                        for suffix in ("source", "updated_at", "updated_by"):
                            source_key = f"{key}_{suffix}"
                            if saved_details.get(source_key):
                                details[source_key] = saved_details[source_key]
                    elif key in {"tracking", "carrier"} and details.get(key):
                        details[f"{key}_source"] = details.get(f"{key}_source") or "api"
                public["details"] = details
                previous[identity] = public
                record = {
                    **project_order(public),
                    "id": uuid4(), "organization_id": job["organization_id"],
                    "seller_id": job["seller_id"], "account_id": job["account_id"],
                    "environment": job["environment"], "marketplace": job["marketplace"],
                    "external_line_id": str(item["external_line_id"]),
                    "order_id": str(item["order_id"]),
                    "order_created_at": timestamp(item.get("created_at")),
                    "status": str(item.get("status") or ""),
                    "storefront": str(item.get("storefront") or ""),
                    "search_text": " ".join(str(item.get(key) or "") for key in (
                        "order_id", "external_line_id", "product_name", "ean", "sku",
                    )).casefold() + " " + " ".join(str(details.get(key) or "") for key in (
                        "tracking", "carrier",
                    )).casefold(),
                    "canonical_json": json.dumps(public, ensure_ascii=False, allow_nan=False),
                    "raw_json": json.dumps(item.get("raw", {}), ensure_ascii=False,
                                           allow_nan=False),
                    "updated_at": now,
                    "projection_updated_at": now,
                }
                statement = insert(lines).values(**record)
                statement = statement.on_conflict_do_update(
                    index_elements=["seller_id", "account_id", "environment", "order_id",
                                    "external_line_id"],
                    set_={key: getattr(statement.excluded, key) for key in record
                          if key not in {"id", "seller_id", "account_id", "environment",
                                         "external_line_id", "organization_id"}},
                )
                connection.execute(statement)
