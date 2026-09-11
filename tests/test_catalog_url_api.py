from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import test_tenancy_api
from fastapi.testclient import TestClient
from marketplace_hub_api import catalogs as catalogs_api
from marketplace_hub_api.main import create_app
from marketplace_hub_core.catalogs.fetching import DownloadedCatalog, DownloadedCatalogFile
from marketplace_hub_core.catalogs.repository import SqlCatalogsRepository
from marketplace_hub_core.catalogs.schema import seller_price_lists
from marketplace_hub_core.catalogs.service import CatalogsService
from marketplace_hub_core.marketplace_connections.security import decrypt_credentials
from marketplace_hub_core.settings import Settings
from pydantic import SecretStr
from sqlalchemy import select

workspace = test_tenancy_api.workspace

INNPRO_LIGHT = b"""<?xml version="1.0" encoding="UTF-8"?>
<offer file_format="IOF" version="3.0"><products currency="EUR">
<product id="4145"><price net="37.70"/><srp net="49.59"/><sizes>
<size id="0" code_producer="SKU-LIGHT" code_external="6930460000040"
 weight="1030"><stock quantity="140"/></size>
</sizes></product></products></offer>"""

INNPRO_FULL = b"""<?xml version="1.0" encoding="UTF-8"?>
<offer file_format="IOF" version="3.0"><products language="eng" currency="EUR">
<product id="4145"><producer id="7" name="InnPro"/><category id="8" name="Casa"/>
<description><name xml:lang="eng">Product full name</name>
<long_desc xml:lang="eng">Complete description</long_desc></description>
<price net="49.59"/><sizes><size id="0" code_producer="SKU-FULL"
 code_external="6930460000040"><stock quantity="12"/></size></sizes>
</product></products></offer>"""


class RecordingQueue:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.jobs: list[UUID] = []

    def enqueue(self, job_id: UUID) -> None:
        if self.fail:
            raise RuntimeError("redis detail that must stay private")
        self.jobs.append(job_id)


@pytest.fixture
def configured(workspace):
    workspace.catalog_repository = SqlCatalogsRepository(workspace.engine)
    workspace.catalog_queue = RecordingQueue()
    workspace.settings = Settings(
        environment="test", master_key=SecretStr("catalog-url-test-key"), _env_file=None,
    )
    workspace.catalogs = CatalogsService(
        workspace.catalog_repository,
        workspace.service,
        workspace.catalog_queue,
        workspace.settings.master_key,
    )
    workspace.app = create_app(
        settings=workspace.settings,
        readiness_checks={"fixture": lambda: None},
        auth_service=workspace.auth,
        workspace_service=workspace.service,
        catalogs_service=workspace.catalogs,
    )
    return workspace


def owner(configured, *, read_only: bool = False):
    user = configured.user()
    organization = configured.organization("SELLER")
    seller = configured.seller(organization)
    configured.membership(
        user,
        organization,
        "SELLER_OWNER",
        permission_codes=("WORKSPACE_VIEW", "CATALOG"),
        read_only=read_only,
    )
    client, _ = configured.session(user)
    return client, seller, organization, user


def catalog_path(seller, suffix=""):
    return f"/v1/sellers/{seller}/catalogs{suffix}"


def add_supplier(client, seller) -> str:
    response = client.post(
        catalog_path(seller, "/suppliers"),
        json={"name": "InnPro", "notes": "Feed IOF"},
    )
    assert response.status_code == 201
    return response.json()["created_supplier_id"]


def create_feed(client, seller, supplier_id: str, **changes):
    payload = {
        "supplier_id": supplier_id,
        "name": "Listino InnPro",
        "url": "https://feeds.example.com/catalog.csv?token=super-secret",
        "username": "feed-user",
        "password": "feed-password",
        **changes,
    }
    return client.post(catalog_path(seller, "/price-lists/url"), json=payload)


def test_url_feed_is_encrypted_queued_and_public_response_is_redacted(configured):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)

    response = create_feed(client, seller, supplier_id)

    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["price_list"]["source_type"] == "url"
    assert body["price_list"]["source_host"] == "feeds.example.com"
    assert body["price_list"]["status"] == "queued"
    assert body["price_list"]["file_name"] is None
    assert body["job"]["status"] == "queued"
    assert configured.catalog_queue.jobs == [UUID(body["job"]["id"])]
    assert "super-secret" not in response.text
    assert "feed-password" not in response.text

    with configured.engine.connect() as connection:
        stored = connection.execute(select(seller_price_lists).where(
            seller_price_lists.c.id == UUID(body["price_list"]["id"]),
            seller_price_lists.c.organization_id == organization,
            seller_price_lists.c.seller_id == seller,
        )).mappings().one()
    encrypted = str(stored["source_config_encrypted"])
    assert "super-secret" not in encrypted and "feed-password" not in encrypted


def test_innpro_full_and_light_url_feeds_coexist_with_immutable_identity(configured):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)

    full = create_feed(
        client,
        seller,
        supplier_id,
        name="InnPro FULL",
        provider="innpro",
        feed_role="full",
    )
    light = create_feed(
        client,
        seller,
        supplier_id,
        name="InnPro LIGHT",
        provider="innpro",
        feed_role="light",
    )
    assert full.status_code == light.status_code == 202
    assert (full.json()["price_list"]["provider"], full.json()["price_list"]["feed_role"]) == (
        "innpro", "full"
    )
    assert (
        light.json()["price_list"]["provider"], light.json()["price_list"]["feed_role"]
    ) == ("innpro", "light")

    duplicate_name = create_feed(
        client,
        seller,
        supplier_id,
        name="InnPro FULL",
        provider="innpro",
        feed_role="light",
    )
    assert duplicate_name.status_code == 409

    invalid = create_feed(
        client,
        seller,
        supplier_id,
        name="InnPro standard non valido",
        provider="innpro",
        feed_role="standard",
    )
    assert invalid.status_code == 422
    assert "innpro/full" in invalid.json()["detail"]

    with configured.engine.connect() as connection:
        rows = connection.execute(select(
            seller_price_lists.c.name,
            seller_price_lists.c.provider,
            seller_price_lists.c.feed_role,
        ).where(
            seller_price_lists.c.organization_id == organization,
            seller_price_lists.c.seller_id == seller,
        ).order_by(seller_price_lists.c.name)).all()
    assert rows == [
        ("InnPro FULL", "innpro", "full"),
        ("InnPro LIGHT", "innpro", "light"),
    ]
    for job_id in configured.catalog_queue.jobs:
        source = configured.catalog_repository.source_for_job(job_id)
        assert (source["provider"], source["feed_role"]) in {
            ("innpro", "full"), ("innpro", "light")
        }


def test_worker_activates_versions_and_deduplicates_unchanged_feed(configured):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    created = create_feed(client, seller, supplier_id).json()
    price_list_id = created["price_list"]["id"]
    first_job_id = configured.catalog_queue.jobs[-1]
    csv = b"EAN;SKU;Nome;Costo;Quantita\n0012345678901;SKU-1;Prodotto;12,34;5\n"

    def fetcher(url, *, username, password, on_progress):
        assert url.endswith("token=super-secret")
        assert (username, password) == ("feed-user", "feed-password")
        on_progress(len(csv), len(csv))
        return DownloadedCatalog(
            content=csv,
            file_name="catalog.csv",
            media_type="text/csv",
            source_host="feeds.example.com",
            total_bytes=len(csv),
        )

    configured.catalogs.fetcher = fetcher
    configured.catalogs.run_refresh_job(first_job_id)
    detail = client.get(catalog_path(seller, f"/price-lists/{price_list_id}"))
    assert detail.status_code == 200, detail.text
    assert detail.json()["price_list"]["status"] == "ready"
    assert detail.json()["price_list"]["active_version_number"] == 1
    assert detail.json()["products"][0]["ean"] == "0012345678901"

    refresh = client.post(catalog_path(seller, f"/price-lists/{price_list_id}/refresh"), json={})
    assert refresh.status_code == 202, refresh.text
    second_job_id = configured.catalog_queue.jobs[-1]
    configured.catalogs.run_refresh_job(second_job_id)
    job = client.get(
        catalog_path(seller, f"/price-lists/{price_list_id}/jobs/{second_job_id}")
    )
    assert job.status_code == 200
    assert job.json()["job"]["status"] == "done"
    assert job.json()["job"]["result_version"] == 1
    versions = configured.catalog_repository.version_metadata(
        organization, seller, UUID(price_list_id)
    )
    assert len(versions) == 1


def test_worker_parses_innpro_light_with_declared_role(configured):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    created = create_feed(
        client,
        seller,
        supplier_id,
        name="InnPro LIGHT",
        provider="innpro",
        feed_role="light",
        url="https://feeds.example.com/stock-light.xml",
    ).json()
    job_id = configured.catalog_queue.jobs[-1]
    configured.catalogs.fetcher = lambda *args, **kwargs: DownloadedCatalog(
        content=INNPRO_LIGHT,
        file_name="stock-light.xml",
        media_type="application/xml",
        source_host="feeds.example.com",
        total_bytes=len(INNPRO_LIGHT),
    )

    configured.catalogs.run_refresh_job(job_id)

    detail = client.get(
        catalog_path(seller, f"/price-lists/{created['price_list']['id']}")
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["price_list"]["status"] == "ready"
    assert body["price_list"]["provider"] == "innpro"
    assert body["price_list"]["feed_role"] == "light"
    assert body["price_list"]["file_format"] == "iof"
    assert len(body["products"]) == 1
    product = body["products"][0]
    assert product["ean"] == "6930460000040"
    assert product["sku"] == "SKU-LIGHT"
    assert product["name"] == ""
    assert product["cost"] == "37.7"
    assert product["quantity"] == "140"
    versions = configured.catalog_repository.version_metadata(
        organization, seller, UUID(created["price_list"]["id"])
    )
    assert versions[0]["provider"] == "innpro"
    assert versions[0]["feed_role"] == "light"


def test_worker_rejects_innpro_role_mismatch_without_activating_version(configured):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    create_feed(
        client,
        seller,
        supplier_id,
        name="InnPro FULL",
        provider="innpro",
        feed_role="full",
        url="https://feeds.example.com/stock-full.xml",
    )
    job_id = configured.catalog_queue.jobs[-1]
    configured.catalogs.fetcher = lambda *args, **kwargs: DownloadedCatalog(
        content=INNPRO_LIGHT,
        file_name="wrong-role.xml",
        media_type="application/xml",
        source_host="feeds.example.com",
        total_bytes=len(INNPRO_LIGHT),
    )

    configured.catalogs.run_refresh_job(job_id)

    job = configured.catalog_repository.job(organization, seller, job_id)
    assert job["status"] == "error"
    assert job["error_code"] == "invalid_feed"
    dashboard = client.get(catalog_path(seller)).json()
    assert dashboard["price_lists"][0]["status"] == "error"
    assert dashboard["price_lists"][0]["active_version_number"] == 0


def test_innpro_builtin_worker_uses_disk_spool_and_cleans_it(
    configured, monkeypatch, tmp_path,
):
    client, seller, _, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    created = create_feed(
        client,
        seller,
        supplier_id,
        name="InnPro FULL",
        provider="innpro",
        feed_role="full",
        url="https://feeds.example.com/stock-full.xml",
    ).json()
    job_id = configured.catalog_queue.jobs[-1]
    spool = tmp_path / "innpro-full.download"
    spool.write_bytes(INNPRO_FULL)

    def spooled_fetcher(*args, **kwargs):
        assert kwargs["maximum_bytes"] == 200 * 1024 * 1024
        assert kwargs["deadline_seconds"] == 300
        return DownloadedCatalogFile(
            path=spool,
            file_name="stock-full.xml",
            media_type="application/xml",
            source_host="feeds.example.com",
            total_bytes=len(INNPRO_FULL),
        )

    monkeypatch.setattr(
        "marketplace_hub_core.catalogs.fetching.fetch_catalog_to_file",
        spooled_fetcher,
    )
    configured.catalogs.fetcher = None

    configured.catalogs.run_refresh_job(job_id)

    assert not spool.exists()
    detail = client.get(
        catalog_path(seller, f"/price-lists/{created['price_list']['id']}")
    ).json()
    assert detail["price_list"]["status"] == "ready"
    assert detail["products"][0]["name"] == "Product full name"


def test_queue_failure_is_sanitized_and_keeps_visible_error_job(configured):
    client, seller, _, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    configured.catalog_queue.fail = True

    response = create_feed(client, seller, supplier_id)

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Il servizio di importazione non è disponibile. Riprova tra poco."
    )
    assert "redis detail" not in response.text
    dashboard = client.get(catalog_path(seller)).json()
    assert dashboard["price_lists"][0]["status"] == "error"
    assert dashboard["price_lists"][0]["latest_job"]["error_code"] == "queue_unavailable"


def test_stale_job_never_fetches_a_changed_source_configuration(configured):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    created = create_feed(client, seller, supplier_id).json()
    price_list_id = UUID(created["price_list"]["id"])
    job_id = configured.catalog_queue.jobs[-1]
    with configured.engine.begin() as connection:
        connection.execute(seller_price_lists.update().where(
            seller_price_lists.c.id == price_list_id,
            seller_price_lists.c.organization_id == organization,
            seller_price_lists.c.seller_id == seller,
        ).values(source_config_revision=2))

    called = False

    def forbidden_fetch(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("a stale job must not perform a network request")

    configured.catalogs.fetcher = forbidden_fetch
    configured.catalogs.run_refresh_job(job_id)

    assert called is False
    job = configured.catalog_repository.job(organization, seller, job_id)
    assert job["status"] == "error"
    assert job["error_code"] == "source_changed"


def test_url_route_rejects_unauthorized_or_malformed_secret_without_queueing(configured):
    client, seller, _, _ = owner(configured, read_only=True)
    foreign_supplier = uuid4()
    secret = "must-not-appear"
    forbidden = create_feed(
        client,
        seller,
        str(foreign_supplier),
        url=f"https://feeds.example.com/catalog.csv?token={secret}",
    )
    assert forbidden.status_code == 403
    assert secret not in forbidden.text
    assert configured.catalog_queue.jobs == []

    anonymous = TestClient(configured.app).post(
        catalog_path(seller, "/price-lists/url"),
        json={
            "supplier_id": str(foreign_supplier),
            "name": "Riservato",
            "url": f"https://feeds.example.com/{secret}",
            "username": "",
            "password": "",
        },
    )
    assert anonymous.status_code == 401
    assert secret not in anonymous.text


@pytest.mark.parametrize(
    "suffix",
    [
        "/price-lists/url",
        f"/price-lists/{uuid4()}/url",
    ],
    ids=["create", "update"],
)
def test_url_json_is_not_read_before_authentication_and_catalog_write_authorization(
    configured, monkeypatch, suffix,
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
    body = b'{"url":"preflight-secret"'
    headers = {"content-type": "application/json"}

    anonymous = anonymous_client.post(
        catalog_path(seller, suffix), content=body, headers=headers,
    )
    forged = forged_client.post(
        catalog_path(seller, suffix), content=body, headers=headers,
    )
    forbidden = read_only_client.post(
        catalog_path(seller, suffix), content=body, headers=headers,
    )

    assert [anonymous.status_code, forged.status_code, forbidden.status_code] == [401, 401, 403]
    assert reads == 0
    assert configured.catalog_queue.jobs == []
    for response in (anonymous, forged, forbidden):
        assert response.headers["cache-control"] == "no-store"
        assert "preflight-secret" not in response.text


@pytest.mark.parametrize(
    "suffix",
    [
        "/price-lists/url",
        f"/price-lists/{uuid4()}/url",
    ],
    ids=["create", "update"],
)
def test_url_json_validation_and_16_kib_limit_do_not_mutate_or_queue(
    configured, suffix,
):
    client, seller, _, _ = owner(configured)
    before = client.get(catalog_path(seller)).json()
    malformed_secret = "malformed-body-secret"

    malformed = client.post(
        catalog_path(seller, suffix),
        content=f'{{"url":"{malformed_secret}"'.encode(),
        headers={"content-type": "application/json"},
    )
    oversized_secret = b"oversized-body-secret"
    oversized_body = b'{"url":"' + oversized_secret + b"x" * (
        catalogs_api.MAX_CATALOG_JSON_BYTES
    ) + b'"}'
    oversized = client.post(
        catalog_path(seller, suffix),
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
    assert configured.catalog_queue.jobs == []
    assert client.get(catalog_path(seller)).json() == before


def test_url_source_update_rejects_cross_origin_keep_without_mutation_or_leak(
    configured,
):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    created = create_feed(client, seller, supplier_id).json()
    price_list_id = created["price_list"]["id"]
    queue_before = list(configured.catalog_queue.jobs)

    with configured.engine.connect() as connection:
        before_cross_origin = connection.execute(select(
            seller_price_lists.c.source_config_encrypted,
            seller_price_lists.c.source_host,
            seller_price_lists.c.source_config_revision,
            seller_price_lists.c.active_version_number,
            seller_price_lists.c.updated_at,
        ).where(
            seller_price_lists.c.id == UUID(price_list_id),
            seller_price_lists.c.organization_id == organization,
            seller_price_lists.c.seller_id == seller,
        )).mappings().one()

    cross_origin_secret = "must-not-cross-origin"
    cross_origin = client.post(
        catalog_path(seller, f"/price-lists/{price_list_id}/url"),
        json={
            "url": (
                "https://other.example.com/list.csv"
                f"?token={cross_origin_secret}"
            ),
            "credentials_mode": "keep",
            "expected_config_revision": 1,
        },
    )
    assert cross_origin.status_code == 422
    assert cross_origin.json() == {
        "detail": (
            "Non puoi conservare le credenziali quando cambi il server del feed. "
            "Sostituiscile o rimuovile."
        )
    }
    assert cross_origin_secret not in cross_origin.text
    assert "feed-password" not in cross_origin.text
    with configured.engine.connect() as connection:
        after_cross_origin = connection.execute(select(
            seller_price_lists.c.source_config_encrypted,
            seller_price_lists.c.source_host,
            seller_price_lists.c.source_config_revision,
            seller_price_lists.c.active_version_number,
            seller_price_lists.c.updated_at,
        ).where(
            seller_price_lists.c.id == UUID(price_list_id),
            seller_price_lists.c.organization_id == organization,
            seller_price_lists.c.seller_id == seller,
        )).mappings().one()
    assert dict(after_cross_origin) == dict(before_cross_origin)
    assert configured.catalog_queue.jobs == queue_before


def test_url_source_update_keeps_replaces_and_removes_credentials_without_refresh(
    configured,
):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    created = create_feed(client, seller, supplier_id).json()
    price_list_id = created["price_list"]["id"]
    initial_job = configured.catalog_queue.jobs[-1]
    csv = b"EAN;SKU;Nome;Costo\n0012345678901;SKU-1;Prodotto;12,34\n"

    configured.catalogs.fetcher = lambda *args, **kwargs: DownloadedCatalog(
        content=csv,
        file_name="catalog.csv",
        media_type="text/csv",
        source_host="feeds.example.com",
        total_bytes=len(csv),
    )
    configured.catalogs.run_refresh_job(initial_job)
    queue_before = list(configured.catalog_queue.jobs)

    kept = client.post(
        catalog_path(seller, f"/price-lists/{price_list_id}/url"),
        json={
            "url": "https://feeds.example.com/list.csv?key=new-query-secret",
            "credentials_mode": "keep",
            "expected_config_revision": 1,
        },
    )
    assert kept.status_code == 200, kept.text
    assert kept.headers["cache-control"] == "no-store"
    public = kept.json()["price_list"]
    assert public["source_host"] == "feeds.example.com"
    assert public["source_config_revision"] == 2
    assert public["active_version_number"] == 1
    assert public["row_count"] == 1
    assert "new-query-secret" not in kept.text
    assert "feed-password" not in kept.text
    assert configured.catalog_queue.jobs == queue_before

    def stored_source():
        with configured.engine.connect() as connection:
            encrypted = connection.scalar(select(
                seller_price_lists.c.source_config_encrypted,
            ).where(
                seller_price_lists.c.id == UUID(price_list_id),
                seller_price_lists.c.organization_id == organization,
                seller_price_lists.c.seller_id == seller,
            ))
        return decrypt_credentials(str(encrypted), configured.settings.master_key)

    assert stored_source() == {
        "url": "https://feeds.example.com/list.csv?key=new-query-secret",
        "username": "feed-user",
        "password": "feed-password",
    }

    replaced = client.post(
        catalog_path(seller, f"/price-lists/{price_list_id}/url"),
        json={
            "url": "https://replacement.example.com/feed.csv?token=replace-secret",
            "credentials_mode": "replace",
            "expected_config_revision": 2,
            "username": "replacement-user",
            "password": "replacement-password",
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["price_list"]["source_config_revision"] == 3
    assert "replace-secret" not in replaced.text
    assert "replacement-password" not in replaced.text
    assert stored_source() == {
        "url": "https://replacement.example.com/feed.csv?token=replace-secret",
        "username": "replacement-user",
        "password": "replacement-password",
    }

    removed = client.post(
        catalog_path(seller, f"/price-lists/{price_list_id}/url"),
        json={
            "url": "https://public.example.com/feed.csv?public=still-private",
            "credentials_mode": "remove",
            "expected_config_revision": 3,
        },
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["price_list"]["source_config_revision"] == 4
    assert "still-private" not in removed.text
    assert stored_source() == {
        "url": "https://public.example.com/feed.csv?public=still-private",
        "username": "",
        "password": "",
    }
    assert configured.catalog_queue.jobs == queue_before


def test_url_source_update_rejects_active_job_stale_revision_and_bad_credentials(
    configured,
):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    created = create_feed(client, seller, supplier_id).json()
    price_list_id = created["price_list"]["id"]
    job_id = configured.catalog_queue.jobs[-1]
    path = catalog_path(seller, f"/price-lists/{price_list_id}/url")

    active_secret = "active-query-secret"
    active = client.post(path, json={
        "url": f"https://active.example.com/feed.csv?token={active_secret}",
        "credentials_mode": "remove",
        "expected_config_revision": 1,
    })
    assert active.status_code == 409
    assert active_secret not in active.text

    configured.catalog_repository.fail_job(
        organization,
        seller,
        job_id,
        error_code="download_failed",
        message="Download non riuscito.",
    )
    accepted = client.post(path, json={
        "url": "https://accepted.example.com/feed.csv?token=accepted-secret",
        "credentials_mode": "remove",
        "expected_config_revision": 1,
    })
    assert accepted.status_code == 200

    stale_secret = "stale-query-secret"
    stale = client.post(path, json={
        "url": f"https://stale.example.com/feed.csv?token={stale_secret}",
        "credentials_mode": "remove",
        "expected_config_revision": 1,
    })
    assert stale.status_code == 409
    assert stale_secret not in stale.text

    for payload in (
        {
            "url": "https://example.com/feed.csv",
            "credentials_mode": "replace",
            "expected_config_revision": 2,
            "username": "only-user",
        },
        {
            "url": "https://example.com/feed.csv",
            "credentials_mode": "keep",
            "expected_config_revision": 2,
            "password": "unexpected-secret",
        },
        {
            "url": "https://example.com/feed.csv",
            "credentials_mode": "invalid-mode",
            "expected_config_revision": 2,
            "password": "schema-secret",
        },
    ):
        rejected = client.post(path, json=payload)
        assert rejected.status_code == 422
        if payload.get("password"):
            assert payload["password"] not in rejected.text

    with configured.engine.connect() as connection:
        stored = connection.execute(select(
            seller_price_lists.c.source_host,
            seller_price_lists.c.source_config_revision,
        ).where(seller_price_lists.c.id == UUID(price_list_id))).mappings().one()
    assert dict(stored) == {
        "source_host": "accepted.example.com", "source_config_revision": 2,
    }


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
@pytest.mark.parametrize("target", ["price_list", "supplier"])
def test_delete_rejects_active_refresh_with_conflict_and_preserves_catalog(
    configured, running, target,
):
    client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(client, seller)
    created = create_feed(client, seller, supplier_id).json()
    price_list_id = created["price_list"]["id"]
    job_id = configured.catalog_queue.jobs[-1]
    if running:
        assert configured.catalog_repository.claim_job(
            organization, seller, job_id,
        ) is True
    expected_status = "running" if running else "queued"
    before = client.get(catalog_path(seller)).json()

    if target == "price_list":
        endpoint = catalog_path(seller, f"/price-lists/{price_list_id}")
        confirmation = "ELIMINA"
    else:
        endpoint = catalog_path(seller, f"/suppliers/{supplier_id}")
        confirmation = "InnPro"
    response = client.request(
        "DELETE", endpoint, json={"confirmation": confirmation},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Attendi il completamento dell'aggiornamento del listino."
    }
    assert response.headers["cache-control"] == "no-store"
    assert client.get(catalog_path(seller)).json() == before
    assert configured.catalog_repository.job(
        organization, seller, job_id,
    )["status"] == expected_status


def test_url_source_update_enforces_tenant_write_scope_before_reading_secrets(configured):
    owner_client, seller, organization, _ = owner(configured)
    supplier_id = add_supplier(owner_client, seller)
    created = create_feed(owner_client, seller, supplier_id).json()
    price_list_id = created["price_list"]["id"]
    configured.catalog_repository.fail_job(
        organization,
        seller,
        configured.catalog_queue.jobs[-1],
        error_code="download_failed",
        message="Download non riuscito.",
    )
    secret = "scope-query-secret"
    payload = {
        "url": f"https://scope.example.com/feed.csv?token={secret}",
        "credentials_mode": "remove",
        "expected_config_revision": 1,
    }

    viewer = configured.user()
    configured.membership(
        viewer,
        organization,
        "SELLER_USER",
        permission_codes=("WORKSPACE_VIEW", "CATALOG"),
        read_only=True,
    )
    read_only_client, _ = configured.session(viewer)
    forbidden = read_only_client.post(
        catalog_path(seller, f"/price-lists/{price_list_id}/url"), json=payload,
    )
    assert forbidden.status_code == 403
    assert secret not in forbidden.text

    foreign_client, _, _, _ = owner(configured)
    hidden = foreign_client.post(
        catalog_path(seller, f"/price-lists/{price_list_id}/url"), json=payload,
    )
    assert hidden.status_code == 404
    assert secret not in hidden.text

    anonymous = TestClient(configured.app).post(
        catalog_path(seller, f"/price-lists/{price_list_id}/url"), json=payload,
    )
    assert anonymous.status_code == 401
    assert secret not in anonymous.text
