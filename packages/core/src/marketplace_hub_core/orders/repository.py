import json
from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, or_, select, tuple_

from marketplace_hub_core.orders.filters import OrderFilters
from marketplace_hub_core.orders.projections import project_order
from marketplace_hub_core.orders.schema import order_lines as lines
from marketplace_hub_core.orders.schema import order_sync_jobs as jobs


def utc(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def timestamp(value):
    try:
        return utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


class OrdersNotFoundError(ValueError):
    pass


class SqlOrdersRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def _scope(table, organization_id, seller_id, account_id, environment):
        return (table.c.organization_id == organization_id, table.c.seller_id == seller_id,
                table.c.account_id == account_id, table.c.environment == environment)

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
            identities = {(str(item["order_id"]), str(item["external_line_id"])) for item in items}
            previous = {
                (row["order_id"], row["external_line_id"]): json.loads(row["canonical_json"])
                for row in connection.execute(select(
                    lines.c.order_id, lines.c.external_line_id, lines.c.canonical_json,
                ).where(
                    *self._scope(lines, job["organization_id"], job["seller_id"],
                                 job["account_id"], job["environment"]),
                    tuple_(lines.c.order_id, lines.c.external_line_id).in_(identities),
                )).mappings()
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
