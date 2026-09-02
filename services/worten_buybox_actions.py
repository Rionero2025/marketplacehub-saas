from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from services.worten import WORTEN_OFFER_COLUMNS


def _number(value, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", ".").strip().rstrip("%").strip())
    except (TypeError, ValueError):
        return default


def buybox_outcome(item: dict) -> str:
    """Normalize a saved result into the four Worten outcome filters."""
    rank = _number(item.get("our_rank"))
    if rank is None:
        return "Non trovate"
    return "Vinte" if int(rank) == 1 else "Perse"


def apply_offer_editor_changes(
    existing_selection: Iterable,
    existing_prices: dict,
    row_skus: Iterable,
    editor_state: dict | None,
) -> tuple[list[str], dict[str, float]]:
    """Persist checkbox and proposed-price edits across Streamlit reruns."""
    selected = {
        str(value or "").strip()
        for value in existing_selection
        if str(value or "").strip()
    }
    prices = {}
    for key, value in (existing_prices or {}).items():
        sku = str(key or "").strip()
        parsed_price = _number(value)
        if sku and parsed_price is not None:
            prices[sku] = float(parsed_price)
    ordered_skus = [str(value or "").strip() for value in row_skus]
    if not isinstance(editor_state, dict):
        return sorted(selected), prices
    edited_rows = editor_state.get("edited_rows", {})
    if not isinstance(edited_rows, dict):
        return sorted(selected), prices
    for raw_index, changes in edited_rows.items():
        if not isinstance(changes, dict):
            continue
        try:
            row_index = int(raw_index)
            sku = ordered_skus[row_index]
        except (IndexError, TypeError, ValueError):
            continue
        if not sku:
            continue
        if "Seleziona" in changes:
            if bool(changes["Seleziona"]):
                selected.add(sku)
            else:
                selected.discard(sku)
        if "Prezzo da inviare" in changes:
            price = _number(changes["Prezzo da inviare"])
            if price is not None and price > 0:
                prices[sku] = round(float(price), 2)
    return sorted(selected), prices


def buybox_alignment_price(item: dict, *, undercut: float = 0.01) -> float | None:
    """Return the product price needed to beat the visible winning customer total.

    Worten does not expose an authoritative target price.  The safest actionable
    estimate is therefore the visible winning total minus our shipping and a
    one-cent undercut.  Seller quality and marketplace rules can still affect the
    final Buy Box assignment.
    """
    winner_total = _number(item.get("winner_total"))
    if winner_total is None:
        return None
    own_shipping = _number(item.get("our_shipping"), 0.0) or 0.0
    step = max(0.0, _number(undercut, 0.01) or 0.0)
    return round(max(0.01, winner_total - own_shipping - step), 2)


def evaluate_worten_price(
    price,
    *,
    shipping=0,
    commission_pct=0,
    total_cost=None,
) -> dict:
    """Calculate revenue and safety status for a proposed Worten price."""
    product_price = _number(price)
    shipping_price = _number(shipping, 0.0) or 0.0
    rate = _number(commission_pct, 0.0) or 0.0
    cost = _number(total_cost)
    if product_price is None or product_price <= 0:
        return {
            "total": None,
            "commission_eur": None,
            "profit_eur": None,
            "margin_pct": None,
            "status": "Non calcolabile",
        }
    total = product_price + shipping_price
    commission = total * rate / 100
    if cost is None:
        return {
            "total": round(total, 2),
            "commission_eur": round(commission, 2),
            "profit_eur": None,
            "margin_pct": None,
            "status": "Non calcolabile",
        }
    profit = total - commission - cost
    margin = profit / cost * 100 if cost > 0 else None
    status = (
        "Perdita"
        if profit < 0
        else ("Margine sotto 10%" if margin is None or margin < 10 else "Guadagno")
    )
    return {
        "total": round(total, 2),
        "commission_eur": round(commission, 2),
        "profit_eur": round(profit, 2),
        "margin_pct": None if margin is None else round(margin, 2),
        "status": status,
    }


def build_worten_price_update_plan(
    items: Iterable[dict],
    selected_skus: Iterable,
    proposed_prices: dict | None = None,
    *,
    use_buybox_recommendation: bool = False,
) -> dict:
    """Build a deterministic batch price plan for the selected Worten offers.

    In automatic mode every lost Buy Box row uses the current recommended price,
    so the user can send selected offers without first copying the recommendation
    into the editable table.  Won rows are deliberately left unchanged.
    """
    selected = {
        str(value or "").strip()
        for value in selected_skus
        if str(value or "").strip()
    }
    prepared_prices = {
        str(key or "").strip(): parsed
        for key, value in (proposed_prices or {}).items()
        if str(key or "").strip()
        and (parsed := _number(value)) is not None
    }
    updates = []
    unavailable = []
    unchanged = []
    for item in items:
        sku = str(item.get("sku") or "").strip()
        if not sku or sku not in selected:
            continue
        current_price = _number(item.get("our_price"))
        outcome = str(item.get("_buybox_outcome") or buybox_outcome(item))
        if use_buybox_recommendation and outcome == "Vinte":
            unchanged.append({
                "sku": sku,
                "ean": item.get("ean"),
                "reason": "Buy Box già vinta",
            })
            continue
        proposed_price = (
            buybox_alignment_price(item)
            if use_buybox_recommendation
            else _number(prepared_prices.get(sku, current_price))
        )
        if current_price is None:
            unavailable.append({
                "sku": sku,
                "ean": item.get("ean"),
                "reason": "Prezzo attuale non disponibile",
            })
            continue
        if proposed_price is None or proposed_price <= 0:
            unavailable.append({
                "sku": sku,
                "ean": item.get("ean"),
                "reason": (
                    "Prezzo consigliato Buy Box non disponibile"
                    if use_buybox_recommendation
                    else "Prezzo preparato non valido"
                ),
            })
            continue
        proposed_price = round(float(proposed_price), 2)
        if abs(proposed_price - float(current_price)) < 0.005:
            unchanged.append({
                "sku": sku,
                "ean": item.get("ean"),
                "reason": "Prezzo già allineato",
            })
            continue
        financials = evaluate_worten_price(
            proposed_price,
            shipping=item.get("our_shipping"),
            commission_pct=item.get("commission_pct"),
            total_cost=item.get("total_cost_eur"),
        )
        updates.append({
            "sku": sku,
            "ean": item.get("ean"),
            "price": proposed_price,
            "previous_price": float(current_price),
            "buybox_outcome": outcome,
            "commission_pct": item.get("commission_pct"),
            "commission_source": item.get("commission_source"),
            "total_cost_eur": item.get("total_cost_eur"),
            "price_source": (
                "Prezzo consigliato Buy Box"
                if use_buybox_recommendation
                else "Prezzo preparato nella tabella"
            ),
            **financials,
        })
    return {
        "updates": updates,
        "unavailable": unavailable,
        "unchanged": unchanged,
        "selected_count": len(selected),
        "mode": (
            "recommended" if use_buybox_recommendation else "prepared"
        ),
    }


def build_price_update_offer_csv(
    updates: Iterable[dict],
    *,
    channel_code: str = "WRT_PT_ONLINE",
) -> bytes:
    """Build a partial OF01 import updating only existing offer prices."""
    channel = str(channel_code or "").strip()
    channel_column = f"price[channel={channel}]"
    if channel_column not in WORTEN_OFFER_COLUMNS:
        raise ValueError("Canale Worten non supportato per l'aggiornamento prezzo.")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=WORTEN_OFFER_COLUMNS,
        delimiter=";",
        lineterminator="\n",
    )
    writer.writeheader()
    seen = set()
    for item in updates:
        sku = str(item.get("sku") or "").strip()
        price = _number(item.get("price"))
        if not sku or sku in seen or price is None or price <= 0:
            continue
        seen.add(sku)
        formatted_price = f"{float(price):.2f}"
        record = {name: "" for name in WORTEN_OFFER_COLUMNS}
        record.update(
            {
                "sku": sku,
                "price": formatted_price,
                channel_column: formatted_price,
                "update-delete": "update",
            }
        )
        writer.writerow(record)
    if not seen:
        raise ValueError("Nessun prezzo Worten valido da aggiornare.")
    return output.getvalue().encode("utf-8-sig")
