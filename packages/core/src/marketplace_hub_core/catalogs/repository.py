from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select

from marketplace_hub_core.catalogs.schema import (
    seller_price_list_products as products,
)
from marketplace_hub_core.catalogs.schema import seller_price_lists as price_lists
from marketplace_hub_core.catalogs.schema import seller_suppliers as suppliers
from marketplace_hub_core.tenancy.schema import seller_profiles
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError


class CatalogSupplierNotFoundError(ValueError):
    pass


class CatalogPriceListNotFoundError(ValueError):
    pass


class CatalogConfirmationError(ValueError):
    pass


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _timestamp(value) -> str:
    return value.isoformat() if value else ""


class SqlCatalogsRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _scope(table, organization_id: UUID, seller_id: UUID):
        return (
            table.c.organization_id == organization_id,
            table.c.seller_id == seller_id,
        )

    @staticmethod
    def _require_profile(connection, organization_id: UUID, seller_id: UUID) -> None:
        found = connection.execute(select(seller_profiles.c.id).where(
            seller_profiles.c.id == seller_id,
            seller_profiles.c.organization_id == organization_id,
            seller_profiles.c.active.is_(True),
        )).first()
        if found is None:
            raise SellerNotAccessibleError("Negozio non disponibile.")

    @staticmethod
    def _supplier_public(row, *, price_list_count: int | None = None) -> dict:
        output = {
            "id": str(row["id"]),
            "name": row["name"],
            "notes": row["notes"],
            "created_at": _timestamp(row["created_at"]),
            "updated_at": _timestamp(row["updated_at"]),
        }
        if price_list_count is not None:
            output["price_list_count"] = int(price_list_count)
        return output

    @staticmethod
    def _price_list_public(row) -> dict:
        return {
            "id": str(row["id"]),
            "supplier_id": str(row["supplier_id"]),
            "supplier_name": row.get("supplier_name", ""),
            "name": row["name"],
            # B12.1a only accepts completed uploads. These two values are an
            # explicit DTO contract, derived from the only supported state.
            "source_type": "upload",
            "status": "ready",
            "file_name": row["original_filename"],
            "media_type": row["media_type"],
            "file_format": row["file_format"],
            "artifact_sha256": row["artifact_sha256"],
            "artifact_size": int(row["artifact_size"]),
            "row_count": int(row["product_count"]),
            "created_at": _timestamp(row["created_at"]),
            "updated_at": _timestamp(row["updated_at"]),
        }

    @staticmethod
    def _product_public(row) -> dict:
        return {
            "id": str(row["id"]),
            "source_row": int(row["source_row"]),
            "ean": row["ean"],
            "sku": row["sku"],
            "name": row["name"],
            "cost": _decimal_text(row["cost"]),
            "shipping_cost": _decimal_text(row["shipping_cost"]),
            "total_cost": _decimal_text(row["total_cost"]),
            "quantity": _decimal_text(row["quantity"]),
        }

    def dashboard(self, organization_id: UUID, seller_id: UUID) -> dict:
        with self.engine.connect() as connection:
            self._require_profile(connection, organization_id, seller_id)
            supplier_rows = connection.execute(select(
                suppliers,
                func.count(price_lists.c.id).label("price_list_count"),
            ).outerjoin(
                price_lists,
                (price_lists.c.supplier_id == suppliers.c.id)
                & (price_lists.c.organization_id == suppliers.c.organization_id)
                & (price_lists.c.seller_id == suppliers.c.seller_id),
            ).where(
                *self._scope(suppliers, organization_id, seller_id),
            ).group_by(*suppliers.c).order_by(
                func.lower(suppliers.c.name), suppliers.c.id,
            )).mappings().all()
            list_rows = connection.execute(select(
                *[column for column in price_lists.c if column.name != "artifact_bytes"],
                suppliers.c.name.label("supplier_name"),
            ).join(
                suppliers,
                (suppliers.c.id == price_lists.c.supplier_id)
                & (suppliers.c.organization_id == price_lists.c.organization_id)
                & (suppliers.c.seller_id == price_lists.c.seller_id),
            ).where(
                *self._scope(price_lists, organization_id, seller_id),
            ).order_by(
                func.lower(suppliers.c.name), func.lower(price_lists.c.name), price_lists.c.id,
            )).mappings().all()
        return {
            "suppliers": [
                self._supplier_public(row, price_list_count=row["price_list_count"])
                for row in supplier_rows
            ],
            "price_lists": [self._price_list_public(row) for row in list_rows],
        }

    def add_supplier(
        self, organization_id: UUID, seller_id: UUID, *, name: str, notes: str,
    ) -> UUID:
        supplier_id = uuid4()
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            self._require_profile(connection, organization_id, seller_id)
            connection.execute(suppliers.insert().values(
                id=supplier_id,
                organization_id=organization_id,
                seller_id=seller_id,
                name=name,
                notes=notes,
                created_at=now,
                updated_at=now,
            ))
        return supplier_id

    def supplier(self, organization_id: UUID, seller_id: UUID, supplier_id: UUID) -> dict:
        with self.engine.connect() as connection:
            row = connection.execute(select(suppliers).where(
                suppliers.c.id == supplier_id,
                *self._scope(suppliers, organization_id, seller_id),
            )).mappings().first()
        if row is None:
            raise CatalogSupplierNotFoundError("Fornitore non disponibile.")
        return dict(row)

    def delete_supplier(
        self,
        organization_id: UUID,
        seller_id: UUID,
        supplier_id: UUID,
        *,
        confirmation: str,
    ) -> dict:
        with self.engine.begin() as connection:
            row = connection.execute(select(suppliers).where(
                suppliers.c.id == supplier_id,
                *self._scope(suppliers, organization_id, seller_id),
            ).with_for_update()).mappings().first()
            if row is None:
                raise CatalogSupplierNotFoundError("Fornitore non disponibile.")
            if confirmation != row["name"]:
                raise CatalogConfirmationError(
                    "Scrivi esattamente il nome del fornitore per confermare la rimozione."
                )
            list_count = connection.scalar(select(func.count()).select_from(price_lists).where(
                price_lists.c.supplier_id == supplier_id,
                *self._scope(price_lists, organization_id, seller_id),
            )) or 0
            connection.execute(suppliers.delete().where(
                suppliers.c.id == supplier_id,
                *self._scope(suppliers, organization_id, seller_id),
            ))
        return {"id": str(supplier_id), "name": row["name"], "price_lists_deleted": list_count}

    def add_price_list(
        self,
        organization_id: UUID,
        seller_id: UUID,
        supplier_id: UUID,
        *,
        name: str,
        original_filename: str,
        media_type: str,
        file_format: str,
        artifact: bytes,
        normalized_products: list[dict],
    ) -> UUID:
        price_list_id = uuid4()
        now = datetime.now(UTC)
        digest = hashlib.sha256(artifact).hexdigest()
        with self.engine.begin() as connection:
            self._require_profile(connection, organization_id, seller_id)
            supplier = connection.execute(select(suppliers.c.id).where(
                suppliers.c.id == supplier_id,
                *self._scope(suppliers, organization_id, seller_id),
            ).with_for_update()).first()
            if supplier is None:
                raise CatalogSupplierNotFoundError("Fornitore non disponibile.")
            connection.execute(price_lists.insert().values(
                id=price_list_id,
                organization_id=organization_id,
                seller_id=seller_id,
                supplier_id=supplier_id,
                name=name,
                original_filename=original_filename,
                media_type=media_type,
                file_format=file_format,
                artifact_sha256=digest,
                artifact_size=len(artifact),
                artifact_bytes=artifact,
                product_count=len(normalized_products),
                created_at=now,
                updated_at=now,
            ))
            rows = [{
                "id": uuid4(),
                "organization_id": organization_id,
                "seller_id": seller_id,
                "price_list_id": price_list_id,
                "created_at": now,
                **product,
            } for product in normalized_products]
            for position in range(0, len(rows), 1_000):
                connection.execute(products.insert(), rows[position : position + 1_000])
        return price_list_id

    def detail(
        self,
        organization_id: UUID,
        seller_id: UUID,
        price_list_id: UUID,
        *,
        limit: int,
    ) -> dict:
        with self.engine.connect() as connection:
            row = connection.execute(select(
                *[column for column in price_lists.c if column.name != "artifact_bytes"],
                suppliers.c.name.label("supplier_name"),
            ).join(
                suppliers,
                (suppliers.c.id == price_lists.c.supplier_id)
                & (suppliers.c.organization_id == price_lists.c.organization_id)
                & (suppliers.c.seller_id == price_lists.c.seller_id),
            ).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            )).mappings().first()
            if row is None:
                raise CatalogPriceListNotFoundError("Listino non disponibile.")
            product_rows = connection.execute(select(products).where(
                products.c.price_list_id == price_list_id,
                *self._scope(products, organization_id, seller_id),
            ).order_by(products.c.source_row).limit(limit)).mappings().all()
        return {
            "price_list": self._price_list_public(row),
            "products": [self._product_public(item) for item in product_rows],
            "preview_count": len(product_rows),
            "row_count": int(row["product_count"]),
        }

    def delete_price_list(
        self, organization_id: UUID, seller_id: UUID, price_list_id: UUID,
    ) -> dict:
        with self.engine.begin() as connection:
            row = connection.execute(select(
                price_lists.c.id,
                price_lists.c.name,
                price_lists.c.product_count,
            ).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            ).with_for_update()).mappings().first()
            if row is None:
                raise CatalogPriceListNotFoundError("Listino non disponibile.")
            result = connection.execute(price_lists.delete().where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            ))
            if result.rowcount != 1:
                raise CatalogPriceListNotFoundError("Listino non disponibile.")
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "products_deleted": int(row["product_count"]),
        }

    def raw_artifact(
        self, organization_id: UUID, seller_id: UUID, price_list_id: UUID,
    ) -> bytes:
        """Internal retrieval used by later worker adapters; never expose from the API."""
        with self.engine.connect() as connection:
            artifact = connection.scalar(select(price_lists.c.artifact_bytes).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            ))
        if artifact is None:
            raise CatalogPriceListNotFoundError("Listino non disponibile.")
        return bytes(artifact)
