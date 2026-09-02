from __future__ import annotations

from typing import Any

from services.catalog_intelligence.accounts import load_marketplace_account
from services.catalog_intelligence.marketplaces.kaufland import (
    discover_capabilities as discover_kaufland_capabilities,
)
from services.catalog_intelligence.marketplaces.mirakl import (
    MiraklCatalogClient,
    discover_capabilities as discover_mirakl_capabilities,
)
from services.catalog_intelligence.models import Capability
from services.catalog_intelligence.repository import save_capabilities
from services.catalog_intelligence.utils import clean_text
from services.kaufland import KauflandClient


def _kaufland_client(account: dict[str, Any], environment: str) -> KauflandClient:
    credentials = account.get("credentials") or {}
    client_key = clean_text(credentials.get("client_key"))
    secret_key = clean_text(credentials.get("secret_key"))
    if not client_key or not secret_key:
        raise ValueError("Client Key e Secret Key Kaufland sono obbligatorie.")
    return KauflandClient(
        client_key=client_key,
        secret_key=secret_key,
        playground=clean_text(environment).lower() in {"test", "playground"},
    )


def _mirakl_client(account: dict[str, Any]) -> MiraklCatalogClient:
    credentials = account.get("credentials") or {}
    return MiraklCatalogClient(
        api_url=clean_text(credentials.get("api_url")) or "https://marketplace.worten.pt/api",
        api_key=clean_text(credentials.get("api_key")),
        shop_id=clean_text(credentials.get("shop_id")),
    )


def discover_account_capabilities(
    *,
    seller_id: int,
    account_id: int,
    environment: str = "live",
) -> list[Capability]:
    account = load_marketplace_account(account_id, seller_id=seller_id)
    marketplace = account["marketplace"]
    environment = clean_text(environment).lower() or "live"
    if marketplace == "kaufland":
        capabilities = discover_kaufland_capabilities(_kaufland_client(account, environment))
    elif marketplace == "worten":
        capabilities = discover_mirakl_capabilities(_mirakl_client(account))
    else:  # protected by load_marketplace_account, retained for type safety
        raise ValueError(f"Marketplace non supportato: {marketplace}")
    save_capabilities(
        seller_id=seller_id,
        account_id=account_id,
        marketplace=marketplace,
        environment=environment,
        capabilities=capabilities,
    )
    return capabilities


__all__ = [
    "discover_account_capabilities",
]
