from __future__ import annotations

import io
import ipaddress
import json
import math
import re
import socket
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import pandas as pd
import requests

from services.cecotec_orders import (
    clean_identifier,
    clean_text,
    marketplace_status_label,
    normalize_country_code,
    normalize_supplier,
    parse_composite_sku,
)
from services.db import DATA_DIR, connect, execute, json_text, now_iso, rows
from services.fx import get_ecb_rates
from services.kaufland import KauflandClient
from services.kaufland_orders import (
    ORDER_UNIT_STATUSES,
    SHIPPED_ORDER_UNIT_STATUSES,
    currency_to_eur,
    fetch_order_units,
    find_order_unit,
    merge_order_unit,
    normalize_order_unit,
    order_amounts_to_eur,
    response_item,
)
from services.lists import country_cost, download_url, normalize, read_list
from services.profit_sharing import normalized_percentages, split_profit
from services.worten import commission_rate_from_order_line


EXPORT_COLUMNS = [
    "Data",
    "Market",
    "Num. Ordine Market",
    "Fornitore",
    "N Ordine Fornitore",
    "Prodotto",
    "SKU/EAN",
    "Vendita",
    "Acquisto",
    "C. Market",
    "Costo Extra",
    "a Pagare",
    "Margine Lordo",
    "Ricavo Netto",
    "% Ricavo",
    "Tracciabilità e Corriere",
    "Nome Cliente",
    "SCONTRINO",
    "PAGATO",
    "Stato Ordine",
    "Quantità",
    "Rimborso",
    "Note contabili",
    "Nostra quota %",
    "Nostro guadagno",
    "Quota partner %",
    "Guadagno partner",
]

CANCELLED_STATUS_TOKENS = {
    "cancelled", "canceled", "refused", "cancel", "annullato", "cancellato",
    "annullare", "no_stock", "out_of_stock",
}
RETURNED_STATUS_TOKENS = {
    "returned", "returned_paid", "refunded", "refund", "fully_refunded",
    "partially_refunded", "waiting_refund", "waiting_refund_payment",
}

# Qualunque riga che contiene uno di questi stati/avvisi deve restare a zero,
# anche quando API, listini o un Excel di confronto contengono importi positivi.
ZERO_ECONOMIC_DB_FIELDS = {
    "sale_original_eur", "refund_eur", "sale_eur", "purchase_cost_eur",
    "commission_eur", "extra_cost_eur", "payout_eur",
}


def _status_search_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, Mapping):
            value = " ".join(clean_text(item) for item in value.values())
        elif isinstance(value, (list, tuple, set)):
            value = " ".join(clean_text(item) for item in value)
        text = clean_text(value)
        if text:
            parts.append(text)
    normalized = unicodedata.normalize("NFKD", " ".join(parts).lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _zero_economics_reason(*values: Any) -> str:
    """Return the reason that forces every economic value to zero."""
    text = _status_search_text(*values)
    if not text:
        return ""
    padded = f" {text} "
    if " no stock " in padded or " out of stock " in padded or " nostock " in padded:
        return "no stock"
    if re.search(r"\brimborsat", text) or re.search(r"\b(?:fully |partially )?refunded\b", text):
        return "rimborsato"
    if re.search(r"\bannullare\b", text):
        return "annullare"
    if re.search(r"\bannullat[oaie]?\b", text):
        return "annullato"
    if re.search(r"\bcancellat[oaie]?\b", text) or re.search(r"\bcancell?ed\b", text):
        return "cancellato"
    if re.search(r"\bcancel(?:led|ed)?\b", text) or re.search(r"\brefus(?:ed|al)?\b", text):
        return "cancellato"
    return ""


def _must_zero_economics(*values: Any) -> bool:
    return bool(_zero_economics_reason(*values))


def _zero_economic_note(note: Any, reason: str) -> str:
    current = clean_text(note)
    message = f"{reason}: tutti i valori economici azzerati"
    if current and message.lower() not in current.lower():
        return clean_text(f"{current}; {message}")
    return current or message


def _zero_economic_record(item: Mapping[str, Any], reason: str | None = None) -> dict[str, Any]:
    output = dict(item)
    reason = reason or _zero_economics_reason(
        output.get("raw_status"), output.get("status_label"), output.get("note"),
        output.get("supplier_order_number"),
    )
    if not reason:
        return output
    for field in ZERO_ECONOMIC_DB_FIELDS:
        output[field] = 0.0
    output["payment_estimated"] = ""
    output["cost_source"] = f"Non dovuto · {reason}"
    output["note"] = _zero_economic_note(output.get("note"), reason)
    return output


@dataclass
class CatalogSource:
    supplier_name: str
    supplier_key: str
    list_name: str
    path: str
    updated_at: str
    frame: pd.DataFrame
    price_list_id: int = 0
    source_kind: str = "listino"
    priority: int = 0
    source_url: str = ""


def ensure_schema() -> None:
    """Create accounting storage without modifying older installations manually."""
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounting_order_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                row_key TEXT NOT NULL,
                order_id TEXT NOT NULL DEFAULT '',
                order_line_id TEXT NOT NULL DEFAULT '',
                order_created TEXT NOT NULL DEFAULT '',
                country_code TEXT NOT NULL DEFAULT '',
                market_label TEXT NOT NULL DEFAULT '',
                raw_status TEXT NOT NULL DEFAULT '',
                status_label TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                composite_sku TEXT NOT NULL DEFAULT '',
                product_title TEXT NOT NULL DEFAULT '',
                ean TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 1,
                sale_original_eur REAL,
                refund_eur REAL NOT NULL DEFAULT 0,
                sale_eur REAL,
                purchase_cost_eur REAL,
                commission_eur REAL,
                payout_eur REAL,
                cost_source TEXT NOT NULL DEFAULT '',
                financial_source TEXT NOT NULL DEFAULT '',
                tracking TEXT NOT NULL DEFAULT '',
                customer_name TEXT NOT NULL DEFAULT '',
                payment_estimated TEXT NOT NULL DEFAULT '',
                supplier_order_number TEXT NOT NULL DEFAULT '',
                extra_cost_eur REAL NOT NULL DEFAULT 0,
                receipt TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL,
                UNIQUE(marketplace_account_id,marketplace,row_key)
            );
            CREATE INDEX IF NOT EXISTS idx_accounting_orders_scope
            ON accounting_order_lines(
                seller_id,marketplace_account_id,marketplace,order_created
            );
            CREATE INDEX IF NOT EXISTS idx_accounting_orders_filters
            ON accounting_order_lines(
                marketplace_account_id,marketplace,raw_status,supplier,country_code
            );
            CREATE TABLE IF NOT EXISTS accounting_catalog_settings (
                seller_id INTEGER PRIMARY KEY REFERENCES sellers(id) ON DELETE CASCADE,
                configured INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounting_catalog_preferences (
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(seller_id,price_list_id)
            );
            CREATE INDEX IF NOT EXISTS idx_accounting_catalog_preferences_seller
            ON accounting_catalog_preferences(seller_id,enabled,price_list_id);
            CREATE TABLE IF NOT EXISTS accounting_exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                date_from TEXT NOT NULL DEFAULT '',
                date_to TEXT NOT NULL DEFAULT '',
                row_count INTEGER NOT NULL DEFAULT 0,
                total_sale_eur REAL NOT NULL DEFAULT 0,
                total_purchase_eur REAL NOT NULL DEFAULT 0,
                total_commission_eur REAL NOT NULL DEFAULT 0,
                total_payout_eur REAL NOT NULL DEFAULT 0,
                total_net_revenue_eur REAL NOT NULL DEFAULT 0,
                total_our_profit_eur REAL NOT NULL DEFAULT 0,
                total_partner_profit_eur REAL NOT NULL DEFAULT 0,
                our_profit_pct REAL NOT NULL DEFAULT 0,
                partner_profit_pct REAL NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounting_export_rows (
                export_id INTEGER NOT NULL REFERENCES accounting_exports(id) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                PRIMARY KEY(export_id,row_key)
            );
            CREATE TABLE IF NOT EXISTS accounting_excel_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                file_name TEXT NOT NULL,
                sheet_name TEXT NOT NULL DEFAULT '',
                source_rows INTEGER NOT NULL DEFAULT 0,
                matched_rows INTEGER NOT NULL DEFAULT 0,
                updated_rows INTEGER NOT NULL DEFAULT 0,
                filled_fields INTEGER NOT NULL DEFAULT 0,
                conflicts INTEGER NOT NULL DEFAULT 0,
                unmatched_rows INTEGER NOT NULL DEFAULT 0,
                ambiguous_rows INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_accounting_excel_imports_scope
            ON accounting_excel_imports(
                seller_id,marketplace_account_id,marketplace,created_at
            );
            CREATE TABLE IF NOT EXISTS accounting_manual_overrides (
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                row_key TEXT NOT NULL,
                field_name TEXT NOT NULL,
                value_json TEXT NOT NULL DEFAULT 'null',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(marketplace_account_id,marketplace,row_key,field_name)
            );
            CREATE INDEX IF NOT EXISTS idx_accounting_manual_overrides_scope
            ON accounting_manual_overrides(
                marketplace_account_id,marketplace,row_key,updated_at
            );
            CREATE TABLE IF NOT EXISTS accounting_sync_state (
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                environment TEXT NOT NULL DEFAULT 'production',
                last_started_at TEXT NOT NULL DEFAULT '',
                last_completed_at TEXT NOT NULL DEFAULT '',
                last_requested_from TEXT NOT NULL DEFAULT '',
                last_requested_to TEXT NOT NULL DEFAULT '',
                last_effective_from TEXT NOT NULL DEFAULT '',
                last_effective_to TEXT NOT NULL DEFAULT '',
                last_order_created TEXT NOT NULL DEFAULT '',
                last_order_id TEXT NOT NULL DEFAULT '',
                last_status TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                last_fetched_rows INTEGER NOT NULL DEFAULT 0,
                last_new_rows INTEGER NOT NULL DEFAULT 0,
                last_updated_rows INTEGER NOT NULL DEFAULT 0,
                total_rows INTEGER NOT NULL DEFAULT 0,
                total_orders INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(
                    marketplace_account_id,marketplace,environment
                )
            );
            CREATE INDEX IF NOT EXISTS idx_accounting_sync_state_scope
            ON accounting_sync_state(
                seller_id,marketplace_account_id,marketplace,last_completed_at
            );
            """
        )
        existing_sync_columns = {
            str(item["name"])
            for item in con.execute("PRAGMA table_info(accounting_sync_state)").fetchall()
        }
        sync_migrations = {
            "last_attempted_at": "TEXT NOT NULL DEFAULT ''",
            "last_fetched_orders": "INTEGER NOT NULL DEFAULT 0",
            "last_new_orders": "INTEGER NOT NULL DEFAULT 0",
            "last_updated_orders": "INTEGER NOT NULL DEFAULT 0",
            "last_unchanged_orders": "INTEGER NOT NULL DEFAULT 0",
            "last_existing_orders": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in sync_migrations.items():
            if column not in existing_sync_columns:
                con.execute(
                    f"ALTER TABLE accounting_sync_state ADD COLUMN {column} {declaration}"
                )

        existing_export_columns = {
            str(item["name"])
            for item in con.execute("PRAGMA table_info(accounting_exports)").fetchall()
        }
        export_migrations = {
            "total_our_profit_eur": "REAL NOT NULL DEFAULT 0",
            "total_partner_profit_eur": "REAL NOT NULL DEFAULT 0",
            "our_profit_pct": "REAL NOT NULL DEFAULT 0",
            "partner_profit_pct": "REAL NOT NULL DEFAULT 100",
        }
        for column, declaration in export_migrations.items():
            if column not in existing_export_columns:
                con.execute(
                    f"ALTER TABLE accounting_exports ADD COLUMN {column} {declaration}"
                )


def _number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, Mapping):
        for key in (
            "amount", "value", "price", "total", "total_amount", "unit_price",
            "shipping_price", "commission", "fee",
        ):
            if key in value:
                return _number(value.get(key), default)
        return default
    if value in (None, ""):
        return default
    try:
        number = float(str(value).strip().replace("€", "").replace(" ", "").replace(",", "."))
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _normalized_mapping(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key).lower().replace("-", "_").replace(" ", "_"): value
        for key, value in item.items()
    }


def _value(item: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    if not isinstance(item, Mapping):
        return default
    normalized = _normalized_mapping(item)
    for key in keys:
        candidate = normalized.get(str(key).lower().replace("-", "_").replace(" ", "_"))
        if candidate is not None:
            return candidate
    return default


def _date_time(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    candidates = [text, text.replace("Z", "+00:00")]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _date_in_range(value: Any, date_from: date, date_to: date) -> bool:
    parsed = _date_time(value)
    return True if parsed is None else date_from <= parsed.date() <= date_to


def payment_estimated(value: Any, days: int = 21) -> str:
    parsed = _date_time(value)
    if parsed is None:
        return ""
    return (parsed.date() + timedelta(days=int(days))).isoformat()


def _is_cancelled(raw_status: Any) -> bool:
    token = re.sub(r"[^a-z0-9]+", "_", clean_text(raw_status).lower()).strip("_")
    reason = _zero_economics_reason(raw_status)
    return (
        token in CANCELLED_STATUS_TOKENS
        or "cancel" in token
        or "refus" in token
        or reason in {"no stock", "annullare", "annullato", "cancellato"}
    )


def _is_returned_or_refunded(raw_status: Any) -> bool:
    token = re.sub(r"[^a-z0-9]+", "_", clean_text(raw_status).lower()).strip("_")
    return token in RETURNED_STATUS_TOKENS or "refund" in token or "return" in token


def _extract_address(source: Any) -> dict[str, str]:
    data = source if isinstance(source, Mapping) else {}
    first = clean_text(_value(data, "first_name", "firstname", "firstName"))
    last = clean_text(_value(data, "last_name", "lastname", "lastName"))
    name = clean_text(_value(data, "full_name", "name", "customer_name"))
    if not name:
        name = clean_text(" ".join(value for value in (first, last) if value))
    company = clean_text(_value(data, "company", "company_name"))
    if company and not name:
        name = company
    return {
        "customer_name": name,
        "country_code": normalize_country_code(
            _value(data, "country_code", "country", "country_iso_code", "country_iso")
        ),
    }


def _first_mapping(source: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> Mapping[str, Any]:
    for path in paths:
        current: Any = source
        valid = True
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                valid = False
                break
            current = current[key]
        if valid and isinstance(current, Mapping):
            return current
    return {}


def _customer_from_order(order: Mapping[str, Any]) -> dict[str, str]:
    address = _first_mapping(
        order,
        (
            ("shipping_address",),
            ("customer", "shipping_address"),
            ("order_customer", "shipping_address"),
            ("billing_address",),
            ("customer", "billing_address"),
            ("buyer",),
        ),
    )
    result = _extract_address(address)
    customer = order.get("customer") if isinstance(order.get("customer"), Mapping) else {}
    buyer = order.get("buyer") if isinstance(order.get("buyer"), Mapping) else {}
    if not result["customer_name"]:
        result["customer_name"] = clean_text(
            _value(customer, "name", "full_name", "firstname", "first_name")
            or _value(buyer, "name", "full_name", "firstname", "first_name")
        )
    if not result["country_code"]:
        result["country_code"] = normalize_country_code(
            _value(order, "country_code", "shipping_country_code", "country")
        )
    return result


def _tracking_text(*sources: Any) -> str:
    carriers: list[str] = []
    tracking_numbers: list[str] = []
    carrier_keys = {
        "carrier", "carrier_code", "carrier_name", "shipping_carrier_code",
        "shipping_carrier", "logistic_partner", "agency",
    }
    tracking_keys = {
        "tracking", "tracking_code", "tracking_number", "tracking_numbers",
        "shipping_tracking", "shipment_tracking", "parcel_number",
    }

    def visit(value: Any, parent_key: str = "") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in carrier_keys:
                    text = clean_text(child)
                    if text and text not in carriers:
                        carriers.append(text)
                elif normalized in tracking_keys:
                    if isinstance(child, (list, tuple, set)):
                        for item in child:
                            text = clean_text(item)
                            if text and text not in tracking_numbers:
                                tracking_numbers.append(text)
                    else:
                        text = clean_text(child)
                        if text and text not in tracking_numbers:
                            tracking_numbers.append(text)
                elif isinstance(child, (Mapping, list, tuple)):
                    visit(child, normalized)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, parent_key)

    for source in sources:
        visit(source)
    parts = []
    if carriers:
        parts.append(" / ".join(carriers))
    if tracking_numbers:
        parts.append(" / ".join(tracking_numbers))
    return " · ".join(parts)


def _ean_candidates_from_value(value: Any) -> tuple[str, ...]:
    """Return every GTIN-shaped identifier contained in *value*.

    Price lists often store barcodes as Excel numbers, scientific notation or in
    supplier-specific columns such as ``code_producer``.  Only 8, 12, 13 or
    14-digit identifiers are accepted, so an alphanumeric supplier SKU can never
    become an accounting EAN match.
    """
    if value is None:
        return ()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ()

    raw = clean_text(value)
    if not raw or raw.lower() in {"nan", "none", "null", "nat"}:
        return ()

    candidates: list[str] = []

    def add(token: str) -> None:
        token = token.strip()
        if len(token) in {8, 12, 13, 14} and token.isdigit() and token not in candidates:
            candidates.append(token)

    # Excel can expose a 13-digit EAN as 6.975116295678E+12. Decimal restores
    # the integer without losing digits (float is still exact in this range).
    numeric_text = raw.replace(" ", "").replace(",", ".")
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+", numeric_text):
        try:
            decimal_value = Decimal(numeric_text)
            if decimal_value == decimal_value.to_integral_value():
                add(format(decimal_value.quantize(Decimal("1")), "f"))
        except (InvalidOperation, ValueError):
            pass

    # Ordinary Excel integers can arrive with one or more trailing decimals.
    integer_like = re.fullmatch(r"([0-9]{8}|[0-9]{12,14})(?:\.0+)?", numeric_text)
    if integer_like:
        add(integer_like.group(1))

    for match in re.finditer(r"(?<![A-Za-z0-9])(\d{8}|\d{12,14})(?![A-Za-z0-9])", raw):
        add(match.group(1))

    # Also accept barcodes formatted with spaces or hyphens, but never join
    # arbitrary letters/numbers from a supplier SKU.
    for token in re.split(r"[;,|/]", raw):
        token = token.strip()
        if token and not re.search(r"[A-Za-z]", token):
            compact = re.sub(r"[\s._-]+", "", token)
            add(compact)

    return tuple(candidates)


def _ean_from_values(*values: Any) -> str:
    for value in values:
        candidates = _ean_candidates_from_value(value)
        if candidates:
            return candidates[0]
    return ""


def _catalog_barcode_columns(frame: pd.DataFrame) -> list[str]:
    """Columns that can legitimately contain an EAN/GTIN in supplier files."""
    result: list[str] = []
    exact = {
        "ean", "ean13", "ean_13", "gtin", "gtin13", "gtin_13",
        "barcode", "upc", "sku", "code_producer", "producer_code",
        "code_external", "external_code", "codice_ean",
    }
    for column in frame.columns:
        normalized = re.sub(r"[^a-z0-9]+", "_", clean_text(column).lower()).strip("_")
        if normalized in exact or any(part in normalized for part in ("ean", "gtin", "barcode")):
            result.append(column)
    return result


def _row_ean_keys(row: Mapping[str, Any], columns: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    for column in columns:
        for candidate in _ean_candidates_from_value(row.get(column)):
            if candidate not in values:
                values.append(candidate)
    return tuple(values)


def _market_label(marketplace: str, country_code: str, storefront: str = "") -> str:
    marketplace = clean_text(marketplace).lower()
    country = normalize_country_code(country_code or storefront)
    if marketplace == "kaufland":
        return f"Kaufland {country or clean_text(storefront).upper()}".strip()
    if marketplace == "worten":
        return "Worten" if country in {"", "PT"} else f"Worten {country}"
    return marketplace.title()


def _make_row_key(marketplace: str, account_id: int, order_id: Any, line_id: Any, sku: Any) -> str:
    return "|".join(
        clean_text(value)
        for value in (marketplace.lower(), account_id, order_id, line_id, sku)
    )


def _catalog_paths(seller_id: int) -> list[dict[str, Any]]:
    """Return every active price list the selected seller can use.

    Accounting must not be limited to lists owned by the seller. Global lists,
    explicitly shared lists and the seller's saved views are valid cost sources.
    Current list files are tried before historical saved views; within each group
    the most recently updated source is preferred.
    """
    return rows(
        """
        SELECT pl.id AS price_list_id,pl.owner_seller_id AS source_seller_id,
               pl.local_path AS path,pl.source_url AS source_url,
               COALESCE(pl.last_download_at,pl.created_at) AS updated_at,
               pl.name AS list_name,s.name AS supplier_name,
               0 AS priority,'listino attivo' AS source_kind
        FROM price_lists pl
        JOIN suppliers s ON s.id=pl.supplier_id
        LEFT JOIN price_list_access a
               ON a.price_list_id=pl.id AND a.seller_id=?
        WHERE pl.active=1 AND pl.local_path<>'' AND (
            pl.owner_seller_id=? OR pl.visibility='global' OR
            (pl.visibility='shared' AND a.seller_id IS NOT NULL)
        )
        UNION ALL
        SELECT pl.id AS price_list_id,sv.seller_id AS source_seller_id,
               sv.snapshot_path AS path,pl.source_url AS source_url,sv.updated_at AS updated_at,
               sv.name AS list_name,s.name AS supplier_name,
               1 AS priority,'vista salvata' AS source_kind
        FROM saved_views sv
        JOIN price_lists pl ON pl.id=sv.price_list_id
        JOIN suppliers s ON s.id=pl.supplier_id
        LEFT JOIN price_list_access a
               ON a.price_list_id=pl.id AND a.seller_id=?
        WHERE sv.seller_id=? AND pl.active=1 AND sv.snapshot_path<>'' AND (
            pl.owner_seller_id=? OR pl.visibility='global' OR
            (pl.visibility='shared' AND a.seller_id IS NOT NULL)
        )
        ORDER BY priority ASC,updated_at DESC
        """,
        (seller_id, seller_id, seller_id, seller_id, seller_id),
    )


def accounting_catalog_options(seller_id: int) -> list[dict[str, Any]]:
    """Return unique active price lists that may be used by Accounting.

    Saved views inherit the setting of their parent price list, therefore the UI
    exposes one selectable entry per ``price_list_id`` rather than one entry per
    snapshot.
    """
    options: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in _catalog_paths(seller_id):
        price_list_id = int(item.get("price_list_id") or 0)
        if not price_list_id or price_list_id in seen:
            continue
        seen.add(price_list_id)
        options.append({
            "price_list_id": price_list_id,
            "supplier_name": clean_text(item.get("supplier_name")),
            "list_name": clean_text(item.get("list_name")),
            "source_url": clean_text(item.get("source_url")),
            "updated_at": clean_text(item.get("updated_at")),
            "source_kind": clean_text(item.get("source_kind")) or "listino attivo",
            "path": clean_text(item.get("path")),
        })
    return options


def accounting_catalog_selection(seller_id: int) -> dict[str, Any]:
    """Return the persistent Accounting whitelist for the selected Seller.

    Existing installations have no preference rows. In that migration state all
    currently accessible lists remain enabled, preserving historical behaviour.
    Once the user saves a selection, it becomes an explicit whitelist: new or
    unselected price lists are excluded until the user enables them.
    """
    options = accounting_catalog_options(seller_id)
    option_ids = [int(item["price_list_id"]) for item in options]
    try:
        settings = rows(
            "SELECT configured FROM accounting_catalog_settings WHERE seller_id=?",
            (seller_id,),
        )
        preferences = rows(
            "SELECT price_list_id,enabled FROM accounting_catalog_preferences WHERE seller_id=?",
            (seller_id,),
        )
    except Exception:
        settings = []
        preferences = []
    configured = bool(settings and int(settings[0].get("configured") or 0)) or bool(preferences)
    preference_map = {
        int(item.get("price_list_id") or 0): bool(int(item.get("enabled") or 0))
        for item in preferences
    }
    enabled_ids = (
        [price_list_id for price_list_id in option_ids if preference_map.get(price_list_id, False)]
        if configured else list(option_ids)
    )
    return {
        "configured": configured,
        "enabled_ids": enabled_ids,
        "options": options,
    }


def save_accounting_catalog_selection(
    seller_id: int, enabled_price_list_ids: Iterable[int],
) -> dict[str, Any]:
    """Persist the exact list of price lists allowed to determine purchase costs."""
    ensure_schema()
    options = accounting_catalog_options(seller_id)
    option_ids = {int(item["price_list_id"]) for item in options}
    enabled_ids = {
        int(value) for value in enabled_price_list_ids
        if int(value) in option_ids
    }
    stamp = now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO accounting_catalog_settings(seller_id,configured,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(seller_id) DO UPDATE SET
                configured=excluded.configured,updated_at=excluded.updated_at
            """,
            (seller_id, 1, stamp),
        )
        for price_list_id in sorted(option_ids):
            con.execute(
                """
                INSERT INTO accounting_catalog_preferences(
                    seller_id,price_list_id,enabled,updated_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(seller_id,price_list_id) DO UPDATE SET
                    enabled=excluded.enabled,updated_at=excluded.updated_at
                """,
                (seller_id, price_list_id, 1 if price_list_id in enabled_ids else 0, stamp),
            )
    return {
        "configured": True,
        "enabled_ids": sorted(enabled_ids),
        "available": len(option_ids),
        "enabled": len(enabled_ids),
    }


def _accounting_enabled_price_list_ids(seller_id: int) -> set[int] | None:
    selection = accounting_catalog_selection(seller_id)
    if not selection["configured"]:
        return None
    return {int(value) for value in selection["enabled_ids"]}


def _resolve_catalog_path(item: Mapping[str, Any]) -> Path | None:
    """Resolve stored paths after the application folder has been upgraded.

    Database paths can still point to the previous Marketplace Hub version. The
    data folder is normally copied to the new version, therefore the suffix that
    follows ``data`` is reconstructed under the current DATA_DIR.
    """
    raw = clean_text(item.get("path"))
    if not raw:
        return None
    original = Path(raw)
    candidates: list[Path] = [original]
    if not original.is_absolute():
        candidates.extend((DATA_DIR.parent / original, DATA_DIR / original))

    parts = list(original.parts)
    lowered = [part.lower() for part in parts]
    if "data" in lowered:
        position = len(lowered) - 1 - lowered[::-1].index("data")
        suffix = parts[position + 1:]
        if suffix:
            candidates.append(DATA_DIR.joinpath(*suffix))

    price_list_id = int(item.get("price_list_id") or 0)
    source_seller_id = int(item.get("source_seller_id") or 0)
    basename = original.name
    source_kind = clean_text(item.get("source_kind")).lower()
    fallback_folders: list[Path] = []
    if price_list_id:
        fallback_folders.append(DATA_DIR / "price_lists" / str(price_list_id))
    if "vista" in source_kind and source_seller_id:
        fallback_folders.insert(0, DATA_DIR / "saved_views" / str(source_seller_id))
    for folder in fallback_folders:
        if basename:
            candidates.append(folder / basename)
        if folder.exists():
            files = sorted(
                (entry for entry in folder.iterdir() if entry.is_file()),
                key=lambda entry: entry.stat().st_mtime,
                reverse=True,
            )
            candidates.extend(files)

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def load_supplier_catalogs(seller_id: int) -> list[CatalogSource]:
    """Load only catalogues enabled for Accounting and normalize their EAN keys."""
    sources: list[CatalogSource] = []
    seen: set[str] = set()
    enabled_price_list_ids = _accounting_enabled_price_list_ids(seller_id)
    for item in _catalog_paths(seller_id):
        price_list_id = int(item.get("price_list_id") or 0)
        if enabled_price_list_ids is not None and price_list_id not in enabled_price_list_ids:
            continue
        resolved = _resolve_catalog_path(item)
        supplier_name = clean_text(item.get("supplier_name"))
        source_url = clean_text(item.get("source_url"))
        innpro_mode = _innpro_feed_mode_values(
            list_name=item.get("list_name"), source_url=source_url, path=item.get("path")
        )

        # In cloud deployments the database can contain a perfectly valid price
        # list while ``local_path`` still points to the old Windows PC. For Innpro
        # wholesale feeds, rebuild the ephemeral local cache directly from the
        # saved feed URL. This makes Accounting work after SQLite -> PostgreSQL
        # migration even without a persistent disk. Explicit FULL feeds are never
        # downloaded for automatic purchase-cost calculation.
        if resolved is None and _is_innpro_supplier(supplier_name) and source_url and innpro_mode != "full":
            try:
                resolved = download_url(price_list_id, source_url)
            except Exception:
                resolved = None
        if resolved is None:
            continue
        path_key = str(resolved.resolve())
        if path_key in seen:
            continue
        seen.add(path_key)
        try:
            frame = normalize(read_list(resolved))
        except Exception:
            continue
        if frame.empty:
            continue
        frame = frame.copy()
        barcode_columns = _catalog_barcode_columns(frame)
        if not barcode_columns:
            continue
        frame["_ean_keys"] = frame.apply(
            lambda row: _row_ean_keys(row, barcode_columns), axis=1,
        )
        frame["_ean_key"] = frame["_ean_keys"].map(
            lambda values: values[0] if values else "",
        )
        # Accounting can identify a product in two authoritative ways:
        # 1) exact EAN/GTIN; 2) exact supplier SKU extracted from our composite
        #    marketplace SKU (supplier_productcode_cost_minprice).
        # Do not discard catalogue rows that have a supplier SKU but no EAN.
        frame["_sku_key"] = frame["sku"].map(
            lambda value: clean_identifier(value).casefold(),
        )
        frame = frame[
            frame["_ean_keys"].map(bool) | frame["_sku_key"].astype(bool)
        ].copy()
        if frame.empty:
            continue
        sources.append(
            CatalogSource(
                supplier_name=clean_text(item.get("supplier_name")),
                supplier_key=normalize_supplier(item.get("supplier_name")),
                list_name=clean_text(item.get("list_name")),
                path=str(resolved),
                updated_at=clean_text(item.get("updated_at")),
                frame=frame,
                price_list_id=int(item.get("price_list_id") or 0),
                source_kind=clean_text(item.get("source_kind")) or "listino",
                priority=int(item.get("priority") or 0),
                source_url=clean_text(item.get("source_url")),
            )
        )
    return sources


def _supplier_compatible(source_key: str, requested_key: str) -> bool:
    if not requested_key:
        return True
    if source_key == requested_key:
        return True
    return requested_key in source_key or source_key in requested_key


def _is_innpro_supplier(value: Any) -> bool:
    return "innpro" in normalize_supplier(value)


def _innpro_feed_mode_values(*, list_name: Any = "", source_url: Any = "", path: Any = "") -> str:
    """Return ``light``, ``full`` or ``unknown`` for an Innpro catalogue.

    The accounting role must not depend on the human name of the price list. In
    production many valid wholesale lists are named only ``INNPRO 2408`` or
    ``feed INNPRO 0108``. The authoritative signal is the Innpro feed URL
    (``type=light`` / ``type=full``); filenames are only a fallback.
    """
    url = clean_text(source_url)
    try:
        query = parse_qs(urlparse(url).query)
    except Exception:
        query = {}
    for key in ("type", "feed", "mode", "variant", "catalog"):
        values = [clean_text(value).lower() for value in query.get(key, [])]
        if "light" in values:
            return "light"
        if "full" in values:
            return "full"

    descriptors = " ".join(
        clean_text(value).lower()
        for value in (list_name, path)
        if clean_text(value)
    )
    if re.search(r"(?:^|[^a-z0-9])full(?:[^a-z0-9]|$)", descriptors):
        return "full"
    if re.search(r"(?:^|[^a-z0-9])light(?:[^a-z0-9]|$)", descriptors):
        return "light"
    return "unknown"


def _is_innpro_full_source(source: CatalogSource) -> bool:
    if not _is_innpro_supplier(source.supplier_key or source.supplier_name):
        return False
    return _innpro_feed_mode_values(
        list_name=source.list_name, source_url=source.source_url, path=source.path
    ) == "full"


def _is_innpro_light_source(source: CatalogSource) -> bool:
    """Identify a valid Innpro accounting/wholesale source.

    ``type=light`` is preferred and explicit ``type=full`` is always rejected.
    For legacy Innpro feeds without a mode marker, a selected source is accepted
    when it contains normalized purchase costs. This removes the old requirement
    that the word ``Light`` had to appear in the list name.
    """
    if not _is_innpro_supplier(source.supplier_key or source.supplier_name):
        return False
    mode = _innpro_feed_mode_values(
        list_name=source.list_name, source_url=source.source_url, path=source.path
    )
    if mode == "full":
        return False
    if mode == "light":
        return True
    if "cost" not in source.frame.columns:
        return False
    costs = pd.to_numeric(source.frame.get("cost"), errors="coerce").fillna(0)
    return bool(costs.gt(0).any())


def _catalog_search_order(
    catalogs: Sequence[CatalogSource], requested_supplier: str,
) -> list[CatalogSource]:
    """Choose accounting cost sources, with Innpro strictly bound to LIGHT prices.

    Innpro FULL is useful for product content but its price must never be used by
    Accounting.  For Innpro orders only compatible LIGHT catalogues are returned.
    Other suppliers keep the historical preferred-supplier + global-fallback rule.
    """
    preferred = [
        source for source in catalogs
        if _supplier_compatible(source.supplier_key, requested_supplier)
    ]
    if _is_innpro_supplier(requested_supplier):
        return [source for source in preferred if _is_innpro_light_source(source)]
    fallback = [
        source for source in catalogs
        if not _supplier_compatible(source.supplier_key, requested_supplier)
    ]
    return preferred + fallback


def resolve_purchase_cost(
    catalogs: Sequence[CatalogSource],
    *,
    supplier: Any,
    product_code: Any,
    ean: Any,
    country_code: Any,
    quantity: int = 1,
    composite_purchase_cost: Any = None,
) -> dict[str, Any]:
    """Resolve purchase cost by exact EAN, then by composite-SKU product code.

    Marketplace SKUs use ``supplier_productcode_cost_minprice``.  ``product_code``
    is the second component extracted by :func:`parse_composite_sku`.  Accounting
    first tries the order EAN/GTIN and, when that doesn't produce a positive cost,
    matches ``product_code`` exactly against the supplier catalogue ``sku``.
    As a final safety fallback, when neither identifier can be resolved from an
    accessible catalogue, the purchase cost already encoded in the composite
    marketplace SKU is used. This preserves the original authoritative cost
    captured at publication time even when a cloud catalogue is temporarily
    unavailable. No fuzzy product-name match is performed.
    """
    supplier_key = normalize_supplier(supplier)
    ean_clean = _ean_from_values(ean, product_code)
    sku_clean = clean_identifier(product_code)
    sku_key = sku_clean.casefold()
    quantity = max(1, int(quantity or 1))
    embedded_cost = _number(composite_purchase_cost)
    if embedded_cost is not None and embedded_cost <= 0:
        embedded_cost = None

    if not ean_clean and not sku_key:
        if embedded_cost is not None:
            return {
                "unit_cost": round(float(embedded_cost), 4),
                "total_cost": round(float(embedded_cost) * quantity, 2),
                "source": "SKU composito · costo incorporato al momento della pubblicazione",
                "matched_ean": "",
                "matched_sku": "",
                "matched_supplier": clean_text(supplier),
                "matched_list": "SKU composito",
            }
        return {
            "unit_cost": None,
            "total_cost": None,
            "source": "Costo non calcolabile: EAN, codice prodotto e costo SKU composito mancanti nell'ordine",
            "matched_ean": "",
            "matched_sku": "",
            "matched_supplier": "",
            "matched_list": "",
        }

    search_sources = _catalog_search_order(catalogs, supplier_key)
    if _is_innpro_supplier(supplier_key) and not search_sources:
        if embedded_cost is not None:
            return {
                "unit_cost": round(float(embedded_cost), 4),
                "total_cost": round(float(embedded_cost) * quantity, 2),
                "source": "SKU composito · costo incorporato al momento della pubblicazione · feed Innpro non disponibile",
                "matched_ean": ean_clean,
                "matched_sku": sku_clean,
                "matched_supplier": clean_text(supplier),
                "matched_list": "SKU composito",
            }
        return {
            "unit_cost": None,
            "total_cost": None,
            "source": (
                "Costo non calcolabile: per Innpro non è disponibile alcun feed ingrosso "
                "selezionato/utilizzabile. Il nome del listino non deve contenere 'Light': "
                "il programma riconosce i feed type=light e può riscaricarli in cloud"
            ),
            "matched_ean": "",
            "matched_sku": "",
            "matched_supplier": "",
            "matched_list": "",
        }

    for source in search_sources:
        frame = source.frame
        strategies: list[tuple[str, str, pd.Series]] = []

        if ean_clean:
            if "_ean_keys" in frame:
                ean_match = frame["_ean_keys"].map(
                    lambda values: ean_clean in tuple(values or ()),
                )
            else:
                barcode_columns = _catalog_barcode_columns(frame)
                ean_match = frame.apply(
                    lambda row: ean_clean in _row_ean_keys(row, barcode_columns), axis=1,
                )
            strategies.append(("EAN", ean_clean, ean_match))

        if sku_key:
            if "_sku_key" in frame:
                sku_match = frame["_sku_key"].astype(str).eq(sku_key)
            else:
                sku_match = frame.get("sku", pd.Series("", index=frame.index)).map(
                    lambda value: clean_identifier(value).casefold() == sku_key
                )
            strategies.append(("SKU composito", sku_clean, sku_match))

        for match_kind, match_value, match in strategies:
            if not bool(match.any()):
                continue
            selected = frame.loc[match].copy()
            if "cecotec" in source.supplier_key:
                costs = country_cost(
                    selected, normalize_country_code(country_code).lower()
                )
            else:
                costs = pd.to_numeric(
                    selected.get("cost", 0), errors="coerce"
                ).fillna(0)
            positive = costs[costs.gt(0)]
            if positive.empty:
                # The identifier exists in this source but has no usable cost.
                # Continue with the second identifier and then newer/other sources.
                continue

            unit_cost = float(positive.iloc[0])
            matched_index = positive.index[0]
            row_ean = clean_identifier(
                selected.loc[matched_index, "ean"]
                if "ean" in selected.columns else ""
            )
            matched_ean = row_ean or (ean_clean if match_kind == "EAN" else "")
            matched_sku = clean_identifier(
                selected.loc[matched_index, "sku"]
                if "sku" in selected.columns else ""
            )
            global_fallback = bool(
                supplier_key and not _supplier_compatible(source.supplier_key, supplier_key)
            )
            source_text = (
                f"{source.supplier_name} · {source.list_name} · "
                f"{source.source_kind} · match {match_kind} esatto {match_value}"
            )
            if _is_innpro_light_source(source):
                source_text += " · prezzo all'ingrosso Innpro"
            if global_fallback:
                source_text += " · trovato cercando in tutti i listini"
            return {
                "unit_cost": round(unit_cost, 4),
                "total_cost": round(unit_cost * quantity, 2),
                "source": source_text,
                "matched_ean": matched_ean,
                "matched_sku": matched_sku,
                "matched_supplier": source.supplier_name,
                "matched_list": source.list_name,
            }

    if embedded_cost is not None:
        return {
            "unit_cost": round(float(embedded_cost), 4),
            "total_cost": round(float(embedded_cost) * quantity, 2),
            "source": "SKU composito · costo incorporato al momento della pubblicazione · fallback dopo match EAN/SKU",
            "matched_ean": ean_clean,
            "matched_sku": sku_clean,
            "matched_supplier": clean_text(supplier),
            "matched_list": "SKU composito",
        }

    searched = []
    if ean_clean:
        searched.append(f"EAN {ean_clean}")
    if sku_clean:
        searched.append(f"SKU {sku_clean}")
    identifiers = " e ".join(searched) or "identificativi ordine"
    missing_source = (
        f"Costo non calcolabile: {identifiers} non trovati con costo valido "
        f"nei {len(catalogs)} listini caricati"
    )
    if _is_innpro_supplier(supplier_key):
        missing_source = (
            f"Costo non calcolabile: {identifiers} non trovati nei feed Innpro "
            "ingrosso selezionati; i feed esplicitamente FULL non vengono usati come costo"
        )
    return {
        "unit_cost": None,
        "total_cost": None,
        "source": missing_source,
        "matched_ean": "",
        "matched_sku": "",
        "matched_supplier": "",
        "matched_list": "",
    }


def _mirakl_credentials(credentials: Mapping[str, Any]) -> tuple[str, str, str]:
    base_url = clean_text(
        credentials.get("base_url") or credentials.get("api_url")
        or credentials.get("endpoint") or credentials.get("mirakl_url")
        or credentials.get("shop_url")
    ).rstrip("/")
    api_key = clean_text(
        credentials.get("api_key") or credentials.get("token")
        or credentials.get("authorization") or credentials.get("shop_api_key")
    )
    shop_id = clean_identifier(credentials.get("shop_id"))
    if not base_url or not api_key:
        raise RuntimeError("Credenziali Worten incomplete: API URL/API Key mancanti.")
    return base_url, api_key, shop_id


def _refund_amount(line: Mapping[str, Any]) -> float:
    for key in (
        "total_refunded", "total_refund", "refunded_amount", "refund_amount",
        "total_refund_amount", "price_refunded", "amount_refunded",
    ):
        value = _number(_value(line, key))
        if value is not None:
            return max(0.0, value)
    refunds = _value(line, "refunds", "order_line_refunds", default=[])
    total = 0.0
    found = False
    if isinstance(refunds, list):
        for refund in refunds:
            if not isinstance(refund, Mapping):
                continue
            value = _number(
                _value(
                    refund,
                    "total_amount", "amount", "refund_amount", "price_amount",
                    "total_refund", "refunded_amount",
                )
            )
            if value is not None:
                total += abs(value)
                found = True
    return round(total, 2) if found else 0.0


def _mirakl_sale_amount(line: Mapping[str, Any], quantity: int) -> float | None:
    total = _number(
        _value(
            line,
            "total_price", "total_amount", "order_line_total", "line_total",
            "total_price_including_tax",
        )
    )
    if total is not None:
        return round(total, 2)
    unit = _number(_value(line, "unit_price", "price_unit"))
    shipping = _number(_value(line, "shipping_price", "shipping_amount"), 0.0) or 0.0
    if unit is not None:
        return round(unit * max(1, quantity) + shipping, 2)
    price = _number(_value(line, "price", "price_amount"))
    if price is None:
        return None
    # In Mirakl OR11 ``price`` is normally already the line amount. Only
    # multiply when a dedicated unit-price field proves it is unitary.
    return round(price + shipping, 2)


def _mirakl_commission_amount(line: Mapping[str, Any]) -> float | None:
    total = _number(_value(line, "total_commission", "commission_total"))
    if total is not None:
        return round(abs(total), 2)
    fee = _number(_value(line, "commission_fee", "commission_amount"))
    vat = _number(_value(line, "commission_vat", "commission_tax"), 0.0) or 0.0
    if fee is not None:
        return round(abs(fee) + abs(vat), 2)
    extracted = commission_rate_from_order_line(dict(line))
    if extracted and extracted.get("fee") is not None:
        return round(abs(float(extracted["fee"])), 2)
    return None


def _mirakl_direct_payout(line: Mapping[str, Any]) -> float | None:
    for key in (
        "payout_amount", "seller_amount", "shop_amount", "amount_paid",
        "transferred_amount", "payment_amount", "net_amount", "net_proceeds",
    ):
        value = _number(_value(line, key))
        if value is not None:
            return round(value, 2)
    return None


def _currency_code(line: Mapping[str, Any], order: Mapping[str, Any]) -> str:
    value = _value(line, "currency_iso_code", "currency", "currency_code")
    if not value:
        value = _value(order, "currency_iso_code", "currency", "currency_code")
    if isinstance(value, Mapping):
        value = _value(value, "iso_code", "code", "currency")
    return clean_text(value).upper() or "EUR"


def _to_eur(value: float | None, currency: str, rates: Mapping[str, float]) -> float | None:
    if value is None:
        return None
    code = clean_text(currency).upper() or "EUR"
    if code == "EUR":
        return round(float(value), 2)
    rate = float(rates.get(code, 0) or 0)
    return round(float(value) / rate, 2) if rate > 0 else None


def _normalize_worten_line(
    order: Mapping[str, Any],
    line: Mapping[str, Any],
    *,
    account_id: int,
    catalogs: Sequence[CatalogSource],
    fx_rates: Mapping[str, float],
    index: int,
) -> dict[str, Any]:
    order_id = clean_identifier(_value(order, "order_id", "commercial_id", "id"))
    line_id = clean_identifier(
        _value(line, "order_line_id", "id", default=f"{order_id}-{index + 1}")
    )
    created = clean_text(
        _value(order, "created_date", "date_created", "creation_date", "order_date")
    )
    raw_status = clean_text(
        _value(line, "order_line_state", "state", "status")
        or _value(order, "order_state", "state", "status")
    )
    composite_sku = clean_text(
        _value(
            line,
            "offer_sku", "seller_sku", "shop_sku", "offer_id", "sku", "product_sku",
        )
    )
    parsed = parse_composite_sku(composite_sku)
    quantity = max(1, int(_number(_value(line, "quantity"), 1) or 1))
    customer = _customer_from_order(order)
    country_code = customer["country_code"] or normalize_country_code(
        _value(order, "shipping_country_code", "country_code", "country")
    ) or "PT"
    ean = _ean_from_values(
        _value(line, "ean", "ean13", "barcode", "gtin"),
        parsed.product_code,
        _value(line, "product_sku"),
    )
    cost = resolve_purchase_cost(
        catalogs,
        supplier=parsed.supplier,
        product_code=parsed.product_code,
        ean=ean,
        country_code=country_code,
        quantity=quantity,
        composite_purchase_cost=parsed.purchase_cost,
    )
    currency = _currency_code(line, order)
    gross_local = _mirakl_sale_amount(line, quantity)
    refund_local = _refund_amount(line)
    commission_local = _mirakl_commission_amount(line)
    direct_payout_local = _mirakl_direct_payout(line)
    status_label = marketplace_status_label("worten", raw_status)
    status_detail = clean_text(
        _value(line, "cancellation_reason", "cancel_reason", "reason", "message")
        or _value(order, "cancellation_reason", "cancel_reason", "reason", "message")
    )
    zero_reason = _zero_economics_reason(raw_status, status_label, status_detail)
    cancelled = _is_cancelled(raw_status)
    returned = _is_returned_or_refunded(raw_status)
    if zero_reason:
        gross_local = 0.0
        refund_local = 0.0
        commission_local = 0.0
        payout_local = 0.0
        purchase_cost = 0.0
        note = _zero_economic_note(status_detail, zero_reason)
    else:
        gross_value = max(0.0, float(gross_local or 0.0))
        if returned and refund_local <= 0:
            refund_local = gross_value
        refund_local = min(gross_value, max(0.0, float(refund_local or 0.0)))
        net_sale_local = max(0.0, gross_value - refund_local)
        if direct_payout_local is not None:
            payout_local = direct_payout_local
        elif net_sale_local <= 0:
            payout_local = 0.0
            commission_local = 0.0 if commission_local is None else min(commission_local, net_sale_local)
        else:
            payout_local = net_sale_local - float(commission_local or 0.0)
        purchase_cost = cost["total_cost"]
        note = ""
        if refund_local > 0:
            note = "Rimborso totale" if refund_local >= gross_value else "Rimborso parziale"
        if purchase_cost is None:
            note = clean_text(f"{note}; {cost['source']}" if note else cost["source"])

    gross_eur = _to_eur(gross_local, currency, fx_rates)
    refund_eur = _to_eur(refund_local, currency, fx_rates) or 0.0
    sale_eur = None if gross_eur is None else round(max(0.0, gross_eur - refund_eur), 2)
    commission_eur = _to_eur(commission_local, currency, fx_rates)
    payout_eur = _to_eur(payout_local, currency, fx_rates)
    if payout_eur is None and sale_eur is not None:
        payout_eur = round(sale_eur - float(commission_eur or 0.0), 2)
    if zero_reason:
        payment = ""
    else:
        payment = payment_estimated(created) if (payout_eur or 0) > 0 else ""
    tracking = _tracking_text(line, order)
    product_title = clean_text(
        _value(line, "product_title", "product_name", "title", "product_label")
    )
    return {
        "marketplace": "worten",
        "account_id": account_id,
        "row_key": _make_row_key("worten", account_id, order_id, line_id, composite_sku),
        "order_id": order_id,
        "order_line_id": line_id,
        "order_created": created,
        "country_code": country_code,
        "market_label": _market_label("worten", country_code),
        "raw_status": raw_status,
        "status_label": status_label,
        "supplier": clean_text(parsed.supplier),
        "composite_sku": composite_sku,
        "product_title": product_title,
        "ean": ean or clean_identifier(parsed.product_code),
        "quantity": quantity,
        "sale_original_eur": gross_eur,
        "refund_eur": refund_eur,
        "sale_eur": sale_eur,
        "purchase_cost_eur": purchase_cost,
        "commission_eur": commission_eur,
        "payout_eur": payout_eur,
        "cost_source": cost["source"] if not zero_reason else f"Non dovuto · {zero_reason}",
        "financial_source": f"API Mirakl/Worten · valuta {currency}",
        "tracking": tracking,
        "customer_name": customer["customer_name"],
        "payment_estimated": payment,
        "note": note,
        "raw_json": {"order": dict(order), "line": dict(line)},
    }


def fetch_worten_accounting_orders(
    credentials: Mapping[str, Any],
    *,
    account_id: int,
    seller_id: int,
    date_from: date,
    date_to: date,
    updated_since: datetime | None = None,
    updated_to: datetime | None = None,
    max_rows: int = 10_000,
    request_timeout: float = 60.0,
) -> list[dict[str, Any]]:
    base_url, api_key, shop_id = _mirakl_credentials(credentials)
    catalogs = load_supplier_catalogs(seller_id)
    fx = get_ecb_rates().get("rates", {})
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": "MarketplaceHub-Accounting/1.0",
    }
    offset = 0
    output: list[dict[str, Any]] = []
    while len(output) < max_rows:
        params: dict[str, Any] = {"offset": offset, "max": 100}
        if updated_since is not None:
            start_update = updated_since
            if start_update.tzinfo is None:
                start_update = start_update.replace(tzinfo=timezone.utc)
            end_update = updated_to or datetime.combine(
                date_to, datetime.max.time(), tzinfo=timezone.utc
            )
            if end_update.tzinfo is None:
                end_update = end_update.replace(tzinfo=timezone.utc)
            # OR11 officially supports start_update_date/end_update_date and
            # applies its own latency margin so changed orders are not missed.
            params["start_update_date"] = start_update.astimezone(timezone.utc).isoformat()
            params["end_update_date"] = end_update.astimezone(timezone.utc).isoformat()
        else:
            params["start_date"] = datetime.combine(
                date_from, datetime.min.time(), tzinfo=timezone.utc
            ).isoformat()
            params["end_date"] = datetime.combine(
                date_to, datetime.max.time(), tzinfo=timezone.utc
            ).isoformat()
        if shop_id:
            params["shop_id"] = shop_id
        url = base_url + "/orders" if base_url.lower().endswith("/api") else base_url + "/api/orders"
        response = requests.get(url, params=params, headers=headers, timeout=max(5.0, float(request_timeout)))
        response.raise_for_status()
        payload = response.json()
        orders_page = payload.get("orders") if isinstance(payload, Mapping) else None
        if not isinstance(orders_page, list) or not orders_page:
            break
        for order in orders_page:
            if not isinstance(order, Mapping):
                continue
            created = _value(order, "created_date", "date_created", "creation_date", "order_date")
            if updated_since is None and not _date_in_range(created, date_from, date_to):
                continue
            lines = _value(order, "order_lines", "lines", default=[])
            if not isinstance(lines, list):
                continue
            for index, line in enumerate(lines):
                if not isinstance(line, Mapping):
                    continue
                output.append(
                    _normalize_worten_line(
                        order,
                        line,
                        account_id=account_id,
                        catalogs=catalogs,
                        fx_rates=fx,
                        index=index,
                    )
                )
                if len(output) >= max_rows:
                    break
            if len(output) >= max_rows:
                break
        total = int(payload.get("total_count") or 0) if isinstance(payload, Mapping) else 0
        offset += len(orders_page)
        if len(orders_page) < 100 or (total and offset >= total):
            break
    return output


def _kaufland_customer(raw: Mapping[str, Any]) -> dict[str, str]:
    result = _customer_from_order(raw)
    if not result["customer_name"]:
        buyer = raw.get("buyer") if isinstance(raw.get("buyer"), Mapping) else {}
        result["customer_name"] = clean_text(
            _value(buyer, "name", "full_name", "first_name", "firstname")
        )
    return result


def _kaufland_order_manifest(
    client: KauflandClient,
    *,
    maximum: int = 10_000,
) -> list[dict[str, Any]]:
    """Read lightweight order summaries using GET /orders.

    This replaces eight complete status scans during incremental updates.  Full
    order details are requested only for orders whose creation or unit-update
    timestamp falls inside the incremental window.
    """
    result: list[dict[str, Any]] = []
    offset = 0
    cap = max(1, int(maximum))
    while len(result) < cap:
        page_limit = min(100, cap - len(result))
        response = client.orders(limit=page_limit, offset=offset)
        page = response.get("data", []) if isinstance(response, Mapping) else []
        if not isinstance(page, list) or not page:
            break
        result.extend(dict(item) for item in page if isinstance(item, Mapping))
        pagination = response.get("pagination", {}) if isinstance(response, Mapping) else {}
        try:
            total = int(pagination.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        offset += len(page)
        if len(page) < page_limit or (total and offset >= total):
            break
    return result


def _kaufland_units_from_order_detail(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = response_item(detail)
    raw = payload.get("order_units") or payload.get("units") or []
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _kaufland_incremental_units(
    client: KauflandClient,
    *,
    updated_since: datetime,
    date_to: date,
    maximum: int,
) -> list[dict[str, Any]]:
    threshold = updated_since
    if threshold.tzinfo is None:
        threshold = threshold.replace(tzinfo=timezone.utc)
    threshold = threshold.astimezone(timezone.utc)
    end_limit = datetime.combine(date_to, datetime.max.time(), tzinfo=timezone.utc)
    candidates: list[str] = []
    for item in _kaufland_order_manifest(client, maximum=maximum):
        order_id = clean_identifier(item.get("id_order"))
        if not order_id:
            continue
        created = _parse_iso_datetime(item.get("ts_created_iso"))
        updated = _parse_iso_datetime(item.get("ts_units_updated_iso"))
        relevant = max(
            (value for value in (created, updated) if value is not None),
            default=None,
        )
        if relevant is not None and threshold <= relevant <= end_limit:
            candidates.append(order_id)
    units: list[dict[str, Any]] = []
    for order_id in dict.fromkeys(candidates):
        detail = client.order(order_id)
        units.extend(_kaufland_units_from_order_detail(detail))
        if len(units) >= maximum:
            break
    return units[:maximum]


def fetch_kaufland_accounting_orders(
    credentials: Mapping[str, Any],
    *,
    account_id: int,
    seller_id: int,
    date_from: date,
    date_to: date,
    updated_since: datetime | None = None,
    max_rows: int = 10_000,
    include_order_details: bool = True,
    request_timeout: float = 45.0,
    max_attempts: int = 5,
) -> list[dict[str, Any]]:
    client_key = clean_text(credentials.get("client_key") or credentials.get("shop_client_key"))
    secret_key = clean_text(credentials.get("secret_key") or credentials.get("shop_secret_key"))
    if not client_key or not secret_key:
        raise RuntimeError("Credenziali Kaufland incomplete: Client Key/Secret Key mancanti.")
    playground = bool(credentials.get("playground") or credentials.get("test"))
    client = KauflandClient(
        client_key,
        secret_key,
        playground=playground,
        request_timeout=max(3.0, float(request_timeout)),
        max_attempts=max(1, int(max_attempts)),
    )
    catalogs = load_supplier_catalogs(seller_id)
    fx = get_ecb_rates().get("rates", {})
    if updated_since is not None:
        try:
            raw_units = _kaufland_incremental_units(
                client,
                updated_since=updated_since,
                date_to=date_to,
                maximum=max_rows,
            )
        except Exception:
            # Compatibility fallback for older/limited accounts: preserve the
            # complete status scan rather than losing updates.
            raw_units = fetch_order_units(
                client, maximum=max_rows, statuses=ORDER_UNIT_STATUSES
            )
    else:
        raw_units = fetch_order_units(
            client, maximum=max_rows, statuses=ORDER_UNIT_STATUSES
        )
    order_details: dict[str, dict] = {}
    output: list[dict[str, Any]] = []
    for raw in raw_units:
        created = clean_text(raw.get("ts_created_iso"))
        if updated_since is None and not _date_in_range(created, date_from, date_to):
            continue
        merged = dict(raw)
        status = clean_text(raw.get("status")).lower()
        unit_id = clean_identifier(raw.get("id_order_unit"))
        if include_order_details and unit_id and status in SHIPPED_ORDER_UNIT_STATUSES:
            try:
                detail = response_item(client.order_unit(unit_id))
                merged = merge_order_unit(merged, detail)
                if not _tracking_text(merged):
                    order_id = clean_identifier(merged.get("id_order"))
                    if order_id:
                        if order_id not in order_details:
                            order_details[order_id] = response_item(client.order(order_id))
                        order_unit = find_order_unit(order_details[order_id], unit_id)
                        if order_unit:
                            merged = merge_order_unit(merged, order_unit)
            except Exception:
                pass
        financial = normalize_order_unit(merged)
        converted = order_amounts_to_eur(financial, fx)
        composite_sku = clean_text(financial.get("sku"))
        parsed = parse_composite_sku(composite_sku)
        ean = clean_identifier(financial.get("ean")) or _ean_from_values(parsed.product_code)
        country_code = normalize_country_code(financial.get("country_code"))
        cost = resolve_purchase_cost(
            catalogs,
            supplier=parsed.supplier,
            product_code=parsed.product_code,
            ean=ean,
            country_code=country_code,
            quantity=1,
            composite_purchase_cost=parsed.purchase_cost,
        )
        status_label = marketplace_status_label("kaufland", status)
        status_detail = clean_text(
            _value(merged, "cancellation_reason", "cancel_reason", "reason", "message", "note")
        )
        zero_reason = _zero_economics_reason(status, status_label, status_detail)
        returned = _is_returned_or_refunded(status)
        original_sale = converted.get("sold_total_eur")
        refund = 0.0
        sale = original_sale
        commission = converted.get("commission_eur")
        payout = converted.get("payout_eur")
        purchase = cost["total_cost"]
        note = ""
        if zero_reason:
            original_sale = 0.0
            refund = 0.0
            sale = 0.0
            commission = 0.0
            payout = 0.0
            purchase = 0.0
            note = _zero_economic_note(status_detail, zero_reason)
        elif returned:
            refund = round(float(original_sale or 0.0), 2)
            sale = 0.0
            commission = 0.0
            payout = 0.0
            note = "Reso: ricavo marketplace azzerato; costo prodotto mantenuto"
        if purchase is None and not zero_reason:
            note = clean_text(f"{note}; {cost['source']}" if note else cost["source"])
        customer = _kaufland_customer(merged)
        tracking = _tracking_text(merged)
        order_id = clean_identifier(financial.get("id_order"))
        output.append({
            "marketplace": "kaufland",
            "account_id": account_id,
            "row_key": _make_row_key("kaufland", account_id, order_id, unit_id, composite_sku),
            "order_id": order_id,
            "order_line_id": unit_id,
            "order_created": created,
            "country_code": country_code,
            "market_label": _market_label("kaufland", country_code, financial.get("storefront")),
            "raw_status": status,
            "status_label": status_label,
            "supplier": clean_text(parsed.supplier),
            "composite_sku": composite_sku,
            "product_title": clean_text(financial.get("product_name")),
            "ean": ean or clean_identifier(parsed.product_code),
            "quantity": 1,
            "sale_original_eur": original_sale,
            "refund_eur": refund,
            "sale_eur": sale,
            "purchase_cost_eur": purchase,
            "commission_eur": commission,
            "payout_eur": payout,
            "cost_source": cost["source"] if not zero_reason else f"Non dovuto · {zero_reason}",
            "financial_source": f"API Kaufland · conversione BCE da {financial.get('currency') or 'EUR'}",
            "tracking": tracking,
            "customer_name": customer["customer_name"],
            "payment_estimated": "" if zero_reason or returned else payment_estimated(created),
            "note": note,
            "raw_json": dict(merged),
        })
    return output




ACCOUNTING_INLINE_EDIT_FIELDS: dict[str, str] = {
    "supplier": "text",
    "product_title": "text",
    "ean": "identifier",
    "quantity": "integer",
    "sale_eur": "money_nullable",
    "purchase_cost_eur": "money_nullable",
    "commission_eur": "money_nullable",
    "refund_eur": "money_zero",
    "payout_eur": "money_nullable",
    "extra_cost_eur": "money_zero",
    "supplier_order_number": "text",
    "payment_estimated": "text",
    "customer_name": "text",
    "tracking": "text",
    "receipt": "text",
    "note": "text",
}

ACCOUNTING_OVERRIDE_INTERNAL_FIELDS = {
    "sale_original_eur", "cost_source", "financial_source",
}

ACCOUNTING_INLINE_ECONOMIC_FIELDS = {
    "sale_eur", "purchase_cost_eur", "commission_eur", "refund_eur",
    "payout_eur", "extra_cost_eur", "sale_original_eur",
}


def _decode_override_value(value: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _override_rows_for_scope(
    con: Any, account_id: int, marketplace: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in con.execute(
        """SELECT row_key,field_name,value_json FROM accounting_manual_overrides
        WHERE marketplace_account_id=? AND marketplace=?""",
        (int(account_id), clean_text(marketplace).lower()),
    ).fetchall():
        row_key = clean_text(item["row_key"])
        field_name = clean_text(item["field_name"])
        if not row_key or not field_name:
            continue
        result.setdefault(row_key, {})[field_name] = _decode_override_value(item["value_json"])
    return result


def apply_accounting_manual_overrides(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay persistent cell edits on API/listino data with one DB round-trip.

    v302 replaces the previous N+1 lookup (one SELECT for every marketplace
    account) with a single batched SELECT. This matters on the Dashboard and
    agency views where many Seller/account scopes can be present at once.
    """
    values = [dict(item) for item in records]
    scopes: dict[tuple[int, str], list[int]] = {}
    for index, item in enumerate(values):
        account_id = int(item.get("marketplace_account_id") or 0)
        marketplace = clean_text(item.get("marketplace")).lower()
        if account_id and marketplace:
            scopes.setdefault((account_id, marketplace), []).append(index)
    if not scopes:
        return values

    predicates: list[str] = []
    params: list[Any] = []
    for account_id, marketplace in scopes:
        predicates.append("(marketplace_account_id=? AND marketplace=?)")
        params.extend((int(account_id), marketplace))

    override_index: dict[tuple[int, str, str], dict[str, Any]] = {}
    with connect() as con:
        found = con.execute(
            "SELECT marketplace_account_id,marketplace,row_key,field_name,value_json "
            "FROM accounting_manual_overrides WHERE " + " OR ".join(predicates),
            tuple(params),
        ).fetchall()
    for item in found:
        key = (
            int(item.get("marketplace_account_id") or 0),
            clean_text(item.get("marketplace")).lower(),
            clean_text(item.get("row_key")),
        )
        field_name = clean_text(item.get("field_name"))
        if not key[0] or not key[1] or not key[2] or not field_name:
            continue
        override_index.setdefault(key, {})[field_name] = _decode_override_value(
            item.get("value_json")
        )

    for (account_id, marketplace), indexes in scopes.items():
        for index in indexes:
            item = values[index]
            row_key = clean_text(item.get("row_key"))
            overrides = override_index.get((account_id, marketplace, row_key), {})
            for field_name, value in overrides.items():
                if (
                    field_name in ACCOUNTING_INLINE_EDIT_FIELDS
                    or field_name in ACCOUNTING_OVERRIDE_INTERNAL_FIELDS
                ):
                    item[field_name] = value
    return values


def _sanitize_inline_edit(field_name: str, value: Any) -> Any:
    kind = ACCOUNTING_INLINE_EDIT_FIELDS[field_name]
    if kind == "text":
        return clean_text(value)
    if kind == "identifier":
        return clean_identifier(value)
    if kind == "integer":
        number = _number(value)
        return max(1, int(round(number or 1)))
    if kind == "money_zero":
        return round(float(_number(value, 0.0) or 0.0), 2)
    if kind == "money_nullable":
        number = _number(value)
        return None if number is None else round(float(number), 2)
    return value


def _persist_override(
    con: Any,
    *,
    account_id: int,
    marketplace: str,
    row_key: str,
    field_name: str,
    value: Any,
    updated_at: str,
) -> None:
    con.execute(
        """INSERT INTO accounting_manual_overrides(
            marketplace_account_id,marketplace,row_key,field_name,value_json,updated_at
        ) VALUES(?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,marketplace,row_key,field_name)
        DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
        (
            int(account_id), clean_text(marketplace).lower(), clean_text(row_key),
            clean_text(field_name), json.dumps(value, ensure_ascii=False), updated_at,
        ),
    )


def save_accounting_inline_edits(
    changes: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    """Persist direct grid edits and keep them authoritative across restarts/syncs."""
    updated_rows = 0
    updated_fields = 0
    ignored_fields = 0
    with connect() as con:
        for change in changes:
            account_id = int(change.get("marketplace_account_id") or change.get("account_id") or 0)
            marketplace = clean_text(change.get("marketplace")).lower()
            row_key = clean_text(change.get("row_key"))
            requested = change.get("fields") if isinstance(change.get("fields"), Mapping) else change
            if not account_id or not marketplace or not row_key or not isinstance(requested, Mapping):
                continue
            db_row = con.execute(
                """SELECT * FROM accounting_order_lines
                WHERE marketplace_account_id=? AND marketplace=? AND row_key=?""",
                (account_id, marketplace, row_key),
            ).fetchone()
            if db_row is None:
                continue
            current = dict(db_row)
            existing_overrides = _override_rows_for_scope(con, account_id, marketplace).get(row_key, {})
            for field_name, value in existing_overrides.items():
                if field_name in ACCOUNTING_INLINE_EDIT_FIELDS or field_name in ACCOUNTING_OVERRIDE_INTERNAL_FIELDS:
                    current[field_name] = value

            accepted: dict[str, Any] = {}
            for field_name, value in requested.items():
                if field_name not in ACCOUNTING_INLINE_EDIT_FIELDS:
                    continue
                accepted[field_name] = _sanitize_inline_edit(field_name, value)
            if not accepted:
                continue

            # The cancellation/refund rule is always stronger than a manual amount.
            supplier_order = accepted.get(
                "supplier_order_number", current.get("supplier_order_number")
            )
            note_value = accepted.get("note", current.get("note"))
            zero_reason = _zero_economics_reason(
                current.get("raw_status"), current.get("status_label"), note_value,
                supplier_order,
            )
            if zero_reason:
                for field_name in ACCOUNTING_INLINE_ECONOMIC_FIELDS:
                    if field_name in ACCOUNTING_INLINE_EDIT_FIELDS or field_name == "sale_original_eur":
                        accepted[field_name] = 0.0
                accepted["payment_estimated"] = ""
                accepted["cost_source"] = f"Non dovuto · {zero_reason}"
                accepted["financial_source"] = f"Azzeramento automatico · {zero_reason}"
                accepted["note"] = _zero_economic_note(note_value, zero_reason)
            else:
                if "sale_eur" in accepted:
                    accepted["sale_original_eur"] = accepted["sale_eur"]
                # When sale or commission changes, keep payout coherent unless the user
                # explicitly supplied a different payout in the same edit batch.
                if {"sale_eur", "commission_eur"}.intersection(accepted) and "payout_eur" not in accepted:
                    sale_value = _number(accepted.get("sale_eur", current.get("sale_eur")))
                    commission_value = _number(
                        accepted.get("commission_eur", current.get("commission_eur")), 0.0
                    ) or 0.0
                    if sale_value is not None:
                        accepted["payout_eur"] = round(sale_value - commission_value, 2)
                if "purchase_cost_eur" in accepted:
                    accepted["cost_source"] = "Modifica manuale persistente"
                    accepted["note"] = _without_cost_warning(
                        accepted.get("note", current.get("note"))
                    )
                if {"sale_eur", "commission_eur", "refund_eur", "payout_eur"}.intersection(accepted):
                    accepted["financial_source"] = _append_source(
                        current.get("financial_source"), "Modifica manuale persistente"
                    )

            updated_at = now_iso()
            actual_columns = [
                field_name for field_name in accepted
                if field_name in ACCOUNTING_INLINE_EDIT_FIELDS
                or field_name in ACCOUNTING_OVERRIDE_INTERNAL_FIELDS
            ]
            if not actual_columns:
                continue
            assignments = ",".join(f"{field_name}=?" for field_name in actual_columns)
            con.execute(
                f"UPDATE accounting_order_lines SET {assignments},synced_at=? "
                "WHERE marketplace_account_id=? AND marketplace=? AND row_key=?",
                tuple(accepted[field_name] for field_name in actual_columns)
                + (updated_at, account_id, marketplace, row_key),
            )
            for field_name in actual_columns:
                _persist_override(
                    con,
                    account_id=account_id,
                    marketplace=marketplace,
                    row_key=row_key,
                    field_name=field_name,
                    value=accepted[field_name],
                    updated_at=updated_at,
                )
            updated_rows += 1
            updated_fields += len(actual_columns)
            ignored_fields += max(0, len(requested) - len(accepted))
    return {
        "updated_rows": updated_rows,
        "updated_fields": updated_fields,
        "ignored_fields": ignored_fields,
    }


SYNC_FINAL_STATUS_TOKENS = {
    "received", "delivered", "closed", "cancelled", "canceled", "refunded",
    "returned", "refused", "sent_and_autopaid", "paid", "completed",
}


def accounting_sync_environment(
    marketplace: str,
    credentials: Mapping[str, Any] | None = None,
) -> str:
    credentials = credentials or {}
    if clean_text(marketplace).lower() == "kaufland" and bool(
        credentials.get("playground") or credentials.get("test")
    ):
        return "playground"
    return "production"


def accounting_sync_state(
    seller_id: int,
    account_id: int,
    marketplace: str,
    *,
    environment: str = "production",
) -> dict[str, Any]:
    ensure_schema()
    result = rows(
        """
        SELECT * FROM accounting_sync_state
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
          AND environment=?
        LIMIT 1
        """,
        (
            int(seller_id), int(account_id), clean_text(marketplace).lower(),
            clean_text(environment).lower() or "production",
        ),
    )
    return dict(result[0]) if result else {}


def accounting_cache_summary(
    seller_id: int,
    account_id: int,
    marketplace: str,
) -> dict[str, Any]:
    ensure_schema()
    scope = (int(seller_id), int(account_id), clean_text(marketplace).lower())
    summary_rows = rows(
        """
        SELECT COUNT(*) AS total_rows,
               COUNT(DISTINCT CASE WHEN TRIM(order_id)<>'' THEN order_id END) AS total_orders,
               MAX(order_created) AS last_order_created
        FROM accounting_order_lines
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
        """,
        scope,
    )
    summary = dict(summary_rows[0]) if summary_rows else {}
    last_rows = rows(
        """
        SELECT order_id,order_created
        FROM accounting_order_lines
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
          AND TRIM(order_id)<>''
        ORDER BY order_created DESC,id DESC
        LIMIT 1
        """,
        scope,
    )
    latest = dict(last_rows[0]) if last_rows else {}
    return {
        "total_rows": int(summary.get("total_rows") or 0),
        "total_orders": int(summary.get("total_orders") or 0),
        "last_order_created": clean_text(
            latest.get("order_created") or summary.get("last_order_created")
        ),
        "last_order_id": clean_text(latest.get("order_id")),
    }


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _earliest_nonfinal_order_date(
    seller_id: int,
    account_id: int,
    marketplace: str,
) -> date | None:
    records = rows(
        """
        SELECT order_created,raw_status,status_label
        FROM accounting_order_lines
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
          AND TRIM(order_created)<>''
        ORDER BY order_created ASC
        """,
        (int(seller_id), int(account_id), clean_text(marketplace).lower()),
    )
    for item in records:
        status = clean_text(item.get("raw_status") or item.get("status_label")).lower()
        if not any(token in status for token in SYNC_FINAL_STATUS_TOKENS):
            parsed = _parse_iso_datetime(item.get("order_created"))
            if parsed:
                return parsed.date()
    return None


def accounting_incremental_window(
    seller_id: int,
    account_id: int,
    marketplace: str,
    *,
    requested_from: date,
    requested_to: date,
    environment: str = "production",
    full: bool = False,
    overlap_days: int = 7,
) -> tuple[date, date, bool]:
    """Return the API window and whether this is the first synchronization.

    Existing rows are never removed. After the first synchronization only the
    recent safety window and any older non-final orders are requested again.
    """
    state = accounting_sync_state(
        seller_id, account_id, marketplace, environment=environment
    )
    first_sync = not clean_text(state.get("last_completed_at"))
    if full or first_sync:
        return requested_from, requested_to, first_sync

    last_completed = _parse_iso_datetime(state.get("last_completed_at"))
    if not last_completed:
        return requested_from, requested_to, True
    effective_from = last_completed.date() - timedelta(days=max(1, overlap_days))
    oldest_open = _earliest_nonfinal_order_date(seller_id, account_id, marketplace)
    if oldest_open and oldest_open < effective_from:
        effective_from = oldest_open
    # Never request dates after the user-selected end date.
    if effective_from > requested_to:
        effective_from = requested_from
    return effective_from, requested_to, False


def _record_sync_start(
    seller_id: int,
    account_id: int,
    marketplace: str,
    environment: str,
    requested_from: date,
    requested_to: date,
    effective_from: date,
    effective_to: date,
) -> None:
    execute(
        """
        INSERT INTO accounting_sync_state(
            seller_id,marketplace_account_id,marketplace,environment,
            last_started_at,last_attempted_at,last_requested_from,last_requested_to,
            last_effective_from,last_effective_to,last_status,last_error
        ) VALUES(?,?,?,?,?,?,?,?,?,?,'running','')
        ON CONFLICT(marketplace_account_id,marketplace,environment) DO UPDATE SET
            seller_id=excluded.seller_id,last_started_at=excluded.last_started_at,
            last_attempted_at=excluded.last_attempted_at,
            last_requested_from=excluded.last_requested_from,
            last_requested_to=excluded.last_requested_to,
            last_effective_from=excluded.last_effective_from,
            last_effective_to=excluded.last_effective_to,
            last_status='running',last_error=''
        """,
        (
            int(seller_id), int(account_id), clean_text(marketplace).lower(),
            clean_text(environment).lower() or "production", now_iso(), now_iso(),
            requested_from.isoformat(), requested_to.isoformat(),
            effective_from.isoformat(), effective_to.isoformat(),
        ),
    )


def _record_sync_finish(
    seller_id: int,
    account_id: int,
    marketplace: str,
    environment: str,
    *,
    fetched_rows: int,
    new_rows: int,
    updated_rows: int,
    fetched_orders: int = 0,
    new_orders: int = 0,
    updated_orders: int = 0,
    unchanged_orders: int = 0,
    existing_orders: int = 0,
    error: str = "",
) -> None:
    summary = accounting_cache_summary(seller_id, account_id, marketplace)
    execute(
        """
        UPDATE accounting_sync_state SET
            last_completed_at=CASE WHEN ?<>'' THEN ? ELSE last_completed_at END,
            last_order_created=?,last_order_id=?,
            last_status=?,last_error=?,last_fetched_rows=?,last_new_rows=?,
            last_updated_rows=?,last_fetched_orders=?,last_new_orders=?,
            last_updated_orders=?,last_unchanged_orders=?,last_existing_orders=?,
            total_rows=?,total_orders=?
        WHERE marketplace_account_id=? AND marketplace=? AND environment=?
        """,
        (
            now_iso() if not error else "", now_iso() if not error else "",
            summary["last_order_created"], summary["last_order_id"],
            "error" if error else "completed", clean_text(error),
            int(fetched_rows), int(new_rows), int(updated_rows),
            int(fetched_orders), int(new_orders), int(updated_orders),
            int(unchanged_orders), int(existing_orders),
            int(summary["total_rows"]), int(summary["total_orders"]),
            int(account_id), clean_text(marketplace).lower(),
            clean_text(environment).lower() or "production",
        ),
    )


def _sync_fingerprint(item: Mapping[str, Any]) -> str:
    """Stable comparison of API-managed fields, excluding local/manual values."""
    fields = (
        "order_id", "order_line_id", "order_created", "country_code",
        "market_label", "raw_status", "status_label", "supplier",
        "composite_sku", "product_title", "ean", "quantity",
        "sale_original_eur", "refund_eur", "sale_eur", "purchase_cost_eur",
        "commission_eur", "payout_eur", "cost_source", "financial_source",
        "tracking", "customer_name", "payment_estimated", "note", "raw_json",
    )
    normalized: dict[str, Any] = {}
    for field in fields:
        value = item.get(field)
        if field == "raw_json" and isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                pass
        normalized[field] = value
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)


def upsert_accounting_rows_with_stats(
    seller_id: int,
    account_id: int,
    marketplace: str,
    records: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    values = [dict(item) for item in records]
    marketplace_key = clean_text(marketplace).lower()
    before = accounting_cache_summary(seller_id, account_id, marketplace_key)
    existing_order_rows = rows(
        """SELECT DISTINCT order_id FROM accounting_order_lines
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
          AND TRIM(order_id)<>''""",
        (int(seller_id), int(account_id), marketplace_key),
    )
    existing_orders = {
        clean_text(item.get("order_id")) for item in existing_order_rows
        if clean_text(item.get("order_id"))
    }
    keys = [clean_text(item.get("row_key")) for item in values if clean_text(item.get("row_key"))]
    existing_by_key: dict[str, dict[str, Any]] = {}
    if keys:
        for start_index in range(0, len(keys), 400):
            chunk = keys[start_index:start_index + 400]
            placeholders = ",".join("?" for _ in chunk)
            found = rows(
                f"""SELECT * FROM accounting_order_lines
                WHERE marketplace_account_id=? AND marketplace=?
                  AND row_key IN ({placeholders})""",
                (int(account_id), marketplace_key, *chunk),
            )
            existing_by_key.update({
                clean_text(item.get("row_key")): dict(item) for item in found
            })

    fetched_order_ids = {
        clean_text(item.get("order_id")) for item in values
        if clean_text(item.get("order_id"))
    }
    new_order_ids = fetched_order_ids - existing_orders
    changed_order_ids: set[str] = set()
    unchanged_candidates = fetched_order_ids & existing_orders
    for item in values:
        order_id = clean_text(item.get("order_id"))
        row_key = clean_text(item.get("row_key"))
        if not order_id or order_id in new_order_ids:
            continue
        previous = existing_by_key.get(row_key)
        if previous is None or _sync_fingerprint(previous) != _sync_fingerprint(item):
            changed_order_ids.add(order_id)
    unchanged_order_ids = unchanged_candidates - changed_order_ids

    saved = upsert_accounting_rows(seller_id, account_id, marketplace_key, values)
    new_rows = sum(1 for key in keys if key not in existing_by_key)
    return {
        "saved": int(saved),
        "fetched": len(values),
        "new": new_rows,
        "updated": max(0, len(values) - new_rows),
        "fetched_orders": len(fetched_order_ids),
        "new_orders": len(new_order_ids),
        "updated_orders": len(changed_order_ids),
        "unchanged_orders": len(unchanged_order_ids),
        "existing_orders": int(before.get("total_orders") or 0),
    }


def synchronize_accounting_orders(
    credentials: Mapping[str, Any],
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    date_from: date,
    date_to: date,
    full: bool = False,
    overlap_days: int = 7,
) -> dict[str, Any]:
    """Incrementally synchronize an account while preserving its full cache."""
    marketplace_key = clean_text(marketplace).lower()
    environment = accounting_sync_environment(marketplace_key, credentials)
    previous_state = accounting_sync_state(
        seller_id, account_id, marketplace_key, environment=environment
    )
    effective_from, effective_to, first_sync = accounting_incremental_window(
        seller_id,
        account_id,
        marketplace_key,
        requested_from=date_from,
        requested_to=date_to,
        environment=environment,
        full=full,
        overlap_days=overlap_days,
    )
    updated_since: datetime | None = None
    if not full and not first_sync:
        last_completed = _parse_iso_datetime(previous_state.get("last_completed_at"))
        if last_completed is not None:
            # Small overlap protects against clock differences; Mirakl OR11
            # additionally applies its own latency delta.
            updated_since = last_completed - timedelta(minutes=5)
    _record_sync_start(
        seller_id, account_id, marketplace_key, environment,
        date_from, date_to, effective_from, effective_to,
    )
    try:
        if marketplace_key == "kaufland":
            fetched = fetch_kaufland_accounting_orders(
                credentials,
                account_id=account_id,
                seller_id=seller_id,
                date_from=effective_from,
                date_to=effective_to,
                updated_since=updated_since,
            )
        elif marketplace_key == "worten":
            fetched = fetch_worten_accounting_orders(
                credentials,
                account_id=account_id,
                seller_id=seller_id,
                date_from=effective_from,
                date_to=effective_to,
                updated_since=updated_since,
                updated_to=datetime.combine(
                    effective_to, datetime.max.time(), tzinfo=timezone.utc
                ),
            )
        else:
            raise ValueError(
                f"Sincronizzazione contabile non ancora disponibile per {marketplace_key}."
            )
        stats = upsert_accounting_rows_with_stats(
            seller_id, account_id, marketplace_key, fetched
        )
        _record_sync_finish(
            seller_id,
            account_id,
            marketplace_key,
            environment,
            fetched_rows=stats["fetched"],
            new_rows=stats["new"],
            updated_rows=stats["updated"],
            fetched_orders=stats["fetched_orders"],
            new_orders=stats["new_orders"],
            updated_orders=stats["updated_orders"],
            unchanged_orders=stats["unchanged_orders"],
            existing_orders=stats["existing_orders"],
        )
    except Exception as exc:
        _record_sync_finish(
            seller_id, account_id, marketplace_key, environment,
            fetched_rows=0, new_rows=0, updated_rows=0, error=str(exc),
        )
        raise
    summary = accounting_cache_summary(seller_id, account_id, marketplace_key)
    return {
        **stats,
        **summary,
        "environment": environment,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "first_sync": first_sync,
        "full": bool(full),
    }

def upsert_accounting_rows(
    seller_id: int,
    account_id: int,
    marketplace: str,
    records: Iterable[Mapping[str, Any]],
) -> int:
    saved = 0
    for original_item in records:
        reason = _zero_economics_reason(
            original_item.get("raw_status"), original_item.get("status_label"),
            original_item.get("note"), original_item.get("supplier_order_number"),
        )
        item = _zero_economic_record(original_item, reason) if reason else dict(original_item)
        execute(
            """
            INSERT INTO accounting_order_lines(
                seller_id,marketplace_account_id,marketplace,row_key,order_id,
                order_line_id,order_created,country_code,market_label,raw_status,
                status_label,supplier,composite_sku,product_title,ean,quantity,
                sale_original_eur,refund_eur,sale_eur,purchase_cost_eur,
                commission_eur,payout_eur,cost_source,financial_source,tracking,
                customer_name,payment_estimated,note,raw_json,synced_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(marketplace_account_id,marketplace,row_key) DO UPDATE SET
                seller_id=excluded.seller_id,order_id=excluded.order_id,
                order_line_id=excluded.order_line_id,order_created=excluded.order_created,
                country_code=excluded.country_code,market_label=excluded.market_label,
                raw_status=excluded.raw_status,status_label=excluded.status_label,
                supplier=CASE WHEN TRIM(excluded.supplier)<>'' THEN excluded.supplier ELSE accounting_order_lines.supplier END,
                composite_sku=CASE WHEN TRIM(excluded.composite_sku)<>'' THEN excluded.composite_sku ELSE accounting_order_lines.composite_sku END,
                product_title=CASE WHEN TRIM(excluded.product_title)<>'' THEN excluded.product_title ELSE accounting_order_lines.product_title END,
                ean=CASE WHEN TRIM(excluded.ean)<>'' THEN excluded.ean ELSE accounting_order_lines.ean END,
                quantity=CASE WHEN excluded.quantity>0 THEN excluded.quantity ELSE accounting_order_lines.quantity END,
                sale_original_eur=CASE
                    WHEN excluded.sale_original_eur IS NULL THEN accounting_order_lines.sale_original_eur
                    WHEN excluded.sale_original_eur=0 AND COALESCE(accounting_order_lines.sale_original_eur,0)>0 AND LOWER(excluded.raw_status) NOT LIKE '%cancel%' AND LOWER(excluded.raw_status) NOT LIKE '%refus%' AND LOWER(excluded.raw_status) NOT LIKE '%refund%' AND LOWER(excluded.raw_status) NOT LIKE '%return%'
                    THEN accounting_order_lines.sale_original_eur ELSE excluded.sale_original_eur END,
                refund_eur=excluded.refund_eur,
                sale_eur=CASE
                    WHEN excluded.sale_eur IS NULL THEN accounting_order_lines.sale_eur
                    WHEN excluded.sale_eur=0 AND COALESCE(accounting_order_lines.sale_eur,0)>0 AND LOWER(excluded.raw_status) NOT LIKE '%cancel%' AND LOWER(excluded.raw_status) NOT LIKE '%refus%' AND LOWER(excluded.raw_status) NOT LIKE '%refund%' AND LOWER(excluded.raw_status) NOT LIKE '%return%'
                    THEN accounting_order_lines.sale_eur ELSE excluded.sale_eur END,
                purchase_cost_eur=COALESCE(excluded.purchase_cost_eur,accounting_order_lines.purchase_cost_eur),
                commission_eur=CASE
                    WHEN excluded.commission_eur IS NULL THEN accounting_order_lines.commission_eur
                    WHEN excluded.commission_eur=0 AND COALESCE(accounting_order_lines.commission_eur,0)>0 AND LOWER(excluded.raw_status) NOT LIKE '%cancel%' AND LOWER(excluded.raw_status) NOT LIKE '%refus%' AND LOWER(excluded.raw_status) NOT LIKE '%refund%' AND LOWER(excluded.raw_status) NOT LIKE '%return%'
                    THEN accounting_order_lines.commission_eur ELSE excluded.commission_eur END,
                payout_eur=CASE
                    WHEN excluded.payout_eur IS NULL THEN accounting_order_lines.payout_eur
                    WHEN excluded.payout_eur=0 AND COALESCE(accounting_order_lines.payout_eur,0)>0 AND LOWER(excluded.raw_status) NOT LIKE '%cancel%' AND LOWER(excluded.raw_status) NOT LIKE '%refus%' AND LOWER(excluded.raw_status) NOT LIKE '%refund%' AND LOWER(excluded.raw_status) NOT LIKE '%return%'
                    THEN accounting_order_lines.payout_eur ELSE excluded.payout_eur END,
                cost_source=CASE WHEN excluded.purchase_cost_eur IS NOT NULL THEN excluded.cost_source ELSE accounting_order_lines.cost_source END,
                financial_source=CASE
                    WHEN LOWER(excluded.raw_status) NOT LIKE '%cancel%' AND LOWER(excluded.raw_status) NOT LIKE '%refus%' AND LOWER(excluded.raw_status) NOT LIKE '%refund%' AND LOWER(excluded.raw_status) NOT LIKE '%return%' AND (
                        (COALESCE(excluded.sale_eur,0)=0 AND COALESCE(accounting_order_lines.sale_eur,0)>0) OR
                        (COALESCE(excluded.commission_eur,0)=0 AND COALESCE(accounting_order_lines.commission_eur,0)>0) OR
                        (COALESCE(excluded.payout_eur,0)=0 AND COALESCE(accounting_order_lines.payout_eur,0)>0)
                    ) THEN accounting_order_lines.financial_source
                    WHEN excluded.sale_eur IS NOT NULL OR excluded.commission_eur IS NOT NULL OR excluded.payout_eur IS NOT NULL
                    THEN excluded.financial_source ELSE accounting_order_lines.financial_source END,
                tracking=CASE WHEN TRIM(excluded.tracking)<>'' THEN excluded.tracking ELSE accounting_order_lines.tracking END,
                customer_name=CASE WHEN TRIM(excluded.customer_name)<>'' THEN excluded.customer_name ELSE accounting_order_lines.customer_name END,
                payment_estimated=CASE WHEN TRIM(excluded.payment_estimated)<>'' THEN excluded.payment_estimated ELSE accounting_order_lines.payment_estimated END,
                note=CASE WHEN TRIM(excluded.note)<>'' THEN excluded.note ELSE accounting_order_lines.note END,
                raw_json=excluded.raw_json,synced_at=excluded.synced_at
            """,
            (
                seller_id, account_id, marketplace, clean_text(item.get("row_key")),
                clean_text(item.get("order_id")), clean_text(item.get("order_line_id")),
                clean_text(item.get("order_created")), clean_text(item.get("country_code")),
                clean_text(item.get("market_label")), clean_text(item.get("raw_status")),
                clean_text(item.get("status_label")), clean_text(item.get("supplier")),
                clean_text(item.get("composite_sku")), clean_text(item.get("product_title")),
                clean_identifier(item.get("ean")), max(1, int(item.get("quantity") or 1)),
                item.get("sale_original_eur"), float(item.get("refund_eur") or 0),
                item.get("sale_eur"), item.get("purchase_cost_eur"),
                item.get("commission_eur"), item.get("payout_eur"),
                clean_text(item.get("cost_source")), clean_text(item.get("financial_source")),
                clean_text(item.get("tracking")), clean_text(item.get("customer_name")),
                clean_text(item.get("payment_estimated")), clean_text(item.get("note")),
                json_text(item.get("raw_json") or {}), now_iso(),
            ),
        )
        if reason:
            execute(
                """
                UPDATE accounting_order_lines SET
                    sale_original_eur=0,refund_eur=0,sale_eur=0,purchase_cost_eur=0,
                    commission_eur=0,extra_cost_eur=0,payout_eur=0,
                    payment_estimated='',cost_source=?,note=?,synced_at=?
                WHERE marketplace_account_id=? AND marketplace=? AND row_key=?
                """,
                (
                    f"Non dovuto · {reason}", _zero_economic_note(item.get("note"), reason),
                    now_iso(), account_id, clean_text(marketplace).lower(),
                    clean_text(item.get("row_key")),
                ),
            )
        saved += 1
    return saved


def accounting_rows(
    seller_id: int,
    account_id: int,
    marketplace: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    clauses = [
        "seller_id=?", "marketplace_account_id=?", "marketplace=?",
    ]
    params: list[Any] = [seller_id, account_id, marketplace]
    if date_from is not None:
        clauses.append("substr(order_created,1,10)>=?")
        params.append(date_from.isoformat())
    if date_to is not None:
        clauses.append("substr(order_created,1,10)<=?")
        params.append(date_to.isoformat())
    data = rows(
        "SELECT * FROM accounting_order_lines WHERE "
        + " AND ".join(clauses)
        + " ORDER BY order_created DESC,id DESC",
        tuple(params),
    )
    data = apply_accounting_manual_overrides(data)
    return [
        _zero_economic_record(item) if _must_zero_economics(
            item.get("raw_status"), item.get("status_label"), item.get("note"),
            item.get("supplier_order_number"),
        ) else item
        for item in data
    ]



def _ean_from_raw_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return _ean_from_values(value)
    preferred_keys = {"ean", "ean13", "barcode", "gtin", "gtin13", "product_ean"}

    def visit(current: Any) -> str:
        if isinstance(current, Mapping):
            for key, child in current.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in preferred_keys:
                    found = _ean_from_values(child)
                    if found:
                        return found
            for child in current.values():
                if isinstance(child, (Mapping, list, tuple)):
                    found = visit(child)
                    if found:
                        return found
        elif isinstance(current, (list, tuple)):
            for child in current:
                found = visit(child)
                if found:
                    return found
        return ""

    return visit(value)


def _without_cost_warning(note: Any) -> str:
    parts = [clean_text(part) for part in re.split(r"\s*;\s*", clean_text(note))]
    return "; ".join(
        part for part in parts
        if part and not part.lower().startswith("costo non calcolabile")
    )


def refresh_accounting_costs(
    seller_id: int,
    account_id: int,
    marketplace: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, int]:
    """Recalculate cached order costs using exact EAN across all accessible lists."""
    catalog_selection = accounting_catalog_selection(seller_id)
    catalogs = load_supplier_catalogs(seller_id)
    records = accounting_rows(seller_id, account_id, marketplace)
    result = {
        "catalogs": len(catalogs),
        "examined": 0,
        "matched": 0,
        "missing": 0,
        "cancelled": 0,
        "preserved": 0,
    }
    for item in records:
        created = _date_time(item.get("order_created"))
        if date_from and created and created.date() < date_from:
            continue
        if date_to and created and created.date() > date_to:
            continue
        result["examined"] += 1
        raw_status = item.get("raw_status") or item.get("status_label")
        note = _without_cost_warning(item.get("note"))
        zero_reason = _zero_economics_reason(
            raw_status, item.get("status_label"), note, item.get("supplier_order_number")
        )
        if zero_reason:
            execute(
                """
                UPDATE accounting_order_lines SET
                    sale_original_eur=0,refund_eur=0,sale_eur=0,purchase_cost_eur=0,
                    commission_eur=0,extra_cost_eur=0,payout_eur=0,
                    payment_estimated='',cost_source=?,note=?,synced_at=?
                WHERE id=?
                """,
                (
                    f"Non dovuto · {zero_reason}",
                    _zero_economic_note(note, zero_reason), now_iso(), int(item["id"]),
                ),
            )
            result["cancelled"] += 1
            continue

        parsed = parse_composite_sku(item.get("composite_sku"))
        ean = _ean_from_values(
            item.get("ean"), parsed.product_code, _ean_from_raw_json(item.get("raw_json"))
        )
        cost = resolve_purchase_cost(
            catalogs,
            supplier=item.get("supplier") or parsed.supplier,
            product_code=parsed.product_code,
            ean=ean,
            country_code=item.get("country_code"),
            quantity=max(1, int(item.get("quantity") or 1)),
            composite_purchase_cost=parsed.purchase_cost,
        )
        purchase = cost.get("total_cost")
        supplier = clean_text(item.get("supplier")) or clean_text(cost.get("matched_supplier"))
        if purchase is None:
            existing_purchase = _number(item.get("purchase_cost_eur"))
            manual_cost = clean_text(item.get("cost_source")).lower() == "modifica manuale persistente"
            strict_innpro_light = _is_innpro_supplier(item.get("supplier") or parsed.supplier)
            if existing_purchase is not None and existing_purchase > 0 and (
                manual_cost or (not catalog_selection["configured"] and not strict_innpro_light)
            ):
                result["preserved"] += 1
                execute(
                    """UPDATE accounting_order_lines
                    SET ean=?,supplier=?,synced_at=? WHERE id=?""",
                    (ean, supplier, now_iso(), int(item["id"])),
                )
                continue
            result["missing"] += 1
            note = clean_text(f"{note}; {cost['source']}" if note else cost["source"])
        else:
            result["matched"] += 1
        execute(
            """
            UPDATE accounting_order_lines
            SET ean=?,supplier=?,purchase_cost_eur=?,cost_source=?,note=?,synced_at=?
            WHERE id=?
            """,
            (
                ean, supplier, purchase, clean_text(cost.get("source")), note,
                now_iso(), int(item["id"]),
            ),
        )
    return result


MAX_COMPARISON_URL_BYTES = 200 * 1024 * 1024
EXCEL_MAGIC_XLSX = b"PK\x03\x04"
EXCEL_MAGIC_XLS = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _comparison_url_with_scheme(value: Any) -> str:
    """Return a normalized HTTP(S) URL, accepting links pasted without scheme."""
    text = clean_text(value)
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Sono accettati soltanto URL HTTP o HTTPS.")
    if parsed.username or parsed.password:
        raise ValueError("L'URL non può contenere nome utente o password.")
    if not parsed.hostname:
        raise ValueError("URL non valido: dominio mancante.")
    return text


def _google_file_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host not in {"docs.google.com", "drive.google.com", "www.drive.google.com"}:
        return ""
    patterns = (
        r"/spreadsheets/d/([A-Za-z0-9_-]+)",
        r"/file/d/([A-Za-z0-9_-]+)",
        r"/document/d/([A-Za-z0-9_-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, path)
        if match:
            return match.group(1)
    query_id = (parse_qs(parsed.query).get("id") or [""])[0]
    return re.sub(r"[^A-Za-z0-9_-]", "", query_id)


def accounting_comparison_url_candidates(value: Any) -> list[str]:
    """Build download candidates for direct Excel links and Google share URLs."""
    url = _comparison_url_with_scheme(value)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    file_id = _google_file_id(url)
    candidates: list[str] = []
    if file_id and host == "docs.google.com" and "/spreadsheets/" in parsed.path:
        candidates.extend([
            f"https://docs.google.com/spreadsheets/d/{quote(file_id)}/export?format=xlsx",
            f"https://drive.google.com/uc?export=download&id={quote(file_id)}",
        ])
    elif file_id and host in {"drive.google.com", "www.drive.google.com"}:
        candidates.extend([
            f"https://drive.google.com/uc?export=download&id={quote(file_id)}",
            f"https://docs.google.com/spreadsheets/d/{quote(file_id)}/export?format=xlsx",
        ])
    candidates.append(url)
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(candidates))


def _is_public_download_host(hostname: str) -> None:
    """Block localhost/private-network targets to avoid server-side URL abuse."""
    host = clean_text(hostname).strip("[]").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Per sicurezza non sono accettati indirizzi locali o di rete privata.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        raise ValueError(f"Impossibile risolvere il dominio {host}.") from exc
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("Per sicurezza non sono accettati indirizzi locali o di rete privata.")


def _safe_public_url(value: Any) -> str:
    url = _comparison_url_with_scheme(value)
    parsed = urlparse(url)
    _is_public_download_host(parsed.hostname or "")
    return url


def _content_disposition_name(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    extended = re.search(r"filename\*=UTF-8''([^;]+)", text, re.I)
    if extended:
        return unquote(extended.group(1)).strip().strip('"')
    basic = re.search(r"filename\s*=\s*(?:\"([^\"]+)\"|([^;]+))", text, re.I)
    if basic:
        return clean_text(basic.group(1) or basic.group(2)).strip('"')
    return ""


def _excel_file_name(headers: Mapping[str, Any], final_url: str, content: bytes) -> str:
    name = _content_disposition_name(headers.get("content-disposition"))
    if not name:
        name = Path(unquote(urlparse(final_url).path)).name
    name = re.sub(r"[\\/:*?\"<>|]+", "_", clean_text(name)).strip(" .")
    suffix = Path(name).suffix.lower()
    if suffix not in {".xlsx", ".xls"}:
        suffix = ".xls" if content.startswith(EXCEL_MAGIC_XLS) else ".xlsx"
        name = f"{Path(name).stem or 'confronto_contabilita'}{suffix}"
    return name


def _excel_binary_kind(content: bytes) -> str:
    if content.startswith(EXCEL_MAGIC_XLSX):
        return "xlsx"
    if content.startswith(EXCEL_MAGIC_XLS):
        return "xls"
    return ""


def _download_public_url(
    value: Any,
    *,
    max_bytes: int = MAX_COMPARISON_URL_BYTES,
    timeout: tuple[float, float] = (12.0, 90.0),
    max_redirects: int = 6,
    accept: str = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
        "application/vnd.ms-excel,application/octet-stream;q=0.9,*/*;q=0.5"
    ),
    user_agent: str = "MarketplaceHub/1.0 (+Excel comparison import)",
) -> tuple[bytes, Mapping[str, Any], str]:
    """Download one public URL with redirect and size controls."""
    current = _safe_public_url(value)
    headers = {
        "User-Agent": user_agent,
        "Accept": accept,
    }
    with requests.Session() as session:
        for _ in range(max_redirects + 1):
            response = session.get(
                current,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = clean_text(response.headers.get("location"))
                response.close()
                if not location:
                    raise ValueError("Il server ha restituito un reindirizzamento senza destinazione.")
                current = _safe_public_url(urljoin(current, location))
                continue
            try:
                response.raise_for_status()
                length = _number(response.headers.get("content-length"), 0) or 0
                if length > max_bytes:
                    raise ValueError(
                        f"Il file supera il limite di {max_bytes // (1024 * 1024)} MB."
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(
                            f"Il file supera il limite di {max_bytes // (1024 * 1024)} MB."
                        )
                    chunks.append(chunk)
                return b"".join(chunks), dict(response.headers), current
            finally:
                response.close()
    raise ValueError("Troppi reindirizzamenti durante il download del file.")


def download_accounting_comparison_url(
    value: Any,
    *,
    max_bytes: int = MAX_COMPARISON_URL_BYTES,
) -> dict[str, Any]:
    """Download an Excel comparison workbook from a direct or Google share URL.

    Google Sheets links are exported as XLSX. Google Drive links are downloaded
    as the original file. The link must be publicly accessible or shared with
    anyone who has the link; the application does not ask for Google passwords.
    """
    url = _comparison_url_with_scheme(value)
    failures: list[str] = []
    for candidate in accounting_comparison_url_candidates(url):
        try:
            content, headers, final_url = _download_public_url(
                candidate, max_bytes=max_bytes
            )
            kind = _excel_binary_kind(content)
            if not kind:
                content_type = clean_text(headers.get("content-type")).lower()
                if "text/html" in content_type or content.lstrip().startswith((b"<", b"<!")):
                    raise ValueError(
                        "il collegamento restituisce una pagina HTML, probabilmente richiede accesso o autorizzazione"
                    )
                raise ValueError("il contenuto scaricato non è un file Excel .xlsx o .xls")
            file_name = _excel_file_name(headers, final_url, content)
            if kind == "xlsx" and not file_name.lower().endswith(".xlsx"):
                file_name = f"{Path(file_name).stem}.xlsx"
            if kind == "xls" and not file_name.lower().endswith(".xls"):
                file_name = f"{Path(file_name).stem}.xls"
            return {
                "content": content,
                "file_name": file_name,
                "input_url": url,
                "resolved_url": final_url,
                "size_bytes": len(content),
            }
        except Exception as exc:
            parsed_candidate = urlparse(candidate)
            candidate_label = parsed_candidate.hostname or "URL"
            failures.append(f"{candidate_label}: {exc}")
    detail = failures[-1] if failures else "nessun tentativo disponibile"
    raise ValueError(
        "Impossibile scaricare un file Excel dall'URL. Verifica che il link sia "
        f"pubblico/condiviso e che punti a un file .xlsx o .xls. Dettaglio: {detail}"
    )


# Column aliases accepted when an existing accounting workbook is uploaded as a
# fallback/comparison source. Header matching is accent-insensitive and ignores
# punctuation, so both the original spreadsheet and files generated by this app
# are accepted.
EXCEL_COMPARISON_HEADER_ALIASES = {
    "data": "Data",
    "date": "Data",
    "market": "Market",
    "marketplace": "Market",
    "canale": "Market",
    "num ordine market": "Num. Ordine Market",
    "numero ordine market": "Num. Ordine Market",
    "num ordine marketplace": "Num. Ordine Market",
    "numero ordine marketplace": "Num. Ordine Market",
    "ordine marketplace": "Num. Ordine Market",
    "id ordine": "Num. Ordine Market",
    "ordine": "Num. Ordine Market",
    "fornitore": "Fornitore",
    "n ordine fornitore": "N Ordine Fornitore",
    "num ordine fornitore": "N Ordine Fornitore",
    "numero ordine fornitore": "N Ordine Fornitore",
    "ordine fornitore": "N Ordine Fornitore",
    "prodotto": "Prodotto",
    "nome prodotto": "Prodotto",
    "articolo": "Prodotto",
    "sku ean": "SKU/EAN",
    "ean sku": "SKU/EAN",
    "ean": "SKU/EAN",
    "gtin": "SKU/EAN",
    "sku": "SKU/EAN",
    "vendita": "Vendita",
    "prezzo vendita": "Vendita",
    "totale vendita": "Vendita",
    "acquisto": "Acquisto",
    "costo acquisto": "Acquisto",
    "costo prodotto": "Acquisto",
    "c market": "C. Market",
    "commissione market": "C. Market",
    "commissione marketplace": "C. Market",
    "commissione": "C. Market",
    "costo extra": "Costo Extra",
    "costi extra": "Costo Extra",
    "a pagare": "a Pagare",
    "da pagare": "a Pagare",
    "da ricevere": "a Pagare",
    "payout": "a Pagare",
    "margine lordo": "Margine Lordo",
    "ricavo netto": "Ricavo Netto",
    "ricavo": "Ricavo Netto",
    "% ricavo": "% Ricavo",
    "percentuale ricavo": "% Ricavo",
    "tracciabilita e corriere": "Tracciabilità e Corriere",
    "tracciabilita corriere": "Tracciabilità e Corriere",
    "tracking e corriere": "Tracciabilità e Corriere",
    "tracking": "Tracciabilità e Corriere",
    "nome cliente": "Nome Cliente",
    "cliente": "Nome Cliente",
    "scontrino": "SCONTRINO",
    "pagato": "PAGATO",
    "pagamento stimato": "PAGATO",
    "data pagamento": "PAGATO",
    "stato ordine": "Stato Ordine",
    "stato": "Stato Ordine",
    "quantita": "Quantità",
    "q ta": "Quantità",
    "qty": "Quantità",
    "rimborso": "Rimborso",
    "rimborsi": "Rimborso",
    "note contabili": "Note contabili",
    "note": "Note contabili",
}

EXCEL_COMPARISON_FIELD_SPECS = (
    ("Data", "order_created", "date"),
    ("Market", "market_label", "text"),
    ("Fornitore", "supplier", "text"),
    ("N Ordine Fornitore", "supplier_order_number", "text"),
    ("Prodotto", "product_title", "text"),
    ("SKU/EAN", "ean", "ean"),
    ("Vendita", "sale_eur", "money"),
    ("Acquisto", "purchase_cost_eur", "money"),
    ("C. Market", "commission_eur", "money"),
    ("Costo Extra", "extra_cost_eur", "money"),
    ("a Pagare", "payout_eur", "money"),
    ("Tracciabilità e Corriere", "tracking", "text"),
    ("Nome Cliente", "customer_name", "text"),
    ("SCONTRINO", "receipt", "text"),
    ("PAGATO", "payment_estimated", "date"),
    ("Stato Ordine", "status_label", "text"),
    ("Quantità", "quantity", "integer"),
    ("Rimborso", "refund_eur", "money"),
    ("Note contabili", "note", "text"),
)

EXCEL_COMPARISON_ALLOWED_DB_FIELDS = {
    field for _, field, _ in EXCEL_COMPARISON_FIELD_SPECS
} | {"country_code", "sale_original_eur"}


def _normalized_excel_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9%]+", " ", text.lower()).strip()


def _canonical_excel_header(value: Any) -> str:
    key = _normalized_excel_key(value)
    if key in EXCEL_COMPARISON_HEADER_ALIASES:
        return EXCEL_COMPARISON_HEADER_ALIASES[key]
    for column in EXPORT_COLUMNS:
        if _normalized_excel_key(column) == key:
            return column
    return ""


def _excel_engine(file_name: str) -> str | None:
    return "xlrd" if Path(clean_text(file_name)).suffix.lower() == ".xls" else None


def accounting_excel_sheet_names(content: bytes, file_name: str) -> list[str]:
    if not content:
        return []
    book = pd.ExcelFile(io.BytesIO(content), engine=_excel_engine(file_name))
    try:
        return [clean_text(name) for name in book.sheet_names]
    finally:
        close = getattr(book, "close", None)
        if callable(close):
            close()


def _excel_number(value: Any, *, percent: bool = False) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return number / 100.0 if percent and abs(number) > 1 else number
    text = clean_text(value)
    if not text or text.startswith("#"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace("\u00a0", " ").replace("€", "").replace("%", "")
    text = re.sub(r"[^0-9,.-]", "", text)
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    if negative:
        number = -abs(number)
    if not math.isfinite(number):
        return None
    return number / 100.0 if percent and abs(number) > 1 else number


def _excel_date_iso(value: Any, reference_year: int | None = None) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        # Excel's serial-date origin, including the historic leap-year quirk.
        try:
            parsed = pd.Timestamp("1899-12-30") + pd.to_timedelta(float(value), unit="D")
            return parsed.date().isoformat()
        except Exception:
            return ""
    text = clean_text(value)
    if not text:
        return ""
    short = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", text)
    if short:
        year = reference_year or date.today().year
        try:
            return date(year, int(short.group(2)), int(short.group(1))).isoformat()
        except ValueError:
            return ""
    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date().isoformat()
    except ValueError:
        pass
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return ""
    return parsed.date().isoformat()


def _excel_identifier(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(float(value)) and float(value).is_integer():
                return str(int(float(value)))
        except Exception:
            pass
    return clean_identifier(value)


def _excel_ean(value: Any) -> str:
    return _ean_from_values(_excel_identifier(value))


def _normalized_order_id(value: Any) -> str:
    return re.sub(r"\s+", "", clean_text(value)).upper()


def _normalized_product(value: Any) -> str:
    return _normalized_excel_key(value)


def _excel_marketplace(value: Any) -> str:
    key = _normalized_excel_key(value)
    if "kaufland" in key:
        return "kaufland"
    if "worten" in key:
        return "worten"
    return ""


def _country_from_market_label(value: Any) -> str:
    text = clean_text(value).upper()
    match = re.search(r"(?:^|\s)([A-Z]{2})$", text)
    return normalize_country_code(match.group(1)) if match else ""


def _looks_like_tracking(value: Any) -> bool:
    text = clean_text(value)
    if not text:
        return False
    key = _normalized_excel_key(text)
    carriers = {
        "ups", "gls", "dhl", "dpd", "ctt", "correos", "chronopost",
        "hermes", "poste", "brt", "bartolini", "seur", "mrw", "fedex",
        "tnt", "colissimo", "inpost", "packeta", "mondial relay",
    }
    return bool(re.search(r"\d", text)) or any(carrier in key for carrier in carriers)


def read_accounting_comparison_excel(
    content: bytes,
    file_name: str,
    sheet_name: str | None = None,
) -> dict[str, Any]:
    """Read an original/generated accounting workbook and locate its header row."""
    if not content:
        raise ValueError("Il file Excel è vuoto.")
    sheets = accounting_excel_sheet_names(content, file_name)
    if not sheets:
        raise ValueError("Il file Excel non contiene fogli leggibili.")
    selected_sheet = clean_text(sheet_name) or sheets[0]
    if selected_sheet not in sheets:
        raise ValueError(f"Il foglio {selected_sheet!r} non esiste nel file.")
    raw = pd.read_excel(
        io.BytesIO(content), sheet_name=selected_sheet, header=None, dtype=object,
        engine=_excel_engine(file_name),
    )
    if raw.empty:
        raise ValueError("Il foglio selezionato è vuoto.")

    best_row = -1
    best_score = -1
    best_headers: list[str] = []
    for index in range(min(30, len(raw))):
        headers = [_canonical_excel_header(value) for value in raw.iloc[index].tolist()]
        score = len({header for header in headers if header})
        has_order = "Num. Ordine Market" in headers
        if has_order:
            score += 4
        if "Vendita" in headers:
            score += 1
        if score > best_score:
            best_row, best_score, best_headers = index, score, headers
    if best_row < 0 or "Num. Ordine Market" not in best_headers or best_score < 5:
        raise ValueError(
            "Non trovo l’intestazione contabile. Il file deve contenere almeno "
            "‘Num. Ordine Market’ e le colonne del prospetto ordini."
        )

    data = raw.iloc[best_row + 1:].copy()
    output: dict[str, pd.Series] = {}
    for canonical in EXPORT_COLUMNS:
        positions = [position for position, header in enumerate(best_headers) if header == canonical]
        if not positions:
            continue
        combined = data.iloc[:, positions[0]].copy()
        for position in positions[1:]:
            blank = combined.map(lambda value: clean_text(value) == "")
            combined = combined.where(~blank, data.iloc[:, position])
        output[canonical] = combined
    frame = pd.DataFrame(output)
    for canonical in EXPORT_COLUMNS:
        if canonical not in frame:
            frame[canonical] = None
    frame = frame[EXPORT_COLUMNS]
    frame.insert(0, "_excel_row", [best_row + 2 + index for index in range(len(frame))])
    useful = frame.drop(columns=["_excel_row"]).apply(
        lambda row: any(clean_text(value) for value in row), axis=1,
    )
    frame = frame.loc[useful].reset_index(drop=True)
    frame = frame[frame["Num. Ordine Market"].map(_normalized_order_id).ne("")].reset_index(drop=True)
    return {
        "frame": frame,
        "sheet_names": sheets,
        "sheet_name": selected_sheet,
        "header_row": best_row + 1,
        "rows": len(frame),
    }


def _incoming_excel_value(column: str, kind: str, value: Any, reference_year: int | None) -> Any:
    if kind == "money":
        return _excel_number(value)
    if kind == "integer":
        number = _excel_number(value)
        return max(1, int(round(number))) if number is not None and number > 0 else None
    if kind == "date":
        return _excel_date_iso(value, reference_year)
    if kind == "ean":
        return _excel_ean(value)
    return clean_text(value)


def _value_is_missing(value: Any, kind: str) -> bool:
    if kind in {"text", "date", "ean"}:
        return clean_text(value) == ""
    number = _number(value)
    return number is None


def _current_field_is_missing(
    field: str,
    kind: str,
    current: Any,
    incoming: Any,
    record: Mapping[str, Any],
) -> bool:
    if _value_is_missing(current, kind):
        return True
    if kind not in {"money", "integer"}:
        return False
    current_number = _number(current)
    incoming_number = _number(incoming)
    if current_number is None:
        return True
    if incoming_number is None or incoming_number <= 0 or current_number != 0:
        return False
    status = record.get("raw_status") or record.get("status_label")
    zero_locked = _must_zero_economics(
        status, record.get("status_label"), record.get("note"),
        record.get("supplier_order_number"),
    )
    cancelled = _is_cancelled(status)
    returned = _is_returned_or_refunded(status)
    if zero_locked and field in ZERO_ECONOMIC_DB_FIELDS:
        return False
    if field == "extra_cost_eur":
        return True
    if field == "purchase_cost_eur":
        return not cancelled
    if field in {"sale_eur", "commission_eur", "payout_eur"}:
        return not cancelled and not returned
    if field == "refund_eur":
        return returned
    if field == "quantity":
        return current_number <= 0
    return False


def _comparison_values_equal(kind: str, current: Any, incoming: Any) -> bool:
    if kind in {"money", "integer"}:
        left = _number(current)
        right = _number(incoming)
        if left is None or right is None:
            return left is None and right is None
        tolerance = 0.01 if kind == "money" else 0.0
        return abs(float(left) - float(right)) <= tolerance
    if kind == "date":
        left = _excel_date_iso(current)
        right = _excel_date_iso(incoming)
        return left == right
    if kind == "ean":
        return _excel_ean(current) == _excel_ean(incoming)
    return _normalized_excel_key(current) == _normalized_excel_key(incoming)


def _display_comparison_value(value: Any, kind: str) -> Any:
    if kind == "money":
        number = _number(value)
        return None if number is None else round(float(number), 2)
    if kind == "integer":
        number = _number(value)
        return None if number is None else int(round(number))
    return clean_text(value)


def compare_accounting_with_excel(
    records: Iterable[Mapping[str, Any]],
    excel_frame: pd.DataFrame,
    marketplace: str,
) -> dict[str, Any]:
    """Compare cached API rows with Excel and prepare fill-only updates."""
    cached = [dict(item) for item in records]
    selected_marketplace = clean_text(marketplace).lower()
    by_order: dict[str, list[dict[str, Any]]] = {}
    for item in cached:
        order_key = _normalized_order_id(item.get("order_id"))
        if order_key:
            by_order.setdefault(order_key, []).append(item)
    for values in by_order.values():
        values.sort(key=lambda item: (clean_text(item.get("order_line_id")), clean_text(item.get("row_key"))))

    consumed: set[str] = set()
    updates_by_key: dict[str, dict[str, Any]] = {}
    fill_events: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    row_results: list[dict[str, Any]] = []
    matched_rows = skipped_marketplace = ambiguous_rows = unchanged_fields = 0

    for _, excel_row in excel_frame.iterrows():
        excel_row_number = int(excel_row.get("_excel_row") or 0)
        order_id = clean_text(excel_row.get("Num. Ordine Market"))
        order_key = _normalized_order_id(order_id)
        excel_market = _excel_marketplace(excel_row.get("Market"))
        excel_ean = _excel_ean(excel_row.get("SKU/EAN"))
        excel_product = clean_text(excel_row.get("Prodotto"))
        excel_product_key = _normalized_product(excel_product)
        excel_zero_reason = _zero_economics_reason(
            excel_row.get("Stato Ordine"), excel_row.get("Note contabili"),
            excel_row.get("N Ordine Fornitore"),
        )
        base_result = {
            "Riga Excel": excel_row_number,
            "Ordine": order_id,
            "EAN Excel": excel_ean or _excel_identifier(excel_row.get("SKU/EAN")),
            "Prodotto Excel": excel_product,
        }
        if excel_market and excel_market != selected_marketplace:
            skipped_marketplace += 1
            result = {**base_result, "Esito": "Marketplace diverso", "Metodo": "", "Dettaglio": clean_text(excel_row.get("Market"))}
            unmatched.append(result)
            row_results.append(result)
            continue
        candidates = [
            item for item in by_order.get(order_key, [])
            if clean_text(item.get("row_key")) not in consumed
        ]
        selected: dict[str, Any] | None = None
        method = ""
        if excel_ean:
            exact = [item for item in candidates if _excel_ean(item.get("ean")) == excel_ean]
            if len(exact) == 1:
                selected, method = exact[0], "Ordine + EAN"
            elif len(exact) > 1:
                product_exact = [
                    item for item in exact
                    if excel_product_key and _normalized_product(item.get("product_title")) == excel_product_key
                ]
                selected = product_exact[0] if product_exact else exact[0]
                method = "Ordine + EAN (occorrenza)"
        if selected is None and excel_product_key:
            product_exact = [
                item for item in candidates
                if _normalized_product(item.get("product_title")) == excel_product_key
            ]
            if len(product_exact) == 1:
                selected, method = product_exact[0], "Ordine + prodotto"
        if selected is None and len(candidates) == 1:
            selected, method = candidates[0], "Ordine univoco"
        if selected is None:
            esito = "Ordine non trovato" if not by_order.get(order_key) else "Abbinamento ambiguo"
            if esito == "Abbinamento ambiguo":
                ambiguous_rows += 1
            result = {
                **base_result, "Esito": esito, "Metodo": "",
                "Dettaglio": f"{len(candidates)} righe disponibili per l’ordine",
            }
            unmatched.append(result)
            row_results.append(result)
            continue

        row_key = clean_text(selected.get("row_key"))
        consumed.add(row_key)
        matched_rows += 1
        updates = updates_by_key.setdefault(row_key, {
            "row_key": row_key,
            "marketplace_account_id": int(selected.get("marketplace_account_id") or 0),
            "marketplace": clean_text(selected.get("marketplace")).lower(),
            "order_id": clean_text(selected.get("order_id")),
            "fields": {},
            "field_labels": [],
        })
        reference_date = _date_time(selected.get("order_created"))
        reference_year = reference_date.year if reference_date else None
        zero_reason = excel_zero_reason or _zero_economics_reason(
            selected.get("raw_status"), selected.get("status_label"), selected.get("note"),
            selected.get("supplier_order_number"),
        )
        row_fill_count = row_conflict_count = 0
        if zero_reason:
            for economic_field in ZERO_ECONOMIC_DB_FIELDS:
                updates["fields"][economic_field] = 0.0
            updates["fields"]["payment_estimated"] = ""
            updates["fields"]["cost_source"] = f"Non dovuto · {zero_reason}"
            updates["fields"]["note"] = _zero_economic_note(
                excel_row.get("Note contabili") or selected.get("note"), zero_reason,
            )
            updates["field_labels"].append("Valori economici azzerati")
            fill_events.append({
                "Riga Excel": excel_row_number,
                "Ordine": clean_text(selected.get("order_id")),
                "EAN": excel_ean or clean_text(selected.get("ean")),
                "Prodotto": clean_text(selected.get("product_title")) or excel_product,
                "Campo": "Tutti i valori economici",
                "Valore programma": "",
                "Valore Excel": "0",
                "Metodo match": method,
                "row_key": row_key,
                "Azione": f"Azzeramento obbligatorio: {zero_reason}",
            })
            row_fill_count += 1
        legacy_tracking_value = clean_text(excel_row.get("Tracciabilità e Corriere"))
        legacy_customer = ""
        if (
            legacy_tracking_value
            and not clean_text(excel_row.get("Nome Cliente"))
            and not _looks_like_tracking(legacy_tracking_value)
        ):
            # Some older workbooks placed the customer name one column to the
            # left, under Tracking. Recover it without creating a false tracking.
            legacy_customer = legacy_tracking_value
        for column, field, kind in EXCEL_COMPARISON_FIELD_SPECS:
            if zero_reason and field in ZERO_ECONOMIC_DB_FIELDS:
                continue
            source_value = excel_row.get(column)
            if column == "Tracciabilità e Corriere" and legacy_customer:
                source_value = None
            elif column == "Nome Cliente" and legacy_customer and not clean_text(source_value):
                source_value = legacy_customer
            incoming = _incoming_excel_value(column, kind, source_value, reference_year)
            if _value_is_missing(incoming, kind):
                continue
            current = selected.get(field)
            event_base = {
                "Riga Excel": excel_row_number,
                "Ordine": clean_text(selected.get("order_id")),
                "EAN": excel_ean or clean_text(selected.get("ean")),
                "Prodotto": clean_text(selected.get("product_title")) or excel_product,
                "Campo": column,
                "Valore programma": _display_comparison_value(current, kind),
                "Valore Excel": _display_comparison_value(incoming, kind),
                "Metodo match": method,
                "row_key": row_key,
            }
            if _current_field_is_missing(field, kind, current, incoming, selected):
                updates["fields"][field] = incoming
                if column not in updates["field_labels"]:
                    updates["field_labels"].append(column)
                fill_events.append({**event_base, "Azione": "Integra dal file Excel"})
                row_fill_count += 1
            elif _comparison_values_equal(kind, current, incoming):
                unchanged_fields += 1
            else:
                conflicts.append({**event_base, "Azione": "Mantieni il valore del programma"})
                row_conflict_count += 1

        # The country can be recovered from labels such as "Kaufland DE" even
        # though the original workbook has no dedicated country column.
        excel_country = _country_from_market_label(excel_row.get("Market"))
        if excel_country and not clean_text(selected.get("country_code")):
            updates["fields"]["country_code"] = excel_country
            updates["field_labels"].append("Nazione da Market")
            fill_events.append({
                "Riga Excel": excel_row_number, "Ordine": clean_text(selected.get("order_id")),
                "EAN": excel_ean or clean_text(selected.get("ean")),
                "Prodotto": clean_text(selected.get("product_title")) or excel_product,
                "Campo": "Nazione", "Valore programma": "", "Valore Excel": excel_country,
                "Metodo match": method, "row_key": row_key, "Azione": "Integra dal file Excel",
            })
            row_fill_count += 1

        # Compare spreadsheet-derived columns as a diagnostic only. They are
        # always recalculated by Marketplace Hub and are never imported.
        derived = computed_values(selected)
        derived_specs = (
            ("Margine Lordo", derived.get("gross_margin_eur"), "money"),
            ("Ricavo Netto", derived.get("net_revenue_eur"), "money"),
            ("% Ricavo", derived.get("revenue_pct"), "percent"),
        )
        for column, current, kind in derived_specs:
            incoming = _excel_number(excel_row.get(column), percent=(kind == "percent"))
            if incoming is None or current is None:
                continue
            tolerance = 0.01 if kind == "money" else 0.0001
            if abs(float(current) - float(incoming)) > tolerance:
                conflicts.append({
                    "Riga Excel": excel_row_number, "Ordine": clean_text(selected.get("order_id")),
                    "EAN": excel_ean or clean_text(selected.get("ean")),
                    "Prodotto": clean_text(selected.get("product_title")) or excel_product,
                    "Campo": column, "Valore programma": round(float(current), 6),
                    "Valore Excel": round(float(incoming), 6), "Metodo match": method,
                    "row_key": row_key, "Azione": "Differenza calcolata: non importata",
                })
                row_conflict_count += 1
        row_result = {
            **base_result,
            "Esito": "Abbinato",
            "Metodo": method,
            "Dettaglio": f"{row_fill_count} campi integrabili · {row_conflict_count} differenze",
        }
        row_results.append(row_result)

    updates = [item for item in updates_by_key.values() if item.get("fields")]
    return {
        "summary": {
            "excel_rows": int(len(excel_frame)),
            "matched_rows": matched_rows,
            "update_rows": len(updates),
            "fillable_fields": len(fill_events),
            "conflict_fields": len(conflicts),
            "unmatched_rows": len(unmatched),
            "ambiguous_rows": ambiguous_rows,
            "skipped_marketplace": skipped_marketplace,
            "unchanged_fields": unchanged_fields,
        },
        "updates": updates,
        "fills": fill_events,
        "conflicts": conflicts,
        "unmatched": unmatched,
        "rows": row_results,
    }


def _append_source(current: Any, source: str) -> str:
    current_text = clean_text(current)
    source_text = clean_text(source)
    if not current_text:
        return source_text
    if source_text.lower() in current_text.lower():
        return current_text
    return f"{current_text} · {source_text}"


def apply_accounting_excel_updates(
    seller_id: int,
    account_id: int,
    marketplace: str,
    updates: Iterable[Mapping[str, Any]],
    *,
    file_name: str,
    sheet_name: str,
    comparison_summary: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Apply only still-missing values and keep a permanent import audit."""
    source = f"File Excel confronto · {Path(clean_text(file_name)).name} · foglio {clean_text(sheet_name)}"
    update_items = list(updates)
    updated_rows = filled_fields = skipped_fields = 0
    with connect() as con:
        for item in update_items:
            row_key = clean_text(item.get("row_key"))
            if not row_key:
                continue
            current_row = con.execute(
                """SELECT * FROM accounting_order_lines
                WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? AND row_key=?""",
                (seller_id, account_id, clean_text(marketplace).lower(), row_key),
            ).fetchone()
            if current_row is None:
                continue
            current = dict(current_row)
            accepted: dict[str, Any] = {}
            labels: list[str] = []
            fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
            zero_reason = _zero_economics_reason(
                current.get("raw_status"), current.get("status_label"), current.get("note"),
                current.get("supplier_order_number"), fields.get("status_label"),
                fields.get("note"), fields.get("supplier_order_number"),
            )
            if zero_reason:
                accepted.update({field: 0.0 for field in ZERO_ECONOMIC_DB_FIELDS})
                accepted["payment_estimated"] = ""
                accepted["cost_source"] = f"Non dovuto · {zero_reason}"
                accepted["note"] = _zero_economic_note(
                    fields.get("note") or current.get("note"), zero_reason,
                )
                labels.append("Valori economici azzerati")
            label_map = {
                field: column for column, field, _ in EXCEL_COMPARISON_FIELD_SPECS
            }
            kind_map = {
                field: kind for _, field, kind in EXCEL_COMPARISON_FIELD_SPECS
            }
            for field, incoming in fields.items():
                if zero_reason and (field in ZERO_ECONOMIC_DB_FIELDS or field == "payment_estimated"):
                    continue
                if field not in EXCEL_COMPARISON_ALLOWED_DB_FIELDS:
                    continue
                kind = kind_map.get(field, "text")
                if field == "country_code":
                    if not clean_text(current.get(field)) and clean_text(incoming):
                        accepted[field] = normalize_country_code(incoming)
                        labels.append("Nazione")
                    else:
                        skipped_fields += 1
                    continue
                if _current_field_is_missing(field, kind, current.get(field), incoming, current):
                    accepted[field] = incoming
                    labels.append(label_map.get(field, field))
                else:
                    skipped_fields += 1
            if not accepted:
                continue
            if "sale_eur" in accepted and _number(current.get("sale_original_eur")) is None:
                accepted["sale_original_eur"] = accepted["sale_eur"]
            if "purchase_cost_eur" in accepted and not zero_reason:
                accepted["cost_source"] = source
            financial_fields = {
                "sale_eur", "sale_original_eur", "commission_eur", "payout_eur", "refund_eur",
            }
            if financial_fields.intersection(accepted):
                financial_source = source if not zero_reason else f"Azzeramento automatico · {zero_reason}"
                accepted["financial_source"] = _append_source(current.get("financial_source"), financial_source)
            accepted["synced_at"] = now_iso()
            columns = list(accepted)
            placeholders = ",".join(f"{column}=?" for column in columns)
            con.execute(
                f"UPDATE accounting_order_lines SET {placeholders} WHERE id=?",
                tuple(accepted[column] for column in columns) + (int(current["id"]),),
            )
            updated_rows += 1
            filled_fields += len([field for field in accepted if field not in {"cost_source", "financial_source", "synced_at", "sale_original_eur"}])

        summary = dict(comparison_summary or {})
        con.execute(
            """INSERT INTO accounting_excel_imports(
                seller_id,marketplace_account_id,marketplace,file_name,sheet_name,
                source_rows,matched_rows,updated_rows,filled_fields,conflicts,
                unmatched_rows,ambiguous_rows,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                seller_id, account_id, clean_text(marketplace).lower(),
                Path(clean_text(file_name)).name, clean_text(sheet_name),
                int(summary.get("excel_rows") or 0), int(summary.get("matched_rows") or 0),
                updated_rows, filled_fields, int(summary.get("conflict_fields") or 0),
                int(summary.get("unmatched_rows") or 0), int(summary.get("ambiguous_rows") or 0),
                json_text({"summary": summary, "requested_updates": len(update_items), "skipped_fields": skipped_fields}),
                now_iso(),
            ),
        )
    return {"updated_rows": updated_rows, "filled_fields": filled_fields, "skipped_fields": skipped_fields}


def accounting_excel_import_history(
    seller_id: int, account_id: int, marketplace: str,
) -> list[dict[str, Any]]:
    return rows(
        """SELECT * FROM accounting_excel_imports
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
        ORDER BY id DESC LIMIT 50""",
        (seller_id, account_id, clean_text(marketplace).lower()),
    )


def accounting_excel_comparison_report_bytes(comparison: Mapping[str, Any]) -> bytes:
    """Create a compact workbook containing fills, conflicts and unmatched rows."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        sections = (
            ("Dati da integrare", comparison.get("fills") or []),
            ("Differenze", comparison.get("conflicts") or []),
            ("Non abbinati", comparison.get("unmatched") or []),
            ("Righe confrontate", comparison.get("rows") or []),
        )
        for name, values in sections:
            frame = pd.DataFrame(values).drop(columns=["row_key"], errors="ignore")
            if frame.empty:
                frame = pd.DataFrame({"Esito": ["Nessun dato"]})
            frame.to_excel(writer, sheet_name=name[:31], index=False)
            worksheet = writer.sheets[name[:31]]
            worksheet.freeze_panes(1, 0)
            worksheet.autofilter(0, 0, max(1, len(frame)), max(0, len(frame.columns) - 1))
            for index, column in enumerate(frame.columns):
                width = min(45, max(12, len(str(column)) + 2))
                if len(frame):
                    width = min(45, max(width, int(frame[column].astype(str).map(len).quantile(0.90)) + 2))
                worksheet.set_column(index, index, width)
    return buffer.getvalue()

def save_manual_fields(changes: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    with connect() as con:
        for item in changes:
            row_key = clean_text(item.get("row_key"))
            account_id = int(item.get("marketplace_account_id") or item.get("account_id") or 0)
            marketplace = clean_text(item.get("marketplace")).lower()
            if not row_key or not account_id or not marketplace:
                continue
            current_row = con.execute(
                """SELECT * FROM accounting_order_lines
                WHERE marketplace_account_id=? AND marketplace=? AND row_key=?""",
                (account_id, marketplace, row_key),
            ).fetchone()
            if current_row is None:
                continue
            current = dict(current_row)
            supplier_order_number = clean_text(item.get("supplier_order_number"))
            reason = _zero_economics_reason(
                current.get("raw_status"), current.get("status_label"), current.get("note"),
                supplier_order_number or current.get("supplier_order_number"),
            )
            extra_cost = 0.0 if reason else float(item.get("extra_cost_eur") or 0)
            if reason:
                con.execute(
                    """UPDATE accounting_order_lines SET
                        supplier_order_number=?,receipt=?,extra_cost_eur=0,
                        sale_original_eur=0,refund_eur=0,sale_eur=0,purchase_cost_eur=0,
                        commission_eur=0,payout_eur=0,payment_estimated='',
                        cost_source=?,note=?,synced_at=?
                    WHERE marketplace_account_id=? AND marketplace=? AND row_key=?""",
                    (
                        supplier_order_number, clean_text(item.get("receipt")),
                        f"Non dovuto · {reason}",
                        _zero_economic_note(current.get("note"), reason), now_iso(),
                        account_id, marketplace, row_key,
                    ),
                )
            else:
                con.execute(
                    """UPDATE accounting_order_lines SET
                        supplier_order_number=?,extra_cost_eur=?,receipt=?
                    WHERE marketplace_account_id=? AND marketplace=? AND row_key=?""",
                    (
                        supplier_order_number, extra_cost, clean_text(item.get("receipt")),
                        account_id, marketplace, row_key,
                    ),
                )
            count += 1
    return count


def computed_values(item: Mapping[str, Any]) -> dict[str, float | None]:
    if _must_zero_economics(
        item.get("raw_status"), item.get("status_label"), item.get("note"),
        item.get("supplier_order_number"),
    ):
        return {"gross_margin_eur": 0.0, "net_revenue_eur": 0.0, "revenue_pct": 0.0}
    purchase = _number(item.get("purchase_cost_eur"))
    payout = _number(item.get("payout_eur"))
    extra = _number(item.get("extra_cost_eur"), 0.0) or 0.0
    if purchase is None or payout is None:
        return {"gross_margin_eur": None, "net_revenue_eur": None, "revenue_pct": None}
    gross = round(payout - purchase, 2)
    net = round(gross - extra, 2)
    pct = round(net / purchase, 6) if purchase > 0 else None
    return {"gross_margin_eur": gross, "net_revenue_eur": net, "revenue_pct": pct}


def computed_profit_values(
    item: Mapping[str, Any],
    our_profit_pct: float = 0.0,
    partner_profit_pct: float = 100.0,
) -> dict[str, float | None]:
    """Return accounting formulas plus the Seller/BEBOL split for one row.

    This is intentionally derived from the authoritative economic fields every time
    instead of persisting formula results. Manual edits can therefore update margin
    and both shares immediately without creating stale calculated values in the DB.
    """
    result = computed_values(item)
    net = result["net_revenue_eur"]
    if net is None:
        return {
            **result,
            "our_share_eur": None,
            "partner_share_eur": None,
        }
    split = split_profit(net, our_profit_pct, partner_profit_pct)
    return {
        **result,
        "our_share_eur": split["our_amount"],
        "partner_share_eur": split["partner_amount"],
    }


def export_frame(
    records: Iterable[Mapping[str, Any]],
    *,
    our_profit_pct: float = 0.0,
    partner_profit_pct: float = 100.0,
) -> pd.DataFrame:
    output: list[dict[str, Any]] = []
    our_pct, partner_pct = normalized_percentages(our_profit_pct, partner_profit_pct)
    for source_item in records:
        item = _zero_economic_record(source_item) if _must_zero_economics(
            source_item.get("raw_status"), source_item.get("status_label"),
            source_item.get("note"), source_item.get("supplier_order_number"),
        ) else dict(source_item)
        computed = computed_values(item)
        if computed["net_revenue_eur"] is None:
            profit_split = {"our_amount": None, "partner_amount": None}
        else:
            profit_split = split_profit(
                computed["net_revenue_eur"], our_pct, partner_pct
            )
        created = _date_time(item.get("order_created"))
        paid = clean_text(item.get("payment_estimated"))
        paid_date = date.fromisoformat(paid) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", paid) else None
        output.append({
            "Data": created.date() if created else None,
            "Market": clean_text(item.get("market_label")),
            "Num. Ordine Market": clean_text(item.get("order_id")),
            "Fornitore": clean_text(item.get("supplier")).title(),
            "N Ordine Fornitore": clean_text(item.get("supplier_order_number")),
            "Prodotto": clean_text(item.get("product_title")),
            "SKU/EAN": clean_identifier(item.get("ean")),
            "Vendita": _number(item.get("sale_eur")),
            "Acquisto": _number(item.get("purchase_cost_eur")),
            "C. Market": _number(item.get("commission_eur")),
            "Costo Extra": _number(item.get("extra_cost_eur"), 0.0) or 0.0,
            "a Pagare": _number(item.get("payout_eur")),
            "Margine Lordo": computed["gross_margin_eur"],
            "Ricavo Netto": computed["net_revenue_eur"],
            "% Ricavo": computed["revenue_pct"],
            "Tracciabilità e Corriere": clean_text(item.get("tracking")),
            "Nome Cliente": clean_text(item.get("customer_name")),
            "SCONTRINO": clean_text(item.get("receipt")),
            "PAGATO": paid_date,
            "Stato Ordine": clean_text(item.get("status_label")),
            "Quantità": max(1, int(item.get("quantity") or 1)),
            "Rimborso": _number(item.get("refund_eur"), 0.0) or 0.0,
            "Note contabili": clean_text(item.get("note")),
            "Nostra quota %": our_pct / 100.0,
            "Nostro guadagno": profit_split["our_amount"],
            "Quota partner %": partner_pct / 100.0,
            "Guadagno partner": profit_split["partner_amount"],
        })
    return pd.DataFrame(output, columns=EXPORT_COLUMNS)


def export_xlsx_bytes(
    records: Iterable[Mapping[str, Any]],
    sheet_name: str = "Contabilità",
    *,
    our_profit_pct: float = 0.0,
    partner_profit_pct: float = 100.0,
    partner_name: str = "Partner",
) -> bytes:
    our_pct, partner_pct = normalized_percentages(our_profit_pct, partner_profit_pct)
    frame = export_frame(
        records,
        our_profit_pct=our_pct,
        partner_profit_pct=partner_pct,
    )
    partner_label = clean_text(partner_name) or "Partner"
    buffer = io.BytesIO()
    try:
        import xlsxwriter
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Manca la dipendenza XlsxWriter.") from exc
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
    worksheet = workbook.add_worksheet(sheet_name[:31])
    workbook.set_properties({
        "title": "Contabilità ordini Marketplace Hub",
        "subject": "Ordini, costi, commissioni e ricavi",
        "company": "Marketplace Hub",
    })
    header = workbook.add_format({
        "bold": True, "bg_color": "#D9EAD3", "border": 1,
        "align": "center", "valign": "vcenter", "text_wrap": True,
    })
    text = workbook.add_format({"border": 1, "valign": "top", "num_format": "@"})
    center = workbook.add_format({"border": 1, "align": "center", "valign": "top", "num_format": "@"})
    date_fmt = workbook.add_format({"border": 1, "num_format": "dd/mm/yyyy", "align": "center"})
    money = workbook.add_format({"border": 1, "num_format": '€ #,##0.00;[Red]-€ #,##0.00', "align": "right"})
    percent = workbook.add_format({"border": 1, "num_format": "0.00%;[Red]-0.00%", "align": "right"})
    integer = workbook.add_format({"border": 1, "align": "center", "num_format": "0"})
    manual = workbook.add_format({"border": 1, "bg_color": "#FFF2CC", "valign": "top", "num_format": "@"})
    note_fmt = workbook.add_format({"border": 1, "font_color": "#666666", "text_wrap": True, "valign": "top", "num_format": "@"})
    summary_header = workbook.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "#FFFFFF", "border": 1})
    summary_money = workbook.add_format({"bold": True, "num_format": '€ #,##0.00;[Red]-€ #,##0.00', "border": 1})

    display_headers = {
        "Quota partner %": f"Quota {partner_label} %",
        "Guadagno partner": f"Guadagno {partner_label}",
    }
    for col, name in enumerate(EXPORT_COLUMNS):
        worksheet.write(0, col, display_headers.get(name, name), header)
    worksheet.set_row(0, 32)

    money_columns = {
        "Vendita", "Acquisto", "C. Market", "Costo Extra", "a Pagare",
        "Margine Lordo", "Ricavo Netto", "Rimborso", "Nostro guadagno",
        "Guadagno partner",
    }
    manual_columns = {"N Ordine Fornitore", "SCONTRINO"}
    for row_index, row in frame.iterrows():
        excel_row = row_index + 1
        for col_index, name in enumerate(EXPORT_COLUMNS):
            value = row[name]
            if name == "Data" or name == "PAGATO":
                if isinstance(value, date):
                    worksheet.write_datetime(excel_row, col_index, datetime.combine(value, datetime.min.time()), date_fmt)
                else:
                    worksheet.write_blank(excel_row, col_index, None, date_fmt)
            elif name == "Margine Lordo":
                cached = None if value is None or (isinstance(value, float) and math.isnan(value)) else float(value)
                worksheet.write_formula(
                    excel_row, col_index,
                    f'=IF(OR(L{excel_row + 1}="",I{excel_row + 1}=""),"",L{excel_row + 1}-I{excel_row + 1})',
                    money, cached if cached is not None else "",
                )
            elif name == "Ricavo Netto":
                cached = None if value is None or (isinstance(value, float) and math.isnan(value)) else float(value)
                worksheet.write_formula(
                    excel_row, col_index,
                    f'=IF(M{excel_row + 1}="","",M{excel_row + 1}-K{excel_row + 1})',
                    money, cached if cached is not None else "",
                )
            elif name == "% Ricavo":
                cached = None if value is None or (isinstance(value, float) and math.isnan(value)) else float(value)
                worksheet.write_formula(
                    excel_row, col_index,
                    f'=IFERROR(N{excel_row + 1}/I{excel_row + 1},"")',
                    percent, cached if cached is not None else "",
                )
            elif name in {"Nostra quota %", "Quota partner %"}:
                worksheet.write_number(excel_row, col_index, float(value or 0), percent)
            elif name == "Nostro guadagno":
                cached = None if value is None or (isinstance(value, float) and math.isnan(value)) else float(value)
                net_letter = xlsxwriter.utility.xl_col_to_name(EXPORT_COLUMNS.index("Ricavo Netto"))
                pct_letter = xlsxwriter.utility.xl_col_to_name(EXPORT_COLUMNS.index("Nostra quota %"))
                worksheet.write_formula(
                    excel_row, col_index,
                    f'=IF({net_letter}{excel_row + 1}="","",{net_letter}{excel_row + 1}*{pct_letter}{excel_row + 1})',
                    money, cached if cached is not None else "",
                )
            elif name == "Guadagno partner":
                cached = None if value is None or (isinstance(value, float) and math.isnan(value)) else float(value)
                net_letter = xlsxwriter.utility.xl_col_to_name(EXPORT_COLUMNS.index("Ricavo Netto"))
                pct_letter = xlsxwriter.utility.xl_col_to_name(EXPORT_COLUMNS.index("Quota partner %"))
                worksheet.write_formula(
                    excel_row, col_index,
                    f'=IF({net_letter}{excel_row + 1}="","",{net_letter}{excel_row + 1}*{pct_letter}{excel_row + 1})',
                    money, cached if cached is not None else "",
                )
            elif name in money_columns:
                if value is None or (isinstance(value, float) and math.isnan(value)):
                    worksheet.write_blank(excel_row, col_index, None, money)
                else:
                    worksheet.write_number(excel_row, col_index, float(value), money)
            elif name in manual_columns:
                cleaned = clean_text(value)
                if cleaned:
                    worksheet.write_string(excel_row, col_index, cleaned, manual)
                else:
                    worksheet.write_blank(excel_row, col_index, None, manual)
            elif name in {"Quantità"}:
                worksheet.write_number(excel_row, col_index, int(value or 1), integer)
            elif name == "Note contabili":
                cleaned = clean_text(value)
                if cleaned:
                    worksheet.write_string(excel_row, col_index, cleaned, note_fmt)
                else:
                    worksheet.write_blank(excel_row, col_index, None, note_fmt)
            else:
                cleaned = clean_text(value)
                cell_format = center if name in {"Market", "Stato Ordine"} else text
                if cleaned:
                    worksheet.write_string(excel_row, col_index, cleaned, cell_format)
                else:
                    worksheet.write_blank(excel_row, col_index, None, cell_format)

    last_row = max(1, len(frame))
    worksheet.autofilter(0, 0, last_row, len(EXPORT_COLUMNS) - 1)
    worksheet.freeze_panes(1, 0)
    worksheet.set_column("A:A", 12)
    worksheet.set_column("B:B", 15)
    worksheet.set_column("C:C", 20)
    worksheet.set_column("D:D", 15)
    worksheet.set_column("E:E", 22)
    worksheet.set_column("F:F", 48)
    worksheet.set_column("G:G", 18)
    worksheet.set_column("H:O", 14)
    worksheet.set_column("P:P", 29)
    worksheet.set_column("Q:Q", 24)
    worksheet.set_column("R:R", 16)
    worksheet.set_column("S:S", 13)
    worksheet.set_column("T:T", 23)
    worksheet.set_column("U:V", 12)
    worksheet.set_column("W:W", 42)
    worksheet.set_column("X:X", 15)
    worksheet.set_column("Y:Y", 18)
    worksheet.set_column("Z:Z", 15)
    worksheet.set_column("AA:AA", 18)
    worksheet.set_default_row(22)

    if len(frame):
        status_col = EXPORT_COLUMNS.index("Stato Ordine")
        note_col = EXPORT_COLUMNS.index("Note contabili")
        margin_col = EXPORT_COLUMNS.index("Ricavo Netto")
        worksheet.conditional_format(1, status_col, last_row, status_col, {
            "type": "text", "criteria": "containing", "value": "Cancell",
            "format": workbook.add_format({"bg_color": "#F4CCCC", "font_color": "#9C0006"}),
        })
        worksheet.conditional_format(1, status_col, last_row, status_col, {
            "type": "text", "criteria": "containing", "value": "Rimbors",
            "format": workbook.add_format({"bg_color": "#FCE5CD", "font_color": "#9C5700"}),
        })
        worksheet.conditional_format(1, margin_col, last_row, margin_col, {
            "type": "cell", "criteria": "<", "value": 0,
            "format": workbook.add_format({"bg_color": "#F4CCCC", "font_color": "#9C0006"}),
        })
        worksheet.conditional_format(1, note_col, last_row, note_col, {
            "type": "text", "criteria": "containing", "value": "Costo non calcolabile",
            "format": workbook.add_format({"bg_color": "#FFF2CC", "font_color": "#7F6000"}),
        })

    summary_col = len(EXPORT_COLUMNS) + 2
    worksheet.write(0, summary_col, "RIEPILOGO", summary_header)
    summaries = [
        ("Vendite nette", "Vendita"),
        ("Acquisti", "Acquisto"),
        ("Commissioni", "C. Market"),
        ("Rimborsi", "Rimborso"),
        ("Da ricevere", "a Pagare"),
        ("Margine utile", "Ricavo Netto"),
        (f"Nostra quota · {our_pct:g}%", "Nostro guadagno"),
        (f"Quota {partner_label} · {partner_pct:g}%", "Guadagno partner"),
    ]
    summary_values = {
        "Vendita": float(pd.to_numeric(frame["Vendita"], errors="coerce").fillna(0).sum()) if len(frame) else 0.0,
        "Acquisto": float(pd.to_numeric(frame["Acquisto"], errors="coerce").fillna(0).sum()) if len(frame) else 0.0,
        "C. Market": float(pd.to_numeric(frame["C. Market"], errors="coerce").fillna(0).sum()) if len(frame) else 0.0,
        "Rimborso": float(pd.to_numeric(frame["Rimborso"], errors="coerce").fillna(0).sum()) if len(frame) else 0.0,
        "a Pagare": float(pd.to_numeric(frame["a Pagare"], errors="coerce").fillna(0).sum()) if len(frame) else 0.0,
        "Ricavo Netto": float(pd.to_numeric(frame["Ricavo Netto"], errors="coerce").fillna(0).sum()) if len(frame) else 0.0,
        "Nostro guadagno": float(pd.to_numeric(frame["Nostro guadagno"], errors="coerce").fillna(0).sum()) if len(frame) else 0.0,
        "Guadagno partner": float(pd.to_numeric(frame["Guadagno partner"], errors="coerce").fillna(0).sum()) if len(frame) else 0.0,
    }
    for index, (label, column) in enumerate(summaries, start=1):
        worksheet.write(index, summary_col, label, header)
        col_letter = xlsxwriter.utility.xl_col_to_name(EXPORT_COLUMNS.index(column))
        worksheet.write_formula(
            index, summary_col + 1,
            f"=SUM({col_letter}2:{col_letter}{last_row + 1})",
            summary_money, summary_values[column],
        )
    worksheet.set_column(summary_col, summary_col, 19)
    worksheet.set_column(summary_col + 1, summary_col + 1, 16)

    workbook.close()
    return buffer.getvalue()


def _finite_or_zero(value: Any) -> float:
    number = _number(value)
    return float(number) if number is not None else 0.0


def totals(records: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    values = list(records)
    result = {
        "sale": 0.0, "purchase": 0.0, "commission": 0.0,
        "refund": 0.0, "payout": 0.0, "gross_margin": 0.0, "net_revenue": 0.0,
    }
    for source_item in values:
        item = _zero_economic_record(source_item) if _must_zero_economics(
            source_item.get("raw_status"), source_item.get("status_label"),
            source_item.get("note"), source_item.get("supplier_order_number"),
        ) else dict(source_item)
        result["sale"] += _finite_or_zero(item.get("sale_eur"))
        result["purchase"] += _finite_or_zero(item.get("purchase_cost_eur"))
        result["commission"] += _finite_or_zero(item.get("commission_eur"))
        result["refund"] += _finite_or_zero(item.get("refund_eur"))
        result["payout"] += _finite_or_zero(item.get("payout_eur"))
        computed = computed_values(item)
        result["gross_margin"] += _finite_or_zero(computed["gross_margin_eur"])
        result["net_revenue"] += _finite_or_zero(computed["net_revenue_eur"])
    return {key: round(value, 2) for key, value in result.items()}


def default_file_name(marketplace: str, date_from: date, date_to: date) -> str:
    return f"Contabilita_{clean_text(marketplace).title()}_{date_from:%Y%m%d}_{date_to:%Y%m%d}.xlsx"


def save_export(
    seller_id: int,
    account_id: int,
    marketplace: str,
    file_name: str,
    content: bytes,
    records: Sequence[Mapping[str, Any]],
    date_from: date,
    date_to: date,
    *,
    our_profit_pct: float = 0.0,
    partner_profit_pct: float = 100.0,
) -> dict[str, Any]:
    folder = DATA_DIR / "accounting_exports" / str(seller_id) / str(account_id)
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name).strip("_") or "contabilita.xlsx"
    path = folder / f"{timestamp}_{safe}"
    path.write_bytes(content)
    summary = totals(records)
    profit_split = split_profit(
        summary["net_revenue"], our_profit_pct, partner_profit_pct
    )
    export_id = execute(
        """
        INSERT INTO accounting_exports(
            seller_id,marketplace_account_id,marketplace,file_name,file_path,
            date_from,date_to,row_count,total_sale_eur,total_purchase_eur,
            total_commission_eur,total_payout_eur,total_net_revenue_eur,
            total_our_profit_eur,total_partner_profit_eur,our_profit_pct,
            partner_profit_pct,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            seller_id, account_id, marketplace, file_name, str(path),
            date_from.isoformat(), date_to.isoformat(), len(records),
            summary["sale"], summary["purchase"], summary["commission"],
            summary["payout"], summary["net_revenue"],
            profit_split["our_amount"], profit_split["partner_amount"],
            profit_split["our_pct"], profit_split["partner_pct"], now_iso(),
        ),
    )
    with connect() as con:
        con.executemany(
            "INSERT INTO accounting_export_rows(export_id,row_key) VALUES(?,?) ON CONFLICT DO NOTHING",
            [(export_id, clean_text(item.get("row_key"))) for item in records if clean_text(item.get("row_key"))],
        )
    return {
        "id": export_id,
        "file_path": str(path),
        "file_name": file_name,
        **summary,
        **profit_split,
    }


def export_history(seller_id: int, account_id: int, marketplace: str) -> list[dict[str, Any]]:
    return rows(
        """
        SELECT * FROM accounting_exports
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
        ORDER BY id DESC
        """,
        (seller_id, account_id, marketplace),
    )


def previous_exports(row_keys: Iterable[str], account_id: int, marketplace: str) -> dict[str, list[dict[str, Any]]]:
    keys = [clean_text(value) for value in row_keys if clean_text(value)]
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    result = rows(
        f"""
        SELECT aer.row_key,ae.id,ae.file_name,ae.created_at
        FROM accounting_export_rows aer
        JOIN accounting_exports ae ON ae.id=aer.export_id
        WHERE ae.marketplace_account_id=? AND ae.marketplace=?
          AND aer.row_key IN ({placeholders})
        ORDER BY ae.id DESC
        """,
        (account_id, marketplace, *keys),
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in result:
        grouped.setdefault(clean_text(item.get("row_key")), []).append(item)
    return grouped
