from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from services.db import rows
from services.kaufland_profit import product_costs
from services.lists import normalize


def clean_identifier(value) -> str:
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") else text


def historical_sent_offers(
    operation_rows: list[dict],
    environment: str | None = None,
) -> list[dict]:
    """Return every successful Kaufland publication, including old offers.

    An order can refer to an offer that is no longer active. Cost recovery must
    therefore inspect all supplier views that were really published for the
    selected account, without making a deleted offer active again.
    """
    requested_environment = str(environment or "").strip().lower()
    published = []
    ordered = sorted(
        operation_rows,
        key=lambda item: (
            str(item.get("created_at", "")),
            int(item.get("id", 0) or 0),
        ),
        reverse=True,
    )
    for operation in ordered:
        if str(operation.get("operation_type") or "").upper() != "CREA/AGGIORNA":
            continue
        try:
            payload = json.loads(operation.get("details_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        operation_environment = str(
            payload.get("environment") or ""
        ).strip().lower()
        if (
            requested_environment
            and operation_environment
            and operation_environment != requested_environment
        ):
            continue
        try:
            saved_view_id = int(payload.get("saved_view_id") or 0)
        except (TypeError, ValueError):
            saved_view_id = 0
        try:
            price_list_id = int(operation.get("price_list_id") or 0)
        except (TypeError, ValueError):
            price_list_id = 0
        for detail in payload.get("rows") or []:
            if not isinstance(detail, dict) or detail.get("ok") is not True:
                continue
            storefront = str(
                detail.get("paese") or operation.get("storefront") or ""
            ).strip().lower()
            sent_sku = clean_identifier(detail.get("sku_inviato"))
            if not storefront or not sent_sku:
                continue
            published.append(
                {
                    "paese": storefront,
                    "sku_inviato": sent_sku,
                    "ean": clean_identifier(detail.get("ean")),
                    "sku_originale": clean_identifier(
                        detail.get("sku_originale")
                    ),
                    "saved_view_id": saved_view_id,
                    "price_list_id": price_list_id,
                    "created_at": str(operation.get("created_at") or ""),
                    "operation_id": int(operation.get("id") or 0),
                }
            )
    return published


def build_supplier_cost_catalog(
    published_offers: list[dict],
    view_products: list[dict],
) -> dict:
    """Index products from the supplier views actually used on Kaufland."""
    views = {}
    ordered_view_ids = []
    for saved_view in view_products:
        try:
            view_id = int(saved_view.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if view_id <= 0:
            continue
        by_ean, by_sku = {}, {}
        for product in saved_view.get("products") or []:
            if not isinstance(product, dict):
                continue
            ean = clean_identifier(product.get("ean"))
            sku = clean_identifier(product.get("sku"))
            if ean and ean.lower() not in {"nan", "none"}:
                by_ean.setdefault(ean, product)
            if sku and sku.lower() not in {"nan", "none"}:
                by_sku.setdefault(sku, product)
        views[view_id] = {
            "by_ean": by_ean,
            "by_sku": by_sku,
            "name": str(saved_view.get("name") or "").strip(),
            "supplier_name": str(
                saved_view.get("supplier_name") or ""
            ).strip(),
            "price_list_name": str(
                saved_view.get("price_list_name") or ""
            ).strip(),
        }
        ordered_view_ids.append(view_id)

    offers_by_exact_sku = {}
    for offer in published_offers:
        sku = clean_identifier(offer.get("sku_inviato"))
        storefront = str(offer.get("paese") or "").strip().lower()
        if not sku:
            continue
        offers_by_exact_sku.setdefault((storefront, sku), []).append(offer)
        offers_by_exact_sku.setdefault(("", sku), []).append(offer)
    return {
        "views": views,
        "ordered_view_ids": list(dict.fromkeys(ordered_view_ids)),
        "offers_by_exact_sku": offers_by_exact_sku,
    }


def resolve_supplier_purchase_cost(
    catalog: dict,
    *,
    order_sku,
    order_ean,
    storefront,
    product_code="",
) -> dict | None:
    """Resolve an order cost by publication metadata, then EAN/SKU."""
    sku = clean_identifier(order_sku)
    ean = clean_identifier(order_ean)
    code = clean_identifier(product_code)
    country = str(storefront or "").strip().lower()
    exact_offers = list(
        (catalog.get("offers_by_exact_sku") or {}).get((country, sku), [])
    )
    if not exact_offers:
        exact_offers = list(
            (catalog.get("offers_by_exact_sku") or {}).get(("", sku), [])
        )

    preferred_view_ids = [
        int(item.get("saved_view_id") or 0)
        for item in exact_offers
        if int(item.get("saved_view_id") or 0) > 0
    ]
    view_ids = list(
        dict.fromkeys(
            preferred_view_ids
            + list(catalog.get("ordered_view_ids") or [])
        )
    )
    views = catalog.get("views") or {}
    for view_id in view_ids:
        view = views.get(int(view_id))
        if not view:
            continue
        matching_offers = [
            item
            for item in exact_offers
            if int(item.get("saved_view_id") or 0) == int(view_id)
        ]
        ean_candidates = [ean]
        sku_candidates = [sku, code]
        for offer in matching_offers:
            ean_candidates.append(clean_identifier(offer.get("ean")))
            sku_candidates.append(
                clean_identifier(offer.get("sku_originale"))
            )
        if "_" in sku:
            sku_candidates.append(sku.split("_", 1)[1])

        product = None
        matched_by = ""
        matched_value = ""
        for candidate in dict.fromkeys(value for value in ean_candidates if value):
            product = view["by_ean"].get(candidate)
            if product is not None:
                matched_by = "EAN"
                matched_value = candidate
                break
        if product is None:
            for candidate in dict.fromkeys(
                value for value in sku_candidates if value
            ):
                product = view["by_sku"].get(candidate)
                if product is not None:
                    matched_by = "SKU/codice"
                    matched_value = candidate
                    break
        if product is None:
            continue

        cost = product_costs(product, country).get("purchase_cost_eur")
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            continue
        if cost <= 0:
            continue
        source_parts = [
            str(view.get("supplier_name") or "").strip(),
            str(view.get("price_list_name") or "").strip(),
            str(view.get("name") or "").strip(),
        ]
        source_name = " · ".join(
            value for value in source_parts if value
        ) or f"Vista {view_id}"
        return {
            "purchase_cost_eur": round(cost, 2),
            "matched_by": matched_by,
            "matched_value": matched_value,
            "saved_view_id": int(view_id),
            "source_name": source_name,
            "purchase_cost_source": (
                f"Listino pubblicato · {source_name} · "
                f"corrispondenza {matched_by}"
            ),
        }
    return None


def load_published_supplier_cost_catalog(
    seller_id: int,
    account_id: int,
    environment: str,
) -> dict:
    """Load only supplier snapshots previously used by this Kaufland account."""
    operation_rows = rows(
        """SELECT id,price_list_id,storefront,operation_type,details_json,created_at
        FROM operations
        WHERE seller_id=? AND marketplace_account_id=?
          AND marketplace='kaufland'
        ORDER BY created_at,id""",
        (seller_id, account_id),
    )
    published_offers = historical_sent_offers(operation_rows, environment)
    used_view_ids = {
        int(item.get("saved_view_id") or 0)
        for item in published_offers
        if int(item.get("saved_view_id") or 0) > 0
    }
    used_price_list_ids = {
        int(item.get("price_list_id") or 0)
        for item in published_offers
        if int(item.get("price_list_id") or 0) > 0
    }
    available_views = rows(
        """SELECT sv.id,sv.name,sv.snapshot_path,sv.price_list_id,
        pl.name price_list_name,s.name supplier_name,sv.updated_at
        FROM saved_views sv
        JOIN price_lists pl ON pl.id=sv.price_list_id
        JOIN suppliers s ON s.id=pl.supplier_id
        WHERE sv.seller_id=?
        ORDER BY sv.updated_at DESC,sv.id DESC""",
        (seller_id,),
    )
    relevant_views = [
        item
        for item in available_views
        if (
            int(item.get("id") or 0) in used_view_ids
            or int(item.get("price_list_id") or 0) in used_price_list_ids
        )
    ]
    view_products = []
    for saved_view in relevant_views:
        snapshot = Path(str(saved_view.get("snapshot_path") or ""))
        if not snapshot.exists():
            continue
        try:
            frame = normalize(pd.read_pickle(snapshot))
        except Exception:
            continue
        view_products.append(
            {
                **saved_view,
                "products": frame.to_dict("records"),
            }
        )
    return build_supplier_cost_catalog(published_offers, view_products)
