from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.catalog_intelligence.accounts import load_marketplace_account
from services.catalog_intelligence.marketplaces.kaufland import parse_category_attributes
from services.catalog_intelligence.models import TaxonomyAttribute
from services.catalog_intelligence.repository import (
    save_taxonomy_category_enrichment,
    taxonomy_attribute_values,
    taxonomy_attributes,
    taxonomy_category_enrichment,
)
from services.catalog_intelligence.utils import clean_text, load_json
from services.kaufland import KauflandClient


def _attribute_from_mapping(
    row: Mapping[str, Any],
    *,
    snapshot_id: int | None = None,
) -> TaxonomyAttribute:
    external_id = clean_text(row.get("external_id"))
    category_external_id = clean_text(row.get("category_external_id"))
    values = row.get("values")
    if not isinstance(values, list) and snapshot_id and external_id:
        values = [
            {
                "code": item.get("value_code"),
                "label": item.get("label"),
                **(load_json(item.get("raw_json"), {}) if isinstance(load_json(item.get("raw_json"), {}), dict) else {}),
            }
            for item in taxonomy_attribute_values(
                snapshot_id,
                external_id,
                category_external_id=category_external_id,
                limit=50000,
            )
        ]
    return TaxonomyAttribute(
        external_id=external_id,
        category_external_id=category_external_id,
        code=clean_text(row.get("code")) or external_id,
        label=clean_text(row.get("label")) or external_id,
        data_type=clean_text(row.get("data_type")) or "TEXT",
        requirement_level=clean_text(row.get("requirement_level")) or "OPTIONAL",
        required=bool(row.get("required")),
        multiple=bool(row.get("multiple")),
        variant=bool(row.get("variant")),
        unit=clean_text(row.get("unit")),
        locale=clean_text(row.get("locale")),
        value_list_code=clean_text(row.get("value_list_code")),
        constraints=(
            dict(row.get("constraints"))
            if isinstance(row.get("constraints"), Mapping)
            else load_json(row.get("constraints_json"), {})
        ),
        values=list(values or []),
        conditions=(
            list(row.get("conditions"))
            if isinstance(row.get("conditions"), list)
            else []
        ),
        raw=(
            dict(row.get("raw"))
            if isinstance(row.get("raw"), Mapping)
            else load_json(row.get("raw_json"), {})
        ),
    )


def _enrichment_attributes(enrichment: Mapping[str, Any] | None) -> list[TaxonomyAttribute]:
    if not enrichment or clean_text(enrichment.get("status")).upper() != "COMPLETED":
        return []
    payload = load_json(enrichment.get("attributes_json"), [])
    if not isinstance(payload, list):
        return []
    return [_attribute_from_mapping(item) for item in payload if isinstance(item, Mapping)]


def cached_category_attributes(
    *,
    snapshot_id: int,
    category_external_id: str,
    account_id: int | None = None,
    environment: str = "live",
    scope_key: str = "",
) -> list[TaxonomyAttribute]:
    """Return general + category attributes without any network request."""
    result = [
        _attribute_from_mapping(item, snapshot_id=snapshot_id)
        for item in taxonomy_attributes(snapshot_id, clean_text(category_external_id))
    ]
    if account_id is not None:
        enrichment = taxonomy_category_enrichment(
            int(account_id),
            snapshot_id=int(snapshot_id),
            environment=environment,
            scope_key=scope_key,
            category_external_id=clean_text(category_external_id),
        )
        result.extend(_enrichment_attributes(enrichment))
    deduplicated: dict[tuple[str, str], TaxonomyAttribute] = {}
    for item in result:
        key = (item.category_external_id, item.external_id)
        previous = deduplicated.get(key)
        if previous is None:
            deduplicated[key] = item
        elif item.required and not previous.required:
            deduplicated[key] = item
        elif len(item.values) > len(previous.values):
            deduplicated[key] = item
    return sorted(
        deduplicated.values(),
        key=lambda item: (not item.required, item.category_external_id != clean_text(category_external_id), item.label.lower()),
    )


def ensure_category_attributes(
    *,
    seller_id: int,
    account_id: int,
    snapshot: Mapping[str, Any],
    category_external_id: str,
    environment: str = "live",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Load a category schema once and reuse it from the database afterwards.

    Mirakl PM11 already returns attributes for all hierarchy codes, so no extra
    request is necessary.  Kaufland category-specific attributes are lazily
    enriched on the first product assigned to a category and persisted against
    the immutable taxonomy snapshot.
    """
    marketplace = clean_text(snapshot.get("marketplace")).lower()
    scope_key = clean_text(snapshot.get("scope_key"))
    category_id = clean_text(category_external_id)
    if not category_id:
        raise ValueError("Categoria mancante.")

    cached = taxonomy_category_enrichment(
        account_id,
        snapshot_id=int(snapshot["id"]),
        environment=environment,
        scope_key=scope_key,
        category_external_id=category_id,
    )
    if marketplace != "kaufland":
        attributes = cached_category_attributes(
            snapshot_id=int(snapshot["id"]),
            category_external_id=category_id,
            account_id=account_id,
            environment=environment,
            scope_key=scope_key,
        )
        return {"attributes": attributes, "from_cache": True, "enrichment": cached}

    if cached and clean_text(cached.get("status")).upper() == "COMPLETED" and not force_refresh:
        attributes = cached_category_attributes(
            snapshot_id=int(snapshot["id"]),
            category_external_id=category_id,
            account_id=account_id,
            environment=environment,
            scope_key=scope_key,
        )
        return {"attributes": attributes, "from_cache": True, "enrichment": cached}

    account = load_marketplace_account(account_id, seller_id=seller_id)
    credentials = account.get("credentials") or {}
    client_key = clean_text(credentials.get("client_key"))
    secret_key = clean_text(credentials.get("secret_key"))
    if not client_key or not secret_key:
        raise ValueError("Client Key e Secret Key Kaufland sono obbligatorie.")
    client = KauflandClient(
        client_key=client_key,
        secret_key=secret_key,
        playground=clean_text(environment).lower() in {"test", "playground"},
    )
    storefront = clean_text(snapshot.get("storefront")).lower()
    locale = clean_text(snapshot.get("locale"))
    try:
        detail_payload = client.category(
            int(category_id),
            storefront,
            locale=locale,
            include_attributes=True,
        )
        detail: Mapping[str, Any]
        if isinstance(detail_payload, Mapping) and isinstance(detail_payload.get("data"), Mapping):
            detail = detail_payload["data"]
        elif isinstance(detail_payload, Mapping):
            detail = detail_payload
        else:
            detail = {}
        parsed = parse_category_attributes(category_id, detail)
        save_taxonomy_category_enrichment(
            seller_id=seller_id,
            account_id=account_id,
            snapshot_id=int(snapshot["id"]),
            marketplace=marketplace,
            environment=environment,
            scope_key=scope_key,
            category_external_id=category_id,
            category=detail,
            attributes=parsed,
            status="COMPLETED",
        )
    except Exception as exc:
        save_taxonomy_category_enrichment(
            seller_id=seller_id,
            account_id=account_id,
            snapshot_id=int(snapshot["id"]),
            marketplace=marketplace,
            environment=environment,
            scope_key=scope_key,
            category_external_id=category_id,
            category={},
            attributes=[],
            status="FAILED",
            error=str(exc),
        )
        raise

    enrichment = taxonomy_category_enrichment(
        account_id,
        snapshot_id=int(snapshot["id"]),
        environment=environment,
        scope_key=scope_key,
        category_external_id=category_id,
    )
    attributes = cached_category_attributes(
        snapshot_id=int(snapshot["id"]),
        category_external_id=category_id,
        account_id=account_id,
        environment=environment,
        scope_key=scope_key,
    )
    return {"attributes": attributes, "from_cache": False, "enrichment": enrichment}


__all__ = [
    "cached_category_attributes",
    "ensure_category_attributes",
]
