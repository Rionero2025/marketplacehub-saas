from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from services import db
from services.db import accessible_lists
from services.kaufland_order_costs import historical_sent_offers
from services.kaufland_profit import product_costs
from services.lists import normalize
from services.saved_view_storage import load_saved_view_frame


COUNTRY_CURRENCIES = {
    "de": "EUR", "at": "EUR", "fr": "EUR", "it": "EUR", "sk": "EUR",
    "es": "EUR", "nl": "EUR", "pl": "PLN", "cz": "CZK",
}


def clean_identifier(value: Any) -> str:
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") else text


def normalized_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def composed_offer_parts(value: Any) -> dict[str, Any]:
    sku = clean_identifier(value)
    parts = sku.rsplit("_", 3)
    if len(parts) != 4:
        return {"prefix": sku.split("_", 1)[0] if sku else "", "code": "", "cost": None, "minimum": None}

    def number(raw):
        try:
            parsed = float(str(raw).replace(",", "."))
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError):
            return None

    return {
        "prefix": parts[0],
        "code": clean_identifier(parts[1]),
        "cost": number(parts[2]),
        "minimum": number(parts[3]),
    }


def _load_frame(path_value: Any) -> pd.DataFrame | None:
    path = Path(str(path_value or ""))
    if not path.exists() or not path.is_file():
        return None
    try:
        return normalize(pd.read_pickle(path))
    except Exception:
        return None


def _source_products(frame: pd.DataFrame) -> tuple[dict[str, dict], dict[str, dict]]:
    by_ean: dict[str, dict] = {}
    by_sku: dict[str, dict] = {}
    for product in frame.to_dict("records"):
        ean = clean_identifier(product.get("ean"))
        sku = clean_identifier(product.get("sku"))
        if ean and ean.casefold() not in {"nan", "none"}:
            by_ean.setdefault(ean, product)
        if sku and sku.casefold() not in {"nan", "none"}:
            by_sku.setdefault(sku, product)
    return by_ean, by_sku


def _publication_metadata(seller_id: int, account_id: int, environment: str) -> dict[tuple[str, str], dict]:
    operation_rows = db.rows(
        """SELECT id,price_list_id,storefront,operation_type,details_json,created_at
        FROM operations
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace='kaufland'
        ORDER BY created_at,id""",
        (seller_id, account_id),
    )
    history = historical_sent_offers(operation_rows, environment)
    result: dict[tuple[str, str], dict] = {}
    for item in reversed(history):
        key = (
            clean_identifier(item.get("paese")).lower(),
            clean_identifier(item.get("sku_inviato")),
        )
        if not all(key):
            continue
        result[key] = dict(item)
    return result


def catalog_signature(seller_id: int, account_id: int) -> str:
    lists = accessible_lists(seller_id)
    list_ids = [int(item["id"]) for item in lists]
    if not list_ids:
        return "empty"
    placeholders = ",".join("?" for _ in list_ids)
    views = db.rows(
        f"""SELECT sv.id,sv.price_list_id,sv.snapshot_path,sv.snapshot_sha256,sv.snapshot_storage_key,sv.updated_at,
        MAX(CASE WHEN svm.marketplace_account_id=? THEN 1 ELSE 0 END) account_mapped
        FROM saved_views sv
        LEFT JOIN saved_view_marketplaces svm ON svm.saved_view_id=sv.id
        WHERE sv.price_list_id IN ({placeholders})
        GROUP BY sv.id,sv.price_list_id,sv.snapshot_path,sv.snapshot_sha256,sv.snapshot_storage_key,sv.updated_at
        ORDER BY sv.updated_at DESC,sv.id DESC""",
        (account_id, *list_ids),
    )
    values = []
    for item in lists:
        path = Path(str(item.get("local_path") or ""))
        values.append(("list", int(item["id"]), str(path), path.stat().st_mtime_ns if path.exists() else 0))
    for item in views:
        values.append((
            "view", int(item["id"]), str(item.get("snapshot_sha256") or ""),
            str(item.get("updated_at") or ""), item.get("account_mapped"),
        ))
    return str(hash(tuple(values)))


def load_seller_catalog(seller_id: int, account_id: int, environment: str) -> dict:
    """Load every active list available to the seller and index current products.

    The latest usable saved view of every list is preferred. The current list file is
    added as a fallback, so a list remains matchable even when it has no saved view.
    Exact publication metadata is retained only to identify the most likely supplier;
    live API units remain the authoritative source of active offers.
    """
    list_rows = accessible_lists(seller_id)
    if not list_rows:
        return {
            "sources": [], "ean_index": {}, "sku_index": {}, "publication": {},
            "list_count": 0, "source_count": 0, "unavailable": [],
        }
    list_info = {int(item["id"]): dict(item) for item in list_rows}
    list_ids = list(list_info)
    placeholders = ",".join("?" for _ in list_ids)
    view_rows = db.rows(
        f"""SELECT sv.id,sv.seller_id,sv.price_list_id,sv.name,sv.snapshot_path,
        sv.updated_at,pl.name price_list_name,s.name supplier_name,
        MAX(CASE WHEN svm.marketplace_account_id=? THEN 1 ELSE 0 END) account_mapped
        FROM saved_views sv
        JOIN price_lists pl ON pl.id=sv.price_list_id
        JOIN suppliers s ON s.id=pl.supplier_id
        LEFT JOIN saved_view_marketplaces svm ON svm.saved_view_id=sv.id
        WHERE sv.price_list_id IN ({placeholders})
        GROUP BY sv.id,sv.seller_id,sv.price_list_id,sv.name,sv.snapshot_path,
                 sv.updated_at,pl.name,s.name
        ORDER BY account_mapped DESC,sv.updated_at DESC,sv.id DESC""",
        (account_id, *list_ids),
    )
    publication = _publication_metadata(seller_id, account_id, environment)
    published_view_ids = {
        int(item.get("saved_view_id") or 0)
        for item in publication.values()
        if int(item.get("saved_view_id") or 0) > 0
    }

    chosen_view_ids: set[int] = set()
    current_view_by_list: dict[int, int] = {}
    for item in view_rows:
        list_id = int(item["price_list_id"])
        view_id = int(item["id"])
        if list_id not in current_view_by_list:
            current_view_by_list[list_id] = view_id
            chosen_view_ids.add(view_id)
        if view_id in published_view_ids:
            chosen_view_ids.add(view_id)

    sources: list[dict] = []
    unavailable: list[str] = []
    for item in view_rows:
        view_id = int(item["id"])
        if view_id not in chosen_view_ids:
            continue
        try:
            frame = normalize(load_saved_view_frame(item))
        except Exception:
            frame = None
        if frame is None:
            unavailable.append(f"Vista {view_id} · {item.get('price_list_name') or ''}")
            continue
        by_ean, by_sku = _source_products(frame)
        sources.append({
            "source_type": "saved_view",
            "saved_view_id": view_id,
            "price_list_id": int(item["price_list_id"]),
            "supplier_name": str(item.get("supplier_name") or "").strip(),
            "price_list_name": str(item.get("price_list_name") or "").strip(),
            "view_name": str(item.get("name") or "").strip(),
            "updated_at": str(item.get("updated_at") or ""),
            "account_mapped": bool(item.get("account_mapped")),
            "is_current": current_view_by_list.get(int(item["price_list_id"])) == view_id,
            "by_ean": by_ean,
            "by_sku": by_sku,
        })

    loaded_list_ids = {int(item["price_list_id"]) for item in sources if item.get("is_current")}
    for list_id, item in list_info.items():
        if list_id in loaded_list_ids:
            continue
        frame = _load_frame(item.get("local_path"))
        if frame is None:
            unavailable.append(f"Listino {list_id} · {item.get('name') or ''}")
            continue
        by_ean, by_sku = _source_products(frame)
        sources.append({
            "source_type": "price_list",
            "saved_view_id": None,
            "price_list_id": list_id,
            "supplier_name": str(item.get("supplier_name") or "").strip(),
            "price_list_name": str(item.get("name") or "").strip(),
            "view_name": "File corrente del listino",
            "updated_at": str(item.get("last_download_at") or item.get("created_at") or ""),
            "account_mapped": False,
            "is_current": True,
            "by_ean": by_ean,
            "by_sku": by_sku,
        })

    ean_index: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    sku_index: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for source in sources:
        for key, product in source["by_ean"].items():
            ean_index[key].append((source, product))
        for key, product in source["by_sku"].items():
            sku_index[key].append((source, product))

    return {
        "sources": sources,
        "ean_index": dict(ean_index),
        "sku_index": dict(sku_index),
        "publication": publication,
        "list_count": len(list_rows),
        "source_count": len(sources),
        "unavailable": unavailable,
    }


def _rule_map(seller_id: int, price_list_ids: list[int]) -> dict[tuple[int, str], dict]:
    if not price_list_ids:
        return {}
    placeholders = ",".join("?" for _ in price_list_ids)
    result = {}
    for item in db.rows(
        f"""SELECT price_list_id,storefront,commission_pct,settings_json
        FROM commercial_rules
        WHERE seller_id=? AND marketplace='kaufland'
          AND price_list_id IN ({placeholders})""",
        (seller_id, *price_list_ids),
    ):
        try:
            settings = json.loads(item.get("settings_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            settings = {}
        result[(int(item["price_list_id"]), str(item["storefront"]).lower())] = {
            "commission_pct": float(item.get("commission_pct") or 15),
            "multiplier": float(settings.get("multiplier") or 1),
            "currency": str(settings.get("currency") or "").upper(),
        }
    return result


def resolve_offer_cost(
    catalog: dict,
    *,
    seller_id: int,
    storefront: str,
    id_offer: Any,
    ean: Any,
) -> dict:
    country = clean_identifier(storefront).lower()
    offer = clean_identifier(id_offer)
    live_ean = clean_identifier(ean)
    composed = composed_offer_parts(offer)
    publication = (catalog.get("publication") or {}).get((country, offer), {})
    publication_list_id = int(publication.get("price_list_id") or 0)
    publication_view_id = int(publication.get("saved_view_id") or 0)
    original_sku = clean_identifier(publication.get("sku_originale")) or clean_identifier(composed.get("code"))
    publication_ean = clean_identifier(publication.get("ean"))

    ean_candidates = list(dict.fromkeys(value for value in (live_ean, publication_ean) if value))
    code = clean_identifier(composed.get("code"))
    if code.isdigit() and 8 <= len(code) <= 14:
        ean_candidates.append(code)
    sku_candidates = list(dict.fromkeys(value for value in (original_sku, code, offer) if value))
    prefix = normalized_name(composed.get("prefix"))

    raw_candidates: list[tuple[dict, dict, str, str]] = []
    for candidate in ean_candidates:
        for source, product in (catalog.get("ean_index") or {}).get(candidate, []):
            raw_candidates.append((source, product, "EAN", candidate))
    for candidate in sku_candidates:
        for source, product in (catalog.get("sku_index") or {}).get(candidate, []):
            raw_candidates.append((source, product, "SKU/codice", candidate))

    unique: dict[tuple[int, int, str], dict] = {}
    for source, product, matched_by, matched_value in raw_candidates:
        list_id = int(source["price_list_id"])
        view_id = int(source.get("saved_view_id") or 0)
        product_key = clean_identifier(product.get("ean")) or clean_identifier(product.get("sku"))
        key = (list_id, view_id, product_key)
        costs = product_costs(product, country)
        if float(costs.get("total_cost_eur") or 0) <= 0:
            continue
        score = 0
        reasons = []
        if list_id == publication_list_id and publication_list_id:
            score += 120; reasons.append("listino dell'invio")
        if view_id == publication_view_id and publication_view_id:
            score += 25; reasons.append("vista dell'invio")
        source_name = normalized_name(
            f"{source.get('supplier_name', '')} {source.get('price_list_name', '')}"
        )
        if prefix and prefix in source_name:
            score += 55; reasons.append("prefisso fornitore")
        if matched_by == "EAN":
            score += 50; reasons.append("EAN")
        else:
            score += 35; reasons.append("SKU/codice")
        if source.get("account_mapped"):
            score += 8; reasons.append("vista associata all'account")
        if source.get("is_current"):
            score += 6; reasons.append("dati correnti")
        candidate = {
            **costs,
            "score": score,
            "matched_by": matched_by,
            "matched_value": matched_value,
            "matched_price_list_id": list_id,
            "matched_saved_view_id": source.get("saved_view_id"),
            "supplier_name": source.get("supplier_name", ""),
            "price_list_name": source.get("price_list_name", ""),
            "view_name": source.get("view_name", ""),
            "updated_at": source.get("updated_at", ""),
            "match_reason": ", ".join(reasons),
            "original_sku": clean_identifier(product.get("sku")) or original_sku,
        }
        existing = unique.get(key)
        if existing is None or (candidate["score"], candidate["updated_at"]) > (existing["score"], existing["updated_at"]):
            unique[key] = candidate

    candidates = sorted(
        unique.values(),
        key=lambda item: (int(item.get("score") or 0), str(item.get("updated_at") or "")),
        reverse=True,
    )
    if not candidates:
        return {
            "purchase_cost_eur": None,
            "shipping_cost_eur": None,
            "total_cost_eur": None,
            "matched_price_list_id": None,
            "matched_saved_view_id": None,
            "supplier_name": "",
            "price_list_name": "Non associato",
            "view_name": "",
            "cost_match_source": "Nessuna corrispondenza in tutti i listini del Seller",
            "cost_match_count": 0,
            "original_sku": original_sku,
            "alternative_matches": [],
            "publication_price_list_id": publication_list_id or None,
        }
    best = candidates[0]
    alternatives = [
        {
            "supplier_name": item.get("supplier_name", ""),
            "price_list_name": item.get("price_list_name", ""),
            "view_name": item.get("view_name", ""),
            "total_cost_eur": item.get("total_cost_eur"),
            "matched_by": item.get("matched_by", ""),
            "score": item.get("score", 0),
        }
        for item in candidates[:10]
    ]
    source_label = " · ".join(
        value for value in (
            str(best.get("supplier_name") or "").strip(),
            str(best.get("price_list_name") or "").strip(),
            str(best.get("view_name") or "").strip(),
        ) if value
    )
    return {
        **best,
        "cost_match_source": (
            f"{source_label or 'Listino'} · corrispondenza {best['matched_by']} "
            f"({best['match_reason']})"
        ),
        "cost_match_count": len(candidates),
        "alternative_matches": alternatives,
        "publication_price_list_id": publication_list_id or None,
    }


def commercial_fallback(
    seller_id: int,
    *,
    price_list_id: int | None,
    storefront: str,
) -> dict:
    country = str(storefront or "").lower()
    if price_list_id:
        item = db.row(
            """SELECT commission_pct,settings_json FROM commercial_rules
            WHERE seller_id=? AND price_list_id=? AND marketplace='kaufland'
              AND storefront=?""",
            (seller_id, int(price_list_id), country),
        )
        if item:
            try:
                settings = json.loads(item.get("settings_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                settings = {}
            return {
                "commission_pct": float(item.get("commission_pct") or 15),
                "multiplier": float(settings.get("multiplier") or 1),
                "currency": str(settings.get("currency") or COUNTRY_CURRENCIES.get(country, "EUR")).upper(),
                "source": "Regola commerciale del listino abbinato",
            }
    item = db.row(
        """SELECT commission_pct,settings_json FROM commercial_rules
        WHERE seller_id=? AND marketplace='kaufland' AND storefront=?
        ORDER BY updated_at DESC,id DESC LIMIT 1""",
        (seller_id, country),
    )
    if item:
        try:
            settings = json.loads(item.get("settings_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            settings = {}
        return {
            "commission_pct": float(item.get("commission_pct") or 15),
            "multiplier": float(settings.get("multiplier") or 1),
            "currency": str(settings.get("currency") or COUNTRY_CURRENCIES.get(country, "EUR")).upper(),
            "source": "Ultima regola Kaufland del Seller",
        }
    return {
        "commission_pct": 15.0,
        "multiplier": 1.0,
        "currency": COUNTRY_CURRENCIES.get(country, "EUR"),
        "source": "Riserva standard",
    }


def latest_order_commissions(seller_id: int, account_id: int, environment: str) -> dict:
    by_sku: dict[tuple[str, str], dict] = {}
    by_ean: dict[tuple[str, str], dict] = {}
    for item in db.rows(
        """SELECT storefront,sku,ean,id_order,commission_local,commission_pct,
        commission_source,currency,ts_created_iso
        FROM kaufland_order_units
        WHERE seller_id=? AND marketplace_account_id=? AND environment=?
          AND commission_pct IS NOT NULL
        ORDER BY ts_created_iso DESC,id DESC""",
        (seller_id, account_id, environment),
    ):
        record = {
            "rate": float(item.get("commission_pct") or 0),
            "amount_local": item.get("commission_local"),
            "currency": str(item.get("currency") or "").upper(),
            "id_order": str(item.get("id_order") or ""),
            "source": str(item.get("commission_source") or "API ordine Kaufland"),
            "ts_created_iso": str(item.get("ts_created_iso") or ""),
        }
        country = str(item.get("storefront") or "").lower()
        sku = clean_identifier(item.get("sku"))
        ean = clean_identifier(item.get("ean"))
        if sku:
            by_sku.setdefault((country, sku), record)
        if ean:
            by_ean.setdefault((country, ean), record)
    return {"by_sku": by_sku, "by_ean": by_ean}


def resolve_latest_order_commission(index: dict, storefront: str, sku: str, ean: str) -> dict | None:
    country = str(storefront or "").lower()
    return (
        (index.get("by_sku") or {}).get((country, clean_identifier(sku)))
        or (index.get("by_ean") or {}).get((country, clean_identifier(ean)))
    )


def effective_commission(total_eur: Any, variable_pct: Any, fixed_eur: Any = 0) -> dict:
    try:
        total = float(total_eur)
        variable = max(0.0, float(variable_pct or 0))
        fixed = max(0.0, float(fixed_eur or 0))
    except (TypeError, ValueError):
        return {"commission_eur": None, "effective_pct": None}
    if not math.isfinite(total) or total <= 0:
        return {"commission_eur": None, "effective_pct": None}
    amount = total * variable / 100 + fixed
    return {
        "commission_eur": round(amount, 2),
        "effective_pct": round(amount / total * 100, 4),
    }


def ensure_schema() -> None:
    with db.connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS kaufland_buybox_account_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                matched_price_list_id INTEGER REFERENCES price_lists(id) ON DELETE SET NULL,
                matched_saved_view_id INTEGER REFERENCES saved_views(id) ON DELETE SET NULL,
                supplier_name TEXT NOT NULL DEFAULT '',
                price_list_name TEXT NOT NULL DEFAULT '',
                cost_match_source TEXT NOT NULL DEFAULT '',
                cost_match_count INTEGER NOT NULL DEFAULT 0,
                storefront TEXT NOT NULL,
                environment TEXT NOT NULL CHECK(environment IN ('test','live')),
                ean TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL,
                original_sku TEXT NOT NULL DEFAULT '',
                product_title TEXT NOT NULL DEFAULT '',
                inventory_status TEXT NOT NULL DEFAULT '',
                inventory_amount INTEGER,
                id_product INTEGER,
                id_unit INTEGER,
                status TEXT NOT NULL,
                our_rank INTEGER,
                winner_seller TEXT NOT NULL DEFAULT '',
                winner_price REAL,
                winner_shipping REAL,
                winner_total REAL,
                our_price REAL,
                our_shipping REAL,
                our_total REAL,
                target_price REAL,
                currency TEXT NOT NULL DEFAULT '',
                delivery_min INTEGER,
                delivery_max INTEGER,
                own_delivery_min INTEGER,
                own_delivery_max INTEGER,
                own_handling_time INTEGER,
                logistics_status TEXT NOT NULL DEFAULT '',
                offer_count INTEGER NOT NULL DEFAULT 0,
                error_type TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                purchase_cost_eur REAL,
                shipping_cost_eur REAL,
                total_cost_eur REAL,
                commission_pct REAL,
                commission_fixed_eur REAL NOT NULL DEFAULT 0,
                commission_source TEXT NOT NULL DEFAULT '',
                current_commission_eur REAL,
                current_commission_effective_pct REAL,
                actual_order_commission_pct REAL,
                actual_order_commission_local REAL,
                actual_order_commission_currency TEXT NOT NULL DEFAULT '',
                actual_order_id TEXT NOT NULL DEFAULT '',
                target_sales_price REAL,
                target_sales_price_eur REAL,
                target_source TEXT NOT NULL DEFAULT '',
                target_commission_eur REAL,
                target_commission_effective_pct REAL,
                profit_eur REAL,
                profit_pct REAL,
                profit_status TEXT NOT NULL DEFAULT '',
                minimum_price REAL,
                minimum_price_source TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL,
                UNIQUE(marketplace_account_id,storefront,environment,sku)
            );
            CREATE INDEX IF NOT EXISTS idx_kaufland_buybox_account_scope
            ON kaufland_buybox_account_checks(
                seller_id,marketplace_account_id,environment,storefront
            );
            CREATE TABLE IF NOT EXISTS kaufland_buybox_account_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                environment TEXT NOT NULL CHECK(environment IN ('test','live')),
                storefronts TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                rows_json TEXT NOT NULL DEFAULT '[]',
                row_count INTEGER NOT NULL DEFAULT 0,
                latest_checked_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kaufland_buybox_account_views_scope
            ON kaufland_buybox_account_views(
                seller_id,marketplace_account_id,environment,created_at
            );
            CREATE TABLE IF NOT EXISTS kaufland_buybox_account_price_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                matched_price_list_id INTEGER REFERENCES price_lists(id) ON DELETE SET NULL,
                supplier_name TEXT NOT NULL DEFAULT '',
                price_list_name TEXT NOT NULL DEFAULT '',
                storefront TEXT NOT NULL,
                environment TEXT NOT NULL CHECK(environment IN ('test','live')),
                ean TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL,
                id_unit INTEGER,
                source TEXT NOT NULL,
                previous_price REAL,
                new_price REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT '',
                purchase_cost_eur REAL,
                shipping_cost_eur REAL,
                total_cost_eur REAL,
                commission_pct REAL,
                commission_fixed_eur REAL NOT NULL DEFAULT 0,
                commission_source TEXT NOT NULL DEFAULT '',
                commission_eur REAL,
                commission_effective_pct REAL,
                profit_eur REAL,
                profit_pct REAL,
                margin_status TEXT NOT NULL DEFAULT '',
                price_field TEXT NOT NULL DEFAULT 'minimum_price',
                api_result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kaufland_buybox_account_price_updates_scope
            ON kaufland_buybox_account_price_updates(
                seller_id,marketplace_account_id,environment,storefront,sku,created_at
            );
            """
        )
