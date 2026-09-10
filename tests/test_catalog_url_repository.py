from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
import test_tenancy_api
from marketplace_hub_core.catalogs.repository import (
    CatalogPriceListNotFoundError,
    CatalogRefreshInProgressError,
    CatalogRefreshJobNotFoundError,
    CatalogSourceRevisionMismatchError,
    SqlCatalogsRepository,
)
from marketplace_hub_core.catalogs.schema import (
    seller_price_list_products,
    seller_price_list_refresh_jobs,
    seller_price_list_versions,
    seller_price_lists,
)
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql

workspace = test_tenancy_api.workspace


def _product(source_row: int, *, sku: str, cost: str) -> dict:
    amount = Decimal(cost)
    return {
        "source_row": source_row,
        "ean": f"805000000{source_row:04d}",
        "sku": sku,
        "name": f"Prodotto {sku}",
        "cost": amount,
        "shipping_cost": Decimal("1"),
        "total_cost": amount + 1,
        "quantity": Decimal("4"),
        "canonical_json": f'{{"sku":"{sku}"}}',
    }


def _context(workspace):
    user = workspace.user()
    organization_id = workspace.organization("SELLER")
    seller_id = workspace.seller(organization_id)
    supplier_id = SqlCatalogsRepository(workspace.engine).add_supplier(
        organization_id, seller_id, name="InnPro", notes="",
    )
    return user, organization_id, seller_id, supplier_id


def _url_list(repository, context):
    user, organization_id, seller_id, supplier_id = context
    result = repository.create_url_price_list(
        organization_id,
        seller_id,
        supplier_id,
        name="Feed InnPro",
        source_config_encrypted="opaque-fernet-value",
        source_host="feed.example.test",
        requested_by=user.id,
        realm="seller",
    )
    return organization_id, seller_id, result


def test_url_list_and_job_are_atomic_idempotent_and_public_dtos_hide_source(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    organization_id, seller_id, created = _url_list(repository, _context(workspace))
    list_id = UUID(created["price_list_id"])
    job_id = UUID(created["job_id"])

    assert created["created_job"] is True
    assert created["status"] == "queued"
    repeated = repository.create_refresh_job(
        organization_id,
        seller_id,
        list_id,
        requested_by=workspace.user().id,
        realm="seller",
    )
    assert repeated["created_job"] is False
    assert repeated["job_id"] == created["job_id"]

    dashboard = repository.dashboard(organization_id, seller_id)
    public = dashboard["price_lists"][0]
    assert public["source_type"] == "url"
    assert public["status"] == "queued"
    assert public["file_name"] is None
    assert public["active_version_number"] == 0
    assert public["source_host"] == "feed.example.test"
    serialized = repr({"created": created, "dashboard": dashboard})
    assert "opaque-fernet-value" not in serialized
    assert "requested_by" not in serialized
    assert "realm" not in serialized

    internal = repository.source_for_job(job_id)
    assert internal["source_config_encrypted"] == "opaque-fernet-value"
    assert internal["organization_id"] == organization_id
    assert internal["seller_id"] == seller_id


def test_refresh_versions_are_immutable_deduplicated_and_switch_atomically(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    context = _context(workspace)
    organization_id, seller_id, created = _url_list(repository, context)
    list_id = UUID(created["price_list_id"])
    first_job = UUID(created["job_id"])
    first_artifact = b"ean;sku;cost\n8050000000002;FIRST;10\n"
    first_products = [_product(2, sku="FIRST", cost="10")]

    assert repository.claim_job(organization_id, seller_id, first_job) is True
    repository.job_progress(
        organization_id,
        seller_id,
        first_job,
        processed_bytes=10,
        total_bytes=len(first_artifact),
        message="Download in corso.",
    )
    first = repository.activate_remote_version(
        first_job,
        expected_config_revision=1,
        original_filename="feed.csv",
        media_type="text/csv",
        file_format="csv",
        artifact=first_artifact,
        normalized_products=first_products,
    )
    assert first == {"duplicate": False, "version_number": 1}
    assert repository.job(organization_id, seller_id, first_job)["progress"] == 100
    assert repository.detail(
        organization_id, seller_id, list_id, limit=100,
    )["products"][0]["sku"] == "FIRST"

    second = repository.create_refresh_job(
        organization_id,
        seller_id,
        list_id,
        requested_by=context[0].id,
        realm="seller",
    )
    second_job = UUID(second["job_id"])
    second_artifact = b"ean;sku;cost\n8050000000003;SECOND;20\n"
    assert repository.claim_job(organization_id, seller_id, second_job)
    activated = repository.activate_remote_version(
        second_job,
        expected_config_revision=1,
        original_filename="feed.csv",
        media_type="text/csv",
        file_format="csv",
        artifact=second_artifact,
        normalized_products=[_product(3, sku="SECOND", cost="20")],
    )
    assert activated == {"duplicate": False, "version_number": 2}
    assert [item["version_number"] for item in repository.version_metadata(
        organization_id, seller_id, list_id,
    )] == [2, 1]
    assert repository.detail(
        organization_id, seller_id, list_id, limit=100,
    )["products"][0]["sku"] == "SECOND"

    duplicate = repository.create_refresh_job(
        organization_id,
        seller_id,
        list_id,
        requested_by=context[0].id,
        realm="seller",
    )
    duplicate_job = UUID(duplicate["job_id"])
    assert repository.claim_job(organization_id, seller_id, duplicate_job)
    reused = repository.activate_remote_version(
        duplicate_job,
        expected_config_revision=1,
        original_filename="ignored.csv",
        media_type="text/csv",
        file_format="csv",
        artifact=first_artifact,
        normalized_products=first_products,
    )
    assert reused == {"duplicate": True, "version_number": 1}
    assert [item["version_number"] for item in repository.version_metadata(
        organization_id, seller_id, list_id,
    )] == [2, 1]
    assert repository.detail(
        organization_id, seller_id, list_id, limit=100,
    )["products"][0]["sku"] == "FIRST"
    with workspace.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(seller_price_list_products).where(
                seller_price_list_products.c.price_list_id == list_id,
            )
        ) == 2


def test_revision_cas_and_failed_refresh_preserve_previous_snapshot(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    context = _context(workspace)
    organization_id, seller_id, created = _url_list(repository, context)
    list_id = UUID(created["price_list_id"])
    first_job = UUID(created["job_id"])
    artifact = b"ean;sku;cost\n8050000000002;SAFE;10\n"
    assert repository.claim_job(organization_id, seller_id, first_job)
    repository.activate_remote_version(
        first_job,
        expected_config_revision=1,
        original_filename="feed.csv",
        media_type="text/csv",
        file_format="csv",
        artifact=artifact,
        normalized_products=[_product(2, sku="SAFE", cost="10")],
    )

    refresh = repository.create_refresh_job(
        organization_id,
        seller_id,
        list_id,
        requested_by=context[0].id,
        realm="seller",
    )
    refresh_id = UUID(refresh["job_id"])
    assert repository.claim_job(organization_id, seller_id, refresh_id)
    with workspace.engine.begin() as connection:
        connection.execute(seller_price_lists.update().where(
            seller_price_lists.c.id == list_id,
        ).values(source_config_revision=2))

    with pytest.raises(CatalogSourceRevisionMismatchError):
        repository.activate_remote_version(
            refresh_id,
            expected_config_revision=1,
            original_filename="changed.csv",
            media_type="text/csv",
            file_format="csv",
            artifact=b"different",
            normalized_products=[_product(3, sku="UNSAFE", cost="99")],
        )
    repository.fail_job(
        organization_id,
        seller_id,
        refresh_id,
        error_code="source_changed",
        message="Configurazione cambiata.",
    )
    detail = repository.detail(organization_id, seller_id, list_id, limit=100)
    assert detail["products"][0]["sku"] == "SAFE"
    assert detail["price_list"]["active_version_number"] == 1
    assert detail["price_list"]["status"] == "error"
    assert repository.job(organization_id, seller_id, refresh_id)["error_code"] == "source_changed"


def test_refresh_job_scope_and_catalog_cascade_are_tenant_safe(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    context = _context(workspace)
    organization_id, seller_id, created = _url_list(repository, context)
    list_id = UUID(created["price_list_id"])
    job_id = UUID(created["job_id"])
    foreign_organization = workspace.organization("SELLER")
    foreign_seller = workspace.seller(foreign_organization)

    with pytest.raises(CatalogRefreshJobNotFoundError):
        repository.job(foreign_organization, foreign_seller, job_id)
    assert repository.claim_job(foreign_organization, foreign_seller, job_id) is False
    with pytest.raises(CatalogRefreshInProgressError):
        repository.delete_price_list(organization_id, seller_id, list_id)
    repository.fail_job(
        organization_id,
        seller_id,
        job_id,
        error_code="download_failed",
        message="Download non riuscito.",
    )
    repository.delete_price_list(organization_id, seller_id, list_id)
    with workspace.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(seller_price_list_refresh_jobs)
        ) == 0
        assert connection.scalar(
            select(func.count()).select_from(seller_price_list_versions)
        ) == 0


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
def test_active_refresh_blocks_list_and_supplier_deletes_without_mutation(
    workspace, running,
):
    repository = SqlCatalogsRepository(workspace.engine)
    context = _context(workspace)
    organization_id, seller_id, created = _url_list(repository, context)
    supplier_id = context[3]
    list_id = UUID(created["price_list_id"])
    job_id = UUID(created["job_id"])
    if running:
        assert repository.claim_job(organization_id, seller_id, job_id) is True
    expected_status = "running" if running else "queued"
    before = repository.dashboard(organization_id, seller_id)

    with pytest.raises(CatalogRefreshInProgressError):
        repository.delete_price_list(organization_id, seller_id, list_id)
    assert repository.dashboard(organization_id, seller_id) == before

    with pytest.raises(CatalogRefreshInProgressError):
        repository.delete_supplier(
            organization_id,
            seller_id,
            supplier_id,
            confirmation="InnPro",
        )
    assert repository.dashboard(organization_id, seller_id) == before
    assert repository.job(organization_id, seller_id, job_id)["status"] == expected_status


def test_price_list_delete_locks_jobs_before_list_and_rechecks_before_cascade(
    workspace,
):
    repository = SqlCatalogsRepository(workspace.engine)
    organization_id, seller_id, created = _url_list(repository, _context(workspace))
    list_id = UUID(created["price_list_id"])
    job_id = UUID(created["job_id"])
    repository.fail_job(
        organization_id,
        seller_id,
        job_id,
        error_code="download_failed",
        message="Download non riuscito.",
    )
    statements = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.split()).lower()
        if normalized.startswith("select") and (
            "seller_price_lists" in normalized
            or "seller_price_list_refresh_jobs" in normalized
        ):
            statements.append(normalized)

    event.listen(workspace.engine, "before_cursor_execute", record_sql)
    try:
        repository.delete_price_list(organization_id, seller_id, list_id)
    finally:
        event.remove(workspace.engine, "before_cursor_execute", record_sql)

    # Discovery is non-locking; the mutation sequence is active jobs -> list ->
    # active-job recheck, matching activate_remote_version's job -> list order.
    assert "seller_price_lists" in statements[0]
    assert "seller_price_list_refresh_jobs" in statements[1]
    assert "seller_price_lists" in statements[2]
    assert "seller_price_list_refresh_jobs" in statements[3]
    locked_jobs = repository._active_refresh_statement(
        organization_id, seller_id, [list_id], lock=True,
    )
    postgres_sql = str(locked_jobs.compile(dialect=postgresql.dialect()))
    assert "ORDER BY seller_price_list_refresh_jobs.id FOR UPDATE" in postgres_sql


def test_url_source_update_is_scoped_cas_guarded_and_preserves_active_version(workspace):
    repository = SqlCatalogsRepository(workspace.engine)
    context = _context(workspace)
    organization_id, seller_id, created = _url_list(repository, context)
    list_id = UUID(created["price_list_id"])
    initial_job = UUID(created["job_id"])
    artifact = b"ean;sku;cost\n8050000000002;STABLE;10\n"
    assert repository.claim_job(organization_id, seller_id, initial_job)
    repository.activate_remote_version(
        initial_job,
        expected_config_revision=1,
        original_filename="feed.csv",
        media_type="text/csv",
        file_format="csv",
        artifact=artifact,
        normalized_products=[_product(2, sku="STABLE", cost="10")],
    )

    revision = repository.update_url_source(
        organization_id,
        seller_id,
        list_id,
        source_config_encrypted="new-opaque-fernet-value",
        source_host="new-feed.example.test",
        expected_config_revision=1,
    )
    assert revision == 2
    detail = repository.detail(organization_id, seller_id, list_id, limit=100)
    assert detail["price_list"]["source_config_revision"] == 2
    assert detail["price_list"]["source_host"] == "new-feed.example.test"
    assert detail["price_list"]["active_version_number"] == 1
    assert detail["products"][0]["sku"] == "STABLE"
    assert len(repository.version_metadata(organization_id, seller_id, list_id)) == 1
    stored = repository.url_source_configuration(
        organization_id,
        seller_id,
        list_id,
        expected_config_revision=2,
    )
    assert stored["source_config_encrypted"] == "new-opaque-fernet-value"
    assert stored["source_host"] == "new-feed.example.test"

    with pytest.raises(CatalogSourceRevisionMismatchError):
        repository.update_url_source(
            organization_id,
            seller_id,
            list_id,
            source_config_encrypted="stale-value",
            source_host="stale.example.test",
            expected_config_revision=1,
        )
    active = repository.create_refresh_job(
        organization_id,
        seller_id,
        list_id,
        requested_by=context[0].id,
        realm="seller",
    )
    with pytest.raises(CatalogRefreshInProgressError):
        repository.update_url_source(
            organization_id,
            seller_id,
            list_id,
            source_config_encrypted="blocked-value",
            source_host="blocked.example.test",
            expected_config_revision=2,
        )
    repository.fail_job(
        organization_id,
        seller_id,
        UUID(active["job_id"]),
        error_code="download_failed",
        message="Download non riuscito.",
    )

    foreign_organization = workspace.organization("SELLER")
    foreign_seller = workspace.seller(foreign_organization)
    with pytest.raises(CatalogPriceListNotFoundError):
        repository.update_url_source(
            foreign_organization,
            foreign_seller,
            list_id,
            source_config_encrypted="foreign-value",
            source_host="foreign.example.test",
            expected_config_revision=2,
        )

    upload_id = repository.add_price_list(
        organization_id,
        seller_id,
        context[3],
        name="Listino caricato",
        original_filename="upload.csv",
        media_type="text/csv",
        file_format="csv",
        artifact=artifact,
        normalized_products=[_product(4, sku="UPLOAD", cost="12")],
    )
    with pytest.raises(CatalogPriceListNotFoundError):
        repository.update_url_source(
            organization_id,
            seller_id,
            upload_id,
            source_config_encrypted="must-not-be-written",
            source_host="upload.example.test",
            expected_config_revision=1,
        )
