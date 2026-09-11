from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import marketplace_hub_core.catalogs.repository as catalog_repository
import pytest
import test_tenancy_api
from marketplace_hub_core.catalogs.repository import (
    CatalogPriceListNotFoundError,
    CatalogSupplierNotFoundError,
    SqlCatalogsRepository,
)
from marketplace_hub_core.catalogs.schema import (
    seller_price_list_products,
    seller_price_lists,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

workspace = test_tenancy_api.workspace


def _product(source_row: int, *, ean: str = "0012345678901") -> dict:
    return {
        "source_row": source_row,
        "ean": ean,
        "sku": "SKU-1",
        "name": "Prodotto",
        "cost": Decimal("12.34"),
        "shipping_cost": Decimal("1.20"),
        "total_cost": Decimal("13.54"),
        "quantity": Decimal("7"),
        "canonical_json": '{"ean":"0012345678901"}',
    }


def _scope(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    return organization_id, seller_id


def test_repository_price_list_write_is_atomic_when_a_product_row_fails(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    organization_id, seller_id = _scope(workspace)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="Fornitore", notes="",
    )

    with pytest.raises(IntegrityError):
        repository.add_price_list(
            organization_id,
            seller_id,
            supplier_id,
            name="Listino non valido",
            original_filename="catalogo.csv",
            media_type="text/csv",
            file_format="csv",
            artifact=b"ean;sku;cost\n0012345678901;SKU-1;12,34\n",
            normalized_products=[_product(2), _product(2, ean="0012345678902")],
        )

    with workspace.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(seller_price_lists)) == 0
        assert connection.scalar(
            select(func.count()).select_from(seller_price_list_products)
        ) == 0
    assert repository.dashboard(organization_id, seller_id)["price_lists"] == []


def test_repository_artifact_and_mutations_cannot_cross_tenant_scope(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    first_organization, first_seller = _scope(workspace)
    second_organization, second_seller = _scope(workspace)
    first_supplier = repository.add_supplier(
        first_organization, first_seller, name="Primo", notes="",
    )
    second_supplier = repository.add_supplier(
        second_organization, second_seller, name="Secondo", notes="",
    )
    artifact = b"ean;sku;cost\n0012345678901;SKU-1;12,34\n"
    price_list_id = repository.add_price_list(
        first_organization,
        first_seller,
        first_supplier,
        name="Privato",
        original_filename="catalogo.csv",
        media_type="text/csv",
        file_format="csv",
        artifact=artifact,
        normalized_products=[_product(2)],
    )

    assert repository.raw_artifact(first_organization, first_seller, price_list_id) == artifact
    with pytest.raises(CatalogPriceListNotFoundError):
        repository.raw_artifact(second_organization, second_seller, price_list_id)
    with pytest.raises(CatalogSupplierNotFoundError):
        repository.delete_supplier(
            second_organization,
            second_seller,
            first_supplier,
            confirmation="Primo",
        )
    with pytest.raises(CatalogSupplierNotFoundError):
        repository.add_price_list(
            first_organization,
            first_seller,
            second_supplier,
            name="Intrusione",
            original_filename="catalogo.csv",
            media_type="text/csv",
            file_format="csv",
            artifact=artifact,
            normalized_products=[_product(2)],
        )

    dashboard = repository.dashboard(first_organization, first_seller)
    assert [item["id"] for item in dashboard["suppliers"]] == [str(first_supplier)]
    assert [item["id"] for item in dashboard["price_lists"]] == [str(price_list_id)]


def test_product_inserts_are_bounded_by_canonical_payload_bytes(monkeypatch):
    class RecordingConnection:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        def execute(self, _statement, rows) -> None:
            self.batch_sizes.append(len(rows))

    connection = RecordingConnection()
    rows = [_product(index + 1) for index in range(3)]
    for row in rows:
        row["canonical_json"] = "x" * 6
    monkeypatch.setattr(catalog_repository, "PRODUCT_INSERT_BATCH_BYTES", 10)
    monkeypatch.setattr(catalog_repository, "PRODUCT_INSERT_BATCH_ROWS", 100)

    SqlCatalogsRepository._insert_products(
        connection,
        organization_id=uuid4(),
        seller_id=uuid4(),
        price_list_id=uuid4(),
        version_number=1,
        normalized_products=rows,
        now=datetime.now(UTC),
    )

    assert connection.batch_sizes == [1, 1, 1]
