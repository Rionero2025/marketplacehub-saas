from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from services.catalog_intelligence.accounts import load_marketplace_account
from services.catalog_intelligence.marketplaces.kaufland import sync_taxonomy as sync_kaufland
from services.catalog_intelligence.marketplaces.mirakl import (
    MiraklCatalogClient,
    sync_taxonomy as sync_mirakl,
)
from services.catalog_intelligence.models import TaxonomyBundle
from services.catalog_intelligence.repository import (
    complete_taxonomy_sync,
    latest_taxonomy_snapshot,
    save_taxonomy_bundle,
    start_taxonomy_sync,
)
from services.catalog_intelligence.utils import clean_text
from services.kaufland import KauflandClient
from services.db import row


def taxonomy_scope_key(
    marketplace: str,
    *,
    storefront: str = "",
    locale: str = "",
    hierarchy: str = "",
) -> str:
    marketplace = clean_text(marketplace).lower()
    if marketplace == "kaufland":
        return f"{clean_text(storefront).lower()}:{clean_text(locale) or '*'}"
    return f"{clean_text(hierarchy) or 'all'}:{clean_text(locale) or 'pt_PT'}"


def sync_account_taxonomy(
    *,
    seller_id: int,
    account_id: int,
    environment: str = "live",
    storefront: str = "",
    locale: str = "",
    hierarchy: str = "",
    category_attribute_ids: Iterable[str] = (),
    progress=None,
) -> dict[str, Any]:
    account = load_marketplace_account(account_id, seller_id=seller_id)
    marketplace = account["marketplace"]
    environment = clean_text(environment).lower() or "live"
    credentials = account.get("credentials") or {}

    if marketplace == "kaufland":
        selected_storefront = clean_text(storefront).lower()
        selected_locale = clean_text(locale)
    else:
        selected_storefront = "pt"
        selected_locale = clean_text(locale) or "pt_PT"
    scope_key = taxonomy_scope_key(
        marketplace,
        storefront=selected_storefront,
        locale=selected_locale,
        hierarchy=hierarchy,
    )

    run_id = start_taxonomy_sync(
        seller_id=seller_id,
        account_id=account_id,
        marketplace=marketplace,
        environment=environment,
        scope_key=scope_key,
        storefront=selected_storefront,
        locale=selected_locale,
        details={"hierarchy": clean_text(hierarchy)},
    )
    try:
        bundle: TaxonomyBundle
        if marketplace == "kaufland":
            client_key = clean_text(credentials.get("client_key"))
            secret_key = clean_text(credentials.get("secret_key"))
            if not client_key or not secret_key:
                raise ValueError("Client Key e Secret Key Kaufland sono obbligatorie.")
            client = KauflandClient(
                client_key=client_key,
                secret_key=secret_key,
                playground=environment in {"test", "playground"},
            )
            bundle = sync_kaufland(
                client,
                storefront=selected_storefront,
                locale=selected_locale,
                category_attribute_ids=category_attribute_ids,
                progress=progress,
            )
        else:
            client = MiraklCatalogClient(
                api_url=clean_text(credentials.get("api_url")) or "https://marketplace.worten.pt/api",
                api_key=clean_text(credentials.get("api_key")),
                shop_id=clean_text(credentials.get("shop_id")),
            )
            bundle = sync_mirakl(
                client,
                locale=selected_locale,
                hierarchy=clean_text(hierarchy),
            )
        snapshot_id, created = save_taxonomy_bundle(
            seller_id=seller_id,
            account_id=account_id,
            environment=environment,
            bundle=bundle,
        )
        value_count = sum(len(item.values) for item in bundle.attributes)
        complete_taxonomy_sync(
            run_id,
            status="COMPLETED",
            snapshot_id=snapshot_id,
            category_count=len(bundle.categories),
            attribute_count=len(bundle.attributes),
            value_count=value_count,
            details={"created": created, "metadata": bundle.metadata},
        )
        snapshot = latest_taxonomy_snapshot(
            account_id,
            environment=environment,
            scope_key=scope_key,
        )
        return {
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "snapshot": snapshot,
            "created": created,
            "from_cache": False,
            "bundle": bundle,
        }
    except Exception as exc:
        complete_taxonomy_sync(run_id, status="FAILED", error=str(exc))
        raise


def ensure_account_taxonomy(
    *,
    seller_id: int,
    account_id: int,
    environment: str = "live",
    storefront: str = "",
    locale: str = "",
    hierarchy: str = "",
    force_refresh: bool = False,
    progress=None,
) -> dict[str, Any]:
    """Return the saved taxonomy and call the marketplace only when required.

    This is the default entry point for the UI and jobs.  Opening the page,
    classifying products or preparing a feed never downloads categories again.
    A remote synchronization occurs only on the first use or after an explicit
    ``force_refresh=True`` request.
    """
    # Read only account metadata first. A cached taxonomy must remain usable
    # even when credentials are temporarily unavailable or the encryption key
    # has not been loaded yet. Credentials are decrypted only if a real remote
    # synchronization is required.
    account_meta = row(
        "SELECT marketplace FROM marketplace_accounts WHERE id=? AND seller_id=?",
        (int(account_id), int(seller_id)),
    )
    if not account_meta:
        raise ValueError("Account marketplace non trovato per il Seller selezionato.")
    marketplace = clean_text(account_meta.get("marketplace")).lower()
    if marketplace not in {"kaufland", "worten"}:
        raise ValueError(
            f"Creazione Prodotti non supporta ancora il marketplace {marketplace or 'sconosciuto'}."
        )
    selected_storefront = clean_text(storefront).lower() if marketplace == "kaufland" else "pt"
    selected_locale = clean_text(locale) or ("pt_PT" if marketplace == "worten" else "")
    scope_key = taxonomy_scope_key(
        marketplace,
        storefront=selected_storefront,
        locale=selected_locale,
        hierarchy=hierarchy,
    )
    environment = clean_text(environment).lower() or "live"
    cached = latest_taxonomy_snapshot(
        account_id,
        environment=environment,
        scope_key=scope_key,
    )
    if cached and not force_refresh:
        return {
            "run_id": None,
            "snapshot_id": int(cached["id"]),
            "snapshot": cached,
            "created": False,
            "from_cache": True,
            "bundle": None,
        }
    return sync_account_taxonomy(
        seller_id=seller_id,
        account_id=account_id,
        environment=environment,
        storefront=selected_storefront,
        locale=selected_locale,
        hierarchy=hierarchy,
        category_attribute_ids=(),
        progress=progress,
    )


__all__ = [
    "ensure_account_taxonomy",
    "sync_account_taxonomy",
    "taxonomy_scope_key",
]
