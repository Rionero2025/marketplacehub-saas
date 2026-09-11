"""Work on catalog snapshots without changing supplier data or posting offers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import delete, func, or_, select

from marketplace_hub_core.catalogs.measurements import FIELDS, MeasurementFilter
from marketplace_hub_core.catalogs.repository import (
    CatalogPriceListNotFoundError,
    CatalogSourceRevisionMismatchError,
)
from marketplace_hub_core.catalogs.schema import (
    seller_catalog_view_rows as view_rows,
)
from marketplace_hub_core.catalogs.schema import (
    seller_catalog_views as views,
)
from marketplace_hub_core.catalogs.schema import (
    seller_price_list_products as products,
)
from marketplace_hub_core.catalogs.schema import (
    seller_price_lists as lists,
)
from marketplace_hub_core.catalogs.schema import (
    seller_suppliers as suppliers,
)
from marketplace_hub_core.catalogs.service import CatalogValidationError
from marketplace_hub_core.catalogs.work_models import ViewSave, ViewUpdate, WorkRecipe
from marketplace_hub_core.seller_settings.schema import seller_marketplace_accounts as accounts

PAGE_SIZE = 50


def legacy_round(value: float) -> float:
    # pandas/numpy round(2): multiply binary float by 100, round to even, divide.
    # Decimal.quantize or Python round(value, 2) produce different tie results.
    return round(value * 100.0) / 100.0


def prepare_row(row, recipe: WorkRecipe):
    shipping = legacy_round(float(row["shipping_cost"] or 0) + float(recipe.shipping))
    total = legacy_round(float(row["cost"] or 0) + shipping)
    return {
        "id": str(row["id"]),
        "ean": row["ean"] or "",
        "sku": row["sku"] or "",
        "name": row["name"] or "",
        "cost": format(Decimal(row["cost"] or 0), "f"),
        "shipping_cost": f"{shipping:.2f}",
        "total_cost": f"{total:.2f}",
        "quantity": format(Decimal(row["quantity"] or 0), "f"),
        "price": f"{legacy_round(total * (1 + float(recipe.margin) / 100)):.2f}",
        "minimum_price": f"{legacy_round(total * (1 + float(recipe.minimum_margin) / 100)):.2f}",
        **{field: str(row[field]) if row[field] is not None else None for field in FIELDS},
    }


class CatalogWorkRepository:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def scope(table, org, seller):
        return table.c.organization_id == org, table.c.seller_id == seller

    def _view(self, c, org, seller, view_id, *, lock=False):
        statement = select(views).where(views.c.id == view_id, *self.scope(views, org, seller))
        row = c.execute(statement.with_for_update() if lock else statement).mappings().first()
        if row is None:
            raise CatalogPriceListNotFoundError("Vista non disponibile.")
        return row

    @staticmethod
    def _public(row):
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "source_name": row["source_name"],
            "row_count": row["row_count"],
            "revision": row["revision"],
            "updated_at": row["updated_at"].isoformat(),
            "account_ids": json.loads(row["accounts_json"]),
        }

    def index(self, org, seller):
        with self.engine.connect() as c:
            destinations = (
                c.execute(
                    select(
                        accounts.c.id,
                        accounts.c.marketplace,
                        accounts.c.account_name,
                    )
                    .where(*self.scope(accounts, org, seller), accounts.c.active.is_(True))
                    .order_by(accounts.c.marketplace, accounts.c.account_name)
                )
                .mappings()
                .all()
            )
            saved = (
                c.execute(
                    select(views)
                    .where(*self.scope(views, org, seller))
                    .order_by(views.c.updated_at.desc())
                    .limit(500)
                )
                .mappings()
                .all()
            )
        return {
            "seller_id": str(seller),
            "accounts": [
                {"id": str(a["id"]), "marketplace": a["marketplace"], "name": a["account_name"]}
                for a in destinations
            ],
            "views": [self._public(row) for row in saved],
        }

    def _source(self, c, org, seller, recipe, *, lock=False):
        ids = [recipe.price_list_id]
        if recipe.content_list_id:
            ids.append(recipe.content_list_id)
        statement = (
            select(
                lists.c.id,
                lists.c.supplier_id,
                lists.c.name,
                lists.c.provider,
                lists.c.feed_role,
                lists.c.active_version_number,
            )
            .where(lists.c.id.in_(ids), *self.scope(lists, org, seller))
            .order_by(lists.c.id)
        )
        found = c.execute(statement.with_for_update() if lock else statement).mappings().all()
        by_id = {r["id"]: r for r in found}
        if set(ids) != set(by_id):
            raise CatalogPriceListNotFoundError("Listino non disponibile.")
        base = by_id[recipe.price_list_id]
        if base["active_version_number"] != recipe.version:
            raise CatalogSourceRevisionMismatchError(
                "Il listino è cambiato. Riapri la lavorazione."
            )
        if base["provider"] == "innpro" and base["feed_role"] != "light":
            raise CatalogValidationError("Per costi e disponibilità InnPro seleziona il LIGHT.")
        supplier_name = (
            c.scalar(
                select(suppliers.c.name).where(
                    suppliers.c.id == base["supplier_id"],
                    *self.scope(suppliers, org, seller),
                )
            )
            or ""
        )
        if any(
            t in supplier_name.casefold().replace(" ", "")
            for t in ("activeshop", "cecotec", "ecotech")
        ):
            raise CatalogValidationError(
                "Questo fornitore richiede le regole dedicate di costo e destinazione, "
                "ancora da trasferire. La lavorazione generica non è applicabile."
            )
        if recipe.content_list_id:
            full = by_id[recipe.content_list_id]
            if (
                base["provider"] != "innpro"
                or full["provider"] != "innpro"
                or full["feed_role"] != "full"
                or full["supplier_id"] != base["supplier_id"]
            ):
                raise CatalogValidationError("Seleziona il FULL dello stesso fornitore InnPro.")
            if full["active_version_number"] != recipe.content_version:
                raise CatalogSourceRevisionMismatchError(
                    "Il FULL è cambiato. Riapri la lavorazione."
                )
        return base

    def _query(self, org, seller, recipe):
        columns = [
            products.c.id,
            products.c.source_row,
            products.c.ean,
            products.c.sku,
            products.c.cost,
            products.c.shipping_cost,
            products.c.quantity,
        ]
        source = products
        if recipe.content_list_id:
            # Only one exact EAN in the chosen FULL can enrich a LIGHT product.
            # Group solely small public values, never full XML/canonical JSON.
            p = products.alias("full_products")
            full = select(
                p.c.ean,
                func.max(p.c.name).label("name"),
                *[func.max(p.c[field]).label(field) for field in FIELDS],
            ).where(
                p.c.price_list_id == recipe.content_list_id,
                p.c.version_number == recipe.content_version,
                *self.scope(p, org, seller),
                p.c.ean != "",
            )
            full = full.group_by(p.c.ean).having(func.count() == 1).subquery()
            source = products.outerjoin(full, products.c.ean == full.c.ean)
            columns += [func.coalesce(func.nullif(full.c.name, ""), products.c.name).label("name")]
            columns += [
                func.coalesce(products.c[field], full.c[field]).label(field) for field in FIELDS
            ]
        else:
            columns += [products.c.name, *[products.c[field] for field in FIELDS]]
        base = (
            select(*columns)
            .select_from(source)
            .where(
                products.c.price_list_id == recipe.price_list_id,
                products.c.version_number == recipe.version,
                *self.scope(products, org, seller),
            )
            .subquery()
        )
        conditions = [
            func.coalesce(base.c.quantity, 0) >= recipe.min_qty,
            func.coalesce(base.c.cost, 0) >= recipe.min_cost,
        ]
        if recipe.search:
            conditions.append(
                or_(
                    *[
                        func.lower(base.c[field]).contains(
                            recipe.search.lower(),
                            autoescape=True,
                        )
                        for field in ("ean", "sku", "name")
                    ]
                )
            )
        if recipe.max_cost:
            conditions.append(func.coalesce(base.c.cost, 0) <= recipe.max_cost)
        conditions.append(
            MeasurementFilter(
                recipe.measure,
                recipe.exclude,
                recipe.lower,
                recipe.upper,
            ).condition(base)
        )
        return select(base).where(*conditions)

    def preview(self, org, seller, recipe, page):
        with self.engine.connect() as c:
            self._source(c, org, seller, recipe)
            query = self._query(org, seller, recipe)
            total = c.scalar(select(func.count()).select_from(query.subquery()))
            rows = (
                c.execute(
                    query.order_by("source_row").offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
                )
                .mappings()
                .all()
            )
        return {
            "seller_id": str(seller),
            "recipe": recipe.model_dump(mode="json"),
            "page": page,
            "total": total,
            "rows": [prepare_row(r, recipe) for r in rows],
        }

    def _accounts(self, c, org, seller, ids):
        unique = set(ids)
        found = set(
            c.scalars(
                select(accounts.c.id)
                .where(
                    accounts.c.id.in_(unique),
                    *self.scope(accounts, org, seller),
                    accounts.c.active.is_(True),
                )
                .order_by(accounts.c.id)
                .with_for_update()
            )
        )
        if not unique or found != unique:
            raise CatalogValidationError("Scegli account marketplace attivi del tuo negozio.")
        return json.dumps(sorted(str(i) for i in unique))

    @staticmethod
    def _revision(row, expected):
        if row["revision"] != expected:
            raise CatalogSourceRevisionMismatchError(
                "La vista è cambiata. Riaprila prima di salvare."
            )

    @staticmethod
    def _edits(row, edits):
        if edits:
            # Like the original grid, independently edited values stay explicit.
            for key, value in edits.model_dump(exclude_none=True).items():
                if key in ("ean", "sku", "name"):
                    row[key] = value.strip()
                elif key == "weight_kg":
                    row[key] = format(value, "f") if value > 0 else None
                else:
                    row[key] = format(value, "f")
            if not row["ean"] and not row["sku"]:
                raise CatalogValidationError("Mantieni almeno EAN o SKU nella riga.")
        return row

    def save(self, org, seller, payload: ViewSave):
        now = datetime.now(UTC)
        with self.engine.begin() as c:
            name = payload.name.strip()
            if not name:
                raise CatalogValidationError("Inserisci il nome della vista.")
            source = self._source(c, org, seller, payload.recipe, lock=True)
            previous = None
            if payload.overwrite_id:
                previous = self._view(c, org, seller, payload.overwrite_id, lock=True)
                self._revision(previous, payload.expected_revision)
            targets = self._accounts(c, org, seller, payload.account_ids)
            query = self._query(org, seller, payload.recipe)
            eligible = query.subquery()
            referenced = set(payload.selection) | set(payload.edits)
            if referenced:
                available = set(
                    c.scalars(
                        select(eligible.c.id).where(
                            eligible.c.id.in_(referenced),
                        )
                    )
                )
                if available != referenced:
                    raise CatalogValidationError(
                        "La selezione contiene prodotti fuori dalla vista."
                    )
            query = select(eligible)
            if payload.select_all:
                if payload.selection:
                    query = query.where(eligible.c.id.not_in(payload.selection))
            else:
                query = query.where(eligible.c.id.in_(payload.selection))
            total = c.scalar(select(func.count()).select_from(query.subquery()))
            if not total:
                raise CatalogValidationError("Seleziona almeno un prodotto.")
            if previous is not None:
                view_id = previous["id"]
                revision = previous["revision"] + 1
                c.execute(delete(view_rows).where(view_rows.c.view_id == view_id))
            else:
                view_id, revision = uuid4(), 1
                c.execute(
                    views.insert().values(
                        id=view_id,
                        organization_id=org,
                        seller_id=seller,
                        name=name,
                        source_name=source["name"],
                        recipe_json="{}",
                        accounts_json=targets,
                        row_count=0,
                        revision=revision,
                        created_at=now,
                        updated_at=now,
                    )
                )
            position, last = 0, 0
            while True:
                batch = (
                    c.execute(
                        query.where(eligible.c.source_row > last)
                        .order_by(eligible.c.source_row)
                        .limit(100)
                    )
                    .mappings()
                    .all()
                )
                if not batch:
                    break
                records = []
                for raw in batch:
                    position += 1
                    row = self._edits(
                        prepare_row(raw, payload.recipe), payload.edits.get(raw["id"])
                    )
                    records.append(
                        {
                            "view_id": view_id,
                            "id": raw["id"],
                            "position": position,
                            "data_json": json.dumps(row, ensure_ascii=False),
                        }
                    )
                c.execute(view_rows.insert(), records)
                last = batch[-1]["source_row"]
            c.execute(
                views.update()
                .where(views.c.id == view_id)
                .values(
                    name=name,
                    source_name=source["name"],
                    recipe_json=payload.recipe.model_dump_json(),
                    accounts_json=targets,
                    row_count=position,
                    revision=revision,
                    updated_at=now,
                )
            )
            return self._public(self._view(c, org, seller, view_id))

    def detail(self, org, seller, view_id, page):
        with self.engine.connect() as c:
            row = self._view(c, org, seller, view_id)
            data = c.scalars(
                select(view_rows.c.data_json)
                .where(view_rows.c.view_id == view_id)
                .order_by(view_rows.c.position)
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
            ).all()
        return {
            "seller_id": str(seller),
            "view": self._public(row),
            "page": page,
            "total": row["row_count"],
            "rows": [json.loads(r) for r in data],
        }

    def update(self, org, seller, view_id, payload: ViewUpdate):
        with self.engine.begin() as c:
            row = self._view(c, org, seller, view_id, lock=True)
            self._revision(row, payload.expected_revision)
            name = payload.name.strip()
            if not name:
                raise CatalogValidationError("Inserisci il nome della vista.")
            targets = self._accounts(c, org, seller, payload.account_ids)
            touched = set(payload.edits) | set(payload.removed)
            found = (
                c.execute(
                    select(view_rows.c.id, view_rows.c.data_json).where(
                        view_rows.c.view_id == view_id,
                        view_rows.c.id.in_(touched),
                    )
                )
                .mappings()
                .all()
            )
            if {r["id"] for r in found} != touched:
                raise CatalogValidationError("Prodotto non presente nella vista.")
            remaining = row["row_count"] - len(set(payload.removed)) + len(payload.added)
            if remaining < 1:
                raise CatalogValidationError("La vista deve contenere almeno un prodotto.")
            for item in found:
                if item["id"] not in payload.removed:
                    data = self._edits(json.loads(item["data_json"]), payload.edits.get(item["id"]))
                    c.execute(
                        view_rows.update()
                        .where(view_rows.c.view_id == view_id, view_rows.c.id == item["id"])
                        .values(data_json=json.dumps(data))
                    )
            c.execute(
                delete(view_rows).where(
                    view_rows.c.view_id == view_id, view_rows.c.id.in_(payload.removed)
                )
            )
            position = (
                c.scalar(
                    select(func.max(view_rows.c.position)).where(
                        view_rows.c.view_id == view_id,
                    )
                )
                or 0
            )
            for added in payload.added:
                position += 1
                item_id = uuid4()
                data = {
                    "id": str(item_id),
                    "ean": added.ean,
                    "sku": added.sku,
                    "name": added.name,
                    **dict.fromkeys(FIELDS),
                    **{
                        f: "0"
                        for f in (
                            "cost",
                            "shipping_cost",
                            "total_cost",
                            "quantity",
                            "price",
                            "minimum_price",
                        )
                    },
                }
                data = self._edits(data, added)
                c.execute(
                    view_rows.insert().values(
                        view_id=view_id, id=item_id, position=position, data_json=json.dumps(data)
                    )
                )
            c.execute(
                views.update()
                .where(views.c.id == view_id)
                .values(
                    name=name,
                    accounts_json=targets,
                    row_count=remaining,
                    revision=row["revision"] + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            return self._public(self._view(c, org, seller, view_id))

    def remove(self, org, seller, view_id, expected_revision):
        with self.engine.begin() as c:
            row = self._view(c, org, seller, view_id, lock=True)
            self._revision(row, expected_revision)
            c.execute(delete(views).where(views.c.id == view_id))
        return {"id": str(view_id), "deleted": True}
