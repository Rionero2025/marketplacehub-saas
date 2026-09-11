from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import marketplace_hub_core.catalogs.costing as catalog_costing
import pytest
import test_orders_api
import test_tenancy_api
from marketplace_hub_core.catalogs.costing import SqlCatalogCostResolver
from marketplace_hub_core.catalogs.repository import SqlCatalogsRepository
from marketplace_hub_core.catalogs.schema import (
    seller_price_list_products,
    seller_price_list_versions,
    seller_price_lists,
    seller_suppliers,
)
from marketplace_hub_core.orders.repository import SqlOrdersRepository
from marketplace_hub_core.orders.schema import order_lines
from marketplace_hub_core.tenancy.schema import metadata, organizations, seller_profiles
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError

workspace = test_tenancy_api.workspace
configured = test_orders_api.configured

EAN = "6973163547233"


def _product(*, ean: str = EAN, sku: str = "INNPRO-ARTICLE", cost: str) -> dict:
    amount = Decimal(cost)
    return {
        "source_row": 1,
        "ean": ean,
        "sku": sku,
        "name": "Prodotto InnPro",
        "cost": amount,
        "shipping_cost": Decimal("0"),
        "total_cost": amount,
        "quantity": Decimal("10"),
        "canonical_json": '{"provider":"innpro"}',
    }


def _add_innpro_list(
    engine,
    organization_id,
    seller_id,
    *,
    role: str,
    cost: str,
    ean: str = EAN,
    sku: str = "INNPRO-ARTICLE",
    successful_at: datetime | None = None,
    supplier_name: str | None = None,
    price_list_name: str | None = None,
):
    label = role.upper()
    supplier_id = uuid4()
    price_list_id = uuid4()
    now = successful_at or datetime.now(UTC)
    artifact = f"<offer role='{role}'/>".encode()
    artifact_values = {
        "original_filename": f"innpro-{role}.xml",
        "media_type": "application/xml",
        "file_format": "xml",
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "artifact_size": len(artifact),
        "artifact_encoding": "identity",
        "artifact_stored_size": len(artifact),
        "artifact_bytes": artifact,
        "product_count": 1,
    }
    with engine.begin() as connection:
        connection.execute(seller_suppliers.insert().values(
            id=supplier_id,
            organization_id=organization_id,
            seller_id=seller_id,
            name=supplier_name or f"InnPro {label}",
            notes="",
            created_at=now,
            updated_at=now,
        ))
        connection.execute(seller_price_lists.insert().values(
            id=price_list_id,
            organization_id=organization_id,
            seller_id=seller_id,
            supplier_id=supplier_id,
            name=price_list_name or f"Listino InnPro {label}",
            provider="innpro",
            feed_role=role,
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
        connection.execute(seller_price_list_versions.insert().values(
            id=uuid4(),
            organization_id=organization_id,
            seller_id=seller_id,
            price_list_id=price_list_id,
            version_number=1,
            provider="innpro",
            feed_role=role,
            **artifact_values,
            created_at=now,
        ))
        connection.execute(seller_price_list_products.insert().values(
            id=uuid4(),
            organization_id=organization_id,
            seller_id=seller_id,
            price_list_id=price_list_id,
            version_number=1,
            created_at=now,
            **_product(ean=ean, sku=sku, cost=cost),
        ))
    return price_list_id


def _order(*, ean: str = EAN, quantity: str = "3", payout: str = "50.00") -> dict:
    return {
        "ean": ean,
        "sku": "InnPro_INNPRO-ARTICLE_99.00_120.00",
        "quantity": quantity,
        "payout_amount_eur": payout,
        "purchase_cost": "297.00",
        "purchase_cost_eur": "297.00",
        "purchase_cost_source": "SKU composto: terzo valore, EUR",
        "profit_amount": "-247.00",
        "profit_amount_eur": "-247.00",
        "profit_pct": "-83.16",
        "monetary_warnings": [
            "Costo da SKU; confronto prioritario con i listini non ancora disponibile.",
            "Commissione non disponibile dall'API.",
        ],
        "details": {
            "sku_supplier": "InnPro",
            "purchase_cost_method": "SKU composto",
        },
    }


def test_only_active_innpro_light_supplies_exact_ean_cost_and_profit(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    _add_innpro_list(
        workspace.engine, organization_id, seller_id, role="full", cost="356.91",
    )
    _add_innpro_list(
        workspace.engine, organization_id, seller_id, role="light", cost="7.25",
    )

    item = _order()
    result = SqlCatalogCostResolver(workspace.engine).apply(
        organization_id, seller_id, [item],
    )

    assert result == [item]
    assert item["purchase_cost"] == item["purchase_cost_eur"] == "21.75"
    assert item["profit_amount"] == item["profit_amount_eur"] == "28.25"
    assert item["profit_pct"] == "129.89"
    assert item["purchase_cost_source"] == (
        f"Listino InnPro LIGHT · Listino InnPro LIGHT · match EAN esatto {EAN}"
    )
    assert item["details"]["purchase_unit_cost_eur"] == "7.25"
    assert item["details"]["catalog_provider"] == "innpro"
    assert item["details"]["catalog_feed_role"] == "light"
    assert item["details"]["catalog_matched_ean"] == EAN
    assert item["details"]["catalog_price_list_name"] == "Listino InnPro LIGHT"
    assert item["monetary_warnings"] == ["Commissione non disponibile dall'API."]
    assert "356.91" not in str(item)


def test_innpro_full_never_falls_back_to_embedded_sku_cost(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    _add_innpro_list(
        workspace.engine, organization_id, seller_id, role="full", cost="356.91",
    )

    item = _order()
    SqlCatalogCostResolver(workspace.engine).apply(organization_id, seller_id, [item])

    assert item["purchase_cost"] is None
    assert item["purchase_cost_eur"] is None
    assert item["profit_amount"] is None
    assert item["profit_amount_eur"] is None
    assert item["profit_pct"] is None
    assert "EAN non trovato" in item["purchase_cost_source"]
    assert "LIGHT" in item["purchase_cost_source"]
    assert item["details"]["purchase_cost_method"] == "Costo non calcolabile"
    assert item["details"]["purchase_unit_cost_eur"] is None
    assert "SKU composto" not in item["purchase_cost_source"]


def test_finn_products_is_not_misclassified_as_innpro(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    _add_innpro_list(
        workspace.engine, organization_id, seller_id, role="light", cost="1.00",
    )
    item = _order()
    item["sku"] = "Finn Products_ARTICLE_99.00_120.00"
    item["details"]["sku_supplier"] = "Finn Products"

    resolver = SqlCatalogCostResolver(workspace.engine)
    assert resolver.applies_to([item]) is False
    resolver.apply(organization_id, seller_id, [item])

    assert item["purchase_cost_eur"] == "297.00"
    assert item["purchase_cost_source"] == "SKU composto: terzo valore, EUR"
    assert "catalog_price_list_id" not in item["details"]


def test_innpro_order_rejects_provider_list_owned_by_finn_products(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    _add_innpro_list(
        workspace.engine,
        organization_id,
        seller_id,
        role="light",
        cost="1.00",
        supplier_name="Finn Products",
    )
    item = _order()

    resolver = SqlCatalogCostResolver(workspace.engine)
    assert resolver.applies_to([item]) is True
    resolver.apply(organization_id, seller_id, [item])

    assert item["purchase_cost"] is None
    assert item["purchase_cost_eur"] is None
    assert item["profit_amount"] is None
    assert item["profit_amount_eur"] is None
    assert item["profit_pct"] is None
    assert "EAN non trovato" in item["purchase_cost_source"]
    assert item["details"]["purchase_cost_method"] == "Costo non calcolabile"
    assert "catalog_price_list_id" not in item["details"]


def test_innpro_cost_match_is_tenant_scoped(workspace):
    first_organization = workspace.organization("SELLER", "Primo tenant")
    first_seller = workspace.seller(first_organization, "Primo negozio")
    second_organization = workspace.organization("SELLER", "Secondo tenant")
    second_seller = workspace.seller(second_organization, "Secondo negozio")
    _add_innpro_list(
        workspace.engine, first_organization, first_seller, role="light", cost="8.00",
    )
    _add_innpro_list(
        workspace.engine, second_organization, second_seller, role="light", cost="2.00",
    )
    resolver = SqlCatalogCostResolver(workspace.engine)

    first = _order(quantity="2", payout="30.00")
    second = _order(quantity="2", payout="30.00")
    resolver.apply(first_organization, first_seller, [first])
    resolver.apply(second_organization, second_seller, [second])

    assert first["purchase_cost_eur"] == "16.00"
    assert first["profit_amount_eur"] == "14.00"
    assert second["purchase_cost_eur"] == "4.00"
    assert second["profit_amount_eur"] == "26.00"


def test_innpro_does_not_match_catalog_sku_when_ean_differs(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    shared_sku = "MATCHING-SKU-MUST-NOT-BE-USED"
    _add_innpro_list(
        workspace.engine,
        organization_id,
        seller_id,
        role="light",
        cost="4.50",
        ean=EAN,
        sku=shared_sku,
    )
    item = _order(ean="6973163547240")
    item["sku"] = f"InnPro_{shared_sku}_99.00_120.00"

    SqlCatalogCostResolver(workspace.engine).apply(organization_id, seller_id, [item])

    assert item["purchase_cost_eur"] is None
    assert item["profit_amount_eur"] is None
    assert "EAN non trovato" in item["purchase_cost_source"]
    assert item["details"].get("catalog_matched_sku") is None


def test_innpro_uses_valid_composite_product_code_when_order_ean_is_missing(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    _add_innpro_list(
        workspace.engine, organization_id, seller_id, role="light", cost="4.50",
    )
    item = _order(ean="", quantity="2", payout="20.00")
    item["sku"] = f"InnPro_{EAN}_99.00_120.00"

    SqlCatalogCostResolver(workspace.engine).apply(organization_id, seller_id, [item])

    assert item["ean"] == EAN
    assert item["purchase_cost_eur"] == "9.00"
    assert item["details"]["catalog_matched_ean"] == EAN


def test_innpro_uses_nested_raw_ean_when_canonical_identifiers_are_missing(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    _add_innpro_list(
        workspace.engine, organization_id, seller_id, role="light", cost="4.50",
    )
    item = _order(ean="", quantity="2", payout="20.00")
    item["sku"] = "InnPro_ARTICLE-CODE_99.00_120.00"
    item["raw"] = {"product": {"identifiers": {"ean": EAN}}}

    SqlCatalogCostResolver(workspace.engine).apply(organization_id, seller_id, [item])

    assert item["ean"] == EAN
    assert item["purchase_cost_eur"] == "9.00"
    assert item["details"]["catalog_matched_ean"] == EAN


def test_missing_light_match_removes_stale_catalog_cost_and_metadata(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    list_id = _add_innpro_list(
        workspace.engine, organization_id, seller_id, role="light", cost="4.50",
    )
    resolver = SqlCatalogCostResolver(workspace.engine)
    item = _order(quantity="2", payout="20.00")
    resolver.apply(organization_id, seller_id, [item])
    assert item["details"]["catalog_price_list_id"] == str(list_id)

    with workspace.engine.begin() as connection:
        connection.execute(seller_price_list_products.delete().where(
            seller_price_list_products.c.price_list_id == list_id,
        ))
    resolver.apply(organization_id, seller_id, [item])

    assert item["purchase_cost_eur"] is None
    assert item["profit_amount_eur"] is None
    assert "catalog_price_list_id" not in item["details"]
    assert "catalog_matched_ean" not in item["details"]


def test_multiple_light_lists_use_latest_success_not_latest_metadata_edit(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    now = datetime.now(UTC)
    older_id = _add_innpro_list(
        workspace.engine,
        organization_id,
        seller_id,
        role="light",
        cost="8.10",
        successful_at=now - timedelta(days=2),
        supplier_name="InnPro Wholesale A",
        price_list_name="LIGHT precedente",
    )
    newest_id = _add_innpro_list(
        workspace.engine,
        organization_id,
        seller_id,
        role="light",
        cost="6.20",
        successful_at=now - timedelta(days=1),
        supplier_name="InnPro Wholesale B",
        price_list_name="LIGHT ultimo riuscito",
    )
    with workspace.engine.begin() as connection:
        connection.execute(seller_price_lists.update().where(
            seller_price_lists.c.id == older_id,
        ).values(updated_at=now + timedelta(days=1)))

    item = _order(quantity="2", payout="30.00")
    SqlCatalogCostResolver(workspace.engine).apply(
        organization_id, seller_id, [item],
    )

    assert item["purchase_cost_eur"] == "12.40"
    assert item["details"]["catalog_price_list_id"] == str(newest_id)
    assert item["details"]["catalog_price_list_name"] == "LIGHT ultimo riuscito"


def test_multiple_light_lists_remain_bound_to_compatible_order_supplier(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    now = datetime.now(UTC)
    italian_id = _add_innpro_list(
        workspace.engine,
        organization_id,
        seller_id,
        role="light",
        cost="7.50",
        successful_at=now - timedelta(days=1),
        supplier_name="InnPro Italia",
        price_list_name="LIGHT Italia",
    )
    _add_innpro_list(
        workspace.engine,
        organization_id,
        seller_id,
        role="light",
        cost="1.00",
        successful_at=now,
        supplier_name="InnPro Polska",
        price_list_name="LIGHT Polska",
    )
    item = _order(quantity="2", payout="30.00")
    item["details"]["sku_supplier"] = "InnPro Italia"

    SqlCatalogCostResolver(workspace.engine).apply(
        organization_id, seller_id, [item],
    )

    assert item["purchase_cost_eur"] == "15.00"
    assert item["details"]["catalog_price_list_id"] == str(italian_id)
    assert item["details"]["catalog_supplier"] == "InnPro Italia"


def test_latest_compatible_generic_innpro_supplier_keeps_legacy_precedence(workspace):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    now = datetime.now(UTC)
    _add_innpro_list(
        workspace.engine,
        organization_id,
        seller_id,
        role="light",
        cost="7.50",
        successful_at=now - timedelta(days=1),
        supplier_name="InnPro Italia",
        price_list_name="LIGHT Italia precedente",
    )
    generic_id = _add_innpro_list(
        workspace.engine,
        organization_id,
        seller_id,
        role="light",
        cost="6.00",
        successful_at=now,
        supplier_name="InnPro",
        price_list_name="LIGHT InnPro ultimo",
    )
    item = _order(quantity="2", payout="30.00")
    item["details"]["sku_supplier"] = "InnPro Italia"

    SqlCatalogCostResolver(workspace.engine).apply(
        organization_id, seller_id, [item],
    )

    assert item["purchase_cost_eur"] == "12.00"
    assert item["details"]["catalog_price_list_id"] == str(generic_id)
    assert item["details"]["catalog_supplier"] == "InnPro"


def test_innpro_lookup_failure_clears_unsafe_composite_sku_cost(workspace, monkeypatch):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    resolver = SqlCatalogCostResolver(workspace.engine)

    def unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("rolling migration")

    monkeypatch.setattr(resolver, "_innpro_candidates", unavailable)
    item = _order()
    resolver.apply(organization_id, seller_id, [item])

    assert item["purchase_cost_eur"] is None
    assert item["profit_amount_eur"] is None
    assert item["profit_pct"] is None
    assert "temporaneamente non disponibile" in item["purchase_cost_source"]
    assert item["purchase_cost_source"] in item["monetary_warnings"]


def test_order_upsert_survives_catalog_lookup_sql_error_and_fails_closed(
    workspace, monkeypatch,
):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    resolver = SqlCatalogCostResolver(workspace.engine)

    def unavailable(*_args, connection=None, **_kwargs):
        assert connection is not None
        connection.exec_driver_sql("SELECT * FROM catalog_table_not_yet_migrated")

    monkeypatch.setattr(resolver, "_innpro_candidates", unavailable)
    job = {
        "organization_id": organization_id,
        "seller_id": seller_id,
        "account_id": uuid4(),
        "environment": "live",
        "marketplace": "amazon",
    }
    item = test_orders_api.row(
        identifier="rolling-catalog-line",
        ean=EAN,
        sku="InnPro_INNPRO-ARTICLE_99.00_120.00",
        details={"sku_supplier": "InnPro"},
    )

    SqlOrdersRepository(workspace.engine).upsert_batch(
        job,
        [item],
        catalog_cost_resolver=resolver,
    )

    with workspace.engine.connect() as connection:
        saved_json = connection.scalar(select(order_lines.c.canonical_json).where(
            order_lines.c.organization_id == organization_id,
            order_lines.c.seller_id == seller_id,
        ))
    saved = json.loads(saved_json)
    assert saved["purchase_cost_eur"] is None
    assert saved["profit_amount_eur"] is None
    assert "temporaneamente non disponibile" in saved["purchase_cost_source"]


def test_orders_worker_applies_light_cost_before_persisting(configured):
    client, seller_id, organization_id, _, _ = test_orders_api.owner(configured)
    account_id = test_orders_api.account(configured, seller_id, organization_id)
    _add_innpro_list(
        configured.engine,
        organization_id,
        seller_id,
        role="light",
        cost="6.40",
    )
    configured.fetcher.items = [test_orders_api.row(
        ean=EAN,
        sku="InnPro_INNPRO-ARTICLE_99.00_120.00",
        quantity="2",
        payout_amount="35.00",
        payout_amount_eur="35.00",
        purchase_cost="198.00",
        purchase_cost_eur="198.00",
        profit_amount="-163.00",
        profit_amount_eur="-163.00",
        details={"sku_supplier": "InnPro"},
    )]

    started = test_orders_api.start(client, seller_id, account_id)
    assert started.status_code == 202
    test_orders_api.run(configured, started)
    saved = test_orders_api.read(client, seller_id, account_id).json()["items"][0]

    assert saved["purchase_cost"] == saved["purchase_cost_eur"] == "12.80"
    assert saved["profit_amount"] == saved["profit_amount_eur"] == "22.20"
    assert saved["profit_pct"] == "173.44"
    assert saved["details"]["catalog_feed_role"] == "light"
    assert saved["details"]["catalog_matched_ean"] == EAN
    assert saved["purchase_cost_source"].startswith("Listino InnPro LIGHT")


def _sync_innpro_order(configured, client, seller_id, organization_id, *, identifier):
    account_id = test_orders_api.account(configured, seller_id, organization_id)
    configured.fetcher.items = [test_orders_api.row(
        identifier=identifier,
        order_id=f"ORDER-{identifier}",
        ean=EAN,
        sku="InnPro_INNPRO-ARTICLE_99.00_120.00",
        quantity="2",
        payout_amount="35.00",
        payout_amount_eur="35.00",
        purchase_cost="198.00",
        purchase_cost_eur="198.00",
        profit_amount="-163.00",
        profit_amount_eur="-163.00",
        details={"sku_supplier": "InnPro"},
    )]
    started = test_orders_api.start(client, seller_id, account_id)
    assert started.status_code == 202
    test_orders_api.run(configured, started)
    return account_id


def _saved_order(client, seller_id, account_id):
    response = test_orders_api.read(client, seller_id, account_id)
    assert response.status_code == 200
    return response.json()["items"][0]


def test_light_upload_reprices_saved_orders_only_inside_exact_tenant(configured):
    first_client, first_seller, first_organization, _member, _user = test_orders_api.owner(
        configured,
    )
    first_account = _sync_innpro_order(
        configured,
        first_client,
        first_seller,
        first_organization,
        identifier="first-line",
    )
    second_client, second_seller, second_organization, _member, _user = test_orders_api.owner(
        configured,
    )
    second_account = _sync_innpro_order(
        configured,
        second_client,
        second_seller,
        second_organization,
        identifier="second-line",
    )
    assert _saved_order(first_client, first_seller, first_account)["purchase_cost_eur"] is None
    assert _saved_order(second_client, second_seller, second_account)["purchase_cost_eur"] is None

    repository = SqlCatalogsRepository(configured.engine)
    supplier_id = repository.add_supplier(
        first_organization, first_seller, name="InnPro", notes="",
    )
    price_list_id = repository.add_price_list(
        first_organization,
        first_seller,
        supplier_id,
        name="InnPro LIGHT attivo",
        original_filename="light.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer><products/></offer>",
        normalized_products=[_product(cost="6.40")],
        provider="innpro",
        feed_role="light",
    )

    first = _saved_order(first_client, first_seller, first_account)
    second = _saved_order(second_client, second_seller, second_account)
    assert first["purchase_cost_eur"] == "12.80"
    assert first["profit_amount_eur"] == "22.20"
    assert first["details"]["catalog_price_list_id"] == str(price_list_id)
    assert second["purchase_cost_eur"] is None
    with configured.engine.connect() as connection:
        projected = connection.execute(select(
            order_lines.c.catalog_cost,
            order_lines.c.purchase_eur,
        ).where(
            order_lines.c.organization_id == first_organization,
            order_lines.c.seller_id == first_seller,
        )).one()
    assert projected.catalog_cost is True
    assert str(projected.purchase_eur) == "12.80000000"


def test_url_light_activation_and_update_reprice_saved_order(configured):
    client, seller_id, organization_id, _member, user = test_orders_api.owner(configured)
    account_id = _sync_innpro_order(
        configured,
        client,
        seller_id,
        organization_id,
        identifier="url-line",
    )
    repository = SqlCatalogsRepository(configured.engine)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    created = repository.create_url_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="InnPro LIGHT URL",
        source_config_encrypted="opaque-source",
        source_host="feed.example.test",
        requested_by=user.id,
        realm="seller",
        provider="innpro",
        feed_role="light",
    )
    first_job = UUID(created["job_id"])
    assert repository.claim_job(organization_id, seller_id, first_job)
    repository.activate_remote_version(
        first_job,
        expected_config_revision=1,
        original_filename="light.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer version='1'/>",
        normalized_products=[_product(cost="5.25")],
    )
    assert _saved_order(client, seller_id, account_id)["purchase_cost_eur"] == "10.50"

    refresh = repository.create_refresh_job(
        organization_id,
        seller_id,
        UUID(created["price_list_id"]),
        requested_by=user.id,
        realm="seller",
    )
    second_job = UUID(refresh["job_id"])
    assert repository.claim_job(organization_id, seller_id, second_job)
    repository.activate_remote_version(
        second_job,
        expected_config_revision=1,
        original_filename="light.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer version='2'/>",
        normalized_products=[_product(cost="7.10")],
    )
    saved = _saved_order(client, seller_id, account_id)
    assert saved["purchase_cost_eur"] == "14.20"
    assert saved["profit_amount_eur"] == "20.80"
    assert saved["details"]["catalog_price_list_name"] == "InnPro LIGHT URL"

    refresh = repository.create_refresh_job(
        organization_id,
        seller_id,
        UUID(created["price_list_id"]),
        requested_by=user.id,
        realm="seller",
    )
    third_job = UUID(refresh["job_id"])
    assert repository.claim_job(organization_id, seller_id, third_job)
    repository.activate_remote_version(
        third_job,
        expected_config_revision=1,
        original_filename="light.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer version='3'/>",
        normalized_products=[_product(ean="6973163547240", cost="1.00")],
    )
    missing = _saved_order(client, seller_id, account_id)
    assert missing["purchase_cost_eur"] is None
    assert missing["profit_amount_eur"] is None
    assert "catalog_price_list_id" not in missing["details"]
    with configured.engine.connect() as connection:
        assert connection.scalar(select(order_lines.c.catalog_cost).where(
            order_lines.c.organization_id == organization_id,
            order_lines.c.seller_id == seller_id,
        )) is False


def test_light_upload_and_order_refresh_are_one_atomic_write(workspace, monkeypatch):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    repository = SqlCatalogsRepository(workspace.engine)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )

    def fail_refresh(*_args, **_kwargs):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(SqlCatalogCostResolver, "refresh_saved_orders", fail_refresh)
    with pytest.raises(RuntimeError, match="refresh failed"):
        repository.add_price_list(
            organization_id,
            seller_id,
            supplier_id,
            name="InnPro LIGHT atomico",
            original_filename="light.xml",
            media_type="application/xml",
            file_format="iof",
            artifact=b"<offer/>",
            normalized_products=[_product(cost="6.00")],
            provider="innpro",
            feed_role="light",
        )

    with workspace.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(seller_price_lists).where(
                seller_price_lists.c.organization_id == organization_id,
                seller_price_lists.c.seller_id == seller_id,
            )
        ) == 0


def test_deleting_latest_light_reprices_saved_order_from_previous_light(configured):
    client, seller_id, organization_id, _member, _user = test_orders_api.owner(configured)
    account_id = _sync_innpro_order(
        configured,
        client,
        seller_id,
        organization_id,
        identifier="delete-list-line",
    )
    repository = SqlCatalogsRepository(configured.engine)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    previous_id = repository.add_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="InnPro LIGHT precedente",
        original_filename="light-old.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer version='old'/>",
        normalized_products=[_product(cost="5.00")],
        provider="innpro",
        feed_role="light",
    )
    latest_id = repository.add_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="InnPro LIGHT recente",
        original_filename="light-new.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer version='new'/>",
        normalized_products=[_product(cost="7.00")],
        provider="innpro",
        feed_role="light",
    )
    assert _saved_order(client, seller_id, account_id)["purchase_cost_eur"] == "14.00"

    repository.delete_price_list(organization_id, seller_id, latest_id)

    saved = _saved_order(client, seller_id, account_id)
    assert saved["purchase_cost_eur"] == "10.00"
    assert saved["details"]["catalog_price_list_id"] == str(previous_id)


def test_delete_light_preserves_manual_override_but_clears_stale_catalog_metadata(
    configured, monkeypatch,
):
    client, seller_id, organization_id, _member, _user = test_orders_api.owner(configured)
    account_id = _sync_innpro_order(
        configured,
        client,
        seller_id,
        organization_id,
        identifier="manual-after-delete",
    )
    repository = SqlCatalogsRepository(configured.engine)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    price_list_id = repository.add_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="InnPro LIGHT manuale",
        original_filename="light.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer/>",
        normalized_products=[_product(cost="6.00")],
        provider="innpro",
        feed_role="light",
    )
    with configured.engine.begin() as connection:
        row = connection.execute(select(
            order_lines.c.id,
            order_lines.c.canonical_json,
        ).where(
            order_lines.c.organization_id == organization_id,
            order_lines.c.seller_id == seller_id,
            order_lines.c.account_id == account_id,
        )).mappings().one()
        canonical = json.loads(row["canonical_json"])
        canonical.update({
            "purchase_cost": "9.00",
            "purchase_cost_eur": "9.00",
            "purchase_cost_source": "Modifica manuale persistente",
            "profit_amount": "26.00",
            "profit_amount_eur": "26.00",
        })
        canonical["details"]["catalog_cost_source"] = "Listino eliminato"
        connection.execute(order_lines.update().where(
            order_lines.c.id == row["id"],
            order_lines.c.organization_id == organization_id,
            order_lines.c.seller_id == seller_id,
        ).values(
            canonical_json=json.dumps(canonical),
            purchase_eur=Decimal("9.00"),
            profit_eur=Decimal("26.00"),
            catalog_cost=True,
        ))

    refresh_counts = {}
    real_refresh = SqlCatalogCostResolver.refresh_saved_orders

    def capture_refresh(self, *args, **kwargs):
        result = real_refresh(self, *args, **kwargs)
        refresh_counts.update(result)
        return result

    monkeypatch.setattr(SqlCatalogCostResolver, "refresh_saved_orders", capture_refresh)
    repository.delete_price_list(organization_id, seller_id, price_list_id)

    saved = _saved_order(client, seller_id, account_id)
    assert saved["purchase_cost_eur"] == "9.00"
    assert saved["profit_amount_eur"] == "26.00"
    assert saved["purchase_cost_source"] == "Modifica manuale persistente"
    assert saved["details"]["purchase_cost_method"] == "Modifica manuale persistente"
    assert not any(key.startswith("catalog_") for key in saved["details"])
    assert refresh_counts["preserved"] == 1
    assert refresh_counts["missing"] == 0
    with configured.engine.connect() as connection:
        projected = connection.execute(select(
            order_lines.c.catalog_cost,
            order_lines.c.purchase_eur,
        ).where(
            order_lines.c.organization_id == organization_id,
            order_lines.c.seller_id == seller_id,
        )).one()
    assert projected.catalog_cost is False
    assert projected.purchase_eur == Decimal("9.00000000")


def test_deleting_light_rolls_back_when_saved_cost_refresh_fails(workspace, monkeypatch):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    repository = SqlCatalogsRepository(workspace.engine)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    price_list_id = repository.add_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="InnPro LIGHT protetto",
        original_filename="light.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer/>",
        normalized_products=[_product(cost="6.00")],
        provider="innpro",
        feed_role="light",
    )

    def fail_refresh(*_args, **_kwargs):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(SqlCatalogCostResolver, "refresh_saved_orders", fail_refresh)
    with pytest.raises(RuntimeError, match="refresh failed"):
        repository.delete_price_list(organization_id, seller_id, price_list_id)

    with workspace.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(
            seller_price_lists,
        ).where(
            seller_price_lists.c.id == price_list_id,
            seller_price_lists.c.organization_id == organization_id,
            seller_price_lists.c.seller_id == seller_id,
        )) == 1


def test_deleting_light_supplier_clears_only_its_tenant_saved_cost(configured):
    first_client, first_seller, first_organization, _member, _user = test_orders_api.owner(
        configured,
    )
    first_account = _sync_innpro_order(
        configured,
        first_client,
        first_seller,
        first_organization,
        identifier="delete-supplier-first",
    )
    second_client, second_seller, second_organization, _member, _user = test_orders_api.owner(
        configured,
    )
    second_account = _sync_innpro_order(
        configured,
        second_client,
        second_seller,
        second_organization,
        identifier="delete-supplier-second",
    )
    repository = SqlCatalogsRepository(configured.engine)
    first_supplier = repository.add_supplier(
        first_organization, first_seller, name="InnPro Italia", notes="",
    )
    repository.add_price_list(
        first_organization,
        first_seller,
        first_supplier,
        name="InnPro LIGHT Italia",
        original_filename="light-it.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer supplier='it'/>",
        normalized_products=[_product(cost="6.00")],
        provider="innpro",
        feed_role="light",
    )
    second_supplier = repository.add_supplier(
        second_organization, second_seller, name="InnPro", notes="",
    )
    repository.add_price_list(
        second_organization,
        second_seller,
        second_supplier,
        name="InnPro LIGHT altro tenant",
        original_filename="light-other.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer supplier='other'/>",
        normalized_products=[_product(cost="4.00")],
        provider="innpro",
        feed_role="light",
    )
    assert _saved_order(first_client, first_seller, first_account)["purchase_cost_eur"] == "12.00"
    assert _saved_order(second_client, second_seller, second_account)["purchase_cost_eur"] == "8.00"

    repository.delete_supplier(
        first_organization,
        first_seller,
        first_supplier,
        confirmation="InnPro Italia",
    )

    first = _saved_order(first_client, first_seller, first_account)
    second = _saved_order(second_client, second_seller, second_account)
    assert first["purchase_cost_eur"] is None
    assert first["profit_amount_eur"] is None
    assert "catalog_price_list_id" not in first["details"]
    assert second["purchase_cost_eur"] == "8.00"
    assert second["details"]["catalog_supplier"] == "InnPro"


def test_order_sync_and_new_light_activation_are_serialized(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'catalog-order-race.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    metadata.create_all(engine)
    now = datetime.now(UTC)
    organization_id, seller_id, account_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(organizations.insert().values(
            id=organization_id,
            name="Tenant race",
            kind="SELLER",
            active=True,
            created_at=now,
        ))
        connection.execute(seller_profiles.insert().values(
            id=seller_id,
            organization_id=organization_id,
            organization_kind="SELLER",
            name="Seller race",
            legal_name="",
            email="race@example.test",
            active=True,
            created_at=now,
            updated_at=now,
        ))
    catalogs = SqlCatalogsRepository(engine)
    supplier_id = catalogs.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    catalogs.add_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="InnPro LIGHT v1",
        original_filename="v1.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer version='1'/>",
        normalized_products=[_product(cost="5.00")],
        provider="innpro",
        feed_role="light",
    )

    sync_in_apply = threading.Event()
    allow_sync_to_continue = threading.Event()
    activation_requested_lock = threading.Event()
    activation_acquired_lock = threading.Event()
    original_lock = SqlCatalogCostResolver.lock_scope

    def observed_lock(connection, scoped_organization, scoped_seller):
        if threading.current_thread().name == "catalog-activation":
            activation_requested_lock.set()
        result = original_lock(connection, scoped_organization, scoped_seller)
        if threading.current_thread().name == "catalog-activation":
            activation_acquired_lock.set()
        return result

    monkeypatch.setattr(
        SqlCatalogCostResolver,
        "lock_scope",
        staticmethod(observed_lock),
    )

    class PausingResolver(SqlCatalogCostResolver):
        def apply(self, *args, connection=None, **kwargs):
            assert connection is not None
            sync_in_apply.set()
            assert allow_sync_to_continue.wait(5)
            return super().apply(*args, connection=connection, **kwargs)

    item = test_orders_api.row(
        identifier="race-line",
        ean=EAN,
        sku="InnPro_INNPRO-ARTICLE_99.00_120.00",
        quantity="2",
        payout_amount="35.00",
        payout_amount_eur="35.00",
        purchase_cost="198.00",
        purchase_cost_eur="198.00",
        profit_amount="-163.00",
        profit_amount_eur="-163.00",
        details={"sku_supplier": "InnPro"},
    )
    job = {
        "organization_id": organization_id,
        "seller_id": seller_id,
        "account_id": account_id,
        "environment": "live",
        "marketplace": "amazon",
    }
    failures = []

    def sync_order():
        try:
            SqlOrdersRepository(engine).upsert_batch(
                job,
                [item],
                catalog_cost_resolver=PausingResolver(engine),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def activate_new_light():
        try:
            catalogs.add_price_list(
                organization_id,
                seller_id,
                supplier_id,
                name="InnPro LIGHT v2",
                original_filename="v2.xml",
                media_type="application/xml",
                file_format="iof",
                artifact=b"<offer version='2'/>",
                normalized_products=[_product(cost="7.00")],
                provider="innpro",
                feed_role="light",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    sync_thread = threading.Thread(target=sync_order, name="order-sync")
    activation_thread = threading.Thread(
        target=activate_new_light, name="catalog-activation",
    )
    try:
        sync_thread.start()
        assert sync_in_apply.wait(5)
        activation_thread.start()
        assert activation_requested_lock.wait(5)
        assert not activation_acquired_lock.wait(0.1)
        allow_sync_to_continue.set()
        sync_thread.join(5)
        activation_thread.join(5)
        assert not sync_thread.is_alive()
        assert not activation_thread.is_alive()
        assert failures == []

        with engine.connect() as connection:
            saved_json = connection.scalar(select(order_lines.c.canonical_json).where(
                order_lines.c.organization_id == organization_id,
                order_lines.c.seller_id == seller_id,
            ))
        saved = json.loads(saved_json)
        assert saved["purchase_cost_eur"] == "14.00"
        assert saved["details"]["catalog_price_list_name"] == "InnPro LIGHT v2"
    finally:
        allow_sync_to_continue.set()
        sync_thread.join(5)
        activation_thread.join(5)
        engine.dispose()


def test_large_saved_history_refresh_is_bounded_with_fallback_and_tenant_scope(
    workspace, monkeypatch,
):
    first_organization = workspace.organization("SELLER", "Tenant con storico")
    first_seller = workspace.seller(first_organization, "Seller con storico")
    second_organization = workspace.organization("SELLER", "Tenant estraneo")
    second_seller = workspace.seller(second_organization, "Seller estraneo")
    first_account, second_account = uuid4(), uuid4()

    def seed_orders(organization_id, seller_id, account_id, count, prefix):
        items = [test_orders_api.row(
            identifier=f"{prefix}-{index}",
            order_id=f"ORDER-{prefix}-{index}",
            ean=EAN,
            sku="InnPro_INNPRO-ARTICLE_99.00_120.00",
            quantity="2",
            payout_amount="35.00",
            payout_amount_eur="35.00",
            details={"sku_supplier": "InnPro"},
        ) for index in range(count)]
        SqlOrdersRepository(workspace.engine).upsert_batch({
            "organization_id": organization_id,
            "seller_id": seller_id,
            "account_id": account_id,
            "environment": "live",
            "marketplace": "amazon",
        }, items)

    large_count = catalog_costing.ORDER_COST_REFRESH_BATCH_ROWS + 1
    seed_orders(
        first_organization, first_seller, first_account, large_count, "bounded",
    )
    seed_orders(second_organization, second_seller, second_account, 1, "foreign")
    repository = SqlCatalogsRepository(workspace.engine)
    first_supplier = repository.add_supplier(
        first_organization, first_seller, name="InnPro", notes="",
    )
    previous_id = repository.add_price_list(
        first_organization,
        first_seller,
        first_supplier,
        name="InnPro LIGHT fallback",
        original_filename="fallback.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer version='fallback'/>",
        normalized_products=[_product(cost="5.00")],
        provider="innpro",
        feed_role="light",
    )
    latest_id = repository.add_price_list(
        first_organization,
        first_seller,
        first_supplier,
        name="InnPro LIGHT da eliminare",
        original_filename="latest.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer version='latest'/>",
        normalized_products=[_product(cost="7.00")],
        provider="innpro",
        feed_role="light",
    )
    second_supplier = repository.add_supplier(
        second_organization, second_seller, name="InnPro", notes="",
    )
    repository.add_price_list(
        second_organization,
        second_seller,
        second_supplier,
        name="InnPro LIGHT tenant estraneo",
        original_filename="foreign.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer version='foreign'/>",
        normalized_products=[_product(cost="3.00")],
        provider="innpro",
        feed_role="light",
    )

    observed_batch_sizes = []
    real_apply = SqlCatalogCostResolver.apply

    def observed_apply(self, organization_id, seller_id, items, **kwargs):
        observed_batch_sizes.append(len(items))
        return real_apply(self, organization_id, seller_id, items, **kwargs)

    monkeypatch.setattr(SqlCatalogCostResolver, "apply", observed_apply)
    repository.delete_price_list(first_organization, first_seller, latest_id)

    assert len(observed_batch_sizes) == 2
    assert sum(observed_batch_sizes) == large_count
    assert max(observed_batch_sizes) <= catalog_costing.ORDER_COST_REFRESH_BATCH_ROWS
    with workspace.engine.connect() as connection:
        fallback_rows = connection.scalar(select(func.count()).select_from(
            order_lines,
        ).where(
            order_lines.c.organization_id == first_organization,
            order_lines.c.seller_id == first_seller,
            order_lines.c.purchase_eur == Decimal("10.00"),
        ))
        foreign_cost = connection.scalar(select(order_lines.c.purchase_eur).where(
            order_lines.c.organization_id == second_organization,
            order_lines.c.seller_id == second_seller,
        ))
        sample = json.loads(connection.scalar(select(
            order_lines.c.canonical_json,
        ).where(
            order_lines.c.organization_id == first_organization,
            order_lines.c.seller_id == first_seller,
        ).limit(1)))
    assert fallback_rows == large_count
    assert foreign_cost == Decimal("6.00000000")
    assert sample["details"]["catalog_price_list_id"] == str(previous_id)


def test_paginated_refresh_preserves_result_counters(workspace, monkeypatch):
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    repository = SqlCatalogsRepository(workspace.engine)
    supplier_id = repository.add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    repository.add_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="InnPro LIGHT",
        original_filename="light.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"<offer/>",
        normalized_products=[_product(cost="5.00")],
        provider="innpro",
        feed_role="light",
    )
    account_id = uuid4()
    items = [
        test_orders_api.row(
            identifier="matched",
            order_id="ORDER-MATCHED",
            ean=EAN,
            sku="InnPro_MATCHED_10.00_20.00",
            details={"sku_supplier": "InnPro"},
        ),
        test_orders_api.row(
            identifier="manual",
            order_id="ORDER-MANUAL",
            ean="6973163547240",
            sku="InnPro_MANUAL_9.00_20.00",
            purchase_cost="9.00",
            purchase_cost_eur="9.00",
            purchase_cost_source="Modifica manuale persistente",
            details={"sku_supplier": "InnPro"},
        ),
        test_orders_api.row(
            identifier="cancelled",
            order_id="ORDER-CANCELLED",
            ean=EAN,
            sku="InnPro_CANCELLED_10.00_20.00",
            purchase_cost="0.00",
            purchase_cost_eur="0.00",
            payout_amount="0.00",
            payout_amount_eur="0.00",
            details={
                "sku_supplier": "InnPro",
                "zero_economic_reason": "Ordine annullato",
            },
        ),
        test_orders_api.row(
            identifier="other-supplier",
            order_id="ORDER-OTHER",
            ean=EAN,
            sku="Other_MATCHED_10.00_20.00",
            details={"sku_supplier": "Other"},
        ),
        test_orders_api.row(
            identifier="invalid",
            order_id="ORDER-INVALID",
            ean=EAN,
            sku="Other_INVALID_10.00_20.00",
            details={"sku_supplier": "Other"},
        ),
    ]
    SqlOrdersRepository(workspace.engine).upsert_batch({
        "organization_id": organization_id,
        "seller_id": seller_id,
        "account_id": account_id,
        "environment": "live",
        "marketplace": "amazon",
    }, items)
    with workspace.engine.begin() as connection:
        connection.execute(order_lines.update().where(
            order_lines.c.organization_id == organization_id,
            order_lines.c.seller_id == seller_id,
            order_lines.c.external_line_id == "invalid",
        ).values(canonical_json="[invalid"))

    monkeypatch.setattr(catalog_costing, "ORDER_COST_REFRESH_BATCH_ROWS", 2)
    resolver = SqlCatalogCostResolver(workspace.engine)
    with workspace.engine.begin() as connection:
        resolver.lock_scope(connection, organization_id, seller_id)
        counts = resolver.refresh_saved_orders(
            organization_id,
            seller_id,
            connection=connection,
        )

    assert counts == {
        "examined": 3,
        "matched": 1,
        "missing": 0,
        "preserved": 1,
        "cancelled": 1,
        "invalid": 1,
    }
