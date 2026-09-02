from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

from services.kaufland import KauflandClient
from services.worten_tracking_api import WortenTrackingClient


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def split_kaufland_unit_ids(value: Any) -> list[str]:
    """Return individual Kaufland order-unit IDs from stored aggregate values.

    Cecotec order rows can aggregate identical products from the same order and
    therefore store multiple ``id_order_unit`` values in one field, separated by
    commas.  The Kaufland API, however, accepts exactly one numeric unit ID per
    detail request.  This normalizer also accepts JSON arrays and the common
    semicolon/pipe/whitespace separators so old database values remain usable.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        output: list[str] = []
        for item in value:
            output.extend(split_kaufland_unit_ids(item))
        return list(dict.fromkeys(output))

    raw = _text(value)
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return split_kaufland_unit_ids(parsed)

    values = [part.strip() for part in re.split(r"[,;|\s]+", raw) if part.strip()]
    # Unit IDs are numeric in Kaufland.  Preserve only positive integer tokens so
    # a malformed aggregate can never reach ``KauflandClient.order_unit(int(...))``.
    valid = [part for part in values if part.isdigit() and int(part) > 0]
    return list(dict.fromkeys(valid))


def _response_item(response: Any) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        return {}
    data = response.get("data", response)
    if isinstance(data, list):
        return dict(data[0]) if data and isinstance(data[0], Mapping) else {}
    return dict(data) if isinstance(data, Mapping) else {}


def _response_list(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, Mapping):
        return []
    data = response.get("data", response)
    if not isinstance(data, list):
        return []
    return [dict(item) for item in data if isinstance(item, Mapping)]


_STATE_CACHE_LOCK = threading.Lock()
_STATE_CACHE: dict[str, tuple[float, LiveOrderState]] = {}
_STATE_CACHE_TTL_SECONDS = 300.0


def _credential_scope(credentials: Mapping[str, Any]) -> str:
    raw = "|".join([
        _text(credentials.get("client_key")),
        "playground" if credentials.get("playground") or credentials.get("test") else "production",
    ])
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _cache_get(scope: str, unit_id: str, *, allow_stale: bool = False) -> LiveOrderState | None:
    key = f"{scope}:{unit_id}"
    with _STATE_CACHE_LOCK:
        cached = _STATE_CACHE.get(key)
    if not cached:
        return None
    saved_at, state = cached
    if allow_stale or time.monotonic() - saved_at <= _STATE_CACHE_TTL_SECONDS:
        return state
    return None


def _cache_put(scope: str, state: LiveOrderState) -> None:
    if not state.unit_id:
        return
    with _STATE_CACHE_LOCK:
        _STATE_CACHE[f"{scope}:{state.unit_id}"] = (time.monotonic(), state)


WORTEN_ORDER_STATES: dict[str, dict[str, Any]] = {
    "STAGING": {
        "label": "In preparazione",
        "macro": "In attesa",
        "reason": "ordine non ancora pronto per l'accettazione",
    },
    "WAITING_ACCEPTANCE": {
        "label": "In attesa di accettazione",
        "macro": "In attesa",
        "reason": "ordine ancora da accettare sul marketplace",
    },
    "WAITING_DEBIT": {
        "label": "In attesa di addebito",
        "macro": "In attesa",
        "reason": "addebito cliente non ancora completato",
    },
    "WAITING_DEBIT_PAYMENT": {
        "label": "In attesa del pagamento",
        "macro": "In attesa",
        "reason": "pagamento cliente non ancora completato",
    },
    "SHIPPING": {
        "label": "In attesa di spedizione",
        "macro": "Da spedire",
        "can_generate_supplier_order": True,
        "can_mark_shipped": True,
        "reason": "ordine accettato e pronto per la spedizione",
    },
    "SHIPPED": {
        "label": "Spedito",
        "macro": "Spedito",
        "already_shipped": True,
        "reason": "ordine già contrassegnato come spedito",
    },
    "TO_COLLECT": {
        "label": "Disponibile per il ritiro",
        "macro": "Spedito",
        "already_shipped": True,
        "reason": "ordine già spedito e disponibile per il ritiro",
    },
    "RECEIVED": {
        "label": "Ricevuto",
        "macro": "Ricevuto",
        "already_shipped": True,
        "final": True,
        "reason": "consegna ricevuta dal cliente",
    },
    "CLOSED": {
        "label": "Chiuso",
        "macro": "Ricevuto",
        "already_shipped": True,
        "final": True,
        "reason": "ordine concluso sul marketplace",
    },
    "REFUSED": {
        "label": "Rifiutato",
        "macro": "Cancellato",
        "cancelled": True,
        "final": True,
        "reason": "ordine rifiutato",
    },
    "CANCELED": {
        "label": "Cancellato",
        "macro": "Cancellato",
        "cancelled": True,
        "final": True,
        "reason": "ordine cancellato",
    },
    # Alias observed on some Mirakl tenants and historical data.
    "CANCELLED": {
        "label": "Cancellato",
        "macro": "Cancellato",
        "cancelled": True,
        "final": True,
        "reason": "ordine cancellato",
    },
}

WORTEN_REFUND_STATES: dict[str, str] = {
    "WAITING_REFUND_TAX_CONFIRMATION": "In attesa conferma fiscale rimborso",
    "WAITING_REFUND": "Rimborso in attesa",
    "WAITING_REFUND_PAYMENT": "Pagamento rimborso in attesa",
    "REFUNDED": "Rimborsato",
}

KAUFLAND_UNIT_STATES: dict[str, dict[str, Any]] = {
    "open": {
        "label": "Aperto · verifica dati di consegna",
        "macro": "In attesa",
        "reason": "ordine nella finestra iniziale e non ancora spedibile",
    },
    "need_to_be_sent": {
        "label": "In attesa di spedizione",
        "macro": "Da spedire",
        "can_generate_supplier_order": True,
        "can_mark_shipped": True,
        "reason": "unità pronta per la spedizione",
    },
    "sent": {
        "label": "Spedito",
        "macro": "Spedito",
        "already_shipped": True,
        "reason": "unità già contrassegnata come spedita",
    },
    "sent_and_autopaid": {
        "label": "Spedito e pagato automaticamente",
        "macro": "Spedito",
        "already_shipped": True,
        "reason": "unità già spedita e ricavo già disponibile",
    },
    "received": {
        "label": "Ricevuto",
        "macro": "Ricevuto",
        "already_shipped": True,
        "final": True,
        "reason": "unità ricevuta dal cliente",
    },
    "returned": {
        "label": "Restituito",
        "macro": "Restituito/Rimborsato",
        "already_shipped": True,
        "returned": True,
        "final": True,
        "reason": "unità restituita",
    },
    "returned_paid": {
        "label": "Reso rimborsato",
        "macro": "Restituito/Rimborsato",
        "already_shipped": True,
        "returned": True,
        "final": True,
        "reason": "reso già rimborsato",
    },
    "cancelled": {
        "label": "Cancellato",
        "macro": "Cancellato",
        "cancelled": True,
        "final": True,
        "reason": "unità cancellata",
    },
    "canceled": {
        "label": "Cancellato",
        "macro": "Cancellato",
        "cancelled": True,
        "final": True,
        "reason": "unità cancellata",
    },
}


KAUFLAND_STOREFRONT_COUNTRIES: dict[str, tuple[str, str]] = {
    "de": ("DE", "Germania"),
    "at": ("AT", "Austria"),
    "cz": ("CZ", "Repubblica Ceca"),
    "sk": ("SK", "Slovacchia"),
    "pl": ("PL", "Polonia"),
    "fr": ("FR", "Francia"),
    "it": ("IT", "Italia"),
}

COUNTRY_LABELS: dict[str, str] = {
    "AT": "Austria", "BE": "Belgio", "BG": "Bulgaria", "CH": "Svizzera",
    "CZ": "Repubblica Ceca", "DE": "Germania", "DK": "Danimarca",
    "EE": "Estonia", "ES": "Spagna", "FI": "Finlandia", "FR": "Francia",
    "GB": "Regno Unito", "GR": "Grecia", "HR": "Croazia", "HU": "Ungheria",
    "IE": "Irlanda", "IT": "Italia", "LT": "Lituania", "LU": "Lussemburgo",
    "LV": "Lettonia", "NL": "Paesi Bassi", "NO": "Norvegia", "PL": "Polonia",
    "PT": "Portogallo", "RO": "Romania", "SE": "Svezia", "SI": "Slovenia",
    "SK": "Slovacchia",
}


def _country_code(value: Any) -> str:
    raw = _text(value).upper()
    aliases = {
        "GERMANY": "DE", "DEUTSCHLAND": "DE", "AUSTRIA": "AT", "ÖSTERREICH": "AT",
        "ITALY": "IT", "ITALIA": "IT", "FRANCE": "FR", "POLAND": "PL",
        "CZECHIA": "CZ", "CZECH REPUBLIC": "CZ", "SLOVAKIA": "SK",
        "PORTUGAL": "PT", "SPAIN": "ES", "NETHERLANDS": "NL",
    }
    return aliases.get(raw, raw[:2] if len(raw) >= 2 else raw)


def _country_label(value: Any) -> str:
    code = _country_code(value)
    return COUNTRY_LABELS.get(code, code)


def _storefront_meta(value: Any) -> tuple[str, str, str]:
    storefront = _text(value).lower()
    country_code, country_label = KAUFLAND_STOREFRONT_COUNTRIES.get(
        storefront, (_country_code(storefront), _country_label(storefront))
    )
    display = f"Kaufland.{storefront}" if storefront else "Kaufland"
    return storefront, country_code, country_label or display


def _nested_mapping(source: Any, *path: str) -> dict[str, Any]:
    current = source
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return dict(current) if isinstance(current, Mapping) else {}


def _address_payload(source: Any) -> dict[str, Any]:
    """Extract a Kaufland shipping-address payload from any documented envelope."""
    if not isinstance(source, Mapping):
        return {}
    value: Mapping[str, Any] = source
    address_data = value.get("address_data")
    if isinstance(address_data, Mapping):
        data = address_data.get("data")
        if isinstance(data, Mapping):
            return dict(data)
    for key in (
        "shipping_address", "delivery_address", "recipient_address",
        "billing_address", "address",
    ):
        candidate = value.get(key)
        if isinstance(candidate, Mapping):
            nested = _address_payload(candidate)
            return nested or dict(candidate)
    data = value.get("data")
    if isinstance(data, Mapping):
        address_keys = {
            "first_name", "last_name", "street", "house_number", "postcode",
            "postal_code", "city", "country", "country_code", "phone",
        }
        if address_keys.intersection(data.keys()):
            return dict(data)
    address_keys = {
        "first_name", "last_name", "street", "house_number", "postcode",
        "postal_code", "city", "country", "country_code", "phone",
    }
    if address_keys.intersection(value.keys()):
        return dict(value)
    return {}


def _address_summary(source: Any) -> dict[str, Any]:
    address = _address_payload(source)
    first = _text(address.get("first_name") or address.get("firstname"))
    last = _text(address.get("last_name") or address.get("lastname"))
    company = _text(address.get("company_name") or address.get("company"))
    name = _text(address.get("full_name") or address.get("name")) or _text(
        " ".join(part for part in (first, last) if part)
    ) or company
    street = _text(address.get("street") or address.get("address_line_1"))
    house = _text(address.get("house_number") or address.get("street_number"))
    extra = _text(address.get("additional_field") or address.get("address_line_2"))
    street_line = _text(" ".join(part for part in (street, house, extra) if part))
    postal = _text(address.get("postcode") or address.get("postal_code") or address.get("zip"))
    city = _text(address.get("city") or address.get("town"))
    country = _country_code(
        address.get("country") or address.get("country_code") or address.get("country_iso_code")
    )
    available = bool(country and postal and city and (name or street_line))
    return {
        "available": available,
        "name": name,
        "street": street_line,
        "postal_code": postal,
        "city": city,
        "country_code": country,
        "country_label": _country_label(country),
        "phone": _text(address.get("phone") or address.get("phone_number")),
        "raw": address,
    }


def _extract_storefront(source: Any) -> str:
    if not isinstance(source, Mapping):
        return ""
    candidates = [
        source.get("storefront"), source.get("storefront_code"),
        source.get("sales_channel"), source.get("marketplace"),
    ]
    for container_key in ("order", "data"):
        nested = source.get(container_key)
        if isinstance(nested, Mapping):
            candidates.extend([
                nested.get("storefront"), nested.get("storefront_code"),
                nested.get("sales_channel"),
            ])
    for value in candidates:
        text = _text(value).lower()
        if text:
            return text
    return ""


@dataclass(frozen=True)
class LiveOrderState:
    marketplace: str
    order_id: str
    unit_id: str
    raw_status: str
    label: str
    macro_status: str
    can_generate_supplier_order: bool
    can_mark_shipped: bool
    already_shipped: bool
    cancelled: bool
    returned: bool
    final: bool
    reason: str
    refund_status: str = ""
    refund_label: str = ""
    checked_at: str = ""
    storefront: str = ""
    storefront_country_code: str = ""
    storefront_country_label: str = ""
    shipping_country_code: str = ""
    shipping_country_label: str = ""
    shipping_address_available: bool = False
    shipping_address: Mapping[str, Any] | None = None
    order_created_at: str = ""
    raw: Mapping[str, Any] | None = None

    @property
    def actionable(self) -> bool:
        return self.can_generate_supplier_order or self.can_mark_shipped


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_from_rule(
    *,
    marketplace: str,
    order_id: str,
    unit_id: str,
    raw_status: str,
    rule: Mapping[str, Any] | None,
    raw: Mapping[str, Any] | None = None,
    refund_status: str = "",
) -> LiveOrderState:
    if rule is None:
        label = raw_status.replace("_", " ").strip().capitalize() or "Stato non disponibile"
        return LiveOrderState(
            marketplace=marketplace,
            order_id=order_id,
            unit_id=unit_id,
            raw_status=raw_status,
            label=label,
            macro_status="Sconosciuto",
            can_generate_supplier_order=False,
            can_mark_shipped=False,
            already_shipped=False,
            cancelled=False,
            returned=False,
            final=False,
            reason=f"stato API {raw_status or 'non disponibile'} non riconosciuto: operazione bloccata",
            refund_status=refund_status,
            refund_label=WORTEN_REFUND_STATES.get(refund_status, refund_status.replace("_", " ").title()),
            checked_at=_checked_at(),
            raw=raw,
        )
    return LiveOrderState(
        marketplace=marketplace,
        order_id=order_id,
        unit_id=unit_id,
        raw_status=raw_status,
        label=_text(rule.get("label")),
        macro_status=_text(rule.get("macro")),
        can_generate_supplier_order=bool(rule.get("can_generate_supplier_order")),
        can_mark_shipped=bool(rule.get("can_mark_shipped")),
        already_shipped=bool(rule.get("already_shipped")),
        cancelled=bool(rule.get("cancelled")),
        returned=bool(rule.get("returned")),
        final=bool(rule.get("final")),
        reason=_text(rule.get("reason")),
        refund_status=refund_status,
        refund_label=WORTEN_REFUND_STATES.get(refund_status, refund_status.replace("_", " ").title()),
        checked_at=_checked_at(),
        raw=raw,
    )


def _refund_state_from_worten_order(order: Mapping[str, Any]) -> str:
    candidates: list[Any] = [
        order.get("refund_state"),
        order.get("refund_state_code"),
        order.get("refund_status"),
    ]
    lines = order.get("order_lines") or order.get("lines") or []
    if isinstance(lines, list):
        for line in lines:
            if not isinstance(line, Mapping):
                continue
            candidates.extend(
                [line.get("refund_state"), line.get("refund_state_code"), line.get("refund_status")]
            )
    priority = {
        "REFUNDED": 4,
        "WAITING_REFUND_PAYMENT": 3,
        "WAITING_REFUND": 2,
        "WAITING_REFUND_TAX_CONFIRMATION": 1,
    }
    found = [str(value or "").strip().upper() for value in candidates if str(value or "").strip()]
    return max(found, key=lambda value: priority.get(value, 0), default="")


def classify_worten_order(order: Mapping[str, Any], order_id: str = "") -> LiveOrderState:
    order_id_value = _text(order.get("order_id") or order.get("commercial_id") or order_id)
    status = _text(order.get("order_state") or order.get("status") or order.get("state")).upper()
    refund_status = _refund_state_from_worten_order(order)
    return _state_from_rule(
        marketplace="worten",
        order_id=order_id_value,
        unit_id="",
        raw_status=status,
        rule=WORTEN_ORDER_STATES.get(status),
        raw=order,
        refund_status=refund_status,
    )


def classify_kaufland_unit(unit: Mapping[str, Any], unit_id: str = "") -> LiveOrderState:
    unit_data = _response_item(unit) if "data" in unit else dict(unit)
    unit_id_value = _text(unit_data.get("id_order_unit") or unit_id)
    order_id = _text(unit_data.get("id_order"))
    status = _text(unit_data.get("status")).lower()
    return _state_from_rule(
        marketplace="kaufland",
        order_id=order_id,
        unit_id=unit_id_value,
        raw_status=status,
        rule=KAUFLAND_UNIT_STATES.get(status),
        raw=unit_data,
    )


def fetch_worten_order_states(
    credentials: Mapping[str, Any],
    order_ids: Iterable[Any],
) -> tuple[dict[str, LiveOrderState], list[str]]:
    ids = list(dict.fromkeys(_text(value) for value in order_ids if _text(value)))
    if not ids:
        return {}, []
    client = WortenTrackingClient(
        api_url=_text(credentials.get("api_url") or credentials.get("base_url")),
        api_key=_text(credentials.get("api_key") or credentials.get("token")),
        shop_id=credentials.get("shop_id"),
    )
    errors: list[str] = []
    try:
        raw_orders = client.list_orders_by_ids(ids)
    except Exception as exc:
        return {}, [str(exc)]
    result: dict[str, LiveOrderState] = {}
    for order_id in ids:
        raw = raw_orders.get(order_id)
        if isinstance(raw, Mapping):
            result[order_id] = classify_worten_order(raw, order_id)
        else:
            errors.append(f"Ordine {order_id}: non restituito dall'API Worten")
    return result, errors


def _kaufland_client(credentials: Mapping[str, Any]) -> KauflandClient:
    return KauflandClient(
        _text(credentials.get("client_key")),
        _text(credentials.get("secret_key")),
        playground=bool(credentials.get("playground") or credentials.get("test")),
    )


def _fetch_kaufland_status_pages(
    client: KauflandClient,
    status: str,
    wanted: set[str],
    result: dict[str, LiveOrderState],
    scope: str,
    *,
    maximum_pages: int = 100,
) -> None:
    """Resolve selected units through the paginated list endpoint first.

    The operational tables commonly contain many ``need_to_be_sent`` units. A
    single paginated list is substantially cheaper than one detail request for
    every selected row and prevents bursts that trigger HTTP 429.
    """
    offset = 0
    page_size = 100
    for _ in range(maximum_pages):
        response = client.order_units(limit=page_size, offset=offset, status=status)
        values = _response_list(response)
        if not values:
            break
        for raw in values:
            unit_id = _text(raw.get("id_order_unit"))
            if unit_id not in wanted:
                continue
            state = classify_kaufland_unit(raw, unit_id)
            result[unit_id] = state
            _cache_put(scope, state)
        if wanted.issubset(result):
            break
        pagination = response.get("pagination") if isinstance(response, Mapping) else {}
        total = int((pagination or {}).get("total") or 0) if isinstance(pagination, Mapping) else 0
        offset += len(values)
        if len(values) < page_size or (total and offset >= total):
            break


def _order_units_from_order_payload(payload: Any) -> list[dict[str, Any]]:
    """Find order-unit objects in the different Kaufland order envelopes."""
    root = _response_item(payload)
    candidates: list[Any] = [
        root.get("order_units"), root.get("units"), root.get("data"),
    ]
    order = root.get("order")
    if isinstance(order, Mapping):
        candidates.extend([order.get("order_units"), order.get("units")])
    for value in candidates:
        if isinstance(value, list):
            rows = [dict(item) for item in value if isinstance(item, Mapping)]
            if rows:
                return rows
    return []




def _client_order_detail(client: Any, order_id: str) -> Any:
    try:
        return client.order(order_id, embedded="delivery")
    except TypeError:
        return client.order(order_id)


def _client_unit_detail(client: Any, unit_id: str) -> Any:
    try:
        return client.order_unit(unit_id, embedded="delivery")
    except TypeError:
        return client.order_unit(unit_id)


def _fetch_order_manifest_metadata(
    client: Any, wanted_order_ids: Sequence[str], *, maximum_pages: int = 100
) -> dict[str, dict[str, Any]]:
    if not hasattr(client, "orders"):
        return {}
    wanted = set(_text(value) for value in wanted_order_ids if _text(value))
    found: dict[str, dict[str, Any]] = {}
    offset = 0
    for _ in range(maximum_pages):
        response = client.orders(limit=100, offset=offset)
        values = _response_list(response)
        if not values:
            break
        for item in values:
            order_id = _text(item.get("id_order"))
            if order_id in wanted:
                found[order_id] = {
                    "root": dict(item),
                    "storefront": _extract_storefront(item),
                    "created_at": _text(item.get("ts_created_iso")),
                    "units": [],
                }
        if wanted.issubset(found):
            break
        pagination = response.get("pagination") if isinstance(response, Mapping) else {}
        total = int((pagination or {}).get("total") or 0) if isinstance(pagination, Mapping) else 0
        offset += len(values)
        if len(values) < 100 or (total and offset >= total):
            break
    return found

def _shipping_address_entries(response: Any) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_unit: dict[str, dict[str, Any]] = {}
    by_order: dict[str, list[dict[str, Any]]] = {}
    for item in _response_list(response):
        unit_id = _text(item.get("id_order_unit"))
        order_id = _text(item.get("id_order"))
        summary = _address_summary(item)
        entry = {**item, "_address_summary": summary}
        if unit_id:
            by_unit[unit_id] = entry
        if order_id:
            by_order.setdefault(order_id, []).append(entry)
    return by_unit, by_order


def _order_payload_metadata(payload: Any) -> dict[str, Any]:
    root = _response_item(payload)
    storefront = _extract_storefront(root)
    created_at = _text(root.get("ts_created_iso") or root.get("created_at"))
    units = _order_units_from_order_payload(payload)
    return {
        "root": root,
        "storefront": storefront,
        "created_at": created_at,
        "units": units,
    }


def _unit_from_order_metadata(metadata: Mapping[str, Any], unit_id: str) -> dict[str, Any]:
    for item in metadata.get("units") or []:
        if isinstance(item, Mapping) and _text(item.get("id_order_unit")) == unit_id:
            return dict(item)
    return {}


def _enrich_kaufland_state(
    state: LiveOrderState,
    *,
    unit_payload: Mapping[str, Any] | None = None,
    order_metadata: Mapping[str, Any] | None = None,
    address_entry: Mapping[str, Any] | None = None,
) -> LiveOrderState:
    unit = dict(state.raw or {})
    if isinstance(unit_payload, Mapping):
        payload_item = _response_item(unit_payload) if "data" in unit_payload else dict(unit_payload)
        unit.update(payload_item)
    metadata = dict(order_metadata or {})
    order_unit = _unit_from_order_metadata(metadata, state.unit_id)
    if order_unit:
        unit.update(order_unit)

    # A detail/order request is more authoritative than a previous list/cache
    # response, especially while an order transitions from open to need_to_be_sent.
    current_status = _text(unit.get("status") or state.raw_status).lower()
    current = _state_from_rule(
        marketplace="kaufland",
        order_id=_text(unit.get("id_order") or state.order_id),
        unit_id=_text(unit.get("id_order_unit") or state.unit_id),
        raw_status=current_status,
        rule=KAUFLAND_UNIT_STATES.get(current_status),
        raw=unit,
    )

    storefront = _extract_storefront(unit) or _text(metadata.get("storefront"))
    storefront, storefront_country, storefront_label = _storefront_meta(storefront)

    address = _address_summary(address_entry or {})
    if not address.get("available"):
        address = _address_summary(unit)
    if not address.get("available"):
        address = _address_summary(metadata.get("root") or {})

    label = current.label
    macro = current.macro_status
    reason = current.reason
    can_generate = current.can_generate_supplier_order
    can_ship = current.can_mark_shipped

    if current_status == "open":
        if address.get("available"):
            # Creating the supplier order is not the same operation as marking the
            # marketplace unit as shipped. When Kaufland already exposes the full
            # delivery address, the supplier file can be prepared, while /send
            # remains blocked until the API status becomes need_to_be_sent.
            label = "Aperto · dati di consegna disponibili"
            macro = "Da preparare"
            can_generate = True
            can_ship = False
            reason = (
                "ordine aperto con indirizzo di consegna disponibile: può essere "
                "inserito nel file Cecotec; l'aggiornamento spedizione Kaufland "
                "sarà consentito solo dopo il passaggio a need_to_be_sent"
            )
        else:
            label = "Aperto · dati di consegna non ancora disponibili"
            macro = "In attesa"
            can_generate = False
            can_ship = False
            reason = (
                "ordine ancora nella fase iniziale Kaufland e indirizzo non restituito "
                "dall'API; riprovare dopo il passaggio a need_to_be_sent"
            )

    return replace(
        current,
        label=label,
        macro_status=macro,
        can_generate_supplier_order=can_generate,
        can_mark_shipped=can_ship,
        reason=reason,
        storefront=storefront,
        storefront_country_code=storefront_country,
        storefront_country_label=storefront_label,
        shipping_country_code=_text(address.get("country_code")),
        shipping_country_label=_text(address.get("country_label")),
        shipping_address_available=bool(address.get("available")),
        shipping_address=dict(address.get("raw") or {}),
        order_created_at=_text(metadata.get("created_at") or unit.get("ts_created_iso")),
        checked_at=_checked_at(),
        raw=unit,
    )


def _chunks(values: Sequence[str], size: int = 50) -> Iterable[list[str]]:
    for index in range(0, len(values), max(1, size)):
        yield list(values[index:index + max(1, size)])

def fetch_kaufland_unit_states(
    credentials: Mapping[str, Any],
    unit_ids: Iterable[Any],
    *,
    order_ids_by_unit: Mapping[str, Any] | None = None,
    force_refresh: bool = False,
) -> tuple[dict[str, LiveOrderState], list[str]]:
    ids: list[str] = []
    for value in unit_ids:
        ids.extend(split_kaufland_unit_ids(value))
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}, []

    client = _kaufland_client(credentials)
    scope = _credential_scope(credentials)
    wanted = set(ids)
    result: dict[str, LiveOrderState] = {}
    errors: list[str] = []

    unit_to_order = {
        _text(key): _text(value)
        for key, value in (order_ids_by_unit or {}).items()
        if _text(key) and _text(value)
    }

    if not force_refresh:
        for unit_id in ids:
            cached = _cache_get(scope, unit_id)
            # ``open`` is transitory and normally changes after 15+1 minutes.
            # Never let a cached open state decide whether a supplier order can
            # be created; it must be confirmed again against the live endpoints.
            if cached is not None and cached.raw_status != "open":
                result[unit_id] = cached

    rate_limited = False
    for status in ("need_to_be_sent", "open"):
        if wanted.issubset(result):
            break
        try:
            _fetch_kaufland_status_pages(client, status, wanted, result, scope)
        except Exception as exc:
            message = str(exc)
            errors.append(f"Elenco {status}: {message}")
            if "429" in message:
                rate_limited = True
                break

    missing = [unit_id for unit_id in ids if unit_id not in result]
    missing_orders = list(dict.fromkeys(unit_to_order.get(unit_id, "") for unit_id in missing))
    missing_orders = [value for value in missing_orders if value]
    order_metadata: dict[str, dict[str, Any]] = {}

    # An order-detail request resolves every unit in one order and also supplies
    # storefront metadata. It is used before individual detail calls.
    for order_id in ([] if rate_limited else missing_orders):
        try:
            response = _client_order_detail(client, order_id)
            metadata = _order_payload_metadata(response)
            order_metadata[order_id] = metadata
            for raw in metadata.get("units") or []:
                unit_id = _text(raw.get("id_order_unit"))
                if unit_id not in wanted:
                    continue
                state = classify_kaufland_unit(raw, unit_id)
                result[unit_id] = state
        except Exception as exc:
            message = str(exc)
            errors.append(f"Ordine {order_id}: {message}")
            if "429" in message:
                rate_limited = True
                break

    if not rate_limited:
        for unit_id in ids:
            if unit_id in result:
                continue
            try:
                response = _client_unit_detail(client, unit_id)
                state = classify_kaufland_unit(response, unit_id)
                result[unit_id] = state
                if state.order_id and unit_id not in unit_to_order:
                    unit_to_order[unit_id] = state.order_id
            except Exception as exc:
                message = str(exc)
                if "429" in message:
                    errors.append(
                        "Limite richieste Kaufland raggiunto. La verifica è stata "
                        "interrotta e verrà ripresa dal cache al prossimo tentativo."
                    )
                    rate_limited = True
                    break
                errors.append(f"Unità {unit_id}: {message}")

    # A bulk/list result showing ``open`` can become obsolete within minutes.
    # Re-read those units from the detail endpoint so a unit that has already
    # advanced to need_to_be_sent is not incorrectly excluded.
    if not rate_limited:
        for unit_id, state in list(result.items()):
            if state.raw_status != "open":
                continue
            try:
                detail = _client_unit_detail(client, unit_id)
                refreshed = classify_kaufland_unit(detail, unit_id)
                result[unit_id] = refreshed
                if refreshed.order_id:
                    unit_to_order[unit_id] = refreshed.order_id
            except Exception as exc:
                message = str(exc)
                errors.append(f"Aggiornamento unità aperta {unit_id}: {message}")
                if "429" in message:
                    rate_limited = True
                    break

    # Resolve order metadata for selected orders which were already found in the
    # bulk list. This provides the storefront (de/at/pl/...) and a second source
    # for the current status without making one request per row of a grouped order.
    order_ids = list(dict.fromkeys(
        _text(unit_to_order.get(unit_id) or (result.get(unit_id).order_id if result.get(unit_id) else ""))
        for unit_id in ids
    ))
    order_ids = [value for value in order_ids if value]
    if not rate_limited:
        try:
            manifest_metadata = _fetch_order_manifest_metadata(client, order_ids)
            for order_id, metadata in manifest_metadata.items():
                order_metadata.setdefault(order_id, metadata)
        except Exception as exc:
            message = str(exc)
            errors.append(f"Elenco ordini Kaufland: {message}")
            if "429" in message:
                rate_limited = True
    if not rate_limited:
        for order_id in order_ids:
            if order_id in order_metadata:
                continue
            # For absolute accuracy, detail is mandatory for open units and is
            # also requested when the user explicitly forces a refresh.
            related = [
                result[unit_id] for unit_id in ids
                if unit_id in result and (unit_to_order.get(unit_id) or result[unit_id].order_id) == order_id
            ]
            if not any(state.raw_status == "open" for state in related):
                continue
            try:
                response = _client_order_detail(client, order_id)
                metadata = _order_payload_metadata(response)
                order_metadata[order_id] = metadata
                for raw in metadata.get("units") or []:
                    unit_id = _text(raw.get("id_order_unit"))
                    if unit_id in wanted:
                        result[unit_id] = classify_kaufland_unit(raw, unit_id)
            except Exception as exc:
                message = str(exc)
                errors.append(f"Dettaglio ordine {order_id}: {message}")
                if "429" in message:
                    rate_limited = True
                    break

    # The official shipping-addresses endpoint is the authoritative source for
    # destination data. It works in bulk and returns no row while an order is
    # genuinely still inside the cancellation-prior-to-shipment window.
    addresses_by_unit: dict[str, dict[str, Any]] = {}
    if not rate_limited and hasattr(client, "shipping_addresses"):
        for chunk in _chunks(ids, 50):
            try:
                response = client.shipping_addresses(order_unit_ids=chunk)
                chunk_by_unit, _ = _shipping_address_entries(response)
                addresses_by_unit.update(chunk_by_unit)
            except Exception as exc:
                message = str(exc)
                errors.append(f"Indirizzi unità {','.join(chunk)}: {message}")
                if "429" in message:
                    rate_limited = True
                    break

    # Enrich every state with storefront, destination country and address
    # availability. For an ``open`` unit the supplier order is allowed only when
    # Kaufland actually returns complete address data; marking it shipped remains
    # blocked until the status becomes need_to_be_sent.
    enriched: dict[str, LiveOrderState] = {}
    for unit_id, state in result.items():
        order_id = unit_to_order.get(unit_id) or state.order_id
        metadata = order_metadata.get(order_id, {})
        enriched_state = _enrich_kaufland_state(
            state,
            order_metadata=metadata,
            address_entry=addresses_by_unit.get(unit_id),
        )
        enriched[unit_id] = enriched_state
        _cache_put(scope, enriched_state)
    result = enriched

    if rate_limited:
        for unit_id in ids:
            if unit_id in result:
                continue
            stale = _cache_get(scope, unit_id, allow_stale=True)
            if stale is not None:
                result[unit_id] = stale

    return result, list(dict.fromkeys(errors))

def aggregate_kaufland_states(
    states: Sequence[LiveOrderState],
    *,
    order_id: str = "",
) -> dict[str, Any]:
    values = [state for state in states if state.marketplace == "kaufland"]
    supplier_ready = [state for state in values if state.can_generate_supplier_order]
    shippable = [state for state in values if state.can_mark_shipped]
    already = [state for state in values if state.already_shipped]
    cancelled = [state for state in values if state.cancelled]
    pending = [state for state in values if state.raw_status == "open" and not state.can_generate_supplier_order]
    preparable_open = [state for state in values if state.raw_status == "open" and state.can_generate_supplier_order]
    unknown = [state for state in values if state.macro_status == "Sconosciuto"]
    labels = list(dict.fromkeys(f"{state.raw_status}: {state.label}" for state in values))

    if shippable:
        macro = "Da spedire"
        reason = f"{len(shippable)} unità pronte anche per l'aggiornamento spedizione"
    elif preparable_open:
        macro = "Da preparare"
        reason = (
            f"{len(preparable_open)} unità aperte con indirizzo disponibile: "
            "ordine Cecotec consentito, aggiornamento spedizione ancora bloccato"
        )
    elif values and len(already) == len(values):
        macro = "Spedito"
        reason = "tutte le unità risultano già spedite o concluse"
    elif values and len(cancelled) == len(values):
        macro = "Cancellato"
        reason = "tutte le unità risultano cancellate"
    elif pending:
        macro = "In attesa"
        reason = "ordine aperto senza indirizzo di consegna restituito dall'API"
    elif unknown:
        macro = "Sconosciuto"
        reason = "una o più unità hanno uno stato non riconosciuto"
    else:
        macro = "Misto"
        reason = "ordine con unità in stati differenti"

    storefronts = list(dict.fromkeys(state.storefront for state in values if state.storefront))
    storefront_countries = list(dict.fromkeys(
        state.storefront_country_code for state in values if state.storefront_country_code
    ))
    shipping_countries = list(dict.fromkeys(
        state.shipping_country_code for state in values if state.shipping_country_code
    ))
    return {
        "order_id": order_id or (values[0].order_id if values else ""),
        "states": values,
        "raw_status": " | ".join(labels),
        "macro_status": macro,
        "reason": reason,
        "supplier_order_unit_ids": [state.unit_id for state in supplier_ready],
        "shippable_unit_ids": [state.unit_id for state in shippable],
        "already_shipped_unit_ids": [state.unit_id for state in already],
        "cancelled_unit_ids": [state.unit_id for state in cancelled],
        "pending_unit_ids": [state.unit_id for state in pending],
        "preparable_open_unit_ids": [state.unit_id for state in preparable_open],
        "unknown_unit_ids": [state.unit_id for state in unknown],
        "can_generate_supplier_order": bool(supplier_ready),
        "can_mark_shipped": bool(shippable),
        "already_shipped": bool(values) and len(already) == len(values),
        "cancelled": bool(values) and len(cancelled) == len(values),
        "storefronts": storefronts,
        "storefront_country_codes": storefront_countries,
        "shipping_country_codes": shipping_countries,
        "shipping_address_available": bool(values) and all(
            state.shipping_address_available for state in supplier_ready
        ) if supplier_ready else False,
    }

def verify_order_rows(
    *,
    marketplace: str,
    credentials: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Refresh order/unit states for operational rows.

    The returned rows contain the original fields plus ``live_*`` fields.  No
    cached state is trusted for deciding whether a supplier order can be created
    or a marketplace shipment can be confirmed.
    """
    marketplace_key = _text(marketplace).lower()
    updated: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    errors: list[str] = []
    if marketplace_key == "worten":
        states, errors = fetch_worten_order_states(
            credentials, [item.get("order_id") for item in rows]
        )
        for source in rows:
            item = dict(source)
            order_id = _text(item.get("order_id"))
            state = states.get(order_id)
            if state is None:
                item.update({
                    "live_verified": False,
                    "live_can_generate_supplier_order": False,
                    "live_can_mark_shipped": False,
                    "live_reason": "stato live non disponibile",
                })
            else:
                item.update({
                    "raw_status": state.raw_status,
                    "normalized_status": state.macro_status,
                    "status_label": state.label,
                    "live_verified": True,
                    "live_raw_status": state.raw_status,
                    "live_status_label": state.label,
                    "live_macro_status": state.macro_status,
                    "live_can_generate_supplier_order": state.can_generate_supplier_order,
                    "live_can_mark_shipped": state.can_mark_shipped,
                    "live_already_shipped": state.already_shipped,
                    "live_cancelled": state.cancelled,
                    "live_returned": state.returned,
                    "live_reason": state.reason,
                    "live_checked_at": state.checked_at,
                    "live_refund_status": state.refund_status,
                    "live_refund_label": state.refund_label,
                })
                audit.append({
                    "Ordine": order_id,
                    "Riga/unità": _text(item.get("order_line_id")),
                    "Stato API": state.raw_status,
                    "Descrizione": state.label,
                    "Macro-stato": state.macro_status,
                    "Rimborso": state.refund_label,
                    "Ordine Cecotec consentito": "Sì" if state.can_generate_supplier_order else "No",
                    "Aggiornamento spedizione consentito": "Sì" if state.can_mark_shipped else "No",
                    "Motivo": state.reason,
                    "Verificato il": state.checked_at,
                })
            updated.append(item)
        return updated, audit, errors

    if marketplace_key == "kaufland":
        row_units: list[tuple[Mapping[str, Any], list[str]]] = [
            (item, split_kaufland_unit_ids(item.get("order_line_id")))
            for item in rows
        ]
        unit_order_map: dict[str, str] = {}
        flattened_unit_ids: list[str] = []
        for item, unit_ids in row_units:
            order_id = _text(item.get("order_id"))
            for unit_id in unit_ids:
                flattened_unit_ids.append(unit_id)
                if order_id:
                    unit_order_map[unit_id] = order_id

        states, errors = fetch_kaufland_unit_states(
            credentials,
            flattened_unit_ids,
            order_ids_by_unit=unit_order_map,
            force_refresh=force_refresh,
        )
        for source, unit_ids in row_units:
            item = dict(source)
            order_id = _text(item.get("order_id"))
            resolved = [states[unit_id] for unit_id in unit_ids if unit_id in states]
            missing_ids = [unit_id for unit_id in unit_ids if unit_id not in states]

            # Each unit is shown separately in the audit table, even when the
            # Cecotec grid grouped several identical units in one order row.
            for state in resolved:
                marketplace_country = state.storefront_country_label
                if state.storefront_country_code:
                    marketplace_country = (
                        f"{marketplace_country} ({state.storefront_country_code})"
                        if marketplace_country else state.storefront_country_code
                    )
                shipping_country = state.shipping_country_label
                if state.shipping_country_code:
                    shipping_country = (
                        f"{shipping_country} ({state.shipping_country_code})"
                        if shipping_country else state.shipping_country_code
                    )
                audit.append({
                    "Ordine": order_id or state.order_id,
                    "Riga/unità": state.unit_id,
                    "Marketplace": f"Kaufland.{state.storefront}" if state.storefront else "Kaufland",
                    "Nazione marketplace": marketplace_country,
                    "Paese consegna": shipping_country,
                    "Indirizzo disponibile": "Sì" if state.shipping_address_available else "No",
                    "Stato API": state.raw_status,
                    "Descrizione": state.label,
                    "Macro-stato": state.macro_status,
                    "Rimborso": "",
                    "Ordine Cecotec consentito": "Sì" if state.can_generate_supplier_order else "No",
                    "Aggiornamento spedizione consentito": "Sì" if state.can_mark_shipped else "No",
                    "Motivo": state.reason,
                    "Verificato il": state.checked_at,
                })

            if not unit_ids:
                item.update({
                    "live_verified": False,
                    "live_can_generate_supplier_order": False,
                    "live_can_mark_shipped": False,
                    "live_reason": "identificativo unità Kaufland mancante o non valido",
                })
                errors.append(
                    f"Ordine {order_id or 'senza numero'}: nessun id_order_unit valido"
                )
                updated.append(item)
                continue

            if missing_ids:
                item.update({
                    "live_verified": False,
                    "live_can_generate_supplier_order": False,
                    "live_can_mark_shipped": False,
                    "live_reason": (
                        "stato live non disponibile per le unità " + ", ".join(missing_ids)
                    ),
                    "live_missing_unit_ids": missing_ids,
                })
                updated.append(item)
                continue

            aggregate = aggregate_kaufland_states(resolved, order_id=order_id)
            supplier_ids = list(aggregate.get("supplier_order_unit_ids") or [])
            shippable_ids = list(aggregate.get("shippable_unit_ids") or [])
            excluded_ids = [unit_id for unit_id in unit_ids if unit_id not in supplier_ids]
            partial = bool(supplier_ids and excluded_ids)

            if supplier_ids:
                # Supplier-order eligibility is intentionally broader than the
                # marketplace /send eligibility. An ``open`` unit with a complete
                # address can be sent to Cecotec, although Kaufland still blocks
                # marking it shipped until need_to_be_sent.
                item["order_line_id"] = ",".join(supplier_ids)
                item["quantity"] = len(supplier_ids)
                address_state = next(
                    (
                        state for state in resolved
                        if state.unit_id in supplier_ids and state.shipping_address_available
                    ),
                    None,
                )
                if address_state is not None:
                    address = _address_summary(address_state.shipping_address or {})
                    live_values = {
                        "customer_name": address.get("name"),
                        "address": address.get("street"),
                        "postal_code": address.get("postal_code"),
                        "city": address.get("city"),
                        "country_code": address.get("country_code"),
                        "phone": address.get("phone"),
                    }
                    for field, value in live_values.items():
                        if _text(value):
                            item[field] = _text(value)

            raw_status = _text(aggregate.get("raw_status"))
            macro_status = _text(aggregate.get("macro_status"))
            reason = _text(aggregate.get("reason"))
            if partial:
                reason = (
                    f"{len(supplier_ids)} di {len(unit_ids)} unità utilizzabili per Cecotec; "
                    f"escluse: {', '.join(excluded_ids)}"
                )

            item.update({
                "raw_status": raw_status,
                "normalized_status": macro_status,
                "status_label": macro_status,
                "live_verified": True,
                "live_raw_status": raw_status,
                "live_status_label": macro_status,
                "live_macro_status": macro_status,
                "live_can_generate_supplier_order": bool(aggregate.get("can_generate_supplier_order")),
                "live_can_mark_shipped": bool(aggregate.get("can_mark_shipped")),
                "live_already_shipped": bool(aggregate.get("already_shipped")),
                "live_cancelled": bool(aggregate.get("cancelled")),
                "live_returned": bool(resolved) and all(state.returned for state in resolved),
                "live_reason": reason,
                "live_checked_at": max(
                    (state.checked_at for state in resolved if state.checked_at),
                    default=_checked_at(),
                ),
                "live_partial": partial,
                "live_all_unit_ids": unit_ids,
                "live_supplier_order_unit_ids": supplier_ids,
                "live_shippable_unit_ids": shippable_ids,
                "live_excluded_unit_ids": excluded_ids,
                "live_storefronts": list(aggregate.get("storefronts") or []),
                "live_storefront_country_codes": list(aggregate.get("storefront_country_codes") or []),
                "live_shipping_country_codes": list(aggregate.get("shipping_country_codes") or []),
                "live_shipping_address_available": bool(aggregate.get("shipping_address_available")),
            })
            shipping_codes = list(aggregate.get("shipping_country_codes") or [])
            if len(shipping_codes) == 1:
                item["country_code"] = shipping_codes[0]
            storefronts = list(aggregate.get("storefronts") or [])
            if len(storefronts) == 1:
                item["marketplace_storefront"] = storefronts[0]
            updated.append(item)
        return updated, audit, list(dict.fromkeys(errors))

    return [dict(item) for item in rows], [], [f"Marketplace non supportato: {marketplace}"]
