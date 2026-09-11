from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select

from marketplace_hub_core.catalogs.artifacts import (
    MAX_REMOTE_CATALOG_SOURCE_BYTES,
    MAX_STORED_CATALOG_ARTIFACT_BYTES,
    CatalogArtifactEncodingError,
    EncodedCatalogArtifact,
    decode_catalog_artifact,
    encode_catalog_artifact,
    verify_encoded_catalog_artifact,
)
from marketplace_hub_core.catalogs.postgres_artifacts import artifact_write_value
from marketplace_hub_core.catalogs.progress import forecast_job
from marketplace_hub_core.catalogs.schema import (
    seller_price_list_products as products,
)
from marketplace_hub_core.catalogs.schema import (
    seller_price_list_refresh_jobs as refresh_jobs,
)
from marketplace_hub_core.catalogs.schema import seller_price_list_versions as versions
from marketplace_hub_core.catalogs.schema import seller_price_lists as price_lists
from marketplace_hub_core.catalogs.schema import seller_suppliers as suppliers
from marketplace_hub_core.tenancy.schema import seller_profiles
from marketplace_hub_core.tenancy.service import SellerNotAccessibleError

PRODUCT_INSERT_BATCH_ROWS = 250
PRODUCT_INSERT_BATCH_BYTES = 4 * 1024 * 1024


class CatalogSupplierNotFoundError(ValueError):
    pass


class CatalogPriceListNotFoundError(ValueError):
    pass


class CatalogRefreshJobNotFoundError(ValueError):
    pass


class CatalogSourceRevisionMismatchError(RuntimeError):
    pass


class CatalogRefreshInProgressError(RuntimeError):
    pass


class CatalogConfirmationError(ValueError):
    pass


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _timestamp(value) -> str | None:
    return value.isoformat() if value else None


def _safe_error_code(value) -> str | None:
    text = str(value or "")
    return text if re.fullmatch(r"[a-z0-9_]{1,64}", text) else None


def _durable_artifact(
    artifact: bytes,
    *,
    artifact_encoding: str | None,
    artifact_size: int | None,
    artifact_sha256: str | None,
) -> EncodedCatalogArtifact:
    metadata = (artifact_encoding, artifact_size, artifact_sha256)
    if all(value is None for value in metadata):
        return encode_catalog_artifact(artifact)
    if any(value is None for value in metadata):
        raise CatalogArtifactEncodingError(
            "I metadati di archiviazione del listino devono essere completi."
        )
    encoding = str(artifact_encoding)
    raw_size = artifact_size
    raw_sha256 = str(artifact_sha256)
    if encoding not in {"identity", "gzip"}:
        raise CatalogArtifactEncodingError("Codifica del listino non supportata.")
    if isinstance(raw_size, bool) or not isinstance(raw_size, int):
        raise CatalogArtifactEncodingError("La dimensione originale del listino non è valida.")
    if raw_size <= 0 or raw_size > MAX_REMOTE_CATALOG_SOURCE_BYTES:
        raise CatalogArtifactEncodingError("La dimensione originale del listino non è valida.")
    if not re.fullmatch(r"[0-9a-f]{64}", raw_sha256):
        raise CatalogArtifactEncodingError("L'impronta del listino non è valida.")
    if not artifact or len(artifact) > MAX_STORED_CATALOG_ARTIFACT_BYTES:
        raise CatalogArtifactEncodingError("La dimensione archiviata del listino non è valida.")
    encoded = EncodedCatalogArtifact(artifact, encoding, raw_size, raw_sha256)
    verify_encoded_catalog_artifact(encoded)
    return encoded


def _artifact_persistence_values(encoded: EncodedCatalogArtifact) -> dict:
    return {
        "artifact_sha256": encoded.raw_sha256,
        "artifact_size": encoded.raw_size,
        "artifact_encoding": encoded.encoding,
        "artifact_stored_size": encoded.stored_size,
        "artifact_bytes": encoded.content,
    }


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

    def _refresh_light_order_costs(
        self,
        connection,
        organization_id: UUID,
        seller_id: UUID,
        *,
        provider: str,
        feed_role: str,
    ) -> None:
        if provider != "innpro" or feed_role != "light":
            return
        from marketplace_hub_core.catalogs.costing import SqlCatalogCostResolver

        SqlCatalogCostResolver(self.engine).refresh_saved_orders(
            organization_id,
            seller_id,
            connection=connection,
        )

    def _lock_light_cost_scope(
        self,
        connection,
        organization_id: UUID,
        seller_id: UUID,
        *,
        provider: str,
        feed_role: str,
    ) -> None:
        if provider != "innpro" or feed_role != "light":
            return
        from marketplace_hub_core.catalogs.costing import SqlCatalogCostResolver

        SqlCatalogCostResolver(self.engine).lock_scope(
            connection, organization_id, seller_id,
        )

    @staticmethod
    def _active_refresh_statement(
        organization_id: UUID,
        seller_id: UUID,
        price_list_ids: list[UUID],
        *,
        lock: bool,
    ):
        statement = select(refresh_jobs.c.id).where(
            refresh_jobs.c.price_list_id.in_(price_list_ids),
            refresh_jobs.c.organization_id == organization_id,
            refresh_jobs.c.seller_id == seller_id,
            refresh_jobs.c.status.in_(("queued", "running")),
        ).order_by(refresh_jobs.c.id)
        return statement.with_for_update() if lock else statement

    @classmethod
    def _lock_active_refreshes(
        cls,
        connection,
        organization_id: UUID,
        seller_id: UUID,
        price_list_ids: list[UUID],
    ) -> list[UUID]:
        if not price_list_ids:
            return []
        statement = cls._active_refresh_statement(
            organization_id, seller_id, price_list_ids, lock=True,
        )
        return list(connection.scalars(statement))

    @classmethod
    def _has_active_refresh(
        cls,
        connection,
        organization_id: UUID,
        seller_id: UUID,
        price_list_ids: list[UUID],
    ) -> bool:
        if not price_list_ids:
            return False
        statement = cls._active_refresh_statement(
            organization_id, seller_id, price_list_ids, lock=False,
        ).limit(1)
        return connection.execute(statement).first() is not None

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
    def _job_public(row) -> dict:
        processed = int(row["processed_bytes"])
        total = int(row["total_bytes"]) if row["total_bytes"] is not None else None
        progress = 100 if row["status"] == "done" else (
            min(99, int(processed / total * 100)) if total else None
        )
        job_id = str(row["id"])
        return {
            "id": job_id,
            "job_id": job_id,
            "price_list_id": str(row["price_list_id"]),
            "status": row["status"],
            "processed_bytes": processed,
            "total_bytes": total,
            "progress": progress,
            "forecast": forecast_job(row, row.get("_history", ())),
            "message": row["message"],
            "error_code": _safe_error_code(row["error_code"]),
            "result_version": (
                int(row["result_version"]) if row["result_version"] is not None else None
            ),
            "source_config_revision": int(row["source_config_revision"]),
            **{
                key: _timestamp(row[key])
                for key in ("created_at", "started_at", "updated_at", "finished_at")
            },
        }

    @classmethod
    def _price_list_public(cls, row, *, latest_job=None) -> dict:
        active_version = int(row["active_version_number"])
        artifact_size = (
            int(row["artifact_size"]) if row["artifact_size"] is not None else None
        )
        artifact_encoding = row["artifact_encoding"]
        artifact_stored_size = (
            int(row["artifact_stored_size"])
            if row["artifact_stored_size"] is not None else None
        )
        # Rows written briefly by the previous release during a rolling deploy
        # use the pre-encoding representation, which is unambiguously identity.
        if artifact_size is not None and artifact_encoding is None:
            artifact_encoding = "identity"
            artifact_stored_size = artifact_size
        if latest_job is not None and latest_job["status"] in {"queued", "running", "error"}:
            status = latest_job["status"]
        else:
            status = "ready" if active_version > 0 else "pending"
        return {
            "id": str(row["id"]),
            "supplier_id": str(row["supplier_id"]),
            "supplier_name": row.get("supplier_name", ""),
            "name": row["name"],
            "provider": row["provider"],
            "feed_role": row["feed_role"],
            "source_type": row["source_type"],
            "status": status,
            "file_name": row["original_filename"],
            "media_type": row["media_type"],
            "file_format": row["file_format"],
            "artifact_sha256": row["artifact_sha256"],
            "artifact_size": artifact_size,
            "artifact_encoding": artifact_encoding,
            "artifact_stored_size": artifact_stored_size,
            "row_count": int(row["product_count"]),
            "source_host": row["source_host"],
            "source_config_revision": int(row["source_config_revision"]),
            "active_version_number": active_version,
            "last_checked_at": _timestamp(row["last_checked_at"]),
            "last_success_at": _timestamp(row["last_success_at"]),
            "latest_job": cls._job_public(latest_job) if latest_job is not None else None,
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

    @staticmethod
    def _list_columns():
        return [
            column for column in price_lists.c
            if column.name not in {"artifact_bytes", "source_config_encrypted"}
        ]

    @staticmethod
    def _latest_jobs(connection, price_list_ids) -> dict[UUID, dict]:
        if not price_list_ids:
            return {}
        rows = connection.execute(select(refresh_jobs).where(
            refresh_jobs.c.price_list_id.in_(tuple(price_list_ids)),
        ).order_by(
            refresh_jobs.c.created_at.desc(), refresh_jobs.c.id.desc(),
        )).mappings()
        latest = {}
        for row in rows:
            current = latest.setdefault(row["price_list_id"], {**row, "_history": []})
            if (row["id"] != current["id"] and row["total_bytes"]
                    and row["source_config_revision"] == current["source_config_revision"]
                    and len(current["_history"]) < 5):
                current["_history"].append(row)
        return latest

    @staticmethod
    def _insert_products(
        connection,
        *,
        organization_id: UUID,
        seller_id: UUID,
        price_list_id: UUID,
        version_number: int,
        normalized_products: Sequence[dict],
        now: datetime,
    ) -> None:
        batch: list[dict] = []
        batch_bytes = 0
        for product in normalized_products:
            canonical_bytes = len(str(product.get("canonical_json", "")).encode("utf-8"))
            if batch and (
                len(batch) >= PRODUCT_INSERT_BATCH_ROWS
                or batch_bytes + canonical_bytes > PRODUCT_INSERT_BATCH_BYTES
            ):
                connection.execute(products.insert(), batch)
                batch.clear()
                batch_bytes = 0
            batch.append({
                "id": uuid4(),
                "organization_id": organization_id,
                "seller_id": seller_id,
                "price_list_id": price_list_id,
                "version_number": version_number,
                "created_at": now,
                **product,
            })
            batch_bytes += canonical_bytes
            if batch_bytes >= PRODUCT_INSERT_BATCH_BYTES:
                connection.execute(products.insert(), batch)
                batch.clear()
                batch_bytes = 0
        if batch:
            connection.execute(products.insert(), batch)

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
                *self._list_columns(),
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
            latest = self._latest_jobs(connection, [row["id"] for row in list_rows])
        return {
            "suppliers": [
                self._supplier_public(row, price_list_count=row["price_list_count"])
                for row in supplier_rows
            ],
            "price_lists": [
                self._price_list_public(row, latest_job=latest.get(row["id"]))
                for row in list_rows
            ],
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
            # The supplier lock stabilizes the set against new list creation. For
            # existing lists follow the worker order (job -> list), then recheck
            # without locking jobs to cover refreshes created while lists were free.
            list_id_rows = connection.execute(select(
                price_lists.c.id,
                price_lists.c.provider,
                price_lists.c.feed_role,
            ).where(
                price_lists.c.supplier_id == supplier_id,
                *self._scope(price_lists, organization_id, seller_id),
            ).order_by(price_lists.c.id)).all()
            list_ids = [item.id for item in list_id_rows]
            if self._lock_active_refreshes(
                connection, organization_id, seller_id, list_ids,
            ):
                raise CatalogRefreshInProgressError(
                    "Attendi il completamento dell'aggiornamento del listino."
                )
            list_rows = connection.execute(select(
                price_lists.c.id,
                price_lists.c.provider,
                price_lists.c.feed_role,
            ).where(
                price_lists.c.id.in_(list_ids),
                price_lists.c.supplier_id == supplier_id,
                *self._scope(price_lists, organization_id, seller_id),
            ).order_by(price_lists.c.id).with_for_update()).all() if list_ids else []
            list_ids = [item.id for item in list_rows]
            if self._has_active_refresh(
                connection, organization_id, seller_id, list_ids,
            ):
                raise CatalogRefreshInProgressError(
                    "Attendi il completamento dell'aggiornamento del listino."
                )
            refresh_light_costs = any(
                item.provider == "innpro" and item.feed_role == "light"
                for item in list_rows
            )
            if refresh_light_costs:
                self._lock_light_cost_scope(
                    connection,
                    organization_id,
                    seller_id,
                    provider="innpro",
                    feed_role="light",
                )
            connection.execute(suppliers.delete().where(
                suppliers.c.id == supplier_id,
                *self._scope(suppliers, organization_id, seller_id),
            ))
            if refresh_light_costs:
                self._refresh_light_order_costs(
                    connection,
                    organization_id,
                    seller_id,
                    provider="innpro",
                    feed_role="light",
                )
        return {
            "id": str(supplier_id),
            "name": row["name"],
            "price_lists_deleted": len(list_ids),
        }

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
        normalized_products: Sequence[dict],
        provider: str = "generic",
        feed_role: str = "standard",
        artifact_encoding: str | None = None,
        artifact_size: int | None = None,
        artifact_sha256: str | None = None,
    ) -> UUID:
        price_list_id = uuid4()
        now = datetime.now(UTC)
        encoded = _durable_artifact(
            artifact,
            artifact_encoding=artifact_encoding,
            artifact_size=artifact_size,
            artifact_sha256=artifact_sha256,
        )
        artifact_values = {
            "original_filename": original_filename,
            "media_type": media_type,
            "file_format": file_format,
            **_artifact_persistence_values(encoded),
            "product_count": len(normalized_products),
        }
        with self.engine.begin() as connection:
            self._require_profile(connection, organization_id, seller_id)
            supplier = connection.execute(select(suppliers.c.id).where(
                suppliers.c.id == supplier_id,
                *self._scope(suppliers, organization_id, seller_id),
            ).with_for_update()).first()
            if supplier is None:
                raise CatalogSupplierNotFoundError("Fornitore non disponibile.")
            self._lock_light_cost_scope(
                connection,
                organization_id,
                seller_id,
                provider=provider,
                feed_role=feed_role,
            )
            artifact_values["artifact_bytes"] = artifact_write_value(connection, encoded.content)
            connection.execute(price_lists.insert().values(
                id=price_list_id,
                organization_id=organization_id,
                seller_id=seller_id,
                supplier_id=supplier_id,
                name=name,
                provider=provider,
                feed_role=feed_role,
                source_type="upload",
                source_config_encrypted=None,
                source_host="",
                source_config_revision=1,
                active_version_number=1,
                last_checked_at=now,
                last_success_at=now,
                **artifact_values,
                created_at=now,
                updated_at=now,
            ))
            seeded_version = connection.scalar(select(versions.c.id).where(
                versions.c.price_list_id == price_list_id,
                versions.c.version_number == 1,
            ))
            if seeded_version is None:
                # Alembic installs a bridge trigger so the previous release can
                # keep accepting uploads during a rolling deploy. Unit-test and
                # local schemas created directly from metadata do not have that
                # migration trigger, so retain an explicit portable fallback.
                connection.execute(versions.insert().values(
                    id=price_list_id,
                    organization_id=organization_id,
                    seller_id=seller_id,
                    price_list_id=price_list_id,
                    version_number=1,
                    provider=provider,
                    feed_role=feed_role,
                    **artifact_values,
                    created_at=now,
                ))
            self._insert_products(
                connection,
                organization_id=organization_id,
                seller_id=seller_id,
                price_list_id=price_list_id,
                version_number=1,
                normalized_products=normalized_products,
                now=now,
            )
            self._refresh_light_order_costs(
                connection,
                organization_id,
                seller_id,
                provider=provider,
                feed_role=feed_role,
            )
        return price_list_id

    def create_url_price_list(
        self,
        organization_id: UUID,
        seller_id: UUID,
        supplier_id: UUID,
        *,
        name: str,
        source_config_encrypted: str,
        source_host: str,
        requested_by: UUID,
        realm: str,
        provider: str = "generic",
        feed_role: str = "standard",
    ) -> dict:
        price_list_id = uuid4()
        job_id = uuid4()
        now = datetime.now(UTC)
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
                provider=provider,
                feed_role=feed_role,
                source_type="url",
                source_config_encrypted=source_config_encrypted,
                source_host=source_host,
                source_config_revision=1,
                active_version_number=0,
                product_count=0,
                created_at=now,
                updated_at=now,
            ))
            job = self._new_job(
                job_id=job_id,
                organization_id=organization_id,
                seller_id=seller_id,
                price_list_id=price_list_id,
                requested_by=requested_by,
                realm=realm,
                source_config_revision=1,
                now=now,
            )
            connection.execute(refresh_jobs.insert().values(**job))
        return {**self._job_public(job), "created_job": True}

    @staticmethod
    def _new_job(
        *,
        job_id: UUID,
        organization_id: UUID,
        seller_id: UUID,
        price_list_id: UUID,
        requested_by: UUID,
        realm: str,
        source_config_revision: int,
        now: datetime,
    ) -> dict:
        return {
            "id": job_id,
            "organization_id": organization_id,
            "seller_id": seller_id,
            "price_list_id": price_list_id,
            "requested_by": requested_by,
            "realm": realm,
            "source_config_revision": source_config_revision,
            "status": "queued",
            "processed_bytes": 0,
            "total_bytes": None,
            "message": "Aggiornamento listino in coda.",
            "error_code": None,
            "result_version": None,
            "created_at": now,
            "started_at": None,
            "updated_at": now,
            "finished_at": None,
        }

    def create_refresh_job(
        self,
        organization_id: UUID,
        seller_id: UUID,
        price_list_id: UUID,
        *,
        requested_by: UUID,
        realm: str,
        expected_config_revision: int | None = None,
    ) -> dict:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(select(
                price_lists.c.id,
                price_lists.c.source_type,
                price_lists.c.source_config_revision,
            ).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            ).with_for_update()).mappings().first()
            if row is None or row["source_type"] != "url":
                raise CatalogPriceListNotFoundError("Listino URL non disponibile.")
            revision = int(row["source_config_revision"])
            if expected_config_revision is not None and revision != expected_config_revision:
                raise CatalogSourceRevisionMismatchError(
                    "La configurazione del listino è cambiata. Aggiorna la pagina e riprova."
                )
            active = connection.execute(select(refresh_jobs).where(
                refresh_jobs.c.price_list_id == price_list_id,
                *self._scope(refresh_jobs, organization_id, seller_id),
                refresh_jobs.c.status.in_(("queued", "running")),
            ).order_by(refresh_jobs.c.created_at.desc()).limit(1)).mappings().first()
            if active is not None:
                return {**self._job_public(active), "created_job": False}
            job = self._new_job(
                job_id=uuid4(),
                organization_id=organization_id,
                seller_id=seller_id,
                price_list_id=price_list_id,
                requested_by=requested_by,
                realm=realm,
                source_config_revision=revision,
                now=now,
            )
            connection.execute(refresh_jobs.insert().values(**job))
        return {**self._job_public(job), "created_job": True}

    def url_source_configuration(
        self,
        organization_id: UUID,
        seller_id: UUID,
        price_list_id: UUID,
        *,
        expected_config_revision: int,
    ) -> dict:
        """Internal credential read for a compare-and-swap source update."""
        with self.engine.connect() as connection:
            self._require_profile(connection, organization_id, seller_id)
            row = connection.execute(select(
                price_lists.c.source_type,
                price_lists.c.source_config_encrypted,
                price_lists.c.source_host,
                price_lists.c.source_config_revision,
            ).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            )).mappings().first()
        if row is None or row["source_type"] != "url":
            raise CatalogPriceListNotFoundError("Listino URL non disponibile.")
        if int(row["source_config_revision"]) != expected_config_revision:
            raise CatalogSourceRevisionMismatchError(
                "La configurazione del listino è cambiata. Aggiorna la pagina e riprova."
            )
        return {
            "source_config_encrypted": row["source_config_encrypted"],
            "source_host": row["source_host"],
            "source_config_revision": int(row["source_config_revision"]),
        }

    def update_url_source(
        self,
        organization_id: UUID,
        seller_id: UUID,
        price_list_id: UUID,
        *,
        source_config_encrypted: str,
        source_host: str,
        expected_config_revision: int,
    ) -> int:
        """Atomically replace a URL source while retaining its active snapshot."""
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            self._require_profile(connection, organization_id, seller_id)
            row = connection.execute(select(
                price_lists.c.id,
                price_lists.c.source_type,
                price_lists.c.source_config_revision,
            ).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            ).with_for_update()).mappings().first()
            if row is None or row["source_type"] != "url":
                raise CatalogPriceListNotFoundError("Listino URL non disponibile.")
            if int(row["source_config_revision"]) != expected_config_revision:
                raise CatalogSourceRevisionMismatchError(
                    "La configurazione del listino è cambiata. Aggiorna la pagina e riprova."
                )
            # The price-list row lock serializes refresh creation. Keep this job
            # lookup non-locking because worker activation locks job then list.
            active_job = connection.execute(select(refresh_jobs.c.id).where(
                refresh_jobs.c.price_list_id == price_list_id,
                *self._scope(refresh_jobs, organization_id, seller_id),
                refresh_jobs.c.status.in_(("queued", "running")),
            ).limit(1)).first()
            if active_job is not None:
                raise CatalogRefreshInProgressError(
                    "Attendi il completamento dell'aggiornamento del listino."
                )
            next_revision = expected_config_revision + 1
            result = connection.execute(price_lists.update().where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
                price_lists.c.source_type == "url",
                price_lists.c.source_config_revision == expected_config_revision,
            ).values(
                source_config_encrypted=source_config_encrypted,
                source_host=source_host,
                source_config_revision=next_revision,
                updated_at=now,
            ))
            if result.rowcount != 1:
                raise CatalogSourceRevisionMismatchError(
                    "La configurazione del listino è cambiata. Aggiorna la pagina e riprova."
                )
        return next_revision

    def job(
        self, organization_id: UUID, seller_id: UUID, job_id: UUID,
    ) -> dict:
        with self.engine.connect() as connection:
            row = connection.execute(select(refresh_jobs).where(
                refresh_jobs.c.id == job_id,
                *self._scope(refresh_jobs, organization_id, seller_id),
            )).mappings().first()
            if row is not None:
                history = connection.execute(select(refresh_jobs).where(
                    *self._scope(refresh_jobs, organization_id, seller_id),
                    refresh_jobs.c.price_list_id == row["price_list_id"],
                    refresh_jobs.c.source_config_revision == row["source_config_revision"],
                    refresh_jobs.c.created_at < row["created_at"],
                    refresh_jobs.c.total_bytes > 0,
                ).order_by(refresh_jobs.c.created_at.desc()).limit(5)).mappings().all()
                row = {**row, "_history": history}
        if row is None:
            raise CatalogRefreshJobNotFoundError("Aggiornamento listino non disponibile.")
        return self._job_public(row)

    def source_for_job(self, job_id: UUID) -> dict:
        """Internal worker read. The encrypted source never crosses the API boundary."""
        with self.engine.connect() as connection:
            row = connection.execute(select(
                refresh_jobs.c.id,
                refresh_jobs.c.organization_id,
                refresh_jobs.c.seller_id,
                refresh_jobs.c.price_list_id,
                refresh_jobs.c.requested_by,
                refresh_jobs.c.realm,
                refresh_jobs.c.source_config_revision,
                refresh_jobs.c.status,
                price_lists.c.source_type,
                price_lists.c.source_config_revision.label(
                    "current_source_config_revision"
                ),
                price_lists.c.provider,
                price_lists.c.feed_role,
                price_lists.c.source_config_encrypted,
                price_lists.c.source_host,
            ).join(
                price_lists,
                (price_lists.c.id == refresh_jobs.c.price_list_id)
                & (price_lists.c.organization_id == refresh_jobs.c.organization_id)
                & (price_lists.c.seller_id == refresh_jobs.c.seller_id),
            ).where(refresh_jobs.c.id == job_id)).mappings().first()
        if row is None:
            raise CatalogRefreshJobNotFoundError("Aggiornamento listino non disponibile.")
        return dict(row)

    def claim_job(
        self,
        organization_id: UUID,
        seller_id: UUID,
        job_id: UUID,
    ) -> bool:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(refresh_jobs.update().where(
                refresh_jobs.c.id == job_id,
                *self._scope(refresh_jobs, organization_id, seller_id),
                refresh_jobs.c.status == "queued",
            ).values(
                status="running",
                started_at=now,
                updated_at=now,
                message="Download listino in corso.",
            ))
        return result.rowcount == 1

    def job_progress(
        self,
        organization_id: UUID,
        seller_id: UUID,
        job_id: UUID,
        *,
        processed_bytes: int | None = None,
        total_bytes: int | None = None,
        message: str | None = None,
    ) -> None:
        values = {"updated_at": datetime.now(UTC)}
        if processed_bytes is not None:
            values["processed_bytes"] = processed_bytes
        if total_bytes is not None:
            values["total_bytes"] = total_bytes
        if message is not None:
            values["message"] = message
        with self.engine.begin() as connection:
            result = connection.execute(refresh_jobs.update().where(
                refresh_jobs.c.id == job_id,
                *self._scope(refresh_jobs, organization_id, seller_id),
                refresh_jobs.c.status == "running",
            ).values(**values))
        if result.rowcount != 1:
            raise CatalogRefreshJobNotFoundError("Aggiornamento listino non disponibile.")

    def fail_job(
        self,
        organization_id: UUID,
        seller_id: UUID,
        job_id: UUID,
        *,
        error_code: str,
        message: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            job = connection.execute(select(refresh_jobs.c.price_list_id).where(
                refresh_jobs.c.id == job_id,
                *self._scope(refresh_jobs, organization_id, seller_id),
                refresh_jobs.c.status.in_(("queued", "running")),
            ).with_for_update()).mappings().first()
            if job is None:
                raise CatalogRefreshJobNotFoundError("Aggiornamento listino non disponibile.")
            connection.execute(refresh_jobs.update().where(
                refresh_jobs.c.id == job_id,
                *self._scope(refresh_jobs, organization_id, seller_id),
            ).values(
                status="error",
                error_code=error_code,
                message=message,
                updated_at=now,
                finished_at=now,
            ))
            connection.execute(price_lists.update().where(
                price_lists.c.id == job["price_list_id"],
                *self._scope(price_lists, organization_id, seller_id),
            ).values(last_checked_at=now, updated_at=now))

    def activate_remote_version(
        self,
        job_id: UUID,
        *,
        expected_config_revision: int,
        original_filename: str,
        media_type: str,
        file_format: str,
        artifact: bytes,
        normalized_products: Sequence[dict],
        artifact_encoding: str | None = None,
        artifact_size: int | None = None,
        artifact_sha256: str | None = None,
    ) -> dict:
        now = datetime.now(UTC)
        encoded = _durable_artifact(
            artifact,
            artifact_encoding=artifact_encoding,
            artifact_size=artifact_size,
            artifact_sha256=artifact_sha256,
        )
        digest = encoded.raw_sha256
        with self.engine.begin() as connection:
            job = connection.execute(select(refresh_jobs).where(
                refresh_jobs.c.id == job_id,
                refresh_jobs.c.status == "running",
            ).with_for_update()).mappings().first()
            if job is None:
                raise CatalogRefreshJobNotFoundError("Aggiornamento listino non disponibile.")
            current = connection.execute(select(
                *[c for c in price_lists.c if c.name != "artifact_bytes"],
            ).where(
                price_lists.c.id == job["price_list_id"],
                price_lists.c.organization_id == job["organization_id"],
                price_lists.c.seller_id == job["seller_id"],
            ).with_for_update()).mappings().first()
            if current is None or current["source_type"] != "url":
                raise CatalogPriceListNotFoundError("Listino URL non disponibile.")
            if (
                int(job["source_config_revision"]) != expected_config_revision
                or int(current["source_config_revision"]) != expected_config_revision
            ):
                raise CatalogSourceRevisionMismatchError(
                    "La configurazione del listino è cambiata durante l'aggiornamento."
                )
            self._lock_light_cost_scope(
                connection,
                current["organization_id"],
                current["seller_id"],
                provider=current["provider"],
                feed_role=current["feed_role"],
            )

            duplicate = connection.execute(select(
                *[c for c in versions.c if c.name != "artifact_bytes"],
            ).where(
                versions.c.price_list_id == current["id"],
                versions.c.organization_id == current["organization_id"],
                versions.c.seller_id == current["seller_id"],
                versions.c.artifact_sha256 == digest,
            ).with_for_update()).mappings().first()
            if duplicate is None:
                version_number = int(connection.scalar(select(
                    func.coalesce(func.max(versions.c.version_number), 0),
                ).where(
                    versions.c.price_list_id == current["id"],
                    versions.c.organization_id == current["organization_id"],
                    versions.c.seller_id == current["seller_id"],
                )) or 0) + 1
                artifact_values = {
                    "original_filename": original_filename,
                    "media_type": media_type,
                    "file_format": file_format,
                    **_artifact_persistence_values(encoded),
                    "product_count": len(normalized_products),
                }
                artifact_values["artifact_bytes"] = artifact_write_value(
                    connection, encoded.content,
                )
                connection.execute(versions.insert().values(
                    id=uuid4(),
                    organization_id=current["organization_id"],
                    seller_id=current["seller_id"],
                    price_list_id=current["id"],
                    version_number=version_number,
                    provider=current["provider"],
                    feed_role=current["feed_role"],
                    **artifact_values,
                    created_at=now,
                ))
                self._insert_products(
                    connection,
                    organization_id=current["organization_id"],
                    seller_id=current["seller_id"],
                    price_list_id=current["id"],
                    version_number=version_number,
                    normalized_products=normalized_products,
                    now=now,
                )
            else:
                version_number = int(duplicate["version_number"])
                artifact_values = {
                    key: duplicate[key]
                    for key in (
                        "original_filename", "media_type", "file_format", "artifact_sha256",
                        "artifact_size", "artifact_encoding", "artifact_stored_size",
                        "product_count",
                    )
                }
                artifact_values["artifact_bytes"] = select(versions.c.artifact_bytes).where(
                    versions.c.id == duplicate["id"],
                    versions.c.organization_id == current["organization_id"],
                    versions.c.seller_id == current["seller_id"],
                ).scalar_subquery()

            connection.execute(price_lists.update().where(
                price_lists.c.id == current["id"],
                price_lists.c.organization_id == current["organization_id"],
                price_lists.c.seller_id == current["seller_id"],
                price_lists.c.source_config_revision == expected_config_revision,
            ).values(
                active_version_number=version_number,
                last_checked_at=now,
                last_success_at=now,
                updated_at=now,
                **artifact_values,
            ))
            self._refresh_light_order_costs(
                connection,
                current["organization_id"],
                current["seller_id"],
                provider=current["provider"],
                feed_role=current["feed_role"],
            )
            connection.execute(refresh_jobs.update().where(
                refresh_jobs.c.id == job_id,
                refresh_jobs.c.status == "running",
            ).values(
                status="done",
                processed_bytes=encoded.raw_size,
                total_bytes=encoded.raw_size,
                message=(
                    "Listino invariato: versione già acquisita."
                    if duplicate is not None
                    else "Listino aggiornato."
                ),
                error_code=None,
                result_version=version_number,
                updated_at=now,
                finished_at=now,
            ))
        return {"duplicate": duplicate is not None, "version_number": version_number}

    def version_metadata(
        self, organization_id: UUID, seller_id: UUID, price_list_id: UUID,
    ) -> list[dict]:
        with self.engine.connect() as connection:
            found = connection.execute(select(price_lists.c.id).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            )).first()
            if found is None:
                raise CatalogPriceListNotFoundError("Listino non disponibile.")
            rows = connection.execute(select(
                *[column for column in versions.c if column.name != "artifact_bytes"],
            ).where(
                versions.c.price_list_id == price_list_id,
                *self._scope(versions, organization_id, seller_id),
            ).order_by(versions.c.version_number.desc())).mappings().all()
        return [{
            "id": str(row["id"]),
            "price_list_id": str(row["price_list_id"]),
            "version_number": int(row["version_number"]),
            "provider": row["provider"],
            "feed_role": row["feed_role"],
            "file_name": row["original_filename"],
            "media_type": row["media_type"],
            "file_format": row["file_format"],
            "artifact_sha256": row["artifact_sha256"],
            "artifact_size": int(row["artifact_size"]),
            "artifact_encoding": row["artifact_encoding"] or "identity",
            "artifact_stored_size": (
                int(row["artifact_stored_size"])
                if row["artifact_stored_size"] is not None else int(row["artifact_size"])
            ),
            "row_count": int(row["product_count"]),
            "created_at": _timestamp(row["created_at"]),
        } for row in rows]

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
                *self._list_columns(),
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
            latest = self._latest_jobs(connection, [price_list_id]).get(price_list_id)
            product_rows = connection.execute(select(products).where(
                products.c.price_list_id == price_list_id,
                products.c.version_number == row["active_version_number"],
                *self._scope(products, organization_id, seller_id),
            ).order_by(products.c.source_row).limit(limit)).mappings().all()
        return {
            "price_list": self._price_list_public(row, latest_job=latest),
            "products": [self._product_public(item) for item in product_rows],
            "preview_count": len(product_rows),
            "row_count": int(row["product_count"]),
        }

    def delete_price_list(
        self, organization_id: UUID, seller_id: UUID, price_list_id: UUID,
    ) -> dict:
        with self.engine.begin() as connection:
            found = connection.execute(select(price_lists.c.id).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            )).first()
            if found is None:
                raise CatalogPriceListNotFoundError("Listino non disponibile.")
            # The worker activates in job -> list order. Take any active job locks
            # first so the delete cascade can never invert that order.
            if self._lock_active_refreshes(
                connection, organization_id, seller_id, [price_list_id],
            ):
                raise CatalogRefreshInProgressError(
                    "Attendi il completamento dell'aggiornamento del listino."
                )
            row = connection.execute(select(
                price_lists.c.id,
                price_lists.c.name,
                price_lists.c.product_count,
                price_lists.c.provider,
                price_lists.c.feed_role,
            ).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            ).with_for_update()).mappings().first()
            if row is None:
                raise CatalogPriceListNotFoundError("Listino non disponibile.")
            # Refresh creation locks the list first. Rechecking while holding it
            # covers a job inserted after the first job-lock query; this read stays
            # non-locking because an active result aborts before the cascade.
            if self._has_active_refresh(
                connection, organization_id, seller_id, [price_list_id],
            ):
                raise CatalogRefreshInProgressError(
                    "Attendi il completamento dell'aggiornamento del listino."
                )
            self._lock_light_cost_scope(
                connection,
                organization_id,
                seller_id,
                provider=row["provider"],
                feed_role=row["feed_role"],
            )
            result = connection.execute(price_lists.delete().where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            ))
            if result.rowcount != 1:
                raise CatalogPriceListNotFoundError("Listino non disponibile.")
            self._refresh_light_order_costs(
                connection,
                organization_id,
                seller_id,
                provider=row["provider"],
                feed_role=row["feed_role"],
            )
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "products_deleted": int(row["product_count"]),
        }

    def raw_artifact(
        self, organization_id: UUID, seller_id: UUID, price_list_id: UUID,
    ) -> bytes:
        """Internal retrieval used by worker adapters; never expose from the API."""
        with self.engine.connect() as connection:
            row = connection.execute(select(
                price_lists.c.artifact_bytes,
                price_lists.c.artifact_encoding,
                price_lists.c.artifact_stored_size,
                price_lists.c.artifact_size,
                price_lists.c.artifact_sha256,
            ).where(
                price_lists.c.id == price_list_id,
                *self._scope(price_lists, organization_id, seller_id),
            )).mappings().first()
        if row is None or row["artifact_bytes"] is None:
            raise CatalogPriceListNotFoundError("Listino non disponibile.")
        stored = bytes(row["artifact_bytes"])
        encoding = row["artifact_encoding"] or "identity"
        stored_size = (
            int(row["artifact_stored_size"])
            if row["artifact_stored_size"] is not None else int(row["artifact_size"])
        )
        if len(stored) != stored_size:
            raise CatalogArtifactEncodingError("Il listino archiviato risulta incompleto.")
        content = decode_catalog_artifact(stored, encoding)
        if (
            len(content) != int(row["artifact_size"])
            or hashlib.sha256(content).hexdigest() != row["artifact_sha256"]
        ):
            raise CatalogArtifactEncodingError("Il listino archiviato non supera il controllo.")
        return content
