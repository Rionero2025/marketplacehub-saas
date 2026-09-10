"""Kaufland payout schedule and support-ticket hold rules.

The calculations in this module deliberately mirror the original Streamlit
implementation.  They are pure so the worker, rolling-deploy repair path and
SQL projection refresh all use the same rules.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

SHIPPED_ORDER_UNIT_STATUSES = {
    "sent", "sent_and_autopaid", "received", "returned", "returned_paid",
}
KAUFLAND_TICKET_STATUSES = (
    "opened", "buyer_closed", "seller_closed", "both_closed",
    "customer_service_closed_final",
)
CANCELLED_STATUSES = {"cancelled", "canceled"}
LEGACY_RECEIVED_FALLBACK_SOURCE = "API Kaufland: aggiornamento allo stato Ricevuto"


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def parse_timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso_seconds(value) -> str:
    parsed = parse_timestamp(value)
    return parsed.isoformat(timespec="seconds") if parsed else ""


def payment_schedule(
    status: str,
    received_at: str = "",
    released_at: str = "",
    shipped_at: str = "",
    has_tracking: bool = False,
    ticket_delay_seconds: float = 0.0,
    ticket_open: bool = False,
    *,
    current_time: datetime | None = None,
) -> dict:
    """Apply the original Kaufland payout-release rules."""
    normalized_status = _text(status).lower()
    now = current_time or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    release = parse_timestamp(released_at)
    if release is not None:
        days = (release.date() - now.date()).days
        label = (
            f"Disponibile tra {days} giorni" if days > 0
            else "Disponibile oggi" if days == 0
            else f"Disponibile da {abs(days)} giorni"
        )
        return {
            "received_at": iso_seconds(received_at),
            "payment_due_at": release.isoformat(timespec="seconds"),
            "payment_days_remaining": days,
            "payment_available": days <= 0 or normalized_status == "sent_and_autopaid",
            "payment_status": label,
            "payment_date_final": True,
            "payment_rule": "Data effettiva comunicata da Kaufland",
            "ticket_delay_days": 0.0,
            "ticket_open": False,
        }
    if normalized_status == "sent_and_autopaid":
        return {
            "received_at": iso_seconds(received_at),
            "payment_due_at": "",
            "payment_days_remaining": None,
            "payment_available": True,
            "payment_status": "Ricavato già disponibile · data non disponibile",
            "payment_date_final": False,
            "payment_rule": "Pagamento confermato da Kaufland",
            "ticket_delay_days": 0.0,
            "ticket_open": False,
        }

    delivery, shipped = parse_timestamp(received_at), parse_timestamp(shipped_at)
    if has_tracking:
        if delivery is None:
            return {
                "received_at": "", "payment_due_at": "",
                "payment_days_remaining": None, "payment_available": False,
                "payment_status": "Tracking presente · consegna non ancora rilevata",
                "payment_date_final": False,
                "payment_rule": "Con tracking: consegna + 14 giorni",
                "ticket_delay_days": 0.0, "ticket_open": bool(ticket_open),
            }
        due, rule = delivery + timedelta(days=14), "Con tracking: consegna + 14 giorni"
    elif normalized_status in SHIPPED_ORDER_UNIT_STATUSES:
        if shipped is None:
            return {
                "received_at": iso_seconds(received_at), "payment_due_at": "",
                "payment_days_remaining": None, "payment_available": False,
                "payment_status": "Data di spedizione non disponibile",
                "payment_date_final": False,
                "payment_rule": "Senza tracking: spedizione + 21 giorni",
                "ticket_delay_days": 0.0, "ticket_open": bool(ticket_open),
            }
        due, rule = shipped + timedelta(days=21), "Senza tracking: spedizione + 21 giorni"
    else:
        return {
            "received_at": iso_seconds(received_at), "payment_due_at": "",
            "payment_days_remaining": None, "payment_available": False,
            "payment_status": "Non ancora spedito", "payment_date_final": False,
            "payment_rule": "In attesa della spedizione", "ticket_delay_days": 0.0,
            "ticket_open": bool(ticket_open),
        }

    delay_seconds = max(0.0, float(ticket_delay_seconds or 0.0))
    due += timedelta(seconds=delay_seconds)
    days = (due.date() - now.date()).days
    label = (
        "Ticket aperto · data in aggiornamento" if ticket_open
        else f"Tra {days} giorni" if days > 0
        else "Disponibile oggi" if days == 0
        else f"Disponibile da {abs(days)} giorni"
    )
    return {
        "received_at": delivery.isoformat(timespec="seconds") if delivery else "",
        "payment_due_at": due.isoformat(timespec="seconds"),
        "payment_days_remaining": days,
        "payment_available": days <= 0 and not ticket_open,
        "payment_status": label,
        "payment_date_final": not ticket_open,
        "payment_rule": rule,
        "ticket_delay_days": round(delay_seconds / 86400.0, 2),
        "ticket_open": bool(ticket_open),
    }


def ticket_holds(tickets: list[dict], *, current_time: datetime | None = None) -> dict[str, dict]:
    """Merge overlapping ticket intervals and group their hold by order unit."""
    now = current_time or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    intervals_by_unit: dict[str, list[tuple[datetime, datetime, bool, str]]] = {}
    for ticket in tickets or []:
        unit_ids = ticket.get("ids_order_units") or ticket.get("order_unit_ids") or []
        if not isinstance(unit_ids, list | tuple):
            unit_ids = [unit_ids]
        start = parse_timestamp(ticket.get("ts_created_iso") or ticket.get("created_at"))
        is_open = _text(ticket.get("status")).lower() == "opened"
        end = now if is_open else parse_timestamp(
            ticket.get("ts_updated_iso") or ticket.get("updated_at")
        )
        if start is None or end is None or end < start:
            continue
        ticket_id = _text(ticket.get("id_ticket") or ticket.get("external_ticket_id"))
        for unit_id in unit_ids:
            clean_id = _text(unit_id)
            if clean_id:
                intervals_by_unit.setdefault(clean_id, []).append(
                    (start, end, is_open, ticket_id)
                )
    result = {}
    for unit_id, intervals in intervals_by_unit.items():
        intervals.sort(key=lambda item: item[0])
        merged: list[list[datetime]] = []
        for start, end, _is_open, _ticket_id in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        result[unit_id] = {
            "ticket_count": len(intervals),
            "open_ticket_count": sum(1 for item in intervals if item[2]),
            "delay_seconds": sum((end - start).total_seconds() for start, end in merged),
            "ticket_ids": list(dict.fromkeys(item[3] for item in intervals if item[3])),
        }
    return result


def repair_payment_event_details(item: dict, raw: dict | None) -> dict:
    """Repair archived payment timestamps from the immutable provider payload."""
    result = dict(item)
    details = dict(result.get("details") or {})
    if _text(result.get("marketplace")).lower() != "kaufland" or not isinstance(raw, dict):
        result["details"] = details
        return result

    # Imported lazily to keep the schedule functions independent from the
    # larger normalization module while sharing its exact API-key precedence.
    from marketplace_hub_core.orders.normalization import kaufland_payment_timestamps

    events = kaufland_payment_timestamps(raw, _text(result.get("status")).lower())
    if events["received_at"]:
        details["received_at"] = events["received_at"]
        details["received_source"] = events["received_source"]
    elif _text(details.get("received_source")) == LEGACY_RECEIVED_FALLBACK_SOURCE:
        # Old releases mistook ts_updated_iso for delivery.  It may instead be
        # the release update, so both that value and its estimate are unsafe.
        details["received_at"] = None
        details["received_source"] = ""
    for prefix in ("shipped", "released"):
        value = events[f"{prefix}_at"]
        if value:
            details[f"{prefix}_at"] = value
            details[f"{prefix}_source"] = events[f"{prefix}_source"]
    result["details"] = details
    return result


def apply_payment_details(
    item: dict, hold: dict | None = None, *, current_time: datetime | None = None,
) -> dict:
    """Return a canonical row with payment and ticket fields in ``details``."""
    result = dict(item)
    details = dict(result.get("details") or {})
    if str(result.get("marketplace") or "").lower() != "kaufland":
        result["details"] = details
        return result
    hold = hold or {}
    payment = payment_schedule(
        str(result.get("status") or ""),
        received_at=str(details.get("received_at") or ""),
        released_at=str(details.get("released_at") or ""),
        shipped_at=str(details.get("shipped_at") or ""),
        has_tracking=bool(str(details.get("tracking") or "").strip()),
        ticket_delay_seconds=float(hold.get("delay_seconds") or 0.0),
        ticket_open=bool(hold.get("open_ticket_count")),
        current_time=current_time,
    )
    # ``received_at`` is an existing API event field.  Keep its original None/
    # string representation; the schedule only reads it and must not rewrite it.
    payment.pop("received_at", None)
    details.update(payment)
    details["payment_source"] = (
        str(details.get("released_source") or "").strip() or payment["payment_rule"]
    )
    details["ticket_count"] = int(hold.get("ticket_count") or 0)
    details["open_ticket_count"] = int(hold.get("open_ticket_count") or 0)
    details["ticket_ids"] = [str(value) for value in hold.get("ticket_ids", []) if str(value)]
    result["details"] = details
    return result


def selected_payment_deadline(items: list[dict]) -> dict:
    active = [
        item for item in items or []
        if _text(item.get("status")).lower() not in CANCELLED_STATUSES
    ]
    scheduled, unscheduled_ids = [], []
    for item in active:
        nested = item.get("details") if isinstance(item.get("details"), dict) else {}
        details = {**item, **nested}
        identity = _text(item.get("external_line_id") or item.get("id_order_unit")
                         or item.get("order_id") or item.get("id"))
        if not bool(details.get("payment_date_final", True)):
            unscheduled_ids.append(identity)
            continue
        due = parse_timestamp(details.get("payment_due_at"))
        if due is None:
            unscheduled_ids.append(identity)
        else:
            scheduled.append(due)
    latest = max(scheduled) if scheduled else None
    return {
        "payable_units": len(active), "scheduled_units": len(scheduled),
        "unscheduled_units": len(unscheduled_ids), "unscheduled_ids": unscheduled_ids,
        "ignored_cancelled_units": len(items or []) - len(active),
        "all_available": bool(active) and all(bool(
            (item.get("details") or item).get("payment_available")
        ) for item in active),
        "latest_payment_due_at": latest.isoformat(timespec="seconds") if latest else "",
        "all_dates_known": bool(active) and not unscheduled_ids,
    }


def selected_order_financial_summary(items: list[dict]) -> dict:
    result = {
        "selected_units": len(items or []), "payable_units": 0, "cancelled_units": 0,
        "payout_eur": 0.0, "purchase_cost_eur": 0.0, "profit_eur": 0.0,
        "known_cost_units": 0, "unknown_cost_units": 0, "available_units": 0,
        "available_eur": 0.0, "waiting_units": 0, "waiting_eur": 0.0,
    }

    def number(value):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    for item in items or []:
        if _text(item.get("status")).lower() in CANCELLED_STATUSES:
            result["cancelled_units"] += 1
            continue
        nested = item.get("details") if isinstance(item.get("details"), dict) else {}
        details = {**item, **nested}
        result["payable_units"] += 1
        payout = number(item.get("payout_amount_eur", item.get("payout_eur")))
        if payout is not None:
            result["payout_eur"] += payout
        if bool(details.get("payment_available")):
            result["available_units"] += 1
            result["available_eur"] += payout or 0.0
        else:
            result["waiting_units"] += 1
            result["waiting_eur"] += payout or 0.0
        purchase = number(item.get("purchase_cost_eur"))
        profit = number(item.get("profit_amount_eur", item.get("order_profit_eur")))
        if purchase is None or profit is None:
            result["unknown_cost_units"] += 1
        else:
            result["known_cost_units"] += 1
            result["purchase_cost_eur"] += purchase
            result["profit_eur"] += profit
    for key in ("payout_eur", "purchase_cost_eur", "profit_eur", "available_eur", "waiting_eur"):
        result[key] = round(result[key], 2)
    return result
