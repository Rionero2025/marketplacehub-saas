from __future__ import annotations

import json
from typing import Any

from services.catalog_intelligence.utils import clean_text
from services.db import row, rows
from services.security import decrypt_dict

SUPPORTED_MARKETPLACES = {"kaufland", "worten"}


def marketplace_accounts_for_seller(seller_id: int, *, active_only: bool = True) -> list[dict[str, Any]]:
    query = """
        SELECT * FROM marketplace_accounts
        WHERE seller_id=? AND LOWER(marketplace) IN ('kaufland','worten')
    """
    params: list[Any] = [int(seller_id)]
    if active_only:
        query += " AND active=1"
    query += " ORDER BY marketplace,account_name,id"
    return rows(query, params)


def load_marketplace_account(account_id: int, *, seller_id: int | None = None) -> dict[str, Any]:
    query = "SELECT * FROM marketplace_accounts WHERE id=?"
    params: list[Any] = [int(account_id)]
    if seller_id is not None:
        query += " AND seller_id=?"
        params.append(int(seller_id))
    account = row(query, params)
    if not account:
        raise ValueError("Account marketplace non trovato per il Seller selezionato.")
    marketplace = clean_text(account.get("marketplace")).lower()
    if marketplace not in SUPPORTED_MARKETPLACES:
        raise ValueError(f"Creazione Prodotti non supporta ancora il marketplace {marketplace or 'sconosciuto'}.")
    try:
        credentials = decrypt_dict(clean_text(account.get("credentials_encrypted")))
    except Exception as exc:
        raise RuntimeError(f"Impossibile decifrare le credenziali dell'account: {exc}") from exc
    try:
        settings = json.loads(clean_text(account.get("settings_json")) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        settings = {}
    result = dict(account)
    result["marketplace"] = marketplace
    result["credentials"] = credentials
    result["settings"] = settings if isinstance(settings, dict) else {}
    return result


__all__ = [
    "SUPPORTED_MARKETPLACES",
    "load_marketplace_account",
    "marketplace_accounts_for_seller",
]
