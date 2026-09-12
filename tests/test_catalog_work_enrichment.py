import json
from decimal import Decimal
from uuid import UUID, uuid4

import test_catalog_work as work
from marketplace_hub_core.catalogs.schema import seller_price_list_products as products
from marketplace_hub_core.catalogs.schema import seller_price_lists as lists
from marketplace_hub_core.publication.models import Rules
from marketplace_hub_core.publication.pricing import edit_offer, prepare
from sqlalchemy import select, update

configured = work.configured
workspace = work.workspace


def setup(configured):
    client, seller, org, recipe, account, _ = work.setup(configured, 3)
    with configured.engine.begin() as c:
        supplier = c.scalar(
            select(lists.c.supplier_id).where(lists.c.id == UUID(recipe["price_list_id"]))
        )
        c.execute(
            update(lists)
            .where(lists.c.id == UUID(recipe["price_list_id"]))
            .values(provider="innpro", feed_role="light")
        )
        c.execute(
            update(products)
            .where(products.c.price_list_id == UUID(recipe["price_list_id"]))
            .values(name="")
        )
    full_rows = [work.helpers._product(i) for i in [1, 2, 3]]
    for i, r in enumerate(full_rows):
        r.update(
            name=f"FULL name {i}",
            cost=Decimal("999"),
            quantity=Decimal("999"),
            canonical_json=json.dumps(
                {
                    "source": {
                        "brand": "Test Brand",
                        "category": "Tools",
                        "description": "<b>Useful</b> product",
                        "weight_kg": "1.2",
                        "length_cm": "20",
                        "width_cm": "10",
                        "height_cm": "5",
                    }
                }
            ),
        )
    full = configured.catalog_repository.add_price_list(
        org,
        seller,
        supplier,
        name="FULL",
        provider="innpro",
        feed_role="full",
        original_filename="full.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"full",
        normalized_products=full_rows,
    )
    view = client.post(
        work.root(seller) + "/views",
        json={"recipe": recipe, "name": "Existing", "account_ids": [account]},
    ).json()
    recipe.update(content_list_id=str(full), content_version=1)
    uri = work.root(seller) + f"/views/{view['id']}"
    return client, seller, recipe, uri, view, account


def test_existing_view_enrichment_preserves_prices_and_ids_updates_stock(configured):
    client, seller, recipe, uri, view, account = setup(configured)
    old = client.get(uri).json()["rows"]
    edited = client.put(
        uri,
        json={
            "name": "Existing",
            "account_ids": [account],
            "expected_revision": 1,
            "edits": {old[0]["id"]: {"price": "72.35", "name": "Manual name", "weight_kg": "2.5"}},
        },
    )
    assert edited.status_code == 200
    with configured.engine.begin() as c:
        c.execute(
            update(products)
            .where(products.c.price_list_id == UUID(recipe["price_list_id"]))
            .values(quantity=7)
        )
    result = client.post(uri + "/enrich", json={"expected_revision": 2, "recipe": recipe})
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["match_report"] == {
        "total": 3,
        "full_matched": 3,
        "full_missing": 0,
        "full_duplicate": 0,
        "light_matched": 3,
        "light_missing": 0,
        "light_duplicate": 0,
    }
    assert value["view"]["revision"] == 3
    first, second = value["rows"][:2]
    assert (
        first["id"] == old[0]["id"] and first["price"] == "72.35" and first["name"] == "Manual name"
    )
    assert Decimal(first["weight_kg"]) == Decimal("2.5")
    assert Decimal(first["cost"]) == 10 and Decimal(first["quantity"]) == 7
    assert second["name"] == "FULL name 1" and Decimal(second["length_cm"]) == 20
    assert (
        second["product_info"]["brand"] == "Test Brand"
        and second["product_info"]["description"] == "Useful  product"
    )
    assert client.get(uri).json()["rows"][1]["innpro_match"]["full"] == "matched"
    assert (
        client.post(uri + "/enrich", json={"expected_revision": 2, "recipe": recipe}).status_code
        == 409
    )
    prepared, _ = prepare(
        second, "InnPro", "kaufland", Rules(account_id=uuid4(), view_id=uuid4(), revision=3)
    )
    assert prepared["name"] == second["name"] and prepared["length_cm"] == second["length_cm"]


def test_missing_duplicate_eans_never_guess_stock_or_full_content(configured):
    client, seller, recipe, uri, view, account = setup(configured)
    initial = client.get(uri).json()["rows"]
    with configured.engine.begin() as c:
        full = UUID(recipe["content_list_id"])
        c.execute(
            update(products)
            .where(products.c.price_list_id == full, products.c.ean == initial[1]["ean"])
            .values(ean=initial[0]["ean"])
        )
        c.execute(
            update(products)
            .where(
                products.c.price_list_id == UUID(recipe["price_list_id"]),
                products.c.ean == initial[2]["ean"],
            )
            .values(ean="0000000000000")
        )
    result = client.post(uri + "/enrich", json={"expected_revision": 1, "recipe": recipe})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["match_report"]["full_duplicate"] == 1 and data["match_report"]["full_missing"] == 1
    assert data["match_report"]["light_missing"] == 1
    assert data["rows"][0]["name"] == ""
    public, payload = prepare(
        data["rows"][2],
        "InnPro",
        "kaufland",
        Rules(account_id=uuid4(), view_id=uuid4(), revision=1),
    )
    assert "Stock LIGHT" in public["problem"]
    public, payload = edit_offer(
        public,
        payload,
        {"quantity": 2},
        "kaufland",
        Rules(account_id=uuid4(), view_id=uuid4(), revision=1),
    )
    assert public["innpro_match"]["light"] == "manual"


def test_enrichment_scope_and_source_revision_checked_before_mutation(configured):
    client, seller, recipe, uri, view, account = setup(configured)
    other, _, other_recipe, _, _, _ = setup(configured)
    assert other.post(uri + "/enrich", content="bad").status_code == 404
    assert (
        client.post(
            uri + "/enrich",
            json={
                "expected_revision": 1,
                "recipe": {**recipe, "content_list_id": other_recipe["content_list_id"]},
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            uri + "/enrich",
            json={"expected_revision": 1, "recipe": {**recipe, "content_version": 2}},
        ).status_code
        == 409
    )
    readonly, rseller, _, _ = work.api.owner(configured, read_only=True)
    assert (
        readonly.post(work.root(rseller) + f"/views/{view['id']}/enrich", content="bad").status_code
        == 403
    )
    assert client.get(uri).json()["view"]["revision"] == 1
