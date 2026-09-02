from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from services.cecotec_orders import clean_text


ROME_TZ = ZoneInfo("Europe/Rome") if ZoneInfo is not None else timezone(timedelta(hours=1))
LISBON_TZ = ZoneInfo("Europe/Lisbon") if ZoneInfo is not None else timezone.utc
BERLIN_TZ = ZoneInfo("Europe/Berlin") if ZoneInfo is not None else timezone(timedelta(hours=1))


def marketplace_shipping_timezone(marketplace: object):
    key = clean_text(marketplace).lower()
    if key == "worten":
        return LISBON_TZ
    if key == "kaufland":
        return BERLIN_TZ
    return ROME_TZ

# Exact API fields are intentionally preferred over estimates. Mirakl exposes
# shipping_deadline on order resources, while Kaufland exposes
# delivery_time_expires_iso on order units.
_WORTEN_DEADLINE_KEYS = (
    "shipping_deadline",
    "shipping_deadline_date",
    "shipping_deadline_datetime",
    "shipping_due_date",
    "ship_by_date",
    "ship_by",
    "latest_ship_date",
    "latest_shipping_date",
    "dispatch_deadline",
    "shipment_deadline",
)
_KAUFLAND_DEADLINE_KEYS = (
    "delivery_time_expires_iso",
    "delivery_time_expires",
    "shipping_deadline",
    "ship_by_date",
    "dispatch_deadline",
)
_GENERIC_DEADLINE_KEYS = tuple(dict.fromkeys(_WORTEN_DEADLINE_KEYS + _KAUFLAND_DEADLINE_KEYS))


@dataclass(frozen=True)
class ShippingDeadline:
    deadline_utc: datetime | None
    deadline_local: datetime | None
    display: str
    source: str
    status: str
    hours_remaining: float | None
    overdue: bool
    urgent: bool
    estimated: bool = False


def _normal_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _json_value(value: object) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


def _find_values(value: Any, keys: Sequence[str]) -> list[tuple[str, Any]]:
    wanted = {_normal_key(key) for key in keys}
    found: list[tuple[str, Any]] = []

    def visit(current: Any) -> None:
        current = _json_value(current)
        if isinstance(current, Mapping):
            # First inspect direct matches, preserving the API field name.
            for key, child in current.items():
                normalized = _normal_key(key)
                if normalized in wanted and child not in (None, ""):
                    found.append((str(key), child))
            for child in current.values():
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child)
        elif isinstance(current, (list, tuple)):
            for child in current:
                visit(child)

    visit(value)
    return found


def _parse_datetime(value: object, *, default_tz=ROME_TZ) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time(23, 59, 59))
    else:
        text = clean_text(value)
        if not text:
            return None
        # Date-only deadlines are interpreted as end-of-day in the operational
        # timezone so they do not become overdue at midnight UTC unexpectedly.
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = datetime.combine(date.fromisoformat(text), time(23, 59, 59), tzinfo=default_tz)
        else:
            normalized = text.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                parsed = None
                for fmt in (
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%d/%m/%Y %H:%M:%S",
                    "%d/%m/%Y %H:%M",
                ):
                    try:
                        parsed = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue
                if parsed is None:
                    return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed.astimezone(timezone.utc)


def _number(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _estimate_worten_deadline(lines: Sequence[Mapping[str, Any]]) -> tuple[datetime | None, str]:
    """Best-effort fallback when Mirakl did not return shipping_deadline.

    The estimate is labelled explicitly and never presented as an exact API
    deadline. It uses the order acceptance/creation timestamp plus
    leadtime_to_ship days when both values are available.
    """
    for line in lines:
        raw = _json_value(line.get("raw_json"))
        lead_candidates = _find_values(raw, ("leadtime_to_ship", "leadtime-to-ship"))
        lead_days = next((_number(value) for _, value in lead_candidates if _number(value) is not None), None)
        if lead_days is None:
            continue
        base_candidates = _find_values(
            raw,
            (
                "acceptance_decision_date",
                "created_date",
                "date_created",
                "creation_date",
                "order_date",
            ),
        )
        base = next((_parse_datetime(value) for _, value in base_candidates if _parse_datetime(value)), None)
        if base is None:
            base = _parse_datetime(line.get("order_created"))
        if base is not None:
            return base + timedelta(days=max(0.0, lead_days)), "Stima da leadtime_to_ship"
    return None, ""


def shipping_deadline_for_order(
    lines: Iterable[Mapping[str, Any]],
    *,
    marketplace: str,
    reference: datetime | None = None,
    allow_estimate: bool = True,
) -> ShippingDeadline:
    """Return the shipment deadline using exact marketplace data first.

    For Worten/Mirakl the order-level ``shipping_deadline`` is authoritative.
    Nested order-line values are considered only when the order object does not
    expose a deadline.  This prevents an unrelated line date from being chosen
    simply because it is earlier.  The tracking page disables estimates so an
    old ``leadtime_to_ship`` value can never be presented as the real deadline.
    """
    values = [dict(item) for item in lines]
    marketplace_key = clean_text(marketplace).lower()
    keys = _WORTEN_DEADLINE_KEYS if marketplace_key == "worten" else _KAUFLAND_DEADLINE_KEYS
    local_tz = marketplace_shipping_timezone(marketplace_key)

    direct_candidates: list[tuple[datetime, str]] = []
    order_candidates: list[tuple[datetime, str]] = []
    nested_candidates: list[tuple[datetime, str]] = []

    for line in values:
        for key in keys:
            if line.get(key) not in (None, ""):
                parsed = _parse_datetime(line.get(key), default_tz=local_tz)
                if parsed is not None:
                    direct_candidates.append((parsed, key))

        raw = _json_value(line.get("raw_json"))
        if isinstance(raw, Mapping):
            raw_order = _json_value(raw.get("order"))
            if isinstance(raw_order, Mapping):
                for field_name, raw_value in _find_values(raw_order, keys):
                    parsed = _parse_datetime(raw_value, default_tz=local_tz)
                    if parsed is not None:
                        order_candidates.append((parsed, field_name))
            else:
                raw_order = None

            # Only search the complete payload when no explicit order object is
            # available.  Mirakl order lines can contain similarly named fields
            # that are not the seller's shipping deadline.
            if raw_order is None:
                for field_name, raw_value in _find_values(raw, keys):
                    parsed = _parse_datetime(raw_value, default_tz=local_tz)
                    if parsed is not None:
                        nested_candidates.append((parsed, field_name))
        elif raw not in (None, ""):
            for field_name, raw_value in _find_values(raw, keys):
                parsed = _parse_datetime(raw_value, default_tz=local_tz)
                if parsed is not None:
                    nested_candidates.append((parsed, field_name))

    candidates = direct_candidates or order_candidates or nested_candidates
    estimated = False
    if candidates:
        deadline_utc, source = min(candidates, key=lambda item: item[0])
        source_label = f"API · {source}"
    elif marketplace_key == "worten" and allow_estimate:
        deadline_utc, source_label = _estimate_worten_deadline(values)
        estimated = deadline_utc is not None
    else:
        deadline_utc, source_label = None, ""

    if deadline_utc is None:
        return ShippingDeadline(
            deadline_utc=None,
            deadline_local=None,
            display="Non disponibile",
            source="",
            status="NON DISPONIBILE",
            hours_remaining=None,
            overdue=False,
            urgent=False,
            estimated=False,
        )

    now = reference or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    remaining = (deadline_utc - now).total_seconds() / 3600.0
    overdue = remaining < 0
    urgent = not overdue and remaining < 24
    if overdue:
        status = "SCADUTO"
    elif urgent:
        status = "URGENTE <24H"
    else:
        status = "IN TEMPO"
    local = deadline_utc.astimezone(local_tz)
    timezone_label = local.tzname() or ""
    display = local.strftime("%d/%m/%Y %H:%M")
    if timezone_label:
        display += f" ({timezone_label})"
    if estimated:
        display += " · stimata"
    return ShippingDeadline(
        deadline_utc=deadline_utc,
        deadline_local=local,
        display=display,
        source=source_label,
        status=status,
        hours_remaining=remaining,
        overdue=overdue,
        urgent=urgent,
        estimated=estimated,
    )


def is_deadline_in_date_range(
    deadline: ShippingDeadline,
    *,
    date_from: date,
    date_to: date,
) -> bool:
    if deadline.deadline_local is None:
        return False
    day = deadline.deadline_local.date()
    return date_from <= day <= date_to
