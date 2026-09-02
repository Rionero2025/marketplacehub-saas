from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urljoin, urlparse

import requests

from services.cecotec_orders import (
    clean_identifier,
    clean_text,
    normalize_country_code,
    normalize_supplier,
    parse_composite_sku,
)
from services.db import DATA_DIR, accessible_lists, connect, execute, execute_many, now_iso, rows
from services.lists import normalize, read_list
from services.order_tracking import match_tracking_rows, normalized_customer_name
from services.security import decrypt_dict, encrypt_dict


PACKLINK_SERVICE_VERSION = 255
PACKLINK_API_BASE_URL = "https://api.packlink.com/v1/"
PACKLINK_PROVIDER = "packlink_pro"
DEFAULT_TIMEOUT = 30.0
MAX_PAGES = 500
DEFAULT_SERVICE_SOURCE = "PRO"
DEFAULT_PLATFORM_CODE = "PRO"


class PacklinkAPIError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _first(value: Any, *paths: str) -> Any:
    for path in paths:
        current = value
        ok = True
        for part in path.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            else:
                ok = False
                break
        if ok and current not in (None, "", [], {}):
            return current
    return None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _iso(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return text


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, (Mapping, list, tuple, bool)):
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


# Packlink's shipping-service endpoint is strict about postal-code formatting.
# Marketplace APIs and spreadsheets can turn numeric postcodes such as Slovak
# ``03851`` into ``3851``.  Keep the original value as a fallback, but create
# deterministic API-safe candidates before giving up on an HTTP 400.
_FIXED_NUMERIC_POSTCODE_LENGTHS: dict[str, int] = {
    "AT": 4, "BE": 4, "DK": 4, "NO": 4,
    "CZ": 5, "DE": 5, "ES": 5, "FI": 5, "FR": 5, "GR": 5,
    "IT": 5, "SE": 5, "SK": 5, "US": 5,
}


def normalize_packlink_postal_code(country: Any, postal_code: Any) -> str:
    """Return the safest Packlink postcode representation for one country.

    The function is intentionally conservative: alphanumeric postcodes are not
    guessed.  Numeric postcodes are stripped of separators and zero-padded only
    for countries with a fixed, well-known length.
    """
    code = clean_text(country).upper()[:2]
    raw = clean_text(postal_code).upper()
    if not raw:
        return ""
    if re.fullmatch(r"\d+\.0", raw):
        raw = raw[:-2]
    compact = re.sub(r"\s+", "", raw)
    digits = re.sub(r"[^0-9]", "", raw)
    expected = _FIXED_NUMERIC_POSTCODE_LENGTHS.get(code)
    if expected and digits and re.fullmatch(r"[0-9\s-]+", raw):
        if len(digits) <= expected:
            digits = digits.zfill(expected)
        return digits
    if code == "PL" and digits and re.fullmatch(r"[0-9\s-]+", raw):
        digits = digits.zfill(5)
        return f"{digits[:2]}-{digits[2:]}"
    if code == "PT" and digits and re.fullmatch(r"[0-9\s-]+", raw):
        digits = digits.zfill(7)
        return f"{digits[:4]}-{digits[4:]}"
    return compact


def packlink_postal_code_candidates(country: Any, postal_code: Any) -> list[str]:
    """Return a small ordered set of postcode variants worth trying on HTTP 400."""
    code = clean_text(country).upper()[:2]
    raw = clean_text(postal_code).upper()
    normalized = normalize_packlink_postal_code(code, raw)
    candidates: list[str] = []
    for value in (normalized, raw):
        value = clean_text(value)
        if value and value not in candidates:
            candidates.append(value)
    # Czech and Slovak user interfaces commonly display XXX XX. Packlink has
    # accepted both compact and spaced forms over API revisions, so retain the
    # official-looking alternative as a retry only, never as a first guess.
    digits = re.sub(r"[^0-9]", "", normalized)
    if code in {"CZ", "SK"} and len(digits) == 5:
        spaced = f"{digits[:3]} {digits[3:]}"
        if spaced not in candidates:
            candidates.append(spaced)
    return candidates


def _payload_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Return a list of dictionaries from Packlink's common response shapes."""
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            nested = _payload_list(value, *keys)
            if nested:
                return nested
    return [dict(payload)] if payload else []


def normalize_packlink_warehouse(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize Packlink warehouse/address payloads without assuming one API revision."""
    address = _first(raw, "address", "warehouse.address", "contact.address")
    source = address if isinstance(address, Mapping) else raw
    name = clean_text(
        _first(raw, "alias", "label", "warehouse_name", "warehouseName", "name")
        or _first(source, "company", "company_name", "name")
    )
    first_name = clean_text(_first(source, "first_name", "firstname", "name"))
    last_name = clean_text(_first(source, "last_name", "lastname", "surname"))
    contact_name = clean_text(
        _first(source, "contact_name", "contactName", "full_name", "fullName")
        or " ".join(part for part in (first_name, last_name) if part)
        or name
    )
    postal_code = clean_text(_first(source, "zip_code", "zipCode", "postal_code", "postalCode", "zip"))
    city = clean_text(_first(source, "city", "town", "locality"))
    # Packlink's Warehouse DTO accepts postal_code both as a plain code and, in
    # some front-end responses, as "CODE - City". Mirror the official DTO parser.
    if " - " in postal_code:
        code_part, city_part = postal_code.split(" - ", 1)
        postal_code = clean_text(code_part)
        if not city:
            city = clean_text(city_part)
    return {
        "id": clean_text(_first(raw, "id", "warehouse_id", "warehouseId", "uuid")),
        "name": name or contact_name or "Magazzino Packlink",
        "contact_name": contact_name,
        "surname": last_name,
        "company": clean_text(_first(source, "company", "company_name", "companyName")),
        "street1": clean_text(_first(source, "street1", "street", "address", "address1", "address_line_1")),
        "street2": clean_text(_first(source, "street2", "address2", "address_line_2")),
        "zip_code": postal_code,
        "city": city,
        "country": clean_text(_first(source, "country", "country_code", "countryCode", "country_iso_code")).upper(),
        "phone": clean_text(_first(source, "phone", "telephone", "mobile")),
        "email": clean_text(_first(source, "email", "contact.email")),
        "default": bool(_first(raw, "default_selection", "default", "is_default")),
        "raw": dict(raw),
    }


def match_packlink_warehouse_id(
    sender: Mapping[str, Any], warehouses: Sequence[Mapping[str, Any]],
) -> str:
    """Match a local sender address to a Packlink warehouse id, conservatively.

    Packlink's official e-commerce core sends ``additional_data.selectedWarehouseId``
    only when the id comes from Packlink itself. Marketplace Hub therefore never
    sends its local sender-row id. We reuse a Packlink warehouse id only when the
    address match is strong and unambiguous.
    """
    def norm(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", clean_text(value).casefold())

    source = normalize_sender_address(sender)
    scored: list[tuple[int, str]] = []
    for raw in warehouses or []:
        item = dict(raw) if isinstance(raw, Mapping) else {}
        warehouse_id = clean_text(item.get("id"))
        if not warehouse_id:
            continue
        target = normalize_sender_address(item)
        score = 0
        if source.get("country") and source.get("country") == target.get("country"):
            score += 4
        if norm(source.get("zip_code")) and norm(source.get("zip_code")) == norm(target.get("zip_code")):
            score += 5
        if norm(source.get("street1")) and norm(source.get("street1")) == norm(target.get("street1")):
            score += 6
        if norm(source.get("city")) and norm(source.get("city")) == norm(target.get("city")):
            score += 2
        if norm(source.get("company")) and norm(source.get("company")) == norm(target.get("company")):
            score += 1
        scored.append((score, warehouse_id))
    if not scored:
        return ""
    scored.sort(reverse=True)
    best_score = scored[0][0]
    best_ids = list(dict.fromkeys(identifier for score, identifier in scored if score == best_score))
    # Country + postcode + street is 15 points. Country + postcode + city is
    # 11 points and is also sufficiently specific when unique.
    return best_ids[0] if best_score >= 11 and len(best_ids) == 1 else ""


def normalize_packlink_parcel(raw: Mapping[str, Any]) -> dict[str, Any]:
    dimensions = _first(raw, "dimensions", "parcel", "package")
    source = dimensions if isinstance(dimensions, Mapping) else raw
    return {
        "id": clean_text(_first(raw, "id", "parcel_id", "parcelId", "uuid")),
        "name": clean_text(_first(raw, "name", "alias", "label", "title")) or "Pacco Packlink",
        "weight": float(_number(_first(source, "weight", "weight_kg", "weightKg")) or 0),
        "width": float(_number(_first(source, "width")) or 0),
        "height": float(_number(_first(source, "height")) or 0),
        "length": float(_number(_first(source, "length", "depth")) or 0),
        "raw": dict(raw),
    }


def normalize_shipping_service(raw: Mapping[str, Any]) -> dict[str, Any]:
    price = raw.get("price") if isinstance(raw.get("price"), Mapping) else {}
    service_info = raw.get("service_info") if isinstance(raw.get("service_info"), list) else []
    available_dates = raw.get("available_dates") if isinstance(raw.get("available_dates"), Mapping) else {}
    return {
        "id": clean_text(_first(raw, "id", "service_id", "serviceId")),
        "carrier": clean_text(_first(raw, "carrier_name", "carrierName", "carrier.name", "carrier")),
        "service": clean_text(_first(raw, "name", "service_name", "serviceName", "service.name")),
        "currency": clean_text(_first(raw, "currency", "price.currency")).upper() or "EUR",
        "price": _number(_first(price, "total_price", "total", "amount") or _first(raw, "total_price", "totalPrice")),
        "base_price": _number(_first(price, "base_price", "base")),
        "tax_price": _number(_first(price, "tax_price", "tax")),
        "category": clean_text(_first(raw, "category", "type")),
        "transit_time": clean_text(_first(raw, "transit_time", "transitTime")),
        "transit_hours": _integer(_first(raw, "transit_hours", "transitHours")),
        "estimated_delivery": clean_text(_first(raw, "first_estimated_delivery_date", "firstEstimatedDeliveryDate")),
        "dropoff": bool(_first(raw, "dropoff", "departure_dropoff", "departureDropOff") or False),
        "delivery_to_parcelshop": bool(_first(raw, "delivery_to_parcelshop", "destinationDropOff") or False),
        "labels_required": bool(_first(raw, "labels_required", "labelsRequired") or False),
        "service_info": service_info,
        "tags": list(raw.get("tags")) if isinstance(raw.get("tags"), list) else [],
        "available_dates": dict(available_dates),
        "raw": dict(raw),
    }


def first_collection_slot(service: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return Packlink's first valid collection slot without inventing one.

    Packlink's shipping-service response exposes ``available_dates`` values in a
    display-oriented form such as ``[09:00 , 18:00]`` while the draft DTO, when
    these optional fields are used, expects ``HH:MM-HH:MM``.  The official
    e-commerce core also permits these draft fields to remain null, therefore a
    missing slot must never be replaced by an arbitrary next-day window.
    """
    dates = service.get("available_dates") if isinstance(service.get("available_dates"), Mapping) else {}
    for raw_date, raw_time in dates.items():
        day = clean_text(raw_date).replace("-", "/")
        if not re.fullmatch(r"\d{4}/\d{2}/\d{2}", day):
            continue
        time_text = clean_text(raw_time)
        if re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", time_text):
            return day, time_text
        times = re.findall(r"\b\d{2}:\d{2}\b", time_text)
        if len(times) >= 2:
            return day, f"{times[0]}-{times[1]}"
    return None, None


def packlink_content_description(values: Iterable[Any]) -> str:
    """Serialize draft content exactly like Packlink's official Draft DTO.

    The API receives one text field, not a JSON array. Packlink's reference core
    joins item descriptions, removes its forbidden characters and truncates the
    result to 60 characters before POST /v1/shipments.
    """
    content = ", ".join(clean_text(value) for value in values if clean_text(value))
    if not content:
        content = "Prodotti e-commerce"
    forbidden = ";:%&/ºª€$@#()=?¿¡!\\'`´^*Êè"
    translation = str.maketrans("", "", forbidden)
    return content.translate(translation)[:60]


def _tracking_numbers(value: Any) -> list[str]:
    found: list[str] = []

    def visit(current: Any) -> None:
        if isinstance(current, Mapping):
            preferred = (
                "tracking_number", "trackingNumber", "tracking_reference",
                "trackingReference", "tracking_code", "trackingCode",
                "carrier_tracking_number", "carrierTrackingNumber", "number",
            )
            for key in preferred:
                if key in current and current.get(key) not in (None, ""):
                    text = clean_text(current.get(key))
                    if text and text not in found:
                        found.append(text)
            for key, child in current.items():
                normalized = str(key).lower().replace("-", "_")
                if "tracking" in normalized or normalized in {"packages", "parcels"}:
                    if isinstance(child, (Mapping, list, tuple)):
                        visit(child)
                    elif child not in (None, ""):
                        text = clean_text(child)
                        if text and text not in found:
                            found.append(text)
        elif isinstance(current, (list, tuple)):
            for child in current:
                visit(child)

    visit(value)
    return found


def _carrier(value: Any) -> str:
    candidate = _first(
        value,
        "carrier.name", "carrier_name", "carrierName",
        "service.carrier.name", "service.carrier", "service.carrier_name",
        "service_name", "serviceName", "transport.company", "transport.carrier",
    )
    if isinstance(candidate, Mapping):
        candidate = _first(candidate, "name", "label", "code", "slug")
    return clean_text(candidate)


def _recipient(value: Any) -> Mapping[str, Any]:
    candidate = _first(
        value,
        "to", "recipient", "destination", "delivery_address", "deliveryAddress",
        "receiver", "address_to", "addressTo",
    )
    return candidate if isinstance(candidate, Mapping) else {}


def _recipient_name(value: Mapping[str, Any]) -> str:
    recipient = _recipient(value)
    direct = _first(recipient, "name", "full_name", "fullName", "contact_name")
    if direct:
        return clean_text(direct)
    first_name = clean_text(_first(recipient, "first_name", "firstName", "firstname"))
    last_name = clean_text(_first(recipient, "last_name", "lastName", "lastname", "surname"))
    return " ".join(part for part in (first_name, last_name) if part).strip()


def _recipient_email(value: Mapping[str, Any]) -> str:
    recipient = _recipient(value)
    return clean_text(_first(recipient, "email", "contact.email") or _first(value, "email"))


def _recipient_phone(value: Mapping[str, Any]) -> str:
    recipient = _recipient(value)
    return clean_text(
        _first(recipient, "phone", "telephone", "mobile", "contact.phone")
        or _first(value, "phone")
    )


def _recipient_address(value: Mapping[str, Any]) -> str:
    recipient = _recipient(value)
    parts = [
        _first(recipient, "street1", "address1", "address", "street"),
        _first(recipient, "street2", "address2"),
        _first(recipient, "zip_code", "zipCode", "postal_code", "postalCode", "zip"),
        _first(recipient, "city", "locality"),
        _first(recipient, "country", "country_code", "countryCode"),
    ]
    return " · ".join(clean_text(item) for item in parts if clean_text(item))


def normalize_packlink_shipment(raw: Mapping[str, Any], *, position: int = 0) -> dict[str, Any]:
    """Normalize a Packlink shipment into the tracking matcher contract.

    Packlink has returned slightly different field shapes across integrations.
    The normalizer intentionally accepts both flat and nested variants while
    preserving the original response in ``raw_json``.
    """
    reference = clean_text(
        _first(raw, "reference", "shipment_reference", "shipmentReference", "id", "uuid")
    )
    custom_reference = clean_text(
        _first(raw, "shipment_custom_reference", "shipmentCustomReference", "custom_reference", "customReference")
    )
    order_reference = clean_text(
        _first(
            raw,
            "order_reference", "orderReference", "order.reference", "order.number",
            "external_reference", "externalReference", "merchant_reference",
        )
        or custom_reference
    )
    created_at = _iso(
        _first(raw, "created_at", "createdAt", "created", "creation_date", "creationDate", "date")
    )
    status = clean_text(
        _first(raw, "status", "shipment_status", "shipmentStatus", "state", "tracking.status")
    )
    tracking_numbers = _tracking_numbers(raw)
    tracking = " / ".join(dict.fromkeys(tracking_numbers))
    carrier = _carrier(raw)

    return {
        "source_row": int(position),
        "source_reference": reference,
        "reference": reference,
        "marketplace_order_reference": order_reference,
        "order_reference": order_reference,
        "shipment_custom_reference": custom_reference,
        "customer_name": _recipient_name(raw),
        "email": _recipient_email(raw),
        "phone": _recipient_phone(raw),
        "address": _recipient_address(raw),
        "product": clean_text(_first(raw, "content", "contents", "description", "parcel.content")),
        "product_code": clean_text(_first(raw, "sku", "product_sku", "productSku")),
        "eans": [],
        "created_at": created_at,
        "file_status": status,
        "status": status,
        "tracking": tracking,
        "carrier": carrier,
        "service": clean_text(_first(raw, "service.name", "service_name", "serviceName")),
        "price": _number(_first(raw, "price.total", "price.amount", "total_price", "totalPrice", "price")),
        "currency": clean_text(_first(raw, "price.currency", "currency", "currency_code")),
        "raw": dict(raw),
        "raw_json": json.dumps(raw, ensure_ascii=False, default=str),
    }


def _extract_shipments(payload: Any) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)], {}
    if not isinstance(payload, Mapping):
        return [], {}
    candidates = (
        "shipments", "results", "items", "data", "objects", "content", "records",
    )
    for key in candidates:
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)], payload
        if isinstance(value, Mapping):
            nested, _ = _extract_shipments(value)
            if nested:
                return nested, payload
    # Some APIs return a single shipment object for a one-result query.
    if any(key in payload for key in ("reference", "shipment_reference", "order_reference")):
        return [dict(payload)], payload
    return [], payload


def _next_url(metadata: Mapping[str, Any], current_url: str) -> str:
    candidates = [
        _first(metadata, "next", "next_url", "nextUrl", "links.next", "pagination.next"),
        _first(metadata, "meta.next", "paging.next"),
    ]
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            candidate = _first(candidate, "href", "url")
        text = clean_text(candidate)
        if text:
            return urljoin(current_url, text)
    return ""


class PacklinkClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = PACKLINK_API_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
        seller_id: int | None = None,
    ) -> None:
        self.api_key = clean_text(api_key)
        self.seller_id = int(seller_id) if seller_id is not None else None
        self.base_url = str(base_url or PACKLINK_API_BASE_URL).rstrip("/") + "/"
        self.timeout = max(5.0, float(timeout))
        self.session = session or requests.Session()
        if not self.api_key:
            raise ValueError("API key Packlink PRO mancante.")

    @property
    def headers(self) -> dict[str, str]:
        # Packlink's official module sends the generated API key directly in
        # Authorization (Bearer is used only during the OAuth/API-key creation flow).
        return {
            "Authorization": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MarketplaceHub/PacklinkPRO",
            "X-Ecommerce-Name": "Marketplace Hub",
            "X-Ecommerce-Version": str(PACKLINK_SERVICE_VERSION),
            "X-Module-Version": str(PACKLINK_SERVICE_VERSION),
        }

    def _request_url(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> Any:
        request_kwargs: dict[str, Any] = {
            "headers": self.headers,
            "params": dict(params or {}),
            "timeout": self.timeout,
        }
        if json_body is not None:
            request_kwargs["json"] = json_body
        response = self.session.request(method.upper(), url, **request_kwargs)
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"raw": response.text[:4000]}
        if not response.ok:
            message = ""
            if isinstance(payload, Mapping):
                message = clean_text(_first(payload, "message", "error", "detail"))
                if not message and isinstance(payload.get("messages"), list):
                    parts = [
                        clean_text(item.get("message") if isinstance(item, Mapping) else item)
                        for item in payload.get("messages", [])
                    ]
                    message = " | ".join(part for part in parts if part)
            if not message:
                message = clean_text(response.text)[:1000]
            raise PacklinkAPIError(
                f"Packlink API HTTP {response.status_code}: {message or response.reason}",
                status=response.status_code,
                payload=payload,
            )
        return payload

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> Any:
        endpoint = str(endpoint or "").lstrip("/")
        return self._request_url(
            method, urljoin(self.base_url, endpoint), params=params, json_body=json_body
        )

    def user_data(self) -> Any:
        # Endpoint used by Packlink's official e-commerce module to validate
        # the merchant token and obtain client data.
        return self.request("GET", "clients")

    def validate(self) -> dict[str, Any]:
        try:
            payload = self.user_data()
            client = payload
            if isinstance(payload, list):
                client = payload[0] if payload else {}
            if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping):
                client = payload.get("data")
            if not isinstance(client, Mapping):
                client = {}
            return {
                "ok": True,
                "status": 200,
                "message": "Connessione Packlink PRO verificata.",
                "client": dict(client),
            }
        except PacklinkAPIError as error:
            return {
                "ok": False,
                "status": error.status,
                "message": str(error),
                "client": {},
            }
        except Exception as error:
            return {"ok": False, "status": None, "message": str(error), "client": {}}

    def list_shipments(self, *, max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
        """Retrieve the complete shipment collection exposed by Packlink.

        The public Packlink integration code documents shipment detail endpoints;
        Packlink PRO accounts also expose the shipment collection used by the
        dashboard/integrations. Pagination shapes have varied over time, so this
        routine follows server-provided ``next`` links and also understands common
        page/offset metadata without imposing undocumented query filters.
        """
        current_url = urljoin(self.base_url, "shipments")
        params: dict[str, Any] | None = None
        all_rows: list[dict[str, Any]] = []
        seen_references: set[str] = set()
        seen_urls: set[str] = set()

        for _page_index in range(max(1, int(max_pages))):
            request_key = current_url + "?" + "&".join(
                f"{key}={params[key]}" for key in sorted(params or {})
            )
            if request_key in seen_urls:
                break
            seen_urls.add(request_key)
            payload = self._request_url("GET", current_url, params=params)
            batch, metadata = _extract_shipments(payload)
            if not batch:
                break

            new_count = 0
            for item in batch:
                ref = clean_text(
                    _first(item, "reference", "shipment_reference", "shipmentReference", "id", "uuid")
                )
                dedupe_key = ref or json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
                if dedupe_key in seen_references:
                    continue
                seen_references.add(dedupe_key)
                all_rows.append(item)
                new_count += 1

            next_link = _next_url(metadata, current_url)
            if next_link:
                current_url = next_link
                params = None
                continue

            # Pagination metadata variants. Only continue when the response itself
            # proves that more data exists; this avoids relying on undocumented
            # Packlink query parameters during a normal one-page response.
            page = _first(metadata, "page", "current_page", "currentPage", "pagination.page")
            pages = _first(metadata, "pages", "total_pages", "totalPages", "pagination.pages")
            try:
                if page is not None and pages is not None and int(page) < int(pages):
                    params = {"page": int(page) + 1}
                    continue
            except (TypeError, ValueError):
                pass

            offset = _first(metadata, "offset", "pagination.offset")
            limit = _first(metadata, "limit", "page_size", "pageSize", "pagination.limit")
            total = _first(metadata, "total", "count", "total_count", "totalCount", "pagination.total")
            try:
                if offset is not None and limit is not None and total is not None:
                    next_offset = int(offset) + int(limit)
                    if next_offset < int(total):
                        params = {"offset": next_offset, "limit": int(limit)}
                        continue
            except (TypeError, ValueError):
                pass

            # If the response contains an explicit total but no offset metadata,
            # try page pagination only when the total proves records are missing.
            try:
                if total is not None and len(all_rows) < int(total) and new_count > 0:
                    page_number = len(all_rows) // max(1, len(batch)) + 1
                    params = {"page": page_number + 1}
                    continue
            except (TypeError, ValueError):
                pass
            break

        return all_rows

    def shipment(self, reference: str) -> Mapping[str, Any]:
        payload = self.request("GET", f"shipments/{reference}")
        return dict(payload) if isinstance(payload, Mapping) else {}

    def tracking(self, reference: str) -> list[dict[str, Any]]:
        payload = self.request("GET", f"shipments/{reference}/track")
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            values = payload.get("tracking") or payload.get("events") or payload.get("data")
            if isinstance(values, list):
                return [dict(item) for item in values if isinstance(item, Mapping)]
            return [dict(payload)]
        return []

    def warehouses(self) -> list[dict[str, Any]]:
        """Return sender warehouses configured in the Packlink PRO account."""
        payload = self.request("GET", "clients/warehouses")
        return [normalize_packlink_warehouse(item) for item in _payload_list(
            payload, "warehouses", "results", "items", "data"
        )]

    def parcels(self) -> list[dict[str, Any]]:
        """Return reusable parcel templates configured in Packlink PRO."""
        payload = self.request("GET", "users/parcels")
        return [normalize_packlink_parcel(item) for item in _payload_list(
            payload, "parcels", "results", "items", "data"
        )]

    def shipping_services(
        self,
        *,
        from_country: str,
        from_zip: str,
        to_country: str,
        to_zip: str,
        packages: Sequence[Mapping[str, Any]],
        source: str = DEFAULT_SERVICE_SOURCE,
        service_id: str | int | None = None,
    ) -> list[dict[str, Any]]:
        """Search Packlink services with postcode repair/retry on HTTP 400.

        The request shape is still Packlink's official ``/v1/services`` query
        contract.  We only retry a small set of equivalent postcode spellings
        because marketplace feeds can lose leading zeroes (especially CZ/SK).
        Package weight/dimensions are never altered to manufacture a quote.
        """
        origin_country = clean_text(from_country).upper()[:2]
        destination_country = clean_text(to_country).upper()[:2]
        if not all(clean_text(value) for value in (origin_country, from_zip, destination_country, to_zip)):
            raise ValueError("Paese/CAP di partenza e destinazione sono obbligatori per Packlink.")
        if not packages:
            raise ValueError("Inserisci almeno un pacco per richiedere le tariffe Packlink.")

        package_params: dict[str, Any] = {}
        safe_packages: list[dict[str, float | int]] = []
        for index, package in enumerate(packages):
            normalized = package_payload(package)
            safe_packages.append(dict(normalized))
            package_params[f"packages[{index}][height]"] = normalized["height"]
            package_params[f"packages[{index}][width]"] = normalized["width"]
            package_params[f"packages[{index}][length]"] = normalized["length"]
            package_params[f"packages[{index}][weight]"] = normalized["weight"]

        origin_candidates = packlink_postal_code_candidates(origin_country, from_zip)
        destination_candidates = packlink_postal_code_candidates(destination_country, to_zip)
        if not origin_candidates or not destination_candidates:
            raise ValueError("CAP di partenza o destinazione non valido per Packlink.")

        attempts: list[dict[str, str]] = []
        last_error: PacklinkAPIError | None = None
        for origin_zip in origin_candidates:
            for destination_zip in destination_candidates:
                params: dict[str, Any] = {
                    "from[country]": origin_country,
                    "from[zip]": origin_zip,
                    "to[country]": destination_country,
                    "to[zip]": destination_zip,
                    "source": clean_text(source) or DEFAULT_SERVICE_SOURCE,
                    **package_params,
                }
                if service_id not in (None, ""):
                    params["serviceId"] = service_id
                try:
                    payload = self.request("GET", "services", params=params)
                    result = [normalize_shipping_service(item) for item in _payload_list(
                        payload, "services", "results", "items", "data"
                    )]
                    return [item for item in result if item.get("id")]
                except PacklinkAPIError as error:
                    last_error = error
                    attempts.append({"from_zip": origin_zip, "to_zip": destination_zip})
                    if error.status != 400:
                        raise

        if last_error is not None:
            package_text = "; ".join(
                f"{float(item['weight']):.2f} kg · {item['length']}×{item['width']}×{item['height']} cm"
                for item in safe_packages
            )
            detail = (
                f"Packlink ha rifiutato la richiesta tariffe per {destination_country} "
                f"CAP {destination_candidates[0]} con pacco {package_text}. "
                "Il CAP è stato normalizzato e sono state provate le varianti compatibili. "
                "Verifica che peso/dimensioni siano quelli reali: se il collo supera i limiti "
                "dei servizi disponibili, Packlink può non restituire una tariffa."
            )
            raise PacklinkAPIError(
                detail, status=last_error.status,
                payload={"packlink": last_error.payload, "attempts": attempts, "packages": safe_packages},
            ) from last_error
        return []

    def service_details(self, service_id: str | int) -> Mapping[str, Any]:
        payload = self.request("GET", f"services/available/{service_id}/details")
        return dict(payload) if isinstance(payload, Mapping) else {}

    @staticmethod
    def _location_rows(payload: Any) -> list[dict[str, Any]]:
        # Packlink has exposed the location collections both as a bare array and
        # under different wrapper names across API/module revisions.
        return [
            dict(item)
            for item in _payload_list(
                payload,
                "locations", "postal_zones", "postalzones",
                "postal_codes", "postalcodes", "postalCodes",
                "results", "items", "data",
            )
            if isinstance(item, Mapping)
        ]

    def postal_zones(self, country_code: Any, *, origin: bool = False) -> list[dict[str, Any]]:
        """Return Packlink postal-zone records for one ISO2 country.

        Packlink's official ecommerce core resolves the country selector through
        the locations/postalzones endpoints before building a draft.  The id
        returned by this endpoint is different from the ISO2 country code and is
        required by Packlink PRO's draft editor in ``additional_data``.
        """
        country = normalize_country_code(country_code)
        if not country:
            return []
        # Use the endpoint documented by Packlink for the side we are resolving.
        # In particular, never fall back from destination -> origin: an origin
        # postal-zone id can belong to the same ISO country but is not the id
        # expected by the recipient selectors in the Packlink PRO draft editor.
        endpoints = (
            ["locations/postalzones/origins"]
            if origin else
            ["locations/postalzones/destinations"]
        )
        last_error: Exception | None = None
        for endpoint in endpoints:
            try:
                payload = self.request(
                    "GET", endpoint,
                    params={
                        "platform": DEFAULT_PLATFORM_CODE,
                        "platform_country": country,
                        "language": "it",
                    },
                )
            except Exception as exc:
                last_error = exc
                continue
            rows_out: list[dict[str, Any]] = []
            for item in self._location_rows(payload):
                iso = normalize_country_code(_first(item, "isoCode", "iso_code", "country", "country_code"))
                if iso and iso != country:
                    continue
                rows_out.append({
                    "id": _first(item, "id", "postal_zone_id", "postalZoneId"),
                    "name": clean_text(_first(item, "name", "label", "text")),
                    "iso_code": iso or country,
                    "has_postal_codes": bool(_first(item, "hasPostalCodes", "has_postal_codes")),
                    "raw": dict(item),
                })
            rows_out = [item for item in rows_out if item.get("id") not in (None, "")]
            if rows_out:
                return rows_out
        if last_error is not None:
            raise last_error
        return []

    def search_locations(
        self,
        *,
        country_code: Any,
        postal_zone_id: Any,
        query: Any,
    ) -> list[dict[str, Any]]:
        """Resolve Packlink's internal city/postcode id.

        This mirrors the official ecommerce-core ``searchLocations`` request:
        GET locations/postalcodes?platform=...&platform_country=...&postalzone=...&q=...
        """
        country = normalize_country_code(country_code)
        zone_id = clean_text(postal_zone_id)
        query_text = clean_text(query)
        if not country or not zone_id or not query_text:
            return []
        payload = self.request(
            "GET", "locations/postalcodes",
            params={
                "platform": DEFAULT_PLATFORM_CODE,
                "platform_country": country,
                "postalzone": zone_id,
                "q": query_text,
            },
        )
        output: list[dict[str, Any]] = []
        for item in self._location_rows(payload):
            loc_id = _first(item, "id", "zip_code_id", "zipCodeId")
            zipcode = clean_text(_first(item, "zipcode", "zipCode", "zip_code", "postal_code", "postalCode", "zip"))
            city = clean_text(_first(item, "city", "city.name", "locality", "name"))
            state = clean_text(_first(item, "state", "state.name", "province", "province.name"))
            if loc_id in (None, ""):
                continue
            output.append({
                "id": loc_id,
                "zipcode": zipcode,
                "city": city,
                "state": state,
                "text": clean_text(_first(item, "text", "label")),
                "raw": dict(item),
            })
        return output

    def _resolve_packlink_address_location(
        self, address: Mapping[str, Any], *, origin: bool
    ) -> dict[str, Any]:
        country = normalize_country_code(address.get("country"))
        postcode = normalize_packlink_postal_code(country, address.get("zip_code"))
        city = clean_text(address.get("city"))
        if not country or not postcode:
            side = "mittente" if origin else "destinatario"
            raise ValueError(f"Paese o Città/codice postale del {side} mancanti.")

        zones = self.postal_zones(country, origin=origin)
        if not zones:
            side = "mittente" if origin else "destinatario"
            raise PacklinkAPIError(
                f"Packlink non ha restituito la zona del Paese {country} per il {side}. "
                "La bozza non è stata creata per evitare una spedizione INCOMPLETA."
            )
        # The endpoint is already filtered by ISO2. Prefer an exact ISO match.
        zone = next((item for item in zones if item.get("iso_code") == country), zones[0])

        candidates: list[dict[str, Any]] = []
        errors: list[Exception] = []
        # Query the exact postcode first, including Packlink-compatible visual
        # variants (e.g. SK 04011 / 040 11), then use the city only as a fallback.
        # The postcode remains authoritative when choosing the result.
        queries = packlink_postal_code_candidates(country, postcode)
        if city and city not in queries:
            queries.append(city)
        seen_location_ids: set[str] = set()
        for query in queries:
            try:
                rows = self.search_locations(
                    country_code=country, postal_zone_id=zone.get("id"), query=query
                )
                for row in rows:
                    loc_key = clean_text(row.get("id")) or json.dumps(row, sort_keys=True, default=str)
                    if loc_key in seen_location_ids:
                        continue
                    seen_location_ids.add(loc_key)
                    candidates.append(row)
                # A postcode query that already returned an exact match contains
                # enough data (including city) to choose the correct location;
                # avoid an unnecessary extra API request for the city.
                if any(
                    normalize_packlink_postal_code(country, row.get("zipcode")) == postcode
                    for row in rows
                ):
                    break
            except Exception as exc:
                errors.append(exc)
        if not candidates:
            side = "mittente" if origin else "destinatario"
            detail = f"{country} {postcode}" + (f" {city}" if city else "")
            if errors and isinstance(errors[-1], PacklinkAPIError):
                raise PacklinkAPIError(
                    f"Packlink non ha riconosciuto Città o codice postale del {side}: {detail}. "
                    "La bozza non è stata creata per evitare lo stato INCOMPLETO.",
                    status=errors[-1].status, payload=errors[-1].payload,
                ) from errors[-1]
            raise PacklinkAPIError(
                f"Packlink non ha riconosciuto Città o codice postale del {side}: {detail}. "
                "La bozza non è stata creata per evitare lo stato INCOMPLETO."
            )

        def score(item: Mapping[str, Any]) -> tuple[int, int, int]:
            item_zip = normalize_packlink_postal_code(country, item.get("zipcode"))
            item_city = clean_text(item.get("city")).casefold()
            return (
                1 if item_zip == postcode else 0,
                1 if city and item_city == city.casefold() else 0,
                1 if item_zip else 0,
            )

        location = max(candidates, key=score)
        # Never accept a different postcode merely because Packlink returned a
        # fuzzy city result.  This is safer than creating a remotely incomplete
        # or incorrectly routed draft.
        resolved_zip = normalize_packlink_postal_code(country, location.get("zipcode"))
        if resolved_zip and resolved_zip != postcode:
            exact = [
                item for item in candidates
                if normalize_packlink_postal_code(country, item.get("zipcode")) == postcode
            ]
            if exact:
                location = max(exact, key=score)
                resolved_zip = postcode
        if resolved_zip and resolved_zip != postcode:
            side = "mittente" if origin else "destinatario"
            raise PacklinkAPIError(
                f"Packlink ha proposto un codice postale diverso per il {side} "
                f"({resolved_zip} invece di {postcode}). La bozza non è stata creata."
            )
        return {"zone": zone, "location": location, "country": country, "postcode": postcode}

    def _attach_destination_selector_ids(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Populate Packlink PRO's recipient country/locality selector metadata.

        ``to.country``, ``to.zip_code`` and ``to.city`` are the canonical address
        fields.  Packlink's draft DTO also exposes the internal destination
        ``postal_zone_id_to`` and ``zip_code_id_to`` values used by the country
        and city/postcode selectors in the PRO editor.  A custom direct API draft
        is not guaranteed to have those ids enriched server-side, so resolve them
        explicitly from Packlink's official location endpoints before POSTing.

        The sender is intentionally *not* resolved here: it is already identified
        by the real ``selectedWarehouseId``.
        """
        prepared = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=str))
        destination = prepared.get("to")
        if not isinstance(destination, Mapping):
            return prepared

        # A draft with an address must never be posted with unresolved recipient
        # selectors: that is exactly the state Packlink displays as INCOMPLETO.
        resolved = self._resolve_packlink_address_location(destination, origin=False)
        zone = resolved["zone"]
        location = resolved["location"]

        additional = dict(prepared.get("additional_data") or {})
        additional["postal_zone_id_to"] = zone.get("id")
        additional["zip_code_id_to"] = location.get("id")
        additional["postal_zone_name_to"] = clean_text(zone.get("name")) or resolved["country"]
        # Warehouse selection is the canonical sender selector; do not manufacture
        # origin locality ids on top of it.
        additional["postal_zone_id_from"] = None
        additional["zip_code_id_from"] = None
        prepared["additional_data"] = additional

        dest = dict(destination)
        dest["country"] = resolved["country"]
        dest["zip_code"] = resolved["postcode"]
        canonical_city = clean_text(location.get("city"))
        if canonical_city:
            dest["city"] = canonical_city
        prepared["to"] = dest
        return prepared

    def register_integration(self, payload: Mapping[str, Any]) -> str:
        """Register Marketplace Hub as a Packlink store integration.

        Packlink's current official e-commerce core (2026) registers the store
        with ``POST /v1/integrations`` before authenticated calls such as
        ``POST /v1/shipments`` and then includes the returned ``integration_id``
        in ``additional_data``.
        """
        response = self.request("POST", "integrations", json_body=dict(payload))
        integration_id = clean_text(_first(response, "integration_id", "id")) if isinstance(response, Mapping) else ""
        if not integration_id:
            raise PacklinkAPIError(
                "Packlink non ha restituito integration_id durante la registrazione del negozio.",
                payload=response,
            )
        return integration_id

    def prepare_draft_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize the canonical Packlink draft before location resolution.

        Address text is kept in the official ``from``/``to`` DTO fields.  The
        destination selector ids are deliberately resolved in ``create_draft``
        immediately before the POST, so stale ids can never survive a regenerated
        order.
        """
        prepared = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=str))
        for side in ("from", "to"):
            address = prepared.get(side) if isinstance(prepared.get(side), Mapping) else None
            if address is not None:
                address["country"] = normalize_country_code(address.get("country"))
                if address.get("zip_code") and address.get("country"):
                    address["zip_code"] = normalize_packlink_postal_code(address["country"], address["zip_code"])
        if isinstance(prepared.get("additional_data"), Mapping):
            additional = dict(prepared.get("additional_data") or {})
            for key in (
                "postal_zone_id_from", "postal_zone_id_to",
                "zip_code_id_from", "zip_code_id_to", "postal_zone_name_to",
            ):
                # The official AdditionalData DTO may carry these keys as null,
                # but the OrderService does not resolve/fill them for imported orders.
                additional[key] = None
            prepared["additional_data"] = additional
        return prepared

    def create_draft(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create a complete Packlink shipment draft, intended for Ready for payment.

        Packlink's public e-commerce core creates drafts with ``POST /v1/shipments``.
        After creation we also retrieve the new shipment when possible, so the
        UI can display Packlink's own remote status instead of assuming it.
        """
        # First normalize the canonical address DTO, then resolve Packlink's
        # *destination* selector ids using its own location endpoints.  This keeps
        # the recipient Paese / Città o codice postale fields complete in the PRO
        # draft editor while the sender remains tied to selectedWarehouseId.
        submitted_payload = self.prepare_draft_payload(payload)
        if isinstance(submitted_payload.get("to"), Mapping):
            submitted_payload = self._attach_destination_selector_ids(submitted_payload)
        # v235: direct Packlink PRO API mode.  Do NOT force POST /integrations
        # before creating a shipment.  The user's API key already authenticated
        # this flow in releases <=232; module registration is specific to the
        # official Packlink shop-module lifecycle and may reject custom types.
        response = self.request("POST", "shipments", json_body=submitted_payload)
        if not isinstance(response, Mapping):
            response = {}
        reference = clean_text(_first(response, "reference", "shipment_reference", "id"))
        if not reference:
            raise PacklinkAPIError(
                "Packlink non ha restituito il riferimento della spedizione.",
                payload=response,
            )
        remote: dict[str, Any] = {}
        remote_error = ""
        try:
            remote = dict(self.shipment(reference))
        except Exception as exc:
            # Creation already succeeded; an immediately-following GET can be
            # eventually consistent and must not turn a successful POST into an
            # apparent failure.
            remote_error = clean_text(exc)
        status = clean_text(_first(remote, "status", "shipment_status", "state"))
        return {
            "reference": reference,
            "response": dict(response),
            "shipment": remote,
            "remote_status": status,
            "remote_check_error": remote_error,
            "submitted_payload": submitted_payload,
        }


def ensure_schema() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS seller_integrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                credentials_encrypted TEXT NOT NULL DEFAULT '',
                settings_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1,
                connection_status TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(seller_id,provider)
            );
            CREATE INDEX IF NOT EXISTS idx_seller_integrations_scope
            ON seller_integrations(seller_id,provider,active);

            CREATE TABLE IF NOT EXISTS packlink_sender_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                label TEXT NOT NULL DEFAULT '',
                contact_name TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                street1 TEXT NOT NULL DEFAULT '',
                street2 TEXT NOT NULL DEFAULT '',
                zip_code TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_packlink_sender_addresses_scope
            ON packlink_sender_addresses(seller_id,active,is_default,id);

            CREATE TABLE IF NOT EXISTS packlink_shipments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                reference TEXT NOT NULL,
                order_reference TEXT NOT NULL DEFAULT '',
                customer_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                carrier TEXT NOT NULL DEFAULT '',
                service TEXT NOT NULL DEFAULT '',
                tracking TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                shipment_created_at TEXT NOT NULL DEFAULT '',
                price REAL,
                currency TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(seller_id,reference)
            );
            CREATE INDEX IF NOT EXISTS idx_packlink_shipments_order
            ON packlink_shipments(seller_id,order_reference);
            CREATE INDEX IF NOT EXISTS idx_packlink_shipments_created
            ON packlink_shipments(seller_id,shipment_created_at);

            CREATE TABLE IF NOT EXISTS packlink_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                shipment_reference TEXT NOT NULL,
                marketplace_account_id INTEGER REFERENCES marketplace_accounts(id) ON DELETE SET NULL,
                marketplace TEXT NOT NULL DEFAULT '',
                order_id TEXT NOT NULL DEFAULT '',
                customer_name_order TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                match_status TEXT NOT NULL DEFAULT '',
                match_score REAL NOT NULL DEFAULT 0,
                match_reason TEXT NOT NULL DEFAULT '',
                confirmed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                UNIQUE(seller_id,shipment_reference)
            );
            CREATE INDEX IF NOT EXISTS idx_packlink_matches_order
            ON packlink_matches(seller_id,marketplace_account_id,marketplace,order_id);

            CREATE TABLE IF NOT EXISTS packlink_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                fetched INTEGER NOT NULL DEFAULT 0,
                inserted INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS packlink_order_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                order_id TEXT NOT NULL,
                order_key TEXT NOT NULL,
                shipment_reference TEXT NOT NULL,
                service_id TEXT NOT NULL DEFAULT '',
                carrier TEXT NOT NULL DEFAULT '',
                service TEXT NOT NULL DEFAULT '',
                quoted_price REAL,
                currency TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                request_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(seller_id,marketplace_account_id,marketplace,order_id)
            );
            CREATE INDEX IF NOT EXISTS idx_packlink_order_drafts_scope
            ON packlink_order_drafts(seller_id,marketplace_account_id,marketplace,created_at);

            CREATE TABLE IF NOT EXISTS packlink_order_draft_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                order_id TEXT NOT NULL,
                order_key TEXT NOT NULL DEFAULT '',
                shipment_reference TEXT NOT NULL,
                service_id TEXT NOT NULL DEFAULT '',
                carrier TEXT NOT NULL DEFAULT '',
                service TEXT NOT NULL DEFAULT '',
                quoted_price REAL,
                currency TEXT NOT NULL DEFAULT '',
                forced INTEGER NOT NULL DEFAULT 0,
                request_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_packlink_order_draft_history_scope
            ON packlink_order_draft_history(
                seller_id,marketplace_account_id,marketplace,order_id,created_at
            );

            CREATE TABLE IF NOT EXISTS packlink_package_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                package_signature TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                weight REAL NOT NULL,
                length REAL NOT NULL,
                width REAL NOT NULL,
                height REAL NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(seller_id,package_signature)
            );
            CREATE INDEX IF NOT EXISTS idx_packlink_package_profiles_scope
            ON packlink_package_profiles(seller_id,active,last_used_at,id);

            CREATE TABLE IF NOT EXISTS packlink_product_package_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                product_signature TEXT NOT NULL,
                package_profile_id INTEGER NOT NULL
                    REFERENCES packlink_package_profiles(id) ON DELETE CASCADE,
                last_order_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(seller_id,product_signature)
            );
            CREATE INDEX IF NOT EXISTS idx_packlink_product_package_memory_scope
            ON packlink_product_package_memory(seller_id,product_signature,updated_at);
            """
        )



def _sender_address_values(address: Mapping[str, Any]) -> dict[str, str]:
    """Normalize one locally registered sender address."""
    return {
        "label": clean_text(address.get("label")),
        "contact_name": clean_text(address.get("contact_name") or address.get("name")),
        "company": clean_text(address.get("company")),
        "street1": clean_text(address.get("street1") or address.get("address")),
        "street2": clean_text(address.get("street2")),
        "zip_code": clean_text(address.get("zip_code") or address.get("postal_code")),
        "city": clean_text(address.get("city")),
        "country": clean_text(address.get("country") or address.get("country_code")).upper(),
        "phone": clean_text(address.get("phone")),
        "email": clean_text(address.get("email")),
    }


def validate_sender_address(address: Mapping[str, Any]) -> dict[str, str]:
    """Validate a reusable Packlink sender address before storing it."""
    normalized = _sender_address_values(address)
    required = (
        ("nome indirizzo", normalized["label"]),
        ("nome contatto", normalized["contact_name"] or normalized["company"]),
        ("indirizzo", normalized["street1"]),
        ("CAP", normalized["zip_code"]),
        ("città", normalized["city"]),
        ("Paese ISO 2", normalized["country"]),
        ("telefono", normalized["phone"]),
        ("email", normalized["email"]),
    )
    missing = [label for label, value in required if not clean_text(value)]
    if len(normalized["country"]) != 2:
        missing.append("Paese ISO 2")
    if missing:
        raise ValueError(
            "Completa i dati del mittente: " + ", ".join(dict.fromkeys(missing)) + "."
        )
    return normalized


def sender_addresses(seller_id: int, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Return reusable sender addresses stored for one Seller."""
    ensure_schema()
    sql = "SELECT * FROM packlink_sender_addresses WHERE seller_id=?"
    params: tuple[Any, ...] = (int(seller_id),)
    if not include_inactive:
        sql += " AND active=1"
    sql += " ORDER BY is_default DESC,LOWER(label),id"
    return rows(sql, params)


def register_sender_address(
    seller_id: int,
    address: Mapping[str, Any],
    *,
    make_default: bool = False,
) -> int:
    """Persist a new sender address and optionally make it the Seller default."""
    ensure_schema()
    normalized = validate_sender_address(address)
    existing = sender_addresses(seller_id)
    should_default = bool(make_default or not existing)
    timestamp = now_iso()
    address_id = execute(
        """INSERT INTO packlink_sender_addresses(
        seller_id,label,contact_name,company,street1,street2,zip_code,city,country,
        phone,email,is_default,active,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(seller_id), normalized["label"], normalized["contact_name"],
            normalized["company"], normalized["street1"], normalized["street2"],
            normalized["zip_code"], normalized["city"], normalized["country"],
            normalized["phone"], normalized["email"], int(should_default), 1,
            timestamp, timestamp,
        ),
    )
    if should_default and address_id:
        set_default_sender_address(seller_id, int(address_id))
    return int(address_id or 0)


def update_sender_address(
    seller_id: int,
    address_id: int,
    address: Mapping[str, Any],
) -> None:
    """Update one sender address without changing its default status."""
    ensure_schema()
    normalized = validate_sender_address(address)
    execute(
        """UPDATE packlink_sender_addresses SET
        label=?,contact_name=?,company=?,street1=?,street2=?,zip_code=?,city=?,country=?,
        phone=?,email=?,updated_at=? WHERE id=? AND seller_id=?""",
        (
            normalized["label"], normalized["contact_name"], normalized["company"],
            normalized["street1"], normalized["street2"], normalized["zip_code"],
            normalized["city"], normalized["country"], normalized["phone"],
            normalized["email"], now_iso(), int(address_id), int(seller_id),
        ),
    )


def set_default_sender_address(seller_id: int, address_id: int) -> None:
    """Make one active sender address the default for the Seller."""
    ensure_schema()
    available = {
        int(item["id"]) for item in sender_addresses(seller_id)
        if item.get("id") not in (None, "")
    }
    if int(address_id) not in available:
        raise ValueError("Indirizzo mittente non disponibile per questo Seller.")
    with connect() as con:
        con.execute(
            "UPDATE packlink_sender_addresses SET is_default=0,updated_at=? WHERE seller_id=?",
            (now_iso(), int(seller_id)),
        )
        con.execute(
            "UPDATE packlink_sender_addresses SET is_default=1,updated_at=? WHERE id=? AND seller_id=? AND active=1",
            (now_iso(), int(address_id), int(seller_id)),
        )


def delete_sender_address(seller_id: int, address_id: int) -> None:
    """Deactivate one sender address, preserving history and draft references."""
    ensure_schema()
    current = sender_addresses(seller_id)
    target = next((item for item in current if int(item.get("id") or 0) == int(address_id)), None)
    if not target:
        return
    execute(
        "UPDATE packlink_sender_addresses SET active=0,is_default=0,updated_at=? WHERE id=? AND seller_id=?",
        (now_iso(), int(address_id), int(seller_id)),
    )
    remaining = sender_addresses(seller_id)
    if remaining and bool(target.get("is_default")):
        set_default_sender_address(seller_id, int(remaining[0]["id"]))


def sender_address_for_packlink(address: Mapping[str, Any]) -> dict[str, Any]:
    """Map a locally stored sender row to Packlink's address contract."""
    normalized = _sender_address_values(address)
    return {
        "id": "",
        "local_sender_id": int(address.get("id") or 0),
        "name": normalized["label"] or normalized["contact_name"],
        "contact_name": normalized["contact_name"],
        "company": normalized["company"],
        "street1": normalized["street1"],
        "street2": normalized["street2"],
        "zip_code": normalized["zip_code"],
        "city": normalized["city"],
        "country": normalized["country"],
        "phone": normalized["phone"],
        "email": normalized["email"],
    }

def integration_for_seller(seller_id: int, *, include_inactive: bool = True) -> dict[str, Any] | None:
    ensure_schema()
    sql = "SELECT * FROM seller_integrations WHERE seller_id=? AND provider=?"
    params: tuple[Any, ...] = (int(seller_id), PACKLINK_PROVIDER)
    if not include_inactive:
        sql += " AND active=1"
    sql += " LIMIT 1"
    found = rows(sql, params)
    return found[0] if found else None


def integration_credentials(integration: Mapping[str, Any]) -> dict[str, Any]:
    return decrypt_dict(clean_text(integration.get("credentials_encrypted")))


def integration_settings(integration: Mapping[str, Any] | None) -> dict[str, Any]:
    if not integration:
        return {}
    try:
        value = json.loads(clean_text(integration.get("settings_json")) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        value = {}
    return dict(value) if isinstance(value, Mapping) else {}


def update_integration_settings(seller_id: int, changes: Mapping[str, Any]) -> None:
    """Merge non-secret Packlink preferences into the Seller integration."""
    current = integration_for_seller(seller_id, include_inactive=True)
    if not current:
        raise ValueError("Integrazione Packlink PRO non configurata per il Seller.")
    settings = integration_settings(current)
    settings.update(dict(changes or {}))
    execute(
        "UPDATE seller_integrations SET settings_json=?,updated_at=? WHERE seller_id=? AND provider=?",
        (
            json.dumps(settings, ensure_ascii=False, default=str), now_iso(),
            int(seller_id), PACKLINK_PROVIDER,
        ),
    )


def _seller_display_name(seller_id: int) -> str:
    try:
        result = rows("SELECT name FROM sellers WHERE id=? LIMIT 1", (int(seller_id),))
    except Exception:
        result = []
    name = clean_text(result[0].get("name")) if result else ""
    return name or f"Seller {int(seller_id)}"


def _persist_packlink_registration_secret(seller_id: int, secret: str) -> None:
    current = integration_for_seller(seller_id, include_inactive=True)
    if not current:
        raise ValueError("Integrazione Packlink PRO non configurata per il Seller.")
    credentials = integration_credentials(current)
    credentials = dict(credentials)
    credentials["integration_webhook_secret"] = clean_text(secret)
    execute(
        "UPDATE seller_integrations SET credentials_encrypted=?,updated_at=? WHERE seller_id=? AND provider=?",
        (encrypt_dict(credentials), now_iso(), int(seller_id), PACKLINK_PROVIDER),
    )


def ensure_packlink_integration_registered(seller_id: int, client: "PacklinkClient") -> str:
    """Return a server-issued Packlink integration id, registering once if needed.

    Since Packlink core 2026, store integrations are registered with
    ``POST /v1/integrations`` and shipment calls are made only after an
    ``integration_id`` exists. Marketplace Hub is a local/polling application,
    so the webhook URL uses the RFC-reserved ``.invalid`` domain unless the
    Seller has configured a public callback URL. No third party can own that
    fallback domain.
    """
    current = integration_for_seller(seller_id, include_inactive=True)
    if not current:
        raise ValueError("Integrazione Packlink PRO non configurata per il Seller.")
    settings = integration_settings(current)
    draft_api = dict(settings.get("draft_api") or {}) if isinstance(settings.get("draft_api"), Mapping) else {}
    existing = clean_text(draft_api.get("integration_id") or settings.get("integration_id"))
    if existing:
        return existing

    registration = dict(settings.get("integration_registration") or {}) if isinstance(settings.get("integration_registration"), Mapping) else {}
    guid = clean_text(registration.get("guid") or settings.get("integration_guid")) or str(uuid.uuid4())
    credentials = integration_credentials(current)
    webhook_secret = clean_text(credentials.get("integration_webhook_secret")) or secrets.token_urlsafe(32)
    callback_url = clean_text(
        registration.get("status_update_url")
        or settings.get("integration_status_update_url")
    ) or "https://marketplace-hub.invalid/packlink/status"
    payload = {
        "integration_type": "marketplace_hub_module",
        "integration": {
            "guid": guid,
            "name": f"Marketplace Hub - {_seller_display_name(seller_id)}"[:120],
        },
        "webhooks": {
            "http_header_name": "X-Packlink-Webhook-Secret",
            "http_header_value": webhook_secret,
            "status_update_url": callback_url,
        },
    }
    try:
        integration_id = client.register_integration(payload)
    except PacklinkAPIError as exc:
        detail = clean_text(exc)
        raise PacklinkAPIError(
            "Packlink non ha accettato la registrazione del negozio richiesta dal core ufficiale 2026. "
            f"Nessuna bozza è stata creata. Dettaglio: {detail}",
            status=getattr(exc, "status", None),
            payload=getattr(exc, "payload", None),
        ) from exc

    registration.update({
        "guid": guid,
        "integration_id": integration_id,
        "integration_type": "marketplace_hub_module",
        "status_update_url": callback_url,
        "registered_at": now_iso(),
    })
    draft_api["integration_id"] = integration_id
    update_integration_settings(seller_id, {
        "integration_id": integration_id,
        "integration_guid": guid,
        "integration_registration": registration,
        "draft_api": draft_api,
    })
    _persist_packlink_registration_secret(seller_id, webhook_secret)
    return integration_id


def packlink_client_profile(integration: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract only explicit Packlink draft identifiers.

    ``GET /clients`` can expose generic identifiers whose meaning is not
    interchangeable with Draft ``user_id`` and ``client_id``. Packlink's own
    e-commerce core leaves both Draft fields null unless they are explicitly
    populated, so we must not reuse a generic ``id`` for both values.
    """
    settings = integration_settings(integration)
    client = settings.get("client") if isinstance(settings.get("client"), Mapping) else {}
    api = settings.get("draft_api") if isinstance(settings.get("draft_api"), Mapping) else {}
    client_id = clean_text(
        api.get("client_id") or _first(client, "client_id", "clientId", "client.id")
    ) or None
    user_id = clean_text(
        api.get("user_id") or _first(client, "user_id", "userId", "user.id", "owner.id")
    ) or None
    # Marketplace Hub is a direct Packlink PRO API client, not one of Packlink's
    # named official shop modules. Preserve the direct-PRO source that was used
    # successfully before v233 instead of pretending to be a registered module.
    source = clean_text(api.get("source")) or DEFAULT_SERVICE_SOURCE
    if source.lower() == "module_marketplace_hub":
        source = DEFAULT_SERVICE_SOURCE
    return {
        "client_id": client_id,
        "user_id": user_id,
        "platform": clean_text(api.get("platform")) or DEFAULT_PLATFORM_CODE,
        "source": source,
    }


def order_selection_key(account_id: int, marketplace: str, order_id: str) -> str:
    return f"{int(account_id)}|{clean_text(marketplace).lower()}|{clean_text(order_id)}"


def _first_line_value(lines: Sequence[Mapping[str, Any]], *keys: str) -> str:
    """Return the first non-empty cached value across every line of an order."""
    for line in lines or []:
        if not isinstance(line, Mapping):
            continue
        value = _first(line, *keys)
        text = clean_text(value)
        if text:
            return text
    return ""


def _raw_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _address_values(source: Mapping[str, Any]) -> dict[str, str]:
    """Read common Kaufland/Mirakl/Packlink address aliases from one mapping."""
    first_name = clean_text(_first(source, "first_name", "firstname", "firstName"))
    last_name = clean_text(_first(source, "last_name", "lastname", "lastName", "surname"))
    full_name = clean_text(_first(source, "customer_name", "full_name", "fullName", "recipient_name", "recipientName", "name"))
    if not full_name:
        full_name = clean_text(" ".join(part for part in (first_name, last_name) if part))
    company = clean_text(_first(source, "company", "company_name", "companyName"))

    street = clean_text(_first(
        source,
        "address", "street1", "street_1", "street", "address1", "address_line_1", "addressLine1",
    ))
    house_number = clean_text(_first(source, "house_number", "street_number", "houseNumber", "streetNumber"))
    if street and house_number and house_number.casefold() not in street.casefold():
        street = clean_text(f"{street} {house_number}")
    street2 = clean_text(_first(source, "street2", "street_2", "address2", "address_line_2", "addressLine2", "additional_field"))
    if street2 and street2.casefold() not in street.casefold():
        street = clean_text(" ".join(part for part in (street, street2) if part))

    country = normalize_country_code(_first(
        source, "country_code", "countryCode", "country", "country_iso_code", "countryIsoCode", "iso_code", "isoCode",
    ))
    return {
        "customer_name": full_name or company,
        "company": company,
        "address": street,
        "postal_code": clean_text(_first(source, "postal_code", "postalCode", "postcode", "zip_code", "zipCode", "zip")),
        "phone": clean_text(_first(source, "phone", "phone_number", "phoneNumber", "telephone", "mobile")),
        "city": clean_text(_first(source, "city", "town", "locality")),
        "country_code": country,
        "customer_email": clean_text(_first(source, "customer_email", "email", "contact_email", "contactEmail")),
    }


_ADDRESS_CONTAINER_KEYS = (
    "shipping_address", "shippingAddress", "shipping_address_lookup",
    "delivery_address", "deliveryAddress",
    "shipping", "delivery", "ship_to", "shipTo", "recipient", "receiver", "destination",
    "billing_address", "billingAddress",
)
_ADDRESS_WRAPPER_KEYS = (
    "order", "unit", "order_unit", "orderUnit", "line",
    "customer", "order_customer", "orderCustomer", "address_data", "addressData", "data",
)


def _iter_address_candidates(value: Any, *, depth: int = 0) -> Iterable[Mapping[str, Any]]:
    """Yield only plausible shipping-address mappings from cached raw marketplace JSON.

    Traversal is deliberately restricted to order/customer/address wrappers so a
    product's country or an unrelated catalog postcode can never become the
    shipment destination by accident.
    """
    if depth > 5 or not isinstance(value, Mapping):
        return
    current = dict(value)
    direct = _address_values(current)
    if any(direct.get(key) for key in ("address", "postal_code", "city", "country_code")):
        yield current
    for key in _ADDRESS_CONTAINER_KEYS:
        child = current.get(key)
        if isinstance(child, Mapping):
            yield dict(child)
            yield from _iter_address_candidates(child, depth=depth + 1)
    for key in _ADDRESS_WRAPPER_KEYS:
        child = current.get(key)
        if isinstance(child, Mapping):
            yield from _iter_address_candidates(child, depth=depth + 1)


def _coalesced_order_address(order: Mapping[str, Any]) -> dict[str, str]:
    """Recover the most complete recipient address available for one order."""
    values = {
        "customer_name": clean_text(order.get("customer_name")),
        "company": clean_text(order.get("company")),
        "address": clean_text(order.get("address")),
        "postal_code": clean_text(order.get("postal_code")),
        "phone": clean_text(order.get("phone")),
        "city": clean_text(order.get("city")),
        "country_code": normalize_country_code(order.get("country_code")),
        "customer_email": clean_text(order.get("customer_email")),
    }
    lines_value = order.get("lines")
    lines = [dict(item) for item in lines_value if isinstance(item, Mapping)] if isinstance(lines_value, Sequence) and not isinstance(lines_value, (str, bytes, bytearray)) else []

    # Cached normalized columns are authoritative and cheapest to recover.
    aliases = {
        "customer_name": ("customer_name", "full_name", "recipient_name"),
        "company": ("company", "company_name"),
        "address": ("address", "street1", "street", "address_line_1"),
        "postal_code": ("postal_code", "postcode", "zip_code", "zip"),
        "phone": ("phone", "phone_number", "mobile"),
        "city": ("city", "town", "locality"),
        "country_code": ("country_code", "country", "country_iso_code"),
        "customer_email": ("customer_email", "email"),
    }
    for field, keys in aliases.items():
        if not values[field]:
            recovered = _first_line_value(lines, *keys)
            values[field] = normalize_country_code(recovered) if field == "country_code" else recovered

    # Older cache rows can have blank normalized columns while raw_json still
    # contains the shipping address. Recover it without requiring a re-download.
    for line in lines:
        raw = _raw_mapping(line.get("raw_json"))
        for candidate in _iter_address_candidates(raw):
            parsed = _address_values(candidate)
            for field in values:
                if not values[field] and parsed.get(field):
                    values[field] = parsed[field]
            if all(values.get(field) for field in ("address", "postal_code", "city", "country_code")):
                break

    values["country_code"] = normalize_country_code(values.get("country_code"))
    if values["postal_code"] and values["country_code"]:
        values["postal_code"] = normalize_packlink_postal_code(values["country_code"], values["postal_code"])
    return values


def group_marketplace_orders(
    records: Sequence[Mapping[str, Any]],
    *,
    account_id: int,
    marketplace: str,
    account_name: str = "",
) -> list[dict[str, Any]]:
    """Aggregate cached marketplace lines to one selectable row per customer order.

    Address/contact fields are coalesced across all lines instead of trusting the
    first row. This matters for Kaufland/Worten caches where an early line may be
    incomplete even though a later line or raw_json contains the full shipment
    address.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in records:
        order_id = clean_text(raw.get("order_id"))
        if order_id:
            groups[order_id].append(dict(raw))
    result: list[dict[str, Any]] = []
    for order_id, lines in groups.items():
        first = lines[0]
        suppliers = sorted({clean_text(item.get("supplier")) for item in lines if clean_text(item.get("supplier"))})
        titles = [clean_text(item.get("product_title")) for item in lines if clean_text(item.get("product_title"))]
        skus = [clean_text(item.get("composite_sku")) for item in lines if clean_text(item.get("composite_sku"))]
        quantities = [max(1, int(_number(item.get("quantity")) or 1)) for item in lines]
        order_key = order_selection_key(account_id, marketplace, order_id)
        address = _coalesced_order_address({"lines": lines})
        result.append({
            "order_key": order_key,
            "marketplace_account_id": int(account_id),
            "marketplace": clean_text(marketplace).lower(),
            "account_name": clean_text(account_name),
            "order_id": order_id,
            "order_created": _first_line_value(lines, "order_created") or clean_text(first.get("order_created")),
            "raw_status": _first_line_value(lines, "raw_status") or clean_text(first.get("raw_status")),
            "status_label": _first_line_value(lines, "status_label", "normalized_status", "raw_status"),
            "supplier": ", ".join(suppliers),
            "supplier_count": len(suppliers),
            "product_title": " · ".join(dict.fromkeys(titles))[:500],
            "product_count": len(lines),
            "quantity": sum(quantities),
            "composite_sku": " · ".join(dict.fromkeys(skus))[:800],
            **address,
            "storefront": _first_line_value(lines, "storefront"),
            "lines": lines,
        })
    result.sort(key=lambda item: (item.get("order_created") or "", item.get("order_id") or ""), reverse=True)
    return result


def packlink_destination_address(
    order: Mapping[str, Any], *, fallback_phone: Any = ""
) -> dict[str, str]:
    """Build the Packlink recipient from the *current* normalized order row.

    v230 deliberately gives priority to the current top-level order fields shown
    in the Packlink page (country_code, postal_code, city, address, customer data).
    Raw marketplace JSON is only a fallback for missing fields. This prevents a
    stale session candidate or a billing/sender country from replacing the actual
    shipping destination immediately before POST /v1/shipments.

    v253 also accepts a sender-phone fallback. Marketplace customers are not
    always required to provide a telephone number, while Packlink requires one
    for a payment-ready shipment. When the recipient phone is genuinely absent,
    Marketplace Hub therefore uses the sender phone instead of blocking the row.
    """
    recovered = _coalesced_order_address(order)

    def current_or_recovered(current_key: str, recovered_key: str | None = None) -> str:
        recovered_key = recovered_key or current_key
        current = clean_text(order.get(current_key))
        return current or clean_text(recovered.get(recovered_key))

    name = current_or_recovered("customer_name")
    parts = name.split(None, 1)
    first_name = parts[0] if parts else name
    surname = parts[1] if len(parts) > 1 else ""

    country = normalize_country_code(
        clean_text(order.get("country_code")) or recovered.get("country_code")
    )
    postcode = clean_text(order.get("postal_code")) or clean_text(recovered.get("postal_code"))
    if country and postcode:
        postcode = normalize_packlink_postal_code(country, postcode)

    return {
        "name": first_name or name,
        "surname": surname,
        "company": current_or_recovered("company"),
        "street1": current_or_recovered("address"),
        "street2": "",
        "zip_code": postcode,
        "city": current_or_recovered("city"),
        "country": country,
        "phone": current_or_recovered("phone") or clean_text(fallback_phone),
        "email": (
            clean_text(order.get("customer_email"))
            or clean_text(order.get("email"))
            or clean_text(recovered.get("customer_email"))
        ),
    }


def _address_from_order(order: Mapping[str, Any]) -> dict[str, str]:
    """Backward-compatible alias for the v230 fresh recipient builder."""
    return packlink_destination_address(order)


def validate_packlink_destination_against_order(
    payload: Mapping[str, Any], order: Mapping[str, Any]
) -> dict[str, str]:
    """Fail before the API call if the payload destination differs from the current order."""
    expected = packlink_destination_address(order)
    actual = payload.get("to") if isinstance(payload.get("to"), Mapping) else {}
    actual_country = normalize_country_code(actual.get("country"))
    actual_zip = clean_text(actual.get("zip_code"))
    if actual_country and actual_zip:
        actual_zip = normalize_packlink_postal_code(actual_country, actual_zip)
    actual_city = clean_text(actual.get("city"))

    mismatches: list[str] = []
    for label, exp, got in (
        ("Paese", expected.get("country"), actual_country),
        ("Città o codice postale", expected.get("zip_code"), actual_zip),
        ("Città", expected.get("city"), actual_city),
    ):
        if clean_text(exp) and clean_text(exp).casefold() != clean_text(got).casefold():
            mismatches.append(f"{label}: atteso {clean_text(exp)}, payload {clean_text(got) or 'vuoto'}")
    if mismatches:
        raise ValueError(
            "Destinazione Packlink non allineata all'ordine corrente: "
            + "; ".join(mismatches)
            + ". La bozza non è stata inviata."
        )
    return expected


def normalize_sender_address(warehouse: Mapping[str, Any]) -> dict[str, str]:
    """Normalize a saved/manual/Packlink sender without losing postcode/city aliases."""
    contact = clean_text(_first(warehouse, "contact_name", "contactName", "name", "full_name", "fullName"))
    first_name = clean_text(_first(warehouse, "first_name", "firstname", "firstName"))
    last_name = clean_text(_first(warehouse, "surname", "last_name", "lastname", "lastName"))
    if not contact:
        contact = clean_text(" ".join(part for part in (first_name, last_name) if part))
    parts = contact.split(None, 1)
    country = normalize_country_code(_first(warehouse, "country", "country_code", "countryCode", "country_iso_code", "countryIsoCode"))
    postcode = clean_text(_first(warehouse, "zip_code", "zipCode", "postal_code", "postalCode", "postcode", "zip"))
    if postcode and country:
        postcode = normalize_packlink_postal_code(country, postcode)
    return {
        "name": parts[0] if parts else contact,
        "surname": last_name or (parts[1] if len(parts) > 1 else ""),
        "company": clean_text(_first(warehouse, "company", "company_name", "companyName")),
        "street1": clean_text(_first(warehouse, "street1", "street", "street_1", "address", "address1", "address_line_1", "addressLine1")),
        "street2": clean_text(_first(warehouse, "street2", "street_2", "address2", "address_line_2", "addressLine2")),
        "zip_code": postcode,
        "city": clean_text(_first(warehouse, "city", "town", "locality")),
        "country": country,
        "phone": clean_text(_first(warehouse, "phone", "phone_number", "phoneNumber", "telephone", "mobile")),
        "email": clean_text(_first(warehouse, "email", "contact_email", "contactEmail")),
    }



def _packlink_catalog_path(
    raw_path: Any,
    *,
    price_list_id: int = 0,
    seller_id: int = 0,
    source_kind: str = "",
) -> Path | None:
    """Resolve a list/saved-view path after Marketplace Hub folder upgrades."""
    text = clean_text(raw_path)
    if not text:
        return None
    original = Path(text)
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

    if price_list_id:
        list_folder = DATA_DIR / "price_lists" / str(int(price_list_id))
        if original.name:
            candidates.append(list_folder / original.name)
        if list_folder.exists():
            candidates.extend(sorted(
                (entry for entry in list_folder.iterdir() if entry.is_file()),
                key=lambda entry: entry.stat().st_mtime,
                reverse=True,
            ))
    if "vista" in clean_text(source_kind).lower() and seller_id:
        view_folder = DATA_DIR / "saved_views" / str(int(seller_id))
        if original.name:
            candidates.append(view_folder / original.name)
        if view_folder.exists():
            candidates.extend(sorted(
                (entry for entry in view_folder.iterdir() if entry.is_file()),
                key=lambda entry: entry.stat().st_mtime,
                reverse=True,
            ))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def packlink_weight_catalog_signature(seller_id: int) -> str:
    """Return a cheap signature that changes when an accessible catalogue changes."""
    lists = accessible_lists(int(seller_id))
    list_ids = [int(item.get("id") or 0) for item in lists if int(item.get("id") or 0)]
    values: list[tuple[Any, ...]] = []
    for item in lists:
        path = _packlink_catalog_path(
            item.get("local_path"),
            price_list_id=int(item.get("id") or 0),
            seller_id=int(seller_id),
            source_kind="listino attivo",
        )
        values.append((
            "list",
            int(item.get("id") or 0),
            str(path or item.get("local_path") or ""),
            path.stat().st_mtime_ns if path and path.exists() else 0,
            clean_text(item.get("last_download_at")),
        ))
    if list_ids:
        placeholders = ",".join("?" for _ in list_ids)
        for item in rows(
            f"""SELECT id,price_list_id,snapshot_path,updated_at
            FROM saved_views
            WHERE seller_id=? AND price_list_id IN ({placeholders})
            ORDER BY updated_at DESC,id DESC""",
            (int(seller_id), *list_ids),
        ):
            path = _packlink_catalog_path(
                item.get("snapshot_path"),
                price_list_id=int(item.get("price_list_id") or 0),
                seller_id=int(seller_id),
                source_kind="vista salvata",
            )
            values.append((
                "view",
                int(item.get("id") or 0),
                int(item.get("price_list_id") or 0),
                str(path or item.get("snapshot_path") or ""),
                path.stat().st_mtime_ns if path and path.exists() else 0,
                clean_text(item.get("updated_at")),
            ))
    return str(hash(tuple(values)))


def load_packlink_weight_catalog(seller_id: int) -> dict[str, Any]:
    """Load product weights from every active list accessible to the Seller.

    Marketplace Hub normalizes catalogue weights into ``weight_kg``. The current
    supplier file is preferred; the latest saved view of the same list is kept as
    a fallback. Only rows with a positive weight are indexed, keeping this lookup
    much lighter than loading full commercial data into Packlink.
    """
    list_rows = [dict(item) for item in accessible_lists(int(seller_id))]
    if not list_rows:
        return {
            "ean_index": {}, "sku_index": {}, "source_count": 0,
            "weighted_products": 0, "unavailable": [],
        }
    list_info = {int(item["id"]): item for item in list_rows}
    list_ids = list(list_info)
    placeholders = ",".join("?" for _ in list_ids)
    latest_view_by_list: dict[int, dict[str, Any]] = {}
    for item in rows(
        f"""SELECT sv.id,sv.price_list_id,sv.snapshot_path,sv.updated_at,sv.name,
        pl.name price_list_name,s.name supplier_name
        FROM saved_views sv
        JOIN price_lists pl ON pl.id=sv.price_list_id
        JOIN suppliers s ON s.id=pl.supplier_id
        WHERE sv.seller_id=? AND sv.price_list_id IN ({placeholders})
        ORDER BY sv.updated_at DESC,sv.id DESC""",
        (int(seller_id), *list_ids),
    ):
        list_id = int(item.get("price_list_id") or 0)
        latest_view_by_list.setdefault(list_id, dict(item))

    source_specs: list[dict[str, Any]] = []
    for list_id, item in list_info.items():
        source_specs.append({
            "price_list_id": list_id,
            "supplier_name": clean_text(item.get("supplier_name")),
            "price_list_name": clean_text(item.get("name")),
            "source_kind": "listino attivo",
            "path": item.get("local_path"),
            "updated_at": clean_text(item.get("last_download_at") or item.get("created_at")),
            "priority": 0,
        })
        saved_view = latest_view_by_list.get(list_id)
        if saved_view:
            source_specs.append({
                "price_list_id": list_id,
                "supplier_name": clean_text(saved_view.get("supplier_name") or item.get("supplier_name")),
                "price_list_name": clean_text(saved_view.get("price_list_name") or item.get("name")),
                "source_kind": "vista salvata",
                "path": saved_view.get("snapshot_path"),
                "updated_at": clean_text(saved_view.get("updated_at")),
                "priority": 1,
            })

    ean_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sku_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unavailable: list[str] = []
    seen_paths: set[str] = set()
    source_count = 0
    weighted_products = 0
    for spec in sorted(source_specs, key=lambda item: (int(item["priority"]), clean_text(item["updated_at"])), reverse=False):
        path = _packlink_catalog_path(
            spec.get("path"),
            price_list_id=int(spec.get("price_list_id") or 0),
            seller_id=int(seller_id),
            source_kind=clean_text(spec.get("source_kind")),
        )
        if path is None:
            unavailable.append(
                f"{spec.get('supplier_name') or 'Fornitore'} · {spec.get('price_list_name') or 'Listino'}"
            )
            continue
        path_key = str(path.resolve())
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        try:
            frame = normalize(read_list(path))
        except Exception:
            unavailable.append(
                f"{spec.get('supplier_name') or 'Fornitore'} · {spec.get('price_list_name') or 'Listino'}"
            )
            continue
        if frame.empty or "weight_kg" not in frame.columns:
            continue
        source_count += 1
        supplier_name = clean_text(spec.get("supplier_name"))
        supplier_key = normalize_supplier(supplier_name)
        for product in frame.to_dict("records"):
            weight = _number(product.get("weight_kg"))
            if weight is None or weight <= 0:
                continue
            ean = clean_identifier(product.get("ean"))
            sku = clean_identifier(product.get("sku"))
            if not ean and not sku:
                continue
            weighted_products += 1
            entry = {
                "weight_kg": round(float(weight), 4),
                "ean": ean,
                "sku": sku,
                "supplier_name": supplier_name,
                "supplier_key": supplier_key,
                "price_list_name": clean_text(spec.get("price_list_name")),
                "source_kind": clean_text(spec.get("source_kind")),
                "updated_at": clean_text(spec.get("updated_at")),
                "priority": int(spec.get("priority") or 0),
            }
            if ean and ean.casefold() not in {"nan", "none"}:
                ean_index[ean].append(entry)
            if sku and sku.casefold() not in {"nan", "none"}:
                sku_index[sku].append(entry)

    return {
        "ean_index": dict(ean_index),
        "sku_index": dict(sku_index),
        "source_count": source_count,
        "weighted_products": weighted_products,
        "unavailable": unavailable,
    }


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, Mapping) else {}
    except Exception:
        return {}


def _gtin_candidate(value: Any) -> str:
    text = clean_identifier(value).replace(" ", "")
    return text if re.fullmatch(r"\d{8}|\d{12,14}", text) else ""


def _line_weight_identifiers(line: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Extract exact EAN/GTIN and supplier-SKU candidates from one order line."""
    parsed = parse_composite_sku(line.get("composite_sku"))
    eans: list[str] = []
    skus: list[str] = []

    def add_ean(value: Any) -> None:
        candidate = _gtin_candidate(value)
        if candidate and candidate not in eans:
            eans.append(candidate)

    def add_sku(value: Any) -> None:
        candidate = clean_identifier(value)
        if candidate and candidate.casefold() not in {"nan", "none"} and candidate not in skus:
            skus.append(candidate)

    add_ean(line.get("ean"))
    add_ean(parsed.product_code)
    add_sku(parsed.product_code)
    add_sku(line.get("sku"))
    add_sku(line.get("product_sku"))

    raw = _json_mapping(line.get("raw_json"))
    queue: list[Any] = [raw]
    while queue:
        current = queue.pop(0)
        if isinstance(current, Mapping):
            for key, value in current.items():
                key_normalized = re.sub(r"[^a-z0-9]+", "_", clean_text(key).casefold()).strip("_")
                if key_normalized in {
                    "ean", "ean13", "ean_13", "gtin", "barcode", "bar_code", "upc",
                    "product_ean", "item_ean",
                }:
                    add_ean(value)
                if key_normalized in {
                    "sku", "seller_sku", "offer_sku", "shop_sku", "product_sku",
                    "id_offer", "product_code", "reference", "ref",
                }:
                    add_sku(value)
                if isinstance(value, (Mapping, list, tuple)):
                    queue.append(value)
        elif isinstance(current, (list, tuple)):
            queue.extend(current)
    return eans, skus


def _weight_supplier_match(entry: Mapping[str, Any], supplier_key: str) -> bool:
    source_key = normalize_supplier(entry.get("supplier_key") or entry.get("supplier_name"))
    if not supplier_key:
        return True
    return bool(source_key and (
        source_key == supplier_key or source_key in supplier_key or supplier_key in source_key
    ))


def resolve_packlink_line_weight(
    catalog: Mapping[str, Any], line: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve one order-line unit weight from the Seller's product catalogues."""
    parsed = parse_composite_sku(line.get("composite_sku"))
    supplier = clean_text(line.get("supplier") or parsed.supplier)
    supplier_key = normalize_supplier(supplier)
    eans, skus = _line_weight_identifiers(line)
    candidates: list[tuple[int, dict[str, Any], str, str]] = []

    for ean in eans:
        for entry in (catalog.get("ean_index") or {}).get(ean, []):
            score = 200
            if _weight_supplier_match(entry, supplier_key):
                score += 80
            score += 10 if int(entry.get("priority") or 0) == 0 else 0
            candidates.append((score, dict(entry), "EAN", ean))
    for sku in skus:
        for entry in (catalog.get("sku_index") or {}).get(sku, []):
            score = 120
            if _weight_supplier_match(entry, supplier_key):
                score += 80
            score += 10 if int(entry.get("priority") or 0) == 0 else 0
            candidates.append((score, dict(entry), "SKU/codice", sku))

    if not candidates:
        return {
            "unit_weight_kg": None,
            "source": "Peso non presente nei listini associati al Seller",
            "matched_by": "",
            "matched_value": "",
            "supplier_name": supplier,
            "price_list_name": "",
        }
    candidates.sort(
        key=lambda item: (item[0], clean_text(item[1].get("updated_at"))),
        reverse=True,
    )
    _, best, matched_by, matched_value = candidates[0]
    return {
        "unit_weight_kg": float(best["weight_kg"]),
        "source": (
            f"{best.get('supplier_name') or supplier or 'Fornitore'} · "
            f"{best.get('price_list_name') or 'Listino'} · {best.get('source_kind') or 'catalogo'} · "
            f"match {matched_by} {matched_value}"
        ),
        "matched_by": matched_by,
        "matched_value": matched_value,
        "supplier_name": best.get("supplier_name") or supplier,
        "price_list_name": best.get("price_list_name") or "",
    }


def resolve_packlink_order_weight(
    catalog: Mapping[str, Any],
    order: Mapping[str, Any],
    *,
    fallback_weight_kg: float,
) -> dict[str, Any]:
    """Calculate the package weight from product-list weights whenever available.

    * All lines resolved: exact sum of ``unit weight × quantity``.
    * Some lines resolved: exact known sum plus the generic package weight once as
      a conservative fallback for the unresolved part.
    * No line resolved: keep the generic Packlink package weight.
    """
    fallback = max(0.01, float(_number(fallback_weight_kg) or 0.01))
    lines = [dict(item) for item in order.get("lines", []) if isinstance(item, Mapping)]
    if not lines:
        return {
            "weight_kg": round(fallback, 2),
            "source": "Peso generico del pacco: ordine senza righe prodotto disponibili",
            "known_lines": 0, "missing_lines": 0, "known_quantity": 0,
            "missing_quantity": 0, "details": [], "uses_fallback": True,
        }

    details: list[dict[str, Any]] = []
    known_total = 0.0
    known_lines = missing_lines = known_quantity = missing_quantity = 0
    for line in lines:
        quantity = max(1, int(_number(line.get("quantity")) or 1))
        resolved = resolve_packlink_line_weight(catalog, line)
        unit_weight = _number(resolved.get("unit_weight_kg"))
        if unit_weight is not None and unit_weight > 0:
            line_total = float(unit_weight) * quantity
            known_total += line_total
            known_lines += 1
            known_quantity += quantity
            details.append({
                "product": clean_text(line.get("product_title")),
                "sku": clean_text(line.get("composite_sku")),
                "quantity": quantity,
                "unit_weight_kg": round(float(unit_weight), 4),
                "line_weight_kg": round(line_total, 4),
                "source": clean_text(resolved.get("source")),
                "resolved": True,
            })
        else:
            missing_lines += 1
            missing_quantity += quantity
            details.append({
                "product": clean_text(line.get("product_title")),
                "sku": clean_text(line.get("composite_sku")),
                "quantity": quantity,
                "unit_weight_kg": None,
                "line_weight_kg": None,
                "source": clean_text(resolved.get("source")),
                "resolved": False,
            })

    if known_lines and not missing_lines:
        weight = max(0.01, known_total)
        source = (
            f"Peso reale da listino: {known_lines}/{len(lines)} righe trovate · "
            f"somma peso × quantità"
        )
        uses_fallback = False
    elif known_lines:
        weight = max(0.01, known_total + fallback)
        source = (
            f"Peso parziale da listino: {known_lines}/{len(lines)} righe trovate · "
            f"+ {fallback:.2f} kg generici per le righe senza peso"
        )
        uses_fallback = True
    else:
        weight = fallback
        source = (
            f"Peso generico {fallback:.2f} kg: nessuna riga dell'ordine contiene "
            "un peso trovabile nei listini associati"
        )
        uses_fallback = True
    return {
        "weight_kg": round(weight, 2),
        "source": source,
        "known_lines": known_lines,
        "missing_lines": missing_lines,
        "known_quantity": known_quantity,
        "missing_quantity": missing_quantity,
        "details": details,
        "uses_fallback": uses_fallback,
    }


def packlink_package_signature(package: Mapping[str, Any]) -> str:
    """Stable signature for one reusable Packlink package configuration."""
    normalized = package_payload(package)
    return "|".join((
        f"{float(normalized['weight']):.2f}",
        str(int(normalized['length'])),
        str(int(normalized['width'])),
        str(int(normalized['height'])),
    ))


def packlink_order_product_signature(order: Mapping[str, Any]) -> str:
    """Identify the product/quantity composition for package-memory suggestions."""
    parts: list[str] = []
    lines = [item for item in order.get("lines", []) if isinstance(item, Mapping)]
    for line in lines:
        parsed = parse_composite_sku(line.get("composite_sku"))
        identifiers = [
            _gtin_candidate(line.get("ean")),
            _gtin_candidate(parsed.product_code),
            clean_identifier(parsed.product_code),
            clean_identifier(line.get("sku")),
            clean_identifier(line.get("product_sku")),
            clean_text(line.get("product_title")).casefold(),
        ]
        identifier = next((value for value in identifiers if clean_text(value)), "prodotto")
        quantity = max(1, int(_number(line.get("quantity")) or 1))
        parts.append(f"{identifier}:{quantity}")
    if not parts:
        parts.append(clean_text(order.get("composite_sku")) or clean_text(order.get("order_id")) or "ordine")
    raw = "|".join(sorted(parts))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def saved_package_profiles(seller_id: int, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Return every remembered Seller package, newest/most-used first."""
    ensure_schema()
    sql = (
        "SELECT * FROM packlink_package_profiles WHERE seller_id=? AND active=1 "
        "ORDER BY last_used_at DESC,use_count DESC,id DESC"
    )
    params: tuple[Any, ...] = (int(seller_id),)
    if limit is not None:
        sql += " LIMIT ?"
        params = (int(seller_id), int(max(1, limit)))
    return rows(sql, params)


def saved_package_profile(seller_id: int, package_id: int) -> dict[str, Any] | None:
    ensure_schema()
    found = rows(
        "SELECT * FROM packlink_package_profiles WHERE seller_id=? AND id=? AND active=1 LIMIT 1",
        (int(seller_id), int(package_id)),
    )
    return dict(found[0]) if found else None


def save_package_profile(
    seller_id: int,
    package: Mapping[str, Any],
    *,
    label: str = "",
    increment_use: bool = False,
) -> dict[str, Any]:
    """Persist one package configuration independently of shipment creation.

    This is used both by the explicit "save package" action and after a
    successful quote/draft.  Package profiles are de-duplicated by their real
    weight and dimensions, so the Seller can reuse every configuration later.
    """
    ensure_schema()
    payload = package_payload(package)
    signature = packlink_package_signature(payload)
    timestamp = now_iso()
    default_label = (
        f"Pacco {int(payload['length'])}×{int(payload['width'])}×{int(payload['height'])} cm · "
        f"{float(payload['weight']):.2f} kg"
    )
    existing = rows(
        "SELECT * FROM packlink_package_profiles WHERE seller_id=? AND package_signature=? LIMIT 1",
        (int(seller_id), signature),
    )
    if existing:
        current = dict(existing[0])
        new_label = clean_text(label) or clean_text(current.get("label")) or default_label
        new_count = int(current.get("use_count") or 0) + (1 if increment_use else 0)
        execute(
            """UPDATE packlink_package_profiles SET label=?,weight=?,length=?,width=?,height=?,
            use_count=?,last_used_at=?,active=1,updated_at=? WHERE id=? AND seller_id=?""",
            (
                new_label, float(payload["weight"]), float(payload["length"]),
                float(payload["width"]), float(payload["height"]), new_count, timestamp,
                timestamp, int(current["id"]), int(seller_id),
            ),
        )
        profile_id = int(current["id"])
    else:
        profile_id = execute(
            """INSERT INTO packlink_package_profiles(
            seller_id,package_signature,label,weight,length,width,height,use_count,last_used_at,active,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(seller_id), signature, clean_text(label) or default_label,
                float(payload["weight"]), float(payload["length"]), float(payload["width"]),
                float(payload["height"]), 1 if increment_use else 0, timestamp, 1, timestamp, timestamp,
            ),
        )
    found = saved_package_profile(seller_id, profile_id)
    if not found:
        found_rows = rows(
            "SELECT * FROM packlink_package_profiles WHERE seller_id=? AND package_signature=? LIMIT 1",
            (int(seller_id), signature),
        )
        found = dict(found_rows[0]) if found_rows else None
    if not found:
        raise RuntimeError("Impossibile memorizzare il pacco Packlink.")
    return found


def remembered_package_for_order(seller_id: int, order: Mapping[str, Any]) -> dict[str, Any] | None:
    ensure_schema()
    signature = packlink_order_product_signature(order)
    found = rows(
        """SELECT p.* FROM packlink_product_package_memory m
        JOIN packlink_package_profiles p ON p.id=m.package_profile_id
        WHERE m.seller_id=? AND m.product_signature=? AND p.active=1
        ORDER BY m.updated_at DESC LIMIT 1""",
        (int(seller_id), signature),
    )
    return dict(found[0]) if found else None


def remembered_packages_for_orders(
    seller_id: int, orders: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Load package memories for many order cards without N+1 SQL queries.

    The returned mapping is keyed by ``order_key``.  Multiple orders containing
    the same product signature reuse the same profile, as intended.
    """
    ensure_schema()
    signature_to_keys: dict[str, list[str]] = {}
    for order in orders:
        order_key = clean_text(order.get("order_key"))
        if not order_key:
            continue
        signature = packlink_order_product_signature(order)
        signature_to_keys.setdefault(signature, []).append(order_key)
    signatures = list(signature_to_keys)
    if not signatures:
        return {}

    profile_by_signature: dict[str, dict[str, Any]] = {}
    # Keep well below SQLite's common parameter limit and PostgreSQL limits.
    for index in range(0, len(signatures), 400):
        chunk = signatures[index:index + 400]
        placeholders = ",".join("?" for _ in chunk)
        found = rows(
            f"""SELECT m.product_signature,m.updated_at AS memory_updated_at,p.*
            FROM packlink_product_package_memory m
            JOIN packlink_package_profiles p ON p.id=m.package_profile_id
            WHERE m.seller_id=? AND p.active=1
            AND m.product_signature IN ({placeholders})
            ORDER BY m.updated_at DESC""",
            (int(seller_id), *chunk),
        )
        for item in found:
            signature = clean_text(item.get("product_signature"))
            if signature and signature not in profile_by_signature:
                profile_by_signature[signature] = dict(item)

    result: dict[str, dict[str, Any]] = {}
    for signature, order_keys in signature_to_keys.items():
        profile = profile_by_signature.get(signature)
        if not profile:
            continue
        for order_key in order_keys:
            result[order_key] = dict(profile)
    return result


def remember_package_for_order(
    seller_id: int, order: Mapping[str, Any], package: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist a successfully used package and associate it with the order products."""
    profile = save_package_profile(seller_id, package, increment_use=True)
    product_signature = packlink_order_product_signature(order)
    timestamp = now_iso()
    execute(
        """INSERT INTO packlink_product_package_memory(
        seller_id,product_signature,package_profile_id,last_order_id,updated_at
        ) VALUES(?,?,?,?,?)
        ON CONFLICT(seller_id,product_signature) DO UPDATE SET
        package_profile_id=excluded.package_profile_id,last_order_id=excluded.last_order_id,updated_at=excluded.updated_at""",
        (
            int(seller_id), product_signature, int(profile["id"]),
            clean_text(order.get("order_id")), timestamp,
        ),
    )
    return profile


def package_for_packlink_order(
    default_parcel: Mapping[str, Any], weight_info: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep default dimensions but replace generic weight with the order's real weight."""
    package = dict(default_parcel)
    package["weight"] = max(
        0.01,
        float(_number(weight_info.get("weight_kg")) or _number(default_parcel.get("weight")) or 0.01),
    )
    return package

def package_payload(parcel: Mapping[str, Any]) -> dict[str, float | int]:
    weight = float(_number(parcel.get("weight")) or 0)
    width = float(_number(parcel.get("width")) or 0)
    height = float(_number(parcel.get("height")) or 0)
    length = float(_number(parcel.get("length")) or 0)
    if min(weight, width, height, length) <= 0:
        raise ValueError("Peso, larghezza, altezza e lunghezza del pacco devono essere maggiori di zero.")
    return {
        "width": int(width) if width.is_integer() else int(width) + 1,
        "height": int(height) if height.is_integer() else int(height) + 1,
        "length": int(length) if length.is_integer() else int(length) + 1,
        "weight": round(weight, 2),
    }


def order_declared_value(order: Mapping[str, Any], accounting_lines: Sequence[Mapping[str, Any]] = ()) -> float:
    order_id = clean_text(order.get("order_id"))
    values = [
        _number(item.get("sale_original_eur") if item.get("sale_original_eur") is not None else item.get("sale_eur"))
        for item in accounting_lines
        if clean_text(item.get("order_id")) == order_id
    ]
    total = sum(float(value) for value in values if value is not None)
    if total > 0:
        return round(total, 2)
    # Draft creation is still possible when accounting was not synchronized.
    # Packlink requires a numeric content value; use a minimal non-zero fallback
    # and make this visible in the UI so it can be corrected before sending.
    return 1.0


def build_packlink_draft_payload(
    *,
    integration: Mapping[str, Any],
    order: Mapping[str, Any],
    sender: Mapping[str, Any],
    package: Mapping[str, Any],
    service: Mapping[str, Any],
    declared_value: float,
    warehouse_id: str = "",
) -> dict[str, Any]:
    """Build the POST /v1/shipments body following Packlink's official Draft DTO.

    ``warehouse_id`` must be a Packlink warehouse identifier obtained from
    ``GET /v1/clients/warehouses``. Marketplace Hub never sends its own local
    sender-address row id as ``selectedWarehouseId``.
    """
    profile = packlink_client_profile(integration)
    origin = normalize_sender_address(sender)
    destination = packlink_destination_address(
        order, fallback_phone=origin.get("phone")
    )
    missing = [
        label for label, value in (
            ("nome mittente", origin.get("name")),
            ("indirizzo mittente", origin.get("street1")),
            ("CAP mittente", origin.get("zip_code")),
            ("città mittente", origin.get("city")),
            ("Paese mittente", origin.get("country")),
            ("indirizzo destinatario", destination.get("street1")),
            ("CAP destinatario", destination.get("zip_code")),
            ("città destinatario", destination.get("city")),
            ("Paese destinatario", destination.get("country")),
        ) if not clean_text(value)
    ]
    if missing:
        raise ValueError("Dati mancanti per la spedizione Packlink pronta per pagamento: " + ", ".join(missing) + ".")

    service_id = clean_text(service.get("id"))
    if not service_id:
        raise ValueError("Il servizio Packlink selezionato non contiene un service_id valido.")

    # Packlink's official OrderService allows collection_date/time to remain
    # unset. When the *selected quote* explicitly exposes a valid collection
    # slot, however, forwarding that exact slot makes the shipment more complete
    # without inventing any pickup information.
    collection_date, collection_time = first_collection_slot(service)

    content_items: list[str] = []
    for item in order.get("lines", []) if isinstance(order.get("lines"), Sequence) else []:
        if not isinstance(item, Mapping):
            continue
        title = clean_text(item.get("product_title"))
        if not title:
            continue
        quantity = max(1, int(_number(item.get("quantity")) or 1))
        content_items.append(f"{quantity} {title}")
    if not content_items:
        content_items = [clean_text(order.get("product_title")) or "Prodotti e-commerce"]
    content = packlink_content_description(content_items)

    reference = f"{clean_text(order.get('marketplace')).upper()}-{clean_text(order.get('order_id'))}"[:50]
    currency = clean_text(service.get("currency")).upper() or clean_text(order.get("currency")).upper() or "EUR"

    # Packlink's official e-commerce OrderService always identifies the sender
    # with the real default warehouse id returned by ``clients/warehouses``.
    # The country remains the ISO-2 code in the Address DTO; the dashboard
    # renders it as a dropdown, but the API value is still e.g. ``IT``.
    additional_data: dict[str, Any] = {
        # Same keys emitted by Packlink's current reference AdditionalData DTO.
        # Zone/zip ids stay null unless Packlink itself supplied them: never invent them.
        "postal_zone_id_from": None,
        "postal_zone_id_to": None,
        "shipping_service_name": clean_text(service.get("service")),
        "zip_code_id_from": None,
        "zip_code_id_to": None,
        "selectedWarehouseId": clean_text(warehouse_id) or None,
        "parcel_Ids": [],
        "postal_zone_name_to": None,
        "order_id": clean_text(order.get("order_id")),
        "seller_user_id": None,
        "integration_id": None,
        "items": [],
    }
    seller_user_id = clean_text(order.get("seller_user_id"))
    if seller_user_id:
        additional_data["seller_user_id"] = seller_user_id
    settings = integration_settings(integration)
    draft_api = settings.get("draft_api") if isinstance(settings.get("draft_api"), Mapping) else {}
    registration = settings.get("integration_registration") if isinstance(settings.get("integration_registration"), Mapping) else {}
    integration_id = clean_text(
        draft_api.get("integration_id")
        or settings.get("integration_id")
        or registration.get("integration_id")
    )
    if integration_id:
        additional_data["integration_id"] = integration_id
    for line in order.get("lines", []) if isinstance(order.get("lines"), Sequence) else []:
        if not isinstance(line, Mapping):
            continue
        additional_data["items"].append({
            "price": round(float(_number(line.get("sale_eur") or line.get("price") or 0) or 0), 2),
            "title": clean_text(line.get("product_title"))[:250],
            "picture_url": clean_text(line.get("picture_url") or line.get("image_url")),
            "quantity": max(1, int(_number(line.get("quantity")) or 1)),
            "category_name": clean_text(line.get("category_name") or line.get("category")),
        })

    if not additional_data["items"]:
        additional_data["items"].append({
            "price": round(float(_number(order.get("sale_eur") or order.get("price") or 0) or 0), 2),
            "title": clean_text(order.get("product_title") or "Prodotti e-commerce")[:250],
            "picture_url": clean_text(order.get("picture_url") or order.get("image_url")),
            "quantity": max(1, int(_number(order.get("quantity")) or 1)),
            "category_name": clean_text(order.get("category_name") or order.get("category")),
        })

    # These are the fields emitted by Packlink's Draft::toArray plus the same
    # additional_data structure used by Packlink's reference e-commerce core.
    return {
        "user_id": profile["user_id"],
        "client_id": profile["client_id"],
        "platform": profile["platform"],
        "platform_country": origin["country"],
        "source": profile["source"],
        "service": clean_text(service.get("service")),
        "carrier": clean_text(service.get("carrier")),
        "service_id": service_id,
        "collection_date": collection_date,
        "collection_time": collection_time,
        "dropoff_point_id": None,
        "content": content,
        "contentvalue": round(max(0.01, float(declared_value or 0)), 2),
        "content_second_hand": False,
        "shipment_custom_reference": reference,
        "priority": False,
        "contentValue_currency": currency,
        "has_customs": False,
        "from": origin,
        "to": destination,
        "packages": [package_payload(package)],
        "additional_data": additional_data,
    }


def packlink_ready_for_payment_validation(payload: Mapping[str, Any]) -> list[str]:
    """Return missing data that would prevent a shipment from being payment-ready.

    Packlink PRO defines *Pronti per pagamento* as drafts whose shipment
    information is complete and only payment is missing. This validator checks
    the fields Marketplace Hub controls before calling POST /v1/shipments.
    """
    missing: list[str] = []
    origin = payload.get("from") if isinstance(payload.get("from"), Mapping) else {}
    destination = payload.get("to") if isinstance(payload.get("to"), Mapping) else {}
    for prefix, address in (("mittente", origin), ("destinatario", destination)):
        for key, label in (
            ("name", "nome"), ("street1", "indirizzo"),
            ("zip_code", "CAP"), ("city", "città"),
            ("country", "Paese"), ("phone", "telefono"),
            ("email", "email"),
        ):
            if not clean_text(address.get(key)):
                missing.append(f"{prefix}: {label}")
    additional = payload.get("additional_data") if isinstance(payload.get("additional_data"), Mapping) else {}
    if not clean_text(additional.get("selectedWarehouseId")):
        missing.append("indirizzo Packlink del mittente")
    if not clean_text(payload.get("service_id")):
        missing.append("servizio di spedizione")
    if not clean_text(payload.get("service")):
        missing.append("nome servizio")
    if not clean_text(payload.get("carrier")):
        missing.append("corriere")
    packages = payload.get("packages") if isinstance(payload.get("packages"), list) else []
    if not packages:
        missing.append("pacco")
    else:
        try:
            for item in packages:
                package_payload(item if isinstance(item, Mapping) else {})
        except Exception:
            missing.append("peso/dimensioni pacco validi")
    if not clean_text(payload.get("content")):
        missing.append("contenuto")
    if float(_number(payload.get("contentvalue")) or 0) <= 0:
        missing.append("valore contenuto")
    if not clean_text(payload.get("contentValue_currency")):
        missing.append("valuta")
    return list(dict.fromkeys(missing))


def packlink_draft_diagnostic(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a privacy-safe summary useful when Packlink rejects a draft."""
    origin = payload.get("from") if isinstance(payload.get("from"), Mapping) else {}
    destination = payload.get("to") if isinstance(payload.get("to"), Mapping) else {}
    packages = payload.get("packages") if isinstance(payload.get("packages"), list) else []

    def address_status(address: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "country": clean_text(address.get("country")),
            "zip_code": clean_text(address.get("zip_code")),
            "city_present": bool(clean_text(address.get("city"))),
            "street_present": bool(clean_text(address.get("street1"))),
            "name_present": bool(clean_text(address.get("name"))),
            "surname_present": bool(clean_text(address.get("surname"))),
            "phone_present": bool(clean_text(address.get("phone"))),
            "email_present": bool(clean_text(address.get("email"))),
        }

    return {
        "service_id": clean_text(payload.get("service_id")),
        "service": clean_text(payload.get("service")),
        "carrier": clean_text(payload.get("carrier")),
        "platform": clean_text(payload.get("platform")),
        "platform_country": clean_text(payload.get("platform_country")),
        "source": clean_text(payload.get("source")),
        "user_id_present": payload.get("user_id") not in (None, ""),
        "client_id_present": payload.get("client_id") not in (None, ""),
        "collection_date": payload.get("collection_date"),
        "collection_time": payload.get("collection_time"),
        "dropoff_point_id": payload.get("dropoff_point_id"),
        "content_type": type(payload.get("content")).__name__,
        "content_length": len(clean_text(payload.get("content"))),
        "contentvalue": payload.get("contentvalue"),
        "currency": clean_text(payload.get("contentValue_currency")),
        "from": address_status(origin),
        "to": address_status(destination),
        "packages": packages,
        "additional_data_sent": "additional_data" in payload,
    }


def save_order_draft(
    seller_id: int,
    order: Mapping[str, Any],
    service: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    forced: bool = False,
) -> None:
    ensure_schema()
    timestamp = now_iso()
    reference = clean_text(response.get("reference"))
    common = (
        int(seller_id), int(order.get("marketplace_account_id") or 0),
        clean_text(order.get("marketplace")).lower(), clean_text(order.get("order_id")),
        clean_text(order.get("order_key")), reference, clean_text(service.get("id")),
        clean_text(service.get("carrier")), clean_text(service.get("service")),
        service.get("price"), clean_text(service.get("currency")),
        json.dumps(request_payload, ensure_ascii=False, default=str),
        json.dumps(response, ensure_ascii=False, default=str), timestamp,
    )
    # Keep an immutable creation history so a forced recreation never destroys
    # the previous Packlink reference.
    execute(
        """INSERT INTO packlink_order_draft_history(
        seller_id,marketplace_account_id,marketplace,order_id,order_key,shipment_reference,
        service_id,carrier,service,quoted_price,currency,forced,request_json,response_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        common[:11] + (1 if forced else 0,) + common[11:],
    )
    execute(
        """INSERT INTO packlink_order_drafts(
        seller_id,marketplace_account_id,marketplace,order_id,order_key,shipment_reference,
        service_id,carrier,service,quoted_price,currency,status,request_json,response_json,
        created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(seller_id,marketplace_account_id,marketplace,order_id) DO UPDATE SET
        order_key=excluded.order_key,shipment_reference=excluded.shipment_reference,
        service_id=excluded.service_id,carrier=excluded.carrier,service=excluded.service,
        quoted_price=excluded.quoted_price,currency=excluded.currency,status=excluded.status,
        request_json=excluded.request_json,response_json=excluded.response_json,
        created_at=excluded.created_at,updated_at=excluded.updated_at""",
        common[:11] + (
            clean_text(response.get("remote_status")) or "ready_for_payment",
        ) + common[11:] + (timestamp,),
    )
    # Deterministic relationship: the draft was created from this marketplace order.
    execute(
        """INSERT INTO packlink_matches(
        seller_id,shipment_reference,marketplace_account_id,marketplace,order_id,
        customer_name_order,supplier,match_status,match_score,match_reason,confirmed,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(seller_id,shipment_reference) DO UPDATE SET
        marketplace_account_id=excluded.marketplace_account_id,marketplace=excluded.marketplace,
        order_id=excluded.order_id,customer_name_order=excluded.customer_name_order,
        supplier=excluded.supplier,match_status=excluded.match_status,match_score=excluded.match_score,
        match_reason=excluded.match_reason,confirmed=excluded.confirmed,updated_at=excluded.updated_at""",
        (
            int(seller_id), reference, int(order.get("marketplace_account_id") or 0),
            clean_text(order.get("marketplace")).lower(), clean_text(order.get("order_id")),
            clean_text(order.get("customer_name")), clean_text(order.get("supplier")),
            "Abbinato automaticamente", 100.0,
            "bozza creata dal relativo ordine Marketplace Hub" + (" (forzata)" if forced else ""),
            1, timestamp,
        ),
    )



def last_order_draft_configuration(
    seller_id: int,
    marketplace_account_id: int,
    marketplace: str,
    order_id: str,
) -> dict[str, Any] | None:
    """Return the latest persisted Packlink package/service configuration for one order.

    The source of truth is the immutable draft history when available; the current
    draft row is used as a fallback.  The returned package comes from the exact
    request_json sent to Packlink, so forced recreation can restore the same weight
    and dimensions instead of recomputing defaults from the catalog.
    """
    ensure_schema()
    params = (
        int(seller_id), int(marketplace_account_id), clean_text(marketplace).lower(),
        clean_text(order_id),
    )
    history = rows(
        """SELECT * FROM packlink_order_draft_history
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? AND order_id=?
        ORDER BY created_at DESC,id DESC LIMIT 1""",
        params,
    )
    row_value = dict(history[0]) if history else None
    if row_value is None:
        current = rows(
            """SELECT * FROM packlink_order_drafts
            WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? AND order_id=?
            ORDER BY updated_at DESC,id DESC LIMIT 1""",
            params,
        )
        row_value = dict(current[0]) if current else None
    if not row_value:
        return None

    request_payload: dict[str, Any] = {}
    try:
        raw = row_value.get("request_json")
        parsed = json.loads(raw) if isinstance(raw, str) and raw else {}
        if isinstance(parsed, Mapping):
            request_payload = dict(parsed)
    except Exception:
        request_payload = {}

    package: dict[str, float] | None = None
    packages = request_payload.get("packages")
    if isinstance(packages, list) and packages and isinstance(packages[0], Mapping):
        try:
            package = package_payload(packages[0])
        except Exception:
            package = None

    declared_value = _number(request_payload.get("contentvalue"))
    return {
        "shipment_reference": clean_text(row_value.get("shipment_reference")),
        "service_id": clean_text(row_value.get("service_id")),
        "carrier": clean_text(row_value.get("carrier")),
        "service": clean_text(row_value.get("service")),
        "quoted_price": row_value.get("quoted_price"),
        "currency": clean_text(row_value.get("currency")),
        "declared_value": float(declared_value) if declared_value is not None else None,
        "created_at": clean_text(row_value.get("created_at")),
        "forced": bool(row_value.get("forced")),
        "package": package,
        "request_json": request_payload,
    }

def order_draft_history(seller_id: int, *, order_id: str = "") -> list[dict[str, Any]]:
    ensure_schema()
    if clean_text(order_id):
        return rows(
            """SELECT * FROM packlink_order_draft_history WHERE seller_id=? AND order_id=?
            ORDER BY created_at DESC,id DESC""",
            (int(seller_id), clean_text(order_id)),
        )
    return rows(
        """SELECT * FROM packlink_order_draft_history WHERE seller_id=?
        ORDER BY created_at DESC,id DESC""",
        (int(seller_id),),
    )


def order_drafts(seller_id: int) -> list[dict[str, Any]]:
    ensure_schema()
    return rows(
        """SELECT * FROM packlink_order_drafts WHERE seller_id=?
        ORDER BY created_at DESC,id DESC""",
        (int(seller_id),),
    )


def activate_integration(seller_id: int, api_key: str, *, client_data: Mapping[str, Any] | None = None) -> None:
    ensure_schema()
    timestamp = now_iso()
    # Re-validating the API key must not wipe the Seller's Packlink workflow
    # preferences (warehouse, parcel and draft API identifiers). Preserve the
    # existing settings and refresh only the connection/profile metadata.
    existing = integration_for_seller(seller_id, include_inactive=True)
    settings = integration_settings(existing) if existing else {}
    settings = dict(settings)
    settings["client"] = dict(client_data or {})
    settings["api_base_url"] = PACKLINK_API_BASE_URL
    encrypted = encrypt_dict({"api_key": clean_text(api_key)})
    with connect() as con:
        con.execute(
            """INSERT INTO seller_integrations(
                seller_id,provider,credentials_encrypted,settings_json,active,
                connection_status,last_checked_at,last_error,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(seller_id,provider) DO UPDATE SET
                credentials_encrypted=excluded.credentials_encrypted,
                settings_json=excluded.settings_json,
                active=1,
                connection_status='connected',
                last_checked_at=excluded.last_checked_at,
                last_error='',
                updated_at=excluded.updated_at""",
            (
                int(seller_id), PACKLINK_PROVIDER, encrypted,
                json.dumps(settings, ensure_ascii=False, default=str), 1,
                "connected", timestamp, "", timestamp, timestamp,
            ),
        )


def update_connection_status(seller_id: int, *, ok: bool, error: str = "") -> None:
    ensure_schema()
    with connect() as con:
        con.execute(
            """UPDATE seller_integrations SET connection_status=?,last_checked_at=?,
            last_error=?,updated_at=? WHERE seller_id=? AND provider=?""",
            (
                "connected" if ok else "error", now_iso(), "" if ok else clean_text(error),
                now_iso(), int(seller_id), PACKLINK_PROVIDER,
            ),
        )


def set_integration_active(seller_id: int, active: bool) -> None:
    ensure_schema()
    with connect() as con:
        con.execute(
            "UPDATE seller_integrations SET active=?,updated_at=? WHERE seller_id=? AND provider=?",
            (1 if active else 0, now_iso(), int(seller_id), PACKLINK_PROVIDER),
        )


def delete_integration(seller_id: int) -> None:
    ensure_schema()
    with connect() as con:
        con.execute(
            "DELETE FROM seller_integrations WHERE seller_id=? AND provider=?",
            (int(seller_id), PACKLINK_PROVIDER),
        )


def _shipment_db_tuple(seller_id: int, item: Mapping[str, Any], timestamp: str) -> tuple[Any, ...]:
    return (
        int(seller_id), clean_text(item.get("reference")), clean_text(item.get("order_reference")),
        clean_text(item.get("customer_name")), clean_text(item.get("email")),
        clean_text(item.get("phone")), clean_text(item.get("address")),
        clean_text(item.get("carrier")), clean_text(item.get("service")),
        clean_text(item.get("tracking")), clean_text(item.get("status")),
        clean_text(item.get("created_at")), item.get("price"), clean_text(item.get("currency")),
        clean_text(item.get("raw_json")) or "{}", timestamp, timestamp,
    )


def sync_shipments(seller_id: int, client: PacklinkClient) -> dict[str, Any]:
    ensure_schema()
    run_id = execute(
        "INSERT INTO packlink_sync_runs(seller_id,status,created_at) VALUES(?,?,?)",
        (int(seller_id), "running", now_iso()),
    )
    try:
        raw_shipments = client.list_shipments()
        normalized: list[dict[str, Any]] = []
        skipped = 0
        for index, raw in enumerate(raw_shipments, 1):
            item = normalize_packlink_shipment(raw, position=index)
            if not item["reference"]:
                skipped += 1
                continue
            normalized.append(item)

        existing_refs = {
            clean_text(item.get("reference"))
            for item in rows("SELECT reference FROM packlink_shipments WHERE seller_id=?", (int(seller_id),))
        }
        timestamp = now_iso()
        inserted = sum(1 for item in normalized if item["reference"] not in existing_refs)
        updated = len(normalized) - inserted
        values = [_shipment_db_tuple(seller_id, item, timestamp) for item in normalized]
        if values:
            execute_many(
                """INSERT INTO packlink_shipments(
                    seller_id,reference,order_reference,customer_name,email,phone,address,
                    carrier,service,tracking,status,shipment_created_at,price,currency,raw_json,
                    first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(seller_id,reference) DO UPDATE SET
                    order_reference=excluded.order_reference,
                    customer_name=excluded.customer_name,email=excluded.email,
                    phone=excluded.phone,address=excluded.address,carrier=excluded.carrier,
                    service=excluded.service,tracking=excluded.tracking,status=excluded.status,
                    shipment_created_at=excluded.shipment_created_at,price=excluded.price,
                    currency=excluded.currency,raw_json=excluded.raw_json,
                    last_seen_at=excluded.last_seen_at""",
                values,
            )
        with connect() as con:
            con.execute(
                """UPDATE packlink_sync_runs SET status='success',fetched=?,inserted=?,updated=?,
                completed_at=? WHERE id=?""",
                (len(normalized), inserted, updated, now_iso(), int(run_id)),
            )
        return {
            "ok": True, "fetched": len(normalized), "inserted": inserted,
            "updated": updated, "skipped": skipped, "run_id": run_id,
        }
    except Exception as error:
        with connect() as con:
            con.execute(
                "UPDATE packlink_sync_runs SET status='failed',error=?,completed_at=? WHERE id=?",
                (clean_text(error), now_iso(), int(run_id)),
            )
        raise


def cached_shipments(seller_id: int) -> list[dict[str, Any]]:
    ensure_schema()
    return rows(
        """SELECT id,seller_id,reference,order_reference,customer_name,email,phone,address,
        carrier,service,tracking,status,shipment_created_at,price,currency,first_seen_at,last_seen_at
        FROM packlink_shipments WHERE seller_id=?
        ORDER BY shipment_created_at DESC,id DESC""",
        (int(seller_id),),
    )


def last_sync(seller_id: int) -> dict[str, Any] | None:
    ensure_schema()
    found = rows(
        "SELECT * FROM packlink_sync_runs WHERE seller_id=? ORDER BY id DESC LIMIT 1",
        (int(seller_id),),
    )
    return found[0] if found else None


def _cached_as_matcher_rows(shipments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(shipments, 1):
        result.append({
            "source_row": index,
            "source_reference": clean_text(item.get("reference")),
            "reference": clean_text(item.get("reference")),
            "marketplace_order_reference": clean_text(item.get("order_reference")),
            "customer_name": clean_text(item.get("customer_name")),
            "email": clean_text(item.get("email")),
            "phone": clean_text(item.get("phone")),
            "address": clean_text(item.get("address")),
            "product": "",
            "product_code": "",
            "eans": [],
            "created_at": clean_text(item.get("shipment_created_at")),
            "file_status": clean_text(item.get("status")),
            "tracking": clean_text(item.get("tracking")),
            "carrier": clean_text(item.get("carrier")),
        })
    return result


def match_shipments_across_accounts(
    seller_id: int,
    shipments: Sequence[Mapping[str, Any]],
    account_orders: Mapping[int, tuple[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    """Match Packlink shipments across accounts without an O(shipments × orders) scan.

    Packlink normally carries the shop ``order_reference``.  We therefore index all
    marketplace orders by exact order number first.  Only shipments without that
    direct key fall back to an exact normalized customer-name index and then to the
    existing detailed matcher on the small candidate set.
    """
    source_rows = _cached_as_matcher_rows(shipments)
    grouped_lines: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    marketplace_by_account: dict[int, str] = {}
    reference_index: dict[str, list[tuple[int, str]]] = defaultdict(list)
    customer_index: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for account_id, (marketplace, order_lines) in account_orders.items():
        account_id = int(account_id)
        marketplace_by_account[account_id] = clean_text(marketplace).lower()
        for line in order_lines:
            order_id = clean_text(line.get("order_id"))
            if not order_id:
                continue
            grouped_lines[(account_id, order_id)].append(dict(line))

    for key, lines in grouped_lines.items():
        account_id, order_id = key
        reference_index[order_id.upper()].append(key)
        customer = normalized_customer_name(lines[0].get("customer_name")) if lines else ""
        if customer:
            customer_index[customer].append(key)

    def _date_value(value: Any) -> date | None:
        text = clean_text(value)
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except Exception:
            try:
                return date.fromisoformat(text[:10])
            except Exception:
                return None

    output: list[dict[str, Any]] = []
    for source in source_rows:
        direct_reference = clean_text(source.get("marketplace_order_reference")).upper()
        keys = list(reference_index.get(direct_reference, [])) if direct_reference else []
        direct_hit = bool(keys)

        if not keys:
            customer = normalized_customer_name(source.get("customer_name"))
            keys = list(customer_index.get(customer, [])) if customer else []
            # Common customer names can still produce a large set. Keep only orders
            # reasonably close to the shipment date before invoking the detailed matcher.
            if len(keys) > 50:
                shipment_date = _date_value(source.get("created_at"))
                if shipment_date:
                    narrowed: list[tuple[int, str]] = []
                    for key in keys:
                        lines = grouped_lines.get(key) or []
                        order_date = _date_value(lines[0].get("order_created")) if lines else None
                        if order_date and abs((shipment_date - order_date).days) <= 45:
                            narrowed.append(key)
                    if narrowed:
                        keys = narrowed
                keys = keys[:100]

        candidates: list[dict[str, Any]] = []
        for account_id, order_id in keys:
            lines = grouped_lines.get((account_id, order_id), [])
            if not lines:
                continue
            matched = match_tracking_rows([source], lines)
            if not matched:
                continue
            candidate = dict(matched[0])
            candidate["marketplace_account_id"] = account_id
            candidate["marketplace"] = marketplace_by_account.get(account_id, "")
            candidates.append(candidate)

        candidates.sort(
            key=lambda item: (
                -float(item.get("match_score") or 0),
                int(item.get("marketplace_account_id") or 0),
                clean_text(item.get("order_id")),
            )
        )
        if not candidates:
            output.append({
                **source,
                "marketplace_account_id": None,
                "marketplace": "",
                "match_status": "Non abbinato",
                "match_score": 0.0,
                "match_reason": "nessun ordine compatibile",
                "order_id": "",
                "customer_name_order": "",
                "marketplace_status": "",
                "market_label": "",
                "order_line_ids": [],
                "row_keys": [],
                "supplier": "",
            })
            continue

        best = candidates[0]
        score = float(best.get("match_score") or 0)
        second_score = float(candidates[1].get("match_score") or 0) if len(candidates) > 1 else -1.0
        status = clean_text(best.get("match_status")) or "Non abbinato"

        if direct_hit and len(keys) == 1:
            status = "Abbinato automaticamente"
        elif len(candidates) > 1 and score >= 45 and score - second_score < 15:
            first_scope = (
                int(best.get("marketplace_account_id") or 0),
                clean_text(best.get("order_id")),
            )
            second_scope = (
                int(candidates[1].get("marketplace_account_id") or 0),
                clean_text(candidates[1].get("order_id")),
            )
            if first_scope != second_scope:
                status = "Ambiguo · verifica manuale"

        output.append({**source, **best, "match_status": status})
    return output

def persist_matches(seller_id: int, matches: Sequence[Mapping[str, Any]]) -> None:
    ensure_schema()
    timestamp = now_iso()
    values = []
    for item in matches:
        reference = clean_text(item.get("source_reference") or item.get("reference"))
        if not reference:
            continue
        values.append((
            int(seller_id), reference,
            int(item.get("marketplace_account_id") or 0) or None,
            clean_text(item.get("marketplace")).lower(), clean_text(item.get("order_id")),
            clean_text(item.get("customer_name_order")), clean_text(item.get("supplier")),
            clean_text(item.get("match_status")), float(item.get("match_score") or 0),
            clean_text(item.get("match_reason")), 0, timestamp,
        ))
    if not values:
        return
    execute_many(
        """INSERT INTO packlink_matches(
            seller_id,shipment_reference,marketplace_account_id,marketplace,order_id,
            customer_name_order,supplier,match_status,match_score,match_reason,confirmed,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(seller_id,shipment_reference) DO UPDATE SET
            marketplace_account_id=excluded.marketplace_account_id,
            marketplace=excluded.marketplace,order_id=excluded.order_id,
            customer_name_order=excluded.customer_name_order,supplier=excluded.supplier,
            match_status=excluded.match_status,match_score=excluded.match_score,
            match_reason=excluded.match_reason,updated_at=excluded.updated_at""",
        values,
    )


def saved_matches(seller_id: int) -> list[dict[str, Any]]:
    ensure_schema()
    return rows(
        """SELECT m.*,s.order_reference,s.customer_name AS packlink_customer_name,
        s.carrier,s.service,s.tracking,s.status AS packlink_status,
        s.shipment_created_at,s.address,s.email,s.phone
        FROM packlink_matches m
        JOIN packlink_shipments s
          ON s.seller_id=m.seller_id AND s.reference=m.shipment_reference
        WHERE m.seller_id=? ORDER BY s.shipment_created_at DESC,m.id DESC""",
        (int(seller_id),),
    )


def confirm_match(seller_id: int, reference: str, *, account_id: int, marketplace: str, order_id: str) -> None:
    ensure_schema()
    with connect() as con:
        con.execute(
            """UPDATE packlink_matches SET marketplace_account_id=?,marketplace=?,order_id=?,
            match_status='Abbinato manualmente',confirmed=1,updated_at=?
            WHERE seller_id=? AND shipment_reference=?""",
            (
                int(account_id), clean_text(marketplace).lower(), clean_text(order_id), now_iso(),
                int(seller_id), clean_text(reference),
            ),
        )


def shipments_as_tracking_rows(
    seller_id: int,
    account_id: int,
    marketplace: str,
    *,
    only_matched: bool = True,
) -> list[dict[str, Any]]:
    """Return Packlink matches in the same shape consumed by order_tracking_rows()."""
    ensure_schema()
    sql = """SELECT m.*,s.order_reference,s.customer_name,s.carrier,s.tracking,s.status,s.shipment_created_at
    FROM packlink_matches m JOIN packlink_shipments s
      ON s.seller_id=m.seller_id AND s.reference=m.shipment_reference
    WHERE m.seller_id=? AND m.marketplace_account_id=? AND m.marketplace=?"""
    params: list[Any] = [int(seller_id), int(account_id), clean_text(marketplace).lower()]
    if only_matched:
        sql += " AND m.order_id<>'' AND m.match_status IN ('Abbinato automaticamente','Abbinato manualmente')"
    sql += " ORDER BY s.shipment_created_at DESC"
    data = rows(sql, tuple(params))
    return [
        {
            "source_reference": item.get("shipment_reference"),
            "marketplace_order_reference": item.get("order_reference", ""),
            "customer_name": item.get("customer_name", ""),
            "customer_name_order": item.get("customer_name_order", ""),
            "order_id": item.get("order_id", ""),
            "supplier": item.get("supplier", ""),
            "file_status": item.get("status", ""),
            "tracking": item.get("tracking", ""),
            "carrier": item.get("carrier", ""),
            "match_status": item.get("match_status", ""),
            "match_score": item.get("match_score", 0),
            "match_reason": item.get("match_reason", ""),
            "created_at": item.get("shipment_created_at", ""),
        }
        for item in data
    ]
