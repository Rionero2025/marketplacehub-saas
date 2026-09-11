import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import test_catalog_artifact_repository as helpers
import test_catalogs_api as api
from marketplace_hub_core.catalogs.repository import SqlCatalogsRepository
from marketplace_hub_core.catalogs.schema import seller_price_lists
from marketplace_hub_core.catalogs.work import CatalogWorkRepository, prepare_row
from marketplace_hub_core.catalogs.work_models import WorkRecipe
from marketplace_hub_core.seller_settings.schema import seller_marketplace_accounts
from sqlalchemy import select, update

workspace = helpers.workspace
configured = api.configured


def setup(configured, count=2):
    client, seller, org, user = api.owner(configured)
    repo = SqlCatalogsRepository(configured.engine)
    supplier = repo.add_supplier(org, seller, name="Test supplier", notes="")
    price_list = repo.add_price_list(
        org,
        seller,
        supplier,
        name="Listino",
        original_filename="file.csv",
        media_type="text/csv",
        file_format="csv",
        artifact=b"data",
        normalized_products=[helpers._product(i + 1) for i in range(count)],
    )
    account = add_account(configured.engine, org, seller)
    recipe = {"price_list_id": str(price_list), "version": 1}
    return client, seller, org, recipe, str(account), user


def add_account(engine, org, seller):
    account = uuid4()
    with engine.begin() as c:
        c.execute(
            seller_marketplace_accounts.insert().values(
                id=account,
                seller_id=seller,
                organization_id=org,
                marketplace="kaufland",
                account_name="Target",
                credentials_encrypted="never-expose",
                settings_json="{}",
                active=True,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    return account


def root(seller):
    return api.path(seller, "/work")


def test_work_preview_price_formula_and_snapshot_edit_lifecycle(configured):
    client, seller, org, recipe, account, _ = setup(configured, 121)
    recipe.update(shipping="2.50", margin="35", minimum_margin="10")
    preview = client.post(root(seller) + "/preview", json={"recipe": recipe}).json()
    assert preview["total"] == 121 and len(preview["rows"]) == 50
    first = preview["rows"][0]
    assert first["shipping_cost"] == "3.50"
    assert first["total_cost"] == "13.50"
    assert first["price"] == "18.23" and first["minimum_price"] == "14.85"
    second = preview["rows"][1]
    payload = {
        "recipe": recipe,
        "name": "Vista",
        "account_ids": [account],
        "selection": [first["id"]],
        "edits": {second["id"]: {"price": "99.99"}},
    }
    response = client.post(root(seller) + "/views", json=payload)
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["row_count"] == 120
    uri = root(seller) + f"/views/{saved['id']}"
    detail = client.get(uri).json()
    assert detail["rows"][0]["price"] == "99.99"
    assert len(client.get(uri + "?page=3").json()["rows"]) == 20
    # Supplier data remains unchanged; later imports do not change saved snapshots.
    source = configured.catalog_repository.detail(
        org, seller, UUID(recipe["price_list_id"]), limit=1
    )
    assert source["products"][0]["cost"] == "10"
    changed = client.put(
        uri,
        json={
            "name": "Revised",
            "account_ids": [account],
            "expected_revision": 1,
            "edits": {second["id"]: {"cost": "8.75"}},
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2
    assert client.get(uri).json()["rows"][0]["cost"] == "8.75"
    stale = client.put(
        uri, json={"name": "Lost update", "account_ids": [account], "expected_revision": 1}
    )
    assert stale.status_code == 409
    assert (
        client.request(
            "DELETE", uri, json={"confirmation": "wrong", "expected_revision": 2}
        ).status_code
        == 422
    )
    assert (
        client.request(
            "DELETE", uri, json={"confirmation": "ELIMINA", "expected_revision": 2}
        ).status_code
        == 200
    )
    assert client.get(uri).status_code == 404


def test_cross_seller_sources_targets_views_and_overrides_rejected(configured):
    client, seller, _, recipe, account, _ = setup(configured)
    other_client, other_seller, _, other_recipe, other_account, _ = setup(configured)
    other = other_client.post(root(other_seller) + "/preview", json={"recipe": other_recipe}).json()
    normal = {"recipe": recipe, "name": "View", "account_ids": [account]}
    assert client.post(root(seller) + "/preview", json={"recipe": other_recipe}).status_code == 404
    assert (
        client.post(
            root(seller) + "/views", json={**normal, "account_ids": [other_account]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            root(seller) + "/views", json={**normal, "selection": [other["rows"][0]["id"]]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            root(seller) + "/views",
            json={**normal, "edits": {other["rows"][0]["id"]: {"cost": "0"}}},
        ).status_code
        == 422
    )
    view = client.post(root(seller) + "/views", json=normal).json()
    assert other_client.get(root(other_seller) + f"/views/{view['id']}").status_code == 404
    index = client.get(root(seller))
    assert "never-expose" not in index.text and "credentials" not in index.text


def test_filters_before_pagination_selection_none_stale_version_and_empty_fail(configured):
    client, seller, _, recipe, account, _ = setup(configured, 151)
    filtered = client.post(
        root(seller) + "/preview", json={"recipe": {**recipe, "search": "00151"}}
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert (
        client.post(root(seller) + "/preview", json={"recipe": {**recipe, "search": "%"}}).json()[
            "total"
        ]
        == 0
    )
    payload = {"recipe": recipe, "name": "View", "account_ids": [account]}
    assert (
        client.post(root(seller) + "/views", json={**payload, "select_all": False}).status_code
        == 422
    )
    assert client.post(root(seller) + "/views", json={**payload, "name": "   "}).status_code == 422
    assert (
        client.post(
            root(seller) + "/preview", json={"recipe": {**recipe, "version": 2}}
        ).status_code
        == 409
    )
    for extra in (
        {"exclude": "between", "lower": "10", "upper": "1"},
        {"shipping": "NaN"},
        {"min_cost": "20", "max_cost": "10"},
    ):
        assert (
            client.post(root(seller) + "/preview", json={"recipe": {**recipe, **extra}}).status_code
            == 422
        )


def test_full_enriches_light_by_unique_exact_ean_without_replacing_cost_or_stock(configured):
    client, seller, org, recipe, _, _ = setup(configured)
    repo = configured.catalog_repository
    with configured.engine.begin() as c:
        source = c.execute(
            select(seller_price_lists.c.supplier_id).where(
                seller_price_lists.c.id == UUID(recipe["price_list_id"]),
            )
        ).scalar_one()
        c.execute(
            update(seller_price_lists)
            .where(
                seller_price_lists.c.id == UUID(recipe["price_list_id"]),
            )
            .values(provider="innpro", feed_role="light")
        )
    first = helpers._product(1)
    first.update(cost=Decimal("999"), quantity=Decimal("999"), name="FULL product")
    full = repo.add_price_list(
        org,
        seller,
        source,
        name="FULL",
        provider="innpro",
        feed_role="full",
        original_filename="full.xml",
        media_type="application/xml",
        file_format="iof",
        artifact=b"full",
        normalized_products=[first],
    )
    recipe.update(content_list_id=str(full), content_version=1)
    result = CatalogWorkRepository(configured.engine).preview(
        org,
        seller,
        WorkRecipe(**recipe),
        1,
    )
    assert Decimal(result["rows"][0]["cost"]) == 10
    assert Decimal(result["rows"][0]["quantity"]) == 3
    assert result["rows"][0]["name"] == "FULL product"
    assert (
        client.post(
            root(seller) + "/preview",
            json={
                "recipe": {
                    "price_list_id": str(full),
                    "version": 1,
                }
            },
        ).status_code
        == 422
    )


def test_decimal_rounding_and_shipping_match_reference_formula():
    row = {
        **helpers._product(1),
        "id": uuid4(),
        **dict.fromkeys(
            ("weight_kg", "length_cm", "width_cm", "height_cm"),
        ),
    }
    recipe = WorkRecipe(price_list_id=uuid4(), version=1, shipping="2.5")
    result = prepare_row(row, recipe)
    assert result["total_cost"] == "13.50"
    assert result["price"] == "18.23"  # Matches pandas binary-float multiplication and round(2).


def test_calculations_match_forty_frozen_pandas_reference_results():
    cases = json.loads(
        (Path(__file__).parent / "fixtures/catalog_work_reference_prices.json").read_text()
    )
    for case in cases:
        cost, feed, shipping, margin, minimum = case["input"]
        row = {
            **helpers._product(1),
            "id": uuid4(),
            "cost": Decimal(cost),
            "shipping_cost": Decimal(feed),
            **dict.fromkeys(
                ("weight_kg", "length_cm", "width_cm", "height_cm"),
            ),
        }
        recipe = WorkRecipe(
            price_list_id=uuid4(),
            version=1,
            shipping=shipping,
            margin=margin,
            minimum_margin=minimum,
        )
        actual = prepare_row(row, recipe)
        assert {key: actual[key] for key in case["expected"]} == case["expected"]


def test_saved_view_manual_rows_identifiers_and_overwrite(configured):
    client, seller, _, recipe, account, _ = setup(configured)
    saved = client.post(
        root(seller) + "/views",
        json={
            "recipe": recipe,
            "name": "Manual",
            "account_ids": [account],
        },
    ).json()
    uri = root(seller) + f"/views/{saved['id']}"
    first = client.get(uri).json()["rows"][0]
    modified = client.put(
        uri,
        json={
            "name": "Manual",
            "account_ids": [account],
            "expected_revision": 1,
            "edits": {first["id"]: {"name": "Nuovo nome", "ean": "0000123"}},
            "added": [
                {"sku": "MANUAL", "name": "Manual product", "cost": "12.345", "quantity": "2"}
            ],
        },
    )
    assert modified.status_code == 200, modified.text
    details = client.get(uri).json()
    assert details["rows"][0]["ean"] == "0000123"
    assert details["rows"][2]["cost"] == "12.345"
    assert details["total"] == 3
    replaced = client.post(
        root(seller) + "/views",
        json={
            "recipe": recipe,
            "name": "Replaced",
            "account_ids": [account],
            "overwrite_id": saved["id"],
            "expected_revision": 2,
        },
    )
    assert replaced.status_code == 201, replaced.text
    assert replaced.json()["row_count"] == 2 and replaced.json()["revision"] == 3
    assert len(client.get(root(seller)).json()["views"]) == 1


def test_read_permissions_and_inactive_destinations(configured):
    client, seller, org, recipe, account, _ = setup(configured)
    user = configured.user()
    configured.membership(
        user, org, "SELLER_USER", permission_codes=("WORKSPACE_VIEW", "CATALOG"), read_only=True
    )
    reader, _ = configured.session(user)
    assert reader.get(root(seller)).status_code == 200
    assert reader.post(root(seller) + "/preview", json={"recipe": recipe}).status_code == 200
    assert reader.post(root(seller) + "/views", content=b"not even json").status_code == 403
    with configured.engine.begin() as c:
        c.execute(
            update(seller_marketplace_accounts)
            .where(
                seller_marketplace_accounts.c.id == UUID(account),
            )
            .values(active=False)
        )
    assert (
        client.post(
            root(seller) + "/views",
            json={
                "recipe": recipe,
                "name": "View",
                "account_ids": [account],
            },
        ).status_code
        == 422
    )
