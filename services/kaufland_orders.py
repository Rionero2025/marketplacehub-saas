from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from services.db import connect, execute, json_text, now_iso, row, rows


ProgressCallback = Callable[[int, int, str], None]

STOREFRONT_INFO = {
    "de": ("Germania", "DE", "EUR"),
    "at": ("Austria", "AT", "EUR"),
    "fr": ("Francia", "FR", "EUR"),
    "it": ("Italia", "IT", "EUR"),
    "pl": ("Polonia", "PL", "PLN"),
    "cz": ("Rep. Ceca", "CZ", "CZK"),
    "sk": ("Slovacchia", "SK", "EUR"),
}

STATUS_LABELS = {
    "open": "Aperto",
    "need_to_be_sent": "Da spedire",
    "sent": "Spedito",
    "sent_and_autopaid": "Spedito e pagato automaticamente",
    "received": "Ricevuto",
    "returned": "Restituito",
    "returned_paid": "Reso rimborsato",
    "cancelled": "Cancellato",
    "canceled": "Cancellato",
}

# Valori accettati dal filtro ``status`` di GET /order-units. Non aggiungere
# stati dell'interfaccia Seller Portal che non fanno parte dell'enum API:
# l'intera sincronizzazione verrebbe rifiutata con HTTP 400.
ORDER_UNIT_STATUSES = (
    "cancelled",
    "need_to_be_sent",
    "open",
    "received",
    "returned",
    "returned_paid",
    "sent",
    "sent_and_autopaid",
)

SHIPPED_ORDER_UNIT_STATUSES = {
    "sent",
    "sent_and_autopaid",
    "received",
    "returned",
    "returned_paid",
}

RECEIVED_TIMESTAMP_KEYS = (
    "order_received_timestamp_iso",
    "ts_received_iso",
    "received_at",
    "received_at_iso",
    "ts_delivered_iso",
    "delivered_at",
    "delivery_date",
)

SHIPPED_TIMESTAMP_KEYS = (
    "order_sent_timestamp_iso",
    "ts_sent_iso",
    "sent_at_iso",
    "sent_at",
    "ts_shipped_iso",
    "shipped_at_iso",
    "shipped_at",
)

PAYMENT_RELEASE_TIMESTAMP_KEYS = (
    "revenue_released_timestamp_iso",
    "revenue_released_at",
    "payout_timestamp_iso",
    "payout_at",
    "payment_timestamp_iso",
    "paid_at_iso",
    "paid_at",
)

COMMISSION_KEYS = (
    "commission",
    "commission_amount",
    "commission_gross",
    "marketplace_fee",
    "marketplace_commission",
)


def response_data(response) -> list[dict]:
    if isinstance(response, dict):
        data = response.get("data", [])
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    return []


def response_item(response) -> dict:
    if not isinstance(response, dict):
        return {}
    data = response.get("data", response)
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    return data if isinstance(data, dict) else {}


def _pagination_total(response, fallback: int) -> int:
    if not isinstance(response, dict):
        return fallback
    pagination = response.get("pagination") or {}
    try:
        return max(fallback, int(pagination.get("total", fallback)))
    except (TypeError, ValueError):
        return fallback


def fetch_order_units(
    client,
    maximum: int | None = None,
    progress: ProgressCallback | None = None,
    statuses: tuple[str, ...] | list[str] | None = None,
) -> list[dict]:
    """Read order units for every requested state in API pages of 100 rows.

    Kaufland returns mainly units which still need seller fulfilment when the
    status filter is omitted. Explicit status requests are therefore required
    to include historical sent and cancelled units.
    """
    cap = None if maximum is None else max(1, int(maximum))
    requested_statuses = tuple(statuses or ORDER_UNIT_STATUSES)
    result: list[dict] = []
    seen: set[str] = set()
    for status_index, status in enumerate(requested_statuses, start=1):
        status_result: list[dict] = []
        offset = 0
        status_total = cap or 100
        while cap is None or len(status_result) < cap:
            page_limit = (
                100 if cap is None else min(100, cap - len(status_result))
            )
            response = client.order_units(
                limit=page_limit,
                offset=offset,
                status=status,
            )
            page = response_data(response)
            status_result.extend(page)
            status_total = _pagination_total(response, len(status_result))
            if progress:
                progress(
                    status_index,
                    max(1, len(requested_statuses)),
                    (
                        f"Download ordini {status}: "
                        f"{min(len(status_result), status_total)}/"
                        f"{status_total}"
                    ),
                )
            if (
                not page
                or len(page) < page_limit
                or len(status_result) >= status_total
            ):
                break
            offset += len(page)
        for item in status_result:
            unit_id = _text(item.get("id_order_unit"))
            identity = unit_id or json_text(item)
            if identity not in seen:
                seen.add(identity)
                result.append(item)

    result.sort(
        key=lambda item: (
            _text(item.get("ts_created_iso")),
            _text(item.get("ts_updated_iso")),
            _text(item.get("id_order_unit")),
        ),
        reverse=True,
    )
    return result if cap is None else result[:cap]


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _minor_money(value):
    if value in (None, ""):
        return None
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _minor_money_value(value):
    """Read a Kaufland monetary value which may be scalar or an amount object."""
    if isinstance(value, dict):
        for key in ("amount", "value", "gross", "total"):
            if value.get(key) not in (None, ""):
                return _minor_money(value.get(key))
        return None
    return _minor_money(value)


def _parse_iso(value) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_seconds(value) -> str:
    parsed = _parse_iso(value)
    return parsed.isoformat(timespec="seconds") if parsed else ""


def _received_timestamp(raw: dict, status: str) -> tuple[str, str]:
    if status not in SHIPPED_ORDER_UNIT_STATUSES:
        return "", ""
    for key in RECEIVED_TIMESTAMP_KEYS:
        value = _iso_seconds(raw.get(key))
        if value:
            return value, f"API Kaufland: {key}"
    return "", ""


def _shipped_timestamp(raw: dict, status: str) -> tuple[str, str]:
    if status not in SHIPPED_ORDER_UNIT_STATUSES:
        return "", ""
    for key in SHIPPED_TIMESTAMP_KEYS:
        value = _iso_seconds(raw.get(key))
        if value:
            return value, f"API Kaufland: {key}"
    if status == "sent":
        value = _iso_seconds(raw.get("ts_updated_iso"))
        if value:
            return value, "API Kaufland: passaggio a sent (ts_updated_iso)"
    return "", ""


def _payment_release_timestamp(raw: dict, status: str) -> tuple[str, str]:
    """Return Kaufland's actual proceeds-release time when it is available."""
    for key in PAYMENT_RELEASE_TIMESTAMP_KEYS:
        value = _iso_seconds(raw.get(key))
        if value:
            return value, f"API Kaufland: {key}"
    if status == "sent_and_autopaid":
        value = _iso_seconds(raw.get("ts_updated_iso"))
        if value:
            return (
                value,
                "API Kaufland: stato sent_and_autopaid (ts_updated_iso)",
            )
    return "", ""


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
    """Apply Kaufland's release rules and any ticket-related postponement.

    With tracking, proceeds are estimated 14 days after delivery. Without
    tracking, they are estimated 21 days after the unit was marked as sent.
    An already-released timestamp always wins. Open tickets keep the final date
    provisional; closed-ticket durations are added to the base release date.
    """
    normalized_status = _text(status).lower()
    now = current_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    release = _parse_iso(released_at)
    if release is not None:
        days = (release.date() - now.date()).days
        if days > 0:
            label = f"Disponibile tra {days} giorni"
        elif days == 0:
            label = "Disponibile oggi"
        else:
            label = f"Disponibile da {abs(days)} giorni"
        return {
            "received_at": _iso_seconds(received_at),
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
            "received_at": _iso_seconds(received_at),
            "payment_due_at": "",
            "payment_days_remaining": None,
            "payment_available": True,
            "payment_status": "Ricavato già disponibile · data non disponibile",
            "payment_date_final": False,
            "payment_rule": "Pagamento confermato da Kaufland",
            "ticket_delay_days": 0.0,
            "ticket_open": False,
        }
    delivery = _parse_iso(received_at)
    shipped = _parse_iso(shipped_at)
    if has_tracking:
        if delivery is None:
            return {
                "received_at": "",
                "payment_due_at": "",
                "payment_days_remaining": None,
                "payment_available": False,
                "payment_status": "Tracking presente · consegna non ancora rilevata",
                "payment_date_final": False,
                "payment_rule": "Con tracking: consegna + 14 giorni",
                "ticket_delay_days": 0.0,
                "ticket_open": bool(ticket_open),
            }
        due = delivery + timedelta(days=14)
        rule = "Con tracking: consegna + 14 giorni"
    elif normalized_status in SHIPPED_ORDER_UNIT_STATUSES:
        if shipped is None:
            return {
                "received_at": _iso_seconds(received_at),
                "payment_due_at": "",
                "payment_days_remaining": None,
                "payment_available": False,
                "payment_status": "Data di spedizione non disponibile",
                "payment_date_final": False,
                "payment_rule": "Senza tracking: spedizione + 21 giorni",
                "ticket_delay_days": 0.0,
                "ticket_open": bool(ticket_open),
            }
        due = shipped + timedelta(days=21)
        rule = "Senza tracking: spedizione + 21 giorni"
    else:
        return {
            "received_at": _iso_seconds(received_at),
            "payment_due_at": "",
            "payment_days_remaining": None,
            "payment_available": False,
            "payment_status": "Non ancora spedito",
            "payment_date_final": False,
            "payment_rule": "In attesa della spedizione",
            "ticket_delay_days": 0.0,
            "ticket_open": bool(ticket_open),
        }
    delay_seconds = max(0.0, float(ticket_delay_seconds or 0.0))
    due += timedelta(seconds=delay_seconds)
    delay_days = round(delay_seconds / 86400.0, 2)
    days = (due.date() - now.date()).days
    if ticket_open:
        label = "Ticket aperto · data in aggiornamento"
    elif days > 0:
        label = f"Tra {days} giorni"
    elif days == 0:
        label = "Disponibile oggi"
    else:
        label = f"Disponibile da {abs(days)} giorni"
    return {
        "received_at": delivery.isoformat(timespec="seconds") if delivery else "",
        "payment_due_at": due.isoformat(timespec="seconds"),
        "payment_days_remaining": days,
        "payment_available": days <= 0 and not ticket_open,
        "payment_status": label,
        "payment_date_final": not ticket_open,
        "payment_rule": rule,
        "ticket_delay_days": delay_days,
        "ticket_open": bool(ticket_open),
    }


def ticket_holds(
    account_id: int,
    environment: str,
    *,
    current_time: datetime | None = None,
) -> dict[str, dict]:
    """Return ticket delay intervals grouped by Kaufland order unit."""
    now = current_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    intervals_by_unit: dict[str, list[tuple[datetime, datetime, bool, str]]] = {}
    for ticket in rows(
        """
        SELECT id_ticket,ids_order_units_json,ts_created_iso,ts_updated_iso,status
        FROM kaufland_support_tickets
        WHERE marketplace_account_id=? AND environment=?
        """,
        (account_id, environment),
    ):
        try:
            unit_ids = json.loads(ticket.get("ids_order_units_json") or "[]")
        except (TypeError, ValueError):
            unit_ids = []
        start = _parse_iso(ticket.get("ts_created_iso"))
        is_open = _text(ticket.get("status")).lower() == "opened"
        end = now if is_open else _parse_iso(ticket.get("ts_updated_iso"))
        if start is None or end is None or end < start:
            continue
        for unit_id in unit_ids if isinstance(unit_ids, list) else []:
            clean_id = _text(unit_id)
            if clean_id:
                intervals_by_unit.setdefault(clean_id, []).append(
                    (start, end, is_open, _text(ticket.get("id_ticket")))
                )
    result: dict[str, dict] = {}
    for unit_id, intervals in intervals_by_unit.items():
        intervals.sort(key=lambda item: item[0])
        merged: list[list] = []
        for start, end, is_open, ticket_id in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        delay_seconds = sum(
            (end - start).total_seconds() for start, end in merged
        )
        result[unit_id] = {
            "ticket_count": len(intervals),
            "open_ticket_count": sum(1 for item in intervals if item[2]),
            "delay_seconds": delay_seconds,
            "ticket_ids": list(dict.fromkeys(
                item[3] for item in intervals if item[3]
            )),
        }
    return result


def selected_payment_deadline(items: list[dict]) -> dict:
    """Return the last known payout date for a selected order block."""
    active_items = [
        item
        for item in items or []
        if _text(item.get("status")).lower() not in {"cancelled", "canceled"}
    ]
    scheduled = []
    unscheduled_ids = []
    for item in active_items:
        if not bool(item.get("payment_date_final", True)):
            unscheduled_ids.append(
                _text(item.get("id_order_unit"))
                or _text(item.get("id_order"))
                or _text(item.get("id"))
            )
            continue
        due = _parse_iso(item.get("payment_due_at"))
        if due is None:
            unscheduled_ids.append(
                _text(item.get("id_order_unit"))
                or _text(item.get("id_order"))
                or _text(item.get("id"))
            )
            continue
        scheduled.append(due)
    latest = max(scheduled) if scheduled else None
    return {
        "payable_units": len(active_items),
        "scheduled_units": len(scheduled),
        "unscheduled_units": len(unscheduled_ids),
        "unscheduled_ids": unscheduled_ids,
        "ignored_cancelled_units": len(items or []) - len(active_items),
        "all_available": bool(active_items) and all(
            bool(item.get("payment_available")) for item in active_items
        ),
        "latest_payment_due_at": (
            latest.isoformat(timespec="seconds") if latest else ""
        ),
        "all_dates_known": bool(active_items) and not unscheduled_ids,
    }


def selected_order_financial_summary(items: list[dict]) -> dict:
    """Summarize only the order rows explicitly selected for payout details."""
    result = {
        "selected_units": len(items or []),
        "payable_units": 0,
        "cancelled_units": 0,
        "payout_eur": 0.0,
        "purchase_cost_eur": 0.0,
        "profit_eur": 0.0,
        "known_cost_units": 0,
        "unknown_cost_units": 0,
        "available_units": 0,
        "available_eur": 0.0,
        "waiting_units": 0,
        "waiting_eur": 0.0,
    }

    def number(value) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    for item in items or []:
        status = _text(item.get("status")).lower()
        if status in {"cancelled", "canceled"}:
            result["cancelled_units"] += 1
            continue
        result["payable_units"] += 1
        payout = number(item.get("payout_eur"))
        if payout is not None:
            result["payout_eur"] += payout
        if bool(item.get("payment_available")):
            result["available_units"] += 1
            result["available_eur"] += payout or 0.0
        else:
            result["waiting_units"] += 1
            result["waiting_eur"] += payout or 0.0

        purchase = number(item.get("purchase_cost_eur"))
        profit = number(item.get("order_profit_eur"))
        if purchase is None or profit is None:
            result["unknown_cost_units"] += 1
            continue
        result["known_cost_units"] += 1
        result["purchase_cost_eur"] += purchase
        result["profit_eur"] += profit

    for key in (
        "payout_eur",
        "purchase_cost_eur",
        "profit_eur",
        "available_eur",
        "waiting_eur",
    ):
        result[key] = round(result[key], 2)
    return result


def _first_ean(product: dict) -> str:
    values = product.get("eans") or product.get("ean") or []
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, (list, tuple)):
        values = [values]
    return next((_text(value) for value in values if _text(value)), "")


TRACKING_KEYS = {
    "tracking_numbers",
    "tracking_number",
    "tracking_code",
    "tracking_id",
    "parcel_number",
    "shipment_number",
}
TRACKING_CARRIER_KEYS = {
    "carrier_code",
    "carrier_name",
    "tracking_provider",
    "shipping_provider",
    "shipping_carrier",
    "carrier",
    "provider",
}

TRACKING_IMPORT_ALIASES = {
    "id_order_unit": {
        "idorderunit", "orderunitid", "orderunit", "unitaordine",
        "unitaordineid", "idunitaordine", "bestellpositionid", "orderitemid",
    },
    "id_order": {
        "idorder", "orderid", "ordernumber", "ordine", "numeroordine",
        "bestellnummer", "bestellung", "kauflandorderid",
    },
    "carrier_code": {
        "carriercode", "carrier", "carriername", "corriere", "spedito con",
        "speditocon", "versanddienstleister", "shippingprovider",
        "shippingcarrier",
    },
    "tracking_numbers": {
        "trackingnumbers", "trackingnumber", "tracking", "trackingcode",
        "numerotracking", "numerospedizione", "tracciabilita",
        "sendungsnummer", "paketnummer", "parcelnumber",
    },
    "combined_shipment": {
        "shipment", "shipmentinformation", "shippinginformation",
        "informazionispedizione", "spedizione", "versandinformation",
    },
}


def normalized_header(value) -> str:
    text = unicodedata.normalize("NFKD", _text(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def detect_tracking_columns(columns) -> dict[str, str]:
    """Map common Kaufland/export header names to tracking fields."""
    result: dict[str, str] = {}
    for column in columns:
        normalized = normalized_header(column)
        for field, aliases in TRACKING_IMPORT_ALIASES.items():
            normalized_aliases = {normalized_header(alias) for alias in aliases}
            if field not in result and normalized in normalized_aliases:
                result[field] = str(column)
                break
    return result


def split_shipment_text(value) -> tuple[str, str]:
    """Read values such as ``DPD | 08448875901263`` from portal exports."""
    text = _text(value)
    if not text:
        return "", ""
    for separator in ("|", " - ", ";"):
        if separator in text:
            carrier, tracking = text.split(separator, 1)
            return carrier.strip(), tracking.strip()
    return "", text


def save_order_tracking(
    seller_id: int,
    account_id: int,
    environment: str,
    *,
    id_order_unit: str = "",
    id_order: str = "",
    carrier_code: str = "",
    tracking_numbers: str = "",
) -> int:
    """Persist shipment data without overwriting a non-empty value with blank.

    The public Kaufland GET order endpoints currently do not expose historical
    outbound tracking registered in Seller Portal. This local ledger is
    therefore also used for portal-export and manual backfills.
    """
    unit_id = _text(id_order_unit)
    order_id = _text(id_order)
    carrier = _text(carrier_code)
    tracking = ", ".join(dict.fromkeys(_tracking_values(tracking_numbers)))
    if not unit_id and not order_id:
        raise ValueError("Indica l'ID unità ordine oppure il numero ordine.")
    if not carrier and not tracking:
        raise ValueError("Indica almeno il corriere oppure il tracking.")
    where = "id_order_unit=?" if unit_id else "id_order=?"
    identifier = unit_id or order_id
    with connect() as con:
        cursor = con.execute(
            f"""
            UPDATE kaufland_order_units SET
                carrier_code=CASE WHEN ?<>'' THEN ? ELSE carrier_code END,
                tracking_numbers=CASE
                    WHEN ?<>'' THEN ? ELSE tracking_numbers END,
                synced_at=?
            WHERE seller_id=? AND marketplace_account_id=? AND environment=?
              AND {where}
            """,
            (
                carrier, carrier, tracking, tracking, now_iso(), seller_id,
                account_id, environment, identifier,
            ),
        )
        return max(0, int(cursor.rowcount or 0))


def import_order_tracking(
    seller_id: int,
    account_id: int,
    environment: str,
    records: list[dict],
    column_map: dict[str, str],
) -> dict:
    """Import carrier/tracking from a Seller Portal CSV/XLSX export."""
    updated = 0
    unmatched: list[dict] = []
    invalid: list[dict] = []
    for index, record in enumerate(records, start=1):
        unit_id = _text(record.get(column_map.get("id_order_unit", "")))
        order_id = _text(record.get(column_map.get("id_order", "")))
        carrier = _text(record.get(column_map.get("carrier_code", "")))
        tracking = _text(record.get(column_map.get("tracking_numbers", "")))
        combined = _text(record.get(column_map.get("combined_shipment", "")))
        if combined:
            combined_carrier, combined_tracking = split_shipment_text(combined)
            carrier = carrier or combined_carrier
            tracking = tracking or combined_tracking
        if not unit_id and not order_id:
            invalid.append({"riga": index, "errore": "Identificativo ordine assente"})
            continue
        if not carrier and not tracking:
            invalid.append({"riga": index, "errore": "Tracking/corriere assente"})
            continue
        changed = save_order_tracking(
            seller_id,
            account_id,
            environment,
            id_order_unit=unit_id,
            id_order=order_id,
            carrier_code=carrier,
            tracking_numbers=tracking,
        )
        if changed:
            updated += changed
        else:
            unmatched.append({
                "riga": index,
                "unita_ordine": unit_id,
                "ordine": order_id,
            })
    return {
        "updated": updated,
        "unmatched": unmatched,
        "invalid": invalid,
    }


def _tracking_values(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, dict):
        result: list[str] = []
        matching_values = [
            nested_value
            for key, nested_value in value.items()
            if key in TRACKING_KEYS
        ]
        for nested_value in matching_values:
            result.extend(_tracking_values(nested_value))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for nested_value in value:
            result.extend(_tracking_values(nested_value))
        return result
    return [
        part.strip()
        for part in str(value).split(",")
        if part.strip()
    ]


def _tracking(raw: dict) -> tuple[str, str]:
    """Extract shipment data from direct and nested Kaufland responses."""
    carriers: list[str] = []
    tracking_numbers: list[str] = []

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return

        node_tracking: list[str] = []
        for key in TRACKING_KEYS:
            if key in value:
                node_tracking.extend(_tracking_values(value.get(key)))
        if node_tracking:
            tracking_numbers.extend(node_tracking)
            for key in TRACKING_CARRIER_KEYS:
                carrier = _text(value.get(key))
                if carrier:
                    carriers.append(carrier)
                    break

        # Top-level shipment responses may contain a carrier while tracking is
        # stored in a child object or array.
        if value is raw:
            for key in TRACKING_CARRIER_KEYS:
                carrier = _text(value.get(key))
                if carrier:
                    carriers.append(carrier)
                    break

        for nested_value in value.values():
            if isinstance(nested_value, (dict, list)):
                visit(nested_value)

    visit(raw)
    carrier = ", ".join(dict.fromkeys(
        value for value in carriers if value
    ))
    tracking = ", ".join(dict.fromkeys(
        value for value in tracking_numbers if value
    ))
    return carrier, tracking


def find_order_unit(raw, id_order_unit: str) -> dict:
    """Find a specific order unit inside an order-detail response."""
    wanted = _text(id_order_unit)
    if isinstance(raw, list):
        for item in raw:
            found = find_order_unit(item, wanted)
            if found:
                return found
        return {}
    if not isinstance(raw, dict):
        return {}
    if _text(raw.get("id_order_unit")) == wanted:
        return raw
    for value in raw.values():
        if isinstance(value, (dict, list)):
            found = find_order_unit(value, wanted)
            if found:
                return found
    return {}


def _explicit_commission(raw: dict):
    for key in COMMISSION_KEYS:
        value = _minor_money_value(raw.get(key))
        if value is not None:
            return abs(value), f"API Kaufland: {key}"
    fees = raw.get("fees")
    if isinstance(fees, dict):
        for key in COMMISSION_KEYS:
            value = _minor_money_value(fees.get(key))
            if value is not None:
                return abs(value), f"API Kaufland: fees.{key}"
    if isinstance(fees, list):
        commission_total = 0.0
        found = False
        for fee in fees:
            if not isinstance(fee, dict):
                continue
            fee_type = _text(
                fee.get("type") or fee.get("name") or fee.get("code")
            ).lower()
            if "commission" not in fee_type:
                continue
            value = _minor_money_value(fee)
            if value is not None:
                commission_total += abs(value)
                found = True
        if found:
            return round(commission_total, 2), "API Kaufland: fees"
    return None, ""


def normalize_order_unit(raw: dict) -> dict:
    product = raw.get("product") or {}
    if not isinstance(product, dict):
        product = {}
    storefront = _text(raw.get("storefront")).lower()
    country_name, country_code, default_currency = STOREFRONT_INFO.get(
        storefront, (storefront.upper() or "Sconosciuto", storefront.upper(), "")
    )
    product_price = _minor_money(raw.get("price"))
    shipping = _minor_money(raw.get("shipping_rate"))
    revenue_gross = _minor_money(raw.get("revenue_gross"))
    revenue_net = _minor_money(raw.get("revenue_net"))
    sold_total = (
        round((product_price or 0.0) + (shipping or 0.0), 2)
        if product_price is not None or shipping is not None else None
    )
    # Kaufland exposes product proceeds after the exact order commission in
    # revenue_gross. The commission is charged on the customer total including
    # shipping, but is reflected in the difference between product price and
    # revenue_gross. Prefer an explicit fee field if a tenant returns one.
    commission, commission_source = _explicit_commission(raw)
    if commission is None and product_price is not None and revenue_gross is not None:
        commission = round(max(0.0, product_price - revenue_gross), 2)
        commission_source = "API Kaufland: price − revenue_gross"
    commission_pct = (
        round((commission / sold_total) * 100.0, 4)
        if commission is not None and sold_total not in (None, 0) else None
    )
    payout = (
        round(sold_total - commission, 2)
        if sold_total is not None and commission is not None
        else (
            round(revenue_gross + (shipping or 0.0), 2)
            if revenue_gross is not None else None
        )
    )
    status = _text(raw.get("status")).lower()
    carrier, tracking = _tracking(raw)
    received_at, received_source = _received_timestamp(raw, status)
    shipped_at, shipped_source = _shipped_timestamp(raw, status)
    released_at, payment_source = _payment_release_timestamp(raw, status)
    payment = payment_schedule(
        status,
        received_at=received_at,
        released_at=released_at,
        shipped_at=shipped_at,
        has_tracking=bool(tracking),
    )
    if not payment_source:
        payment_source = payment["payment_rule"]
    return {
        "id_order_unit": _text(raw.get("id_order_unit")),
        "id_order": _text(raw.get("id_order")),
        "storefront": storefront,
        "country_name": country_name,
        "country_code": country_code,
        "currency": _text(raw.get("currency")).upper() or default_currency,
        "ts_created_iso": _text(raw.get("ts_created_iso")),
        "ts_updated_iso": _text(raw.get("ts_updated_iso")),
        "status": status,
        "cancel_reason": _text(raw.get("cancel_reason")),
        "sku": _text(raw.get("id_offer")),
        "ean": _first_ean(product),
        "product_name": _text(product.get("title")),
        "product_url": _text(product.get("url")),
        "product_price_local": product_price,
        "shipping_local": shipping,
        "sold_total_local": sold_total,
        "revenue_gross_local": revenue_gross,
        "revenue_net_local": revenue_net,
        "commission_local": commission,
        "commission_pct": commission_pct,
        "commission_source": commission_source,
        "payout_local": payout,
        "received_at": payment["received_at"],
        "received_source": received_source,
        "shipped_at": shipped_at,
        "shipped_source": shipped_source,
        "payment_due_at": payment["payment_due_at"],
        "payment_source": payment_source,
        "payment_date_final": payment["payment_date_final"],
        "payment_rule": payment["payment_rule"],
        "ticket_delay_days": payment["ticket_delay_days"],
        "ticket_open": payment["ticket_open"],
        "vat_pct": _float_or_none(raw.get("vat")),
        "carrier_code": carrier,
        "tracking_numbers": tracking,
        "raw": raw,
    }


def _float_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def composed_sku_order_financials(
    sku,
    ean,
    payout_eur,
) -> dict:
    """Read cost/minimum from supplier_product-code_cost_minimum.

    The product-code component is deliberately opaque: it may be an EAN, a
    supplier SKU, or any other identifier. Only the final two numeric
    components have a fixed meaning.
    """
    text = _text(sku)
    expected_ean = _text(ean).removesuffix(".0")
    parts = text.rsplit("_", 3)
    empty = {
        "sku_supplier": "",
        "sku_ean": "",
        "sku_product_code": "",
        "order_ean": expected_ean,
        "sku_ean_matches_order": None,
        "sku_ean_note": "SKU composto non riconosciuto",
        "purchase_cost_eur": None,
        "minimum_price_sku_eur": None,
        "order_profit_eur": None,
        "order_profit_pct": None,
        "purchase_cost_method": "Costo non calcolabile",
        "purchase_cost_source": "Costo non calcolabile",
    }
    if (
        len(parts) != 4
        or not parts[0]
        or not parts[1]
    ):
        return empty
    try:
        purchase = float(parts[2].replace(",", "."))
        minimum = float(parts[3].replace(",", "."))
    except (TypeError, ValueError):
        return empty
    if (
        not math.isfinite(purchase)
        or not math.isfinite(minimum)
        or purchase <= 0
        or minimum <= 0
    ):
        return empty
    payout = _float_or_none(payout_eur)
    profit = round(payout - purchase, 2) if payout is not None else None
    profit_pct = (
        round(profit / purchase * 100.0, 2)
        if profit is not None and purchase > 0
        else None
    )
    ean_matches = parts[1] == expected_ean if expected_ean else None
    code_note = (
        "Codice prodotto SKU ed EAN ordine coincidono"
        if ean_matches is True
        else (
            f"Codice prodotto SKU {parts[1]} · EAN ordine {expected_ean}"
            if ean_matches is False
            else (
                f"Codice prodotto SKU {parts[1]} · "
                "EAN ordine non disponibile"
            )
        )
    )
    return {
        "sku_supplier": parts[0],
        "sku_ean": parts[1],
        "sku_product_code": parts[1],
        "order_ean": expected_ean,
        "sku_ean_matches_order": ean_matches,
        "sku_ean_note": code_note,
        "purchase_cost_eur": round(purchase, 2),
        "minimum_price_sku_eur": round(minimum, 2),
        "order_profit_eur": profit,
        "order_profit_pct": profit_pct,
        "purchase_cost_method": "SKU composto",
        "purchase_cost_source": "Terzo valore dello SKU composto",
    }


def merge_order_unit(base: dict, detail: dict) -> dict:
    """Prefer the detailed API response while retaining list-only attributes."""
    merged = dict(base)
    for key, value in detail.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    if isinstance(base.get("product"), dict) or isinstance(detail.get("product"), dict):
        merged["product"] = {
            **(base.get("product") if isinstance(base.get("product"), dict) else {}),
            **(detail.get("product") if isinstance(detail.get("product"), dict) else {}),
        }
    return merged


def upsert_order_unit(
    seller_id: int,
    account_id: int,
    environment: str,
    item: dict,
    detail_checked: bool = False,
) -> None:
    execute(
        """
        INSERT INTO kaufland_order_units(
            seller_id,marketplace_account_id,environment,id_order_unit,id_order,
            storefront,country_code,currency,ts_created_iso,ts_updated_iso,status,
            cancel_reason,sku,ean,product_name,product_url,product_price_local,
            shipping_local,sold_total_local,revenue_gross_local,revenue_net_local,
            commission_local,commission_pct,commission_source,payout_local,
            received_at,received_source,payment_due_at,payment_source,
            vat_pct,carrier_code,
            tracking_numbers,raw_json,detail_checked_at,synced_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,environment,id_order_unit) DO UPDATE SET
            seller_id=excluded.seller_id,id_order=excluded.id_order,
            storefront=excluded.storefront,country_code=excluded.country_code,
            currency=excluded.currency,ts_created_iso=excluded.ts_created_iso,
            ts_updated_iso=excluded.ts_updated_iso,status=excluded.status,
            cancel_reason=excluded.cancel_reason,sku=excluded.sku,ean=excluded.ean,
            product_name=excluded.product_name,product_url=excluded.product_url,
            product_price_local=excluded.product_price_local,
            shipping_local=excluded.shipping_local,
            sold_total_local=excluded.sold_total_local,
            revenue_gross_local=excluded.revenue_gross_local,
            revenue_net_local=excluded.revenue_net_local,
            commission_local=excluded.commission_local,
            commission_pct=excluded.commission_pct,
            commission_source=excluded.commission_source,
            payout_local=excluded.payout_local,
            received_at=excluded.received_at,
            received_source=excluded.received_source,
            payment_due_at=excluded.payment_due_at,
            payment_source=excluded.payment_source,
            vat_pct=excluded.vat_pct,
            carrier_code=CASE
                WHEN excluded.carrier_code<>'' THEN excluded.carrier_code
                ELSE kaufland_order_units.carrier_code END,
            tracking_numbers=CASE
                WHEN excluded.tracking_numbers<>'' THEN excluded.tracking_numbers
                ELSE kaufland_order_units.tracking_numbers END,
            raw_json=excluded.raw_json,
            detail_checked_at=CASE
                WHEN excluded.detail_checked_at<>'' THEN excluded.detail_checked_at
                ELSE kaufland_order_units.detail_checked_at END,
            synced_at=excluded.synced_at
        """,
        (
            seller_id, account_id, environment, item["id_order_unit"],
            item["id_order"], item["storefront"], item["country_code"],
            item["currency"], item["ts_created_iso"], item["ts_updated_iso"],
            item["status"], item["cancel_reason"], item["sku"], item["ean"],
            item["product_name"], item["product_url"],
            item["product_price_local"], item["shipping_local"],
            item["sold_total_local"], item["revenue_gross_local"],
            item["revenue_net_local"], item["commission_local"],
            item["commission_pct"], item["commission_source"],
            item["payout_local"], item["received_at"], item["received_source"],
            item["payment_due_at"], item["payment_source"], item["vat_pct"],
            item["carrier_code"],
            item["tracking_numbers"], json_text(item["raw"]),
            now_iso() if detail_checked else "", now_iso(),
        ),
    )


def sync_orders(
    client,
    seller_id: int,
    account_id: int,
    environment: str,
    maximum: int | None = 1000,
    include_tracking_details: bool = True,
    progress: ProgressCallback | None = None,
) -> dict:
    started = now_iso()
    sync_id = execute(
        """
        INSERT INTO kaufland_order_syncs(
            seller_id,marketplace_account_id,environment,status,started_at
        ) VALUES(?,?,?,?,?)
        """,
        (seller_id, account_id, environment, "running", started),
    )
    saved = 0
    details_checked = 0
    errors: list[dict] = []
    order_details: dict[str, dict] = {}
    try:
        raw_units = fetch_order_units(client, maximum, progress)
        total = len(raw_units)
        for index, raw in enumerate(raw_units, start=1):
            detail_checked = False
            merged = dict(raw)
            status = _text(raw.get("status")).lower()
            unit_id = _text(raw.get("id_order_unit"))
            should_fetch_detail = (
                include_tracking_details
                and unit_id
                and status in SHIPPED_ORDER_UNIT_STATUSES
            )
            if should_fetch_detail:
                try:
                    detail = response_item(client.order_unit(unit_id))
                    merged = merge_order_unit(raw, detail)
                    detail_checked = True
                    details_checked += 1
                    carrier, tracking = _tracking(merged)
                    order_id = _text(merged.get("id_order"))
                    if not tracking and order_id:
                        if order_id not in order_details:
                            order_details[order_id] = response_item(
                                client.order(order_id)
                            )
                        order_unit = find_order_unit(
                            order_details[order_id], unit_id
                        )
                        if order_unit:
                            merged = merge_order_unit(merged, order_unit)
                except Exception as error:
                    errors.append({
                        "id_order_unit": unit_id,
                        "fase": "tracking",
                        "errore": str(error),
                    })
            try:
                normalized = normalize_order_unit(merged)
                if not normalized["id_order_unit"]:
                    raise ValueError("id_order_unit mancante")
                upsert_order_unit(
                    seller_id, account_id, environment, normalized, detail_checked
                )
                saved += 1
            except Exception as error:
                errors.append({
                    "id_order_unit": unit_id,
                    "fase": "salvataggio",
                    "errore": str(error),
                })
            if progress:
                progress(index, max(1, total), "Salvataggio ordini e tracking")
        status = "completed_with_errors" if errors else "completed"
        execute(
            """
            UPDATE kaufland_order_syncs SET status=?,units_seen=?,units_saved=?,
                details_checked=?,errors_json=?,completed_at=? WHERE id=?
            """,
            (
                status, len(raw_units), saved, details_checked,
                json_text(errors), now_iso(), sync_id,
            ),
        )
        return {
            "seen": len(raw_units),
            "saved": saved,
            "details_checked": details_checked,
            "errors": errors,
        }
    except Exception as error:
        execute(
            """
            UPDATE kaufland_order_syncs SET status='failed',units_saved=?,
                details_checked=?,errors_json=?,completed_at=? WHERE id=?
            """,
            (
                saved, details_checked,
                json_text([{"fase": "sincronizzazione", "errore": str(error)}]),
                now_iso(), sync_id,
            ),
        )
        raise


def saved_orders(
    seller_id: int, account_id: int, environment: str
) -> list[dict]:
    result = rows(
        """
        SELECT * FROM kaufland_order_units
        WHERE seller_id=? AND marketplace_account_id=? AND environment=?
        ORDER BY ts_created_iso DESC,id DESC
        """,
        (seller_id, account_id, environment),
    )
    for item in result:
        enrich_saved_order(item)
    return result


def enrich_saved_order(item: dict) -> dict:
    """Add v101 fields to rows created by older Marketplace Hub versions.

    This keeps the UI operational even when an existing Windows installation
    loads an order row before the database migration has populated the new
    commission and payment columns.
    """
    commission = item.get("commission_local")
    sold_total = item.get("sold_total_local")
    if commission in (None, ""):
        product_price = item.get("product_price_local")
        revenue_gross = item.get("revenue_gross_local")
        if product_price not in (None, "") and revenue_gross not in (None, ""):
            commission = round(
                max(0.0, float(product_price) - float(revenue_gross)),
                2,
            )
            item["commission_local"] = commission
    if item.get("commission_pct") in (None, ""):
        item["commission_pct"] = (
            round(float(commission) / float(sold_total) * 100.0, 4)
            if commission not in (None, "") and sold_total not in (None, 0, "")
            else None
        )
    if not _text(item.get("commission_source")):
        item["commission_source"] = (
            "API Kaufland: archivio ordine esistente"
            if commission not in (None, "") else ""
        )
    status = _text(item.get("status")).lower()
    received_at = _text(item.get("received_at"))
    received_source = _text(item.get("received_source"))
    raw = item.get("raw_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    api_received_at, api_received_source = _received_timestamp(raw, status)
    if api_received_at:
        received_at = api_received_at
        received_source = api_received_source
    elif received_source == "API Kaufland: aggiornamento allo stato Ricevuto":
        # Versions up to v106 mistook ts_updated_iso for the delivery date.
        # It may be the release timestamp itself, so the old estimate is unsafe.
        received_at = ""
        received_source = ""
    item["received_at"] = received_at
    item["received_source"] = received_source
    shipped_at, shipped_source = _shipped_timestamp(raw, status)
    released_at, payment_source = _payment_release_timestamp(raw, status)
    tracking = _text(item.get("tracking_numbers"))
    if not tracking:
        _, tracking = _tracking(raw)
    payment = payment_schedule(
            status,
            received_at=received_at,
            released_at=released_at,
            shipped_at=shipped_at,
            has_tracking=bool(tracking),
        )
    item.update(payment)
    item.setdefault("received_source", "")
    item["shipped_at"] = shipped_at
    item["shipped_source"] = shipped_source
    item["payment_source"] = payment_source or payment["payment_rule"]
    item.setdefault("commission_pct", None)
    item.setdefault("commission_source", "")
    item.setdefault("payment_due_at", "")
    return item


def last_sync(
    seller_id: int, account_id: int, environment: str
) -> dict | None:
    return row(
        """
        SELECT * FROM kaufland_order_syncs
        WHERE seller_id=? AND marketplace_account_id=? AND environment=?
        ORDER BY id DESC LIMIT 1
        """,
        (seller_id, account_id, environment),
    )


def currency_to_eur(value, currency: str, rates: dict[str, float]):
    if value in (None, ""):
        return None
    amount = float(value)
    code = _text(currency).upper() or "EUR"
    if code == "EUR":
        return amount
    rate = float(rates.get(code, 0) or 0)
    return amount / rate if rate > 0 else None


def order_amounts_to_eur(
    item: dict,
    rates: dict[str, float],
) -> dict:
    """Convert every monetary field of a Kaufland order unit to EUR."""
    source_currency = _text(item.get("currency")).upper() or "EUR"
    converted = {
        "source_currency": source_currency,
        "currency": "EUR",
    }
    for local_field, eur_field in (
        ("product_price_local", "product_price_eur"),
        ("shipping_local", "shipping_eur"),
        ("sold_total_local", "sold_total_eur"),
        ("commission_local", "commission_eur"),
        ("payout_local", "payout_eur"),
        ("revenue_net_local", "revenue_net_eur"),
    ):
        converted[eur_field] = currency_to_eur(
            item.get(local_field),
            source_currency,
            rates,
        )
    return converted


def country_label(storefront: str) -> str:
    code = _text(storefront).lower()
    return STOREFRONT_INFO.get(code, (code.upper(), code.upper(), ""))[0]


def status_label(status: str) -> str:
    value = _text(status).lower()
    return STATUS_LABELS.get(value, value.replace("_", " ").title() or "—")


def exact_totals_by_currency(items: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for item in items:
        currency = _text(item.get("currency")).upper() or "—"
        target = grouped.setdefault(currency, {
            "Valuta": currency,
            "Venduto": 0.0,
            "Commissioni": 0.0,
            "Da ricevere": 0.0,
            "Unità": 0,
        })
        target["Venduto"] += float(item.get("sold_total_local") or 0)
        target["Commissioni"] += float(item.get("commission_local") or 0)
        target["Da ricevere"] += float(item.get("payout_local") or 0)
        target["Unità"] += 1
    return [
        {
            **item,
            "Venduto": round(item["Venduto"], 2),
            "Commissioni": round(item["Commissioni"], 2),
            "Da ricevere": round(item["Da ricevere"], 2),
        }
        for item in sorted(grouped.values(), key=lambda value: value["Valuta"])
    ]
