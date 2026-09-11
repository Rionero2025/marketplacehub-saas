from __future__ import annotations

import hashlib
import io
from uuid import UUID, uuid4

import pytest
import test_tenancy_api
from fastapi.testclient import TestClient
from marketplace_hub_api import catalogs as catalogs_api
from marketplace_hub_api.main import create_app
from marketplace_hub_core.catalogs.parsing import parse_catalog
from marketplace_hub_core.catalogs.repository import SqlCatalogsRepository
from marketplace_hub_core.catalogs.schema import (
    MAX_CATALOG_ARTIFACT_BYTES,
    seller_price_list_products,
    seller_price_lists,
)
from marketplace_hub_core.catalogs.service import CatalogsService
from marketplace_hub_core.settings import Settings
from openpyxl import Workbook
from sqlalchemy import func, select

workspace = test_tenancy_api.workspace


@pytest.fixture
def configured(workspace):
    workspace.catalog_repository = SqlCatalogsRepository(workspace.engine)
    workspace.catalogs = CatalogsService(workspace.catalog_repository, workspace.service)
    workspace.settings = Settings(environment="test", _env_file=None)
    workspace.app = create_app(
        settings=workspace.settings,
        readiness_checks={"fixture": lambda: None},
        auth_service=workspace.auth,
        workspace_service=workspace.service,
        catalogs_service=workspace.catalogs,
    )
    return workspace


def owner(configured, **membership_options):
    user = configured.user()
    organization = configured.organization("SELLER")
    seller = configured.seller(organization)
    permissions = membership_options.pop(
        "permission_codes", ("WORKSPACE_VIEW", "CATALOG")
    )
    configured.membership(
        user,
        organization,
        "SELLER_OWNER",
        permission_codes=permissions,
        **membership_options,
    )
    client, _ = configured.session(user)
    return client, seller, organization, user


def path(seller, suffix=""):
    return f"/v1/sellers/{seller}/catalogs{suffix}"


def add_supplier(client, seller, name="Hurtel"):
    response = client.post(path(seller, "/suppliers"), json={"name": name, "notes": "UE"})
    assert response.status_code == 201
    return response.json()["created_supplier_id"]


def upload(client, seller, supplier_id, content, *, name="Listino UE", file_name="feed.csv"):
    return client.post(
        path(seller, "/price-lists"),
        data={"supplier_id": supplier_id, "name": name},
        files={"file": (file_name, content, "text/csv")},
    )


def test_catalog_routes_authenticate_before_processing_upload(configured):
    client = TestClient(configured.app)
    seller, supplier, price_list = uuid4(), uuid4(), uuid4()
    requests = (
        client.get(path(seller)),
        client.post(path(seller, "/suppliers"), json={"name": "x"}),
        client.request(
            "DELETE", path(seller, f"/suppliers/{supplier}"), json={"confirmation": "x"}
        ),
        client.post(path(seller, "/price-lists"), content=b"not multipart"),
        client.get(path(seller, f"/price-lists/{price_list}")),
        client.request(
            "DELETE",
            path(seller, f"/price-lists/{price_list}"),
            json={"confirmation": "ELIMINA"},
        ),
    )
    assert {response.status_code for response in requests} == {401}
    assert all(response.headers["cache-control"] == "no-store" for response in requests)


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("POST", "/suppliers"),
        ("DELETE", f"/suppliers/{uuid4()}"),
        ("DELETE", f"/price-lists/{uuid4()}"),
    ],
    ids=["create-supplier", "delete-supplier", "delete-price-list"],
)
def test_catalog_json_is_not_read_before_authentication_and_write_authorization(
    configured, monkeypatch, method, suffix,
):
    _, seller, organization, _ = owner(configured)
    read_only_user = configured.user()
    configured.membership(
        read_only_user,
        organization,
        "SELLER_USER",
        permission_codes=("WORKSPACE_VIEW", "CATALOG"),
        read_only=True,
    )
    read_only_client, _ = configured.session(read_only_user)
    anonymous_client = TestClient(configured.app)
    forged_client = TestClient(configured.app)
    forged_client.cookies.set(configured.settings.session_cookie_name, "forged-session")
    reads = 0

    async def forbidden_reader(_request):
        nonlocal reads
        reads += 1
        raise AssertionError("the JSON body must not be read before the preflight")

    monkeypatch.setattr(catalogs_api, "read_catalog_json", forbidden_reader)
    body = b'{"confirmation":"preflight-secret"'
    headers = {"content-type": "application/json"}

    responses = [
        anonymous_client.request(
            method, path(seller, suffix), content=body, headers=headers,
        ),
        forged_client.request(
            method, path(seller, suffix), content=body, headers=headers,
        ),
        read_only_client.request(
            method, path(seller, suffix), content=body, headers=headers,
        ),
    ]

    assert [response.status_code for response in responses] == [401, 401, 403]
    assert reads == 0
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        assert "preflight-secret" not in response.text


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("POST", "/suppliers"),
        ("DELETE", f"/suppliers/{uuid4()}"),
        ("DELETE", f"/price-lists/{uuid4()}"),
    ],
    ids=["create-supplier", "delete-supplier", "delete-price-list"],
)
def test_catalog_json_rejects_malformed_and_oversize_bodies_atomically(
    configured, method, suffix,
):
    client, seller, _, _ = owner(configured)
    before = client.get(path(seller)).json()
    malformed_secret = "malformed-catalog-secret"

    malformed = client.request(
        method,
        path(seller, suffix),
        content=f'{{"confirmation":"{malformed_secret}"'.encode(),
        headers={"content-type": "application/json"},
    )
    oversized_secret = b"oversized-catalog-secret"
    oversized_body = b'{"confirmation":"' + oversized_secret + b"x" * (
        catalogs_api.MAX_CATALOG_JSON_BYTES
    ) + b'"}'
    oversized = client.request(
        method,
        path(seller, suffix),
        content=oversized_body,
        headers={"content-type": "application/json", "content-length": "10"},
    )

    assert malformed.status_code == 422
    assert malformed.json() == {
        "detail": "Dati non validi. Controlla i campi inseriti."
    }
    assert malformed_secret not in malformed.text
    assert oversized.status_code == 413
    assert oversized.json() == {
        "detail": "Il corpo JSON supera il limite consentito."
    }
    assert oversized_secret.decode() not in oversized.text
    assert malformed.headers["cache-control"] == "no-store"
    assert oversized.headers["cache-control"] == "no-store"
    assert client.get(path(seller)).json() == before


def test_bounded_catalog_json_routes_keep_valid_mutations(configured):
    client, seller, _, _ = owner(configured)

    created_supplier = client.post(
        path(seller, "/suppliers"),
        json={"name": "Fornitore JSON", "notes": "Feed principale"},
    )
    assert created_supplier.status_code == 201, created_supplier.text
    supplier_id = created_supplier.json()["created_supplier_id"]
    raw = b"ean;sku;name;cost\n0012345678901;A;Uno;1\n"
    created_list = upload(client, seller, supplier_id, raw)
    assert created_list.status_code == 201, created_list.text
    price_list_id = created_list.json()["price_list"]["id"]

    deleted_list = client.request(
        "DELETE",
        path(seller, f"/price-lists/{price_list_id}"),
        json={"confirmation": "ELIMINA"},
    )
    assert deleted_list.status_code == 200, deleted_list.text
    deleted_supplier = client.request(
        "DELETE",
        path(seller, f"/suppliers/{supplier_id}"),
        json={"confirmation": "Fornitore JSON"},
    )
    assert deleted_supplier.status_code == 200, deleted_supplier.text
    assert client.get(path(seller)).json()["suppliers"] == []


def test_csv_upload_preserves_identifiers_decimals_artifact_and_public_dto(configured):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    raw = (
        "EAN;SKU;Nome;Prezzo acquisto;Costo spedizione;Quantità\n"
        "0012345678901;REF-01;Caffè;12,34;1,20;7\n"
    ).encode()
    response = upload(client, seller, supplier_id, raw)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    price_list = body["price_list"]
    assert price_list["file_name"] == "feed.csv"
    assert price_list["source_type"] == "upload"
    assert price_list["status"] == "ready"
    assert price_list["row_count"] == body["row_count"] == 1
    assert "artifact_bytes" not in response.text and "canonical_json" not in response.text
    product = body["products"][0]
    assert product == {
        "id": product["id"],
        "source_row": 2,
        "ean": "0012345678901",
        "sku": "REF-01",
        "name": "Caffè",
        "cost": "12.34",
        "shipping_cost": "1.2",
        "total_cost": "13.54",
        "quantity": "7",
    }
    with configured.engine.connect() as connection:
        stored = connection.execute(select(seller_price_lists).where(
            seller_price_lists.c.id == UUID(price_list["id"]),
            seller_price_lists.c.organization_id == organization,
            seller_price_lists.c.seller_id == seller,
        )).mappings().one()
        assert bytes(stored["artifact_bytes"]) == raw
        assert stored["artifact_sha256"] == hashlib.sha256(raw).hexdigest()
        assert stored["artifact_size"] == len(raw)

    dashboard = client.get(path(seller))
    assert dashboard.status_code == 200
    assert dashboard.json()["can_manage"] is True
    assert dashboard.json()["suppliers"][0]["price_list_count"] == 1
    assert dashboard.json()["price_lists"][0]["row_count"] == 1


def test_upload_feed_identity_defaults_validates_and_versions_innpro_light(configured):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller, name="InnPro")
    raw = b"ean;sku;name;cost\n0012345678901;A;Uno;1\n"
    light_raw = b"""<offer file_format="IOF"><products currency="EUR">
    <product id="1"><price net="1.25"/><sizes><size code_producer="SKU-A"
    code_external="0012345678901"><stock quantity="4"/></size></sizes></product>
    </products></offer>"""

    generic = upload(client, seller, supplier_id, raw, name="Generico")
    assert generic.status_code == 201, generic.text
    assert generic.json()["price_list"]["provider"] == "generic"
    assert generic.json()["price_list"]["feed_role"] == "standard"

    light = client.post(
        path(seller, "/price-lists"),
        data={
            "supplier_id": supplier_id,
            "name": "InnPro LIGHT",
            "provider": "innpro",
            "feed_role": "light",
        },
        files={"file": ("light.xml", light_raw, "application/xml")},
    )
    assert light.status_code == 201, light.text
    light_list = light.json()["price_list"]
    assert (light_list["provider"], light_list["feed_role"]) == ("innpro", "light")
    versions = configured.catalog_repository.version_metadata(
        organization, seller, UUID(light_list["id"]),
    )
    assert [(item["provider"], item["feed_role"]) for item in versions] == [
        ("innpro", "light")
    ]

    invalid = client.post(
        path(seller, "/price-lists"),
        data={
            "supplier_id": supplier_id,
            "name": "Ruolo incoerente",
            "provider": "generic",
            "feed_role": "full",
        },
        files={"file": ("feed.csv", raw, "text/csv")},
    )
    assert invalid.status_code == 422
    assert "generic/standard" in invalid.json()["detail"]


def test_feed_level_hurtel_repair_swaps_reference_and_ean_columns():
    _, rows = parse_catalog(
        "hurtel.csv",
        b"EAN;SKU;Name;Cost\nREF-A;0012345678901;Uno;1,25\nREF-B;0012345678902;Due;2,50\n",
    )
    assert [(row["ean"], row["sku"]) for row in rows] == [
        ("0012345678901", "REF-A"),
        ("0012345678902", "REF-B"),
    ]


def test_xlsx_and_xml_are_parsed_without_converting_decimal_strings_to_float():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["decorazione"])
    sheet.append(["EAN", "SKU", "Nome", "Costo", "Shipping cost", "Qty"])
    sheet.append([123456789012, "ABC", "Prodotto", "10,50", "2,25", 3])
    sheet["A3"].number_format = "0000000000000"
    content = io.BytesIO()
    workbook.save(content)
    workbook.close()
    _, xlsx = parse_catalog("catalogo.xlsx", content.getvalue())
    assert xlsx[0]["ean"] == "0123456789012"
    assert xlsx[0]["total_cost"] == pytest.approx(12.75)
    assert xlsx[0]["source_row"] == 3

    _, xml = parse_catalog(
        "catalogo.xml",
        b"<products><product><ean>0000000012345</ean><sku>X</sku>"
        b"<name>XML</name><cost>1,90</cost><quantity>2</quantity></product></products>",
    )
    assert xml[0]["ean"] == "0000000012345"
    assert str(xml[0]["cost"]) == "1.90"


def test_duplicate_names_are_scoped_per_seller_and_return_conflict(configured):
    first, seller, _, _ = owner(configured)
    second, other_seller, _, _ = owner(configured)
    supplier_id = add_supplier(first, seller, "Duplicato")
    assert first.post(path(seller, "/suppliers"), json={
        "name": "Duplicato", "notes": ""
    }).status_code == 409
    assert second.post(path(other_seller, "/suppliers"), json={
        "name": "Duplicato", "notes": ""
    }).status_code == 201

    raw = b"ean;sku;name;cost\n0012345678901;A;Uno;1\n"
    assert upload(first, seller, supplier_id, raw, name="Duplicato").status_code == 201
    assert upload(first, seller, supplier_id, raw, name="Duplicato").status_code == 409


def test_cross_tenant_and_read_only_access_cannot_mutate(configured):
    owner_client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(owner_client, seller)
    foreign_client, foreign_seller, _, _ = owner(configured)
    foreign_supplier = add_supplier(foreign_client, foreign_seller, "Estraneo")

    assert owner_client.get(path(foreign_seller)).status_code == 404
    # The foreign supplier is rejected before the malformed catalog is parsed.
    denied = upload(owner_client, seller, foreign_supplier, b"not-a-catalog")
    assert denied.status_code == 404
    assert owner_client.get(path(seller)).json()["price_lists"] == []

    viewer = configured.user()
    configured.membership(
        viewer,
        organization,
        "SELLER_USER",
        permission_codes=("WORKSPACE_VIEW", "CATALOG"),
        read_only=True,
    )
    read_only, _ = configured.session(viewer)
    dashboard = read_only.get(path(seller))
    assert dashboard.status_code == 200 and dashboard.json()["can_manage"] is False
    assert read_only.post(path(seller, "/suppliers"), json={
        "name": "No", "notes": ""
    }).status_code == 403
    assert upload(
        read_only,
        seller,
        supplier_id,
        b"ean;name\n0012345678901;No\n",
    ).status_code == 403


def test_malformed_unsafe_and_oversize_uploads_are_rejected_atomically(configured):
    client, seller, _, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    for file_name, content, expected in (
        ("bad.xlsx", b"not-an-xlsx", 422),
        ("unsafe.pkl", b"pickle", 422),
        ("empty.csv", b"", 422),
        ("huge.csv", b"x" * (MAX_CATALOG_ARTIFACT_BYTES + 1), 413),
    ):
        response = upload(
            client,
            seller,
            supplier_id,
            content,
            name=f"Listino {file_name}",
            file_name=file_name,
        )
        assert response.status_code == expected, response.text
        assert response.headers["cache-control"] == "no-store"
    with configured.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(seller_price_lists)) == 0
        assert connection.scalar(select(func.count()).select_from(
            seller_price_list_products
        )) == 0


def test_exact_delete_confirmations_and_supplier_cascade_report_counts(configured):
    client, seller, _, _ = owner(configured)
    supplier_id = add_supplier(client, seller, "AB Online")
    raw = b"ean;sku;name;cost\n0012345678901;A;Uno;1\n"
    first = upload(client, seller, supplier_id, raw, name="Uno").json()["price_list"]["id"]
    second = upload(client, seller, supplier_id, raw, name="Due").json()["price_list"]["id"]

    for word in ("", "elimina", " ELIMINA "):
        response = client.request(
            "DELETE", path(seller, f"/price-lists/{first}"), json={"confirmation": word}
        )
        assert response.status_code == 422
    deleted_list = client.request(
        "DELETE", path(seller, f"/price-lists/{first}"), json={"confirmation": "ELIMINA"}
    )
    assert deleted_list.status_code == 200
    assert deleted_list.json()["deleted"]["products_deleted"] == 1
    assert client.get(path(seller, f"/price-lists/{first}")).status_code == 404

    for wrong in ("", "ab online", " AB Online "):
        response = client.request(
            "DELETE",
            path(seller, f"/suppliers/{supplier_id}"),
            json={"confirmation": wrong},
        )
        assert response.status_code == 422
    deleted_supplier = client.request(
        "DELETE",
        path(seller, f"/suppliers/{supplier_id}"),
        json={"confirmation": "AB Online"},
    )
    assert deleted_supplier.status_code == 200
    assert deleted_supplier.json()["deleted"] == {
        "kind": "supplier",
        "id": supplier_id,
        "name": "AB Online",
        "price_lists_deleted": 1,
    }
    assert client.get(path(seller, f"/price-lists/{second}")).status_code == 404
    assert client.get(path(seller)).json()["suppliers"] == []
