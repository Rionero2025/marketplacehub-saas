"""Read-only order import adapters, preserving each marketplace's pagination semantics."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import httpx

from marketplace_hub_core.marketplace_connections.connectors import (
    KAUFLAND_URL,
    WORTEN_URL,
    ConnectionProbeError,
    kaufland_headers,
    normalize_credentials,
)
from marketplace_hub_core.orders.normalization import (
    extract_tracking,
    find_order_unit,
    merge_order_unit,
    normalize_order_line,
)

KAUFLAND_PLAYGROUND_URL = "https://sellerapi-playground.kaufland.com/v2"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ORDER_STATUSES = (
    "cancelled",
    "need_to_be_sent",
    "open",
    "received",
    "returned",
    "returned_paid",
    "sent",
    "sent_and_autopaid",
)
SHIPPED_STATUSES = {"sent", "sent_and_autopaid", "received", "returned", "returned_paid"}
ERROR_MESSAGES = {
    "invalid_credentials": "Il marketplace non ha accettato le credenziali API.",
    "permission_denied": "L’account non autorizza la lettura degli ordini.",
    "invalid_response": "Il marketplace ha restituito dati ordine non riconoscibili.",
    "upstream_unavailable": "Il marketplace non è momentaneamente disponibile.",
    "rate_limited": "Il marketplace ha raggiunto il limite di richieste. Riprova più tardi.",
    "timeout": "Il marketplace non ha risposto entro il tempo disponibile.",
    "unsupported_marketplace": "La sincronizzazione non è disponibile per questo marketplace.",
    "invalid_configuration": "La configurazione dell’account non è valida.",
}


class OrdersFetchError(ValueError):
    def __init__(self, code: str):
        self.code = code if code in ERROR_MESSAGES else "upstream_unavailable"
        super().__init__(ERROR_MESSAGES[self.code])


BatchCallback = Callable[[list[dict], int, int | None], Awaitable[None]]
ProgressCallback = Callable[[str], Awaitable[None]]
GuardCallback = Callable[[], Awaitable[None]]


async def _noop(*args) -> None:
    return None


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _collection(payload: dict, key: str) -> list[dict]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise OrdersFetchError("invalid_response")
    return value


def _total(value) -> int | None:
    try:
        result = int(value)
        return result if result >= 0 and not isinstance(value, bool) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _fingerprint(page: list[dict]) -> str:
    return hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()


def _detail(payload: dict) -> dict:
    value = payload.get("data", payload)
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    if not isinstance(value, dict):
        raise OrdersFetchError("invalid_response")
    return value


async def fetch_ecb_snapshot(client: httpx.AsyncClient) -> dict:
    """Keep the real reference date. Never substitute invented exchange rates."""
    try:
        async with asyncio.timeout(12):
            async with client.stream("GET", ECB_URL) as response:
                if response.status_code != 200:
                    raise ValueError("ECB unavailable")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 100_000:
                        raise ValueError("ECB invalid")
            root = ET.fromstring(data)
            date, rates = "", {}
            for element in root.iter():
                date = element.attrib.get("time", date)
                if "currency" in element.attrib and "rate" in element.attrib:
                    rate = float(element.attrib["rate"])
                    if math.isfinite(rate) and rate > 0:
                        rates[element.attrib["currency"].upper()] = rate
            if not date or not all(code in rates for code in ("PLN", "CZK")):
                raise ValueError("ECB invalid")
            return {
                "date": date,
                "rates": rates,
                "online": True,
                "source": "Banca Centrale Europea",
            }
    except (httpx.HTTPError, ValueError, ET.ParseError, TimeoutError):
        return {"date": None, "rates": {}, "online": False, "source": "Cambio non disponibile"}


class OrdersConnector:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        timestamp: Callable[[], float] = time.time,
        max_attempts: int = 7,
    ) -> None:
        self.transport = transport
        self.pause = pause
        self.clock = clock
        self.timestamp = timestamp
        self.max_attempts = max_attempts
        self.next_request = 0.0

    async def _request(
        self,
        client: httpx.AsyncClient,
        marketplace: str,
        credentials: dict,
        url: str,
        before_request: GuardCallback,
    ) -> dict:
        last_code = "upstream_unavailable"
        for attempt in range(self.max_attempts):
            if marketplace == "kaufland":
                await self.pause(max(0.0, self.next_request - self.clock()))
                self.next_request = self.clock() + 1 / 15
            await before_request()
            headers = (
                kaufland_headers(url, credentials, str(int(self.timestamp())))
                if marketplace == "kaufland"
                else {
                    "Authorization": credentials["api_key"],
                    "Accept": "application/json",
                    "User-Agent": "MarketplaceHub/1.0",
                }
            )
            retry_after = None
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    status = response.status_code
                    if status == 401:
                        raise OrdersFetchError("invalid_credentials")
                    if status == 403:
                        raise OrdersFetchError("permission_denied")
                    if status in {429, 500, 502, 503, 504}:
                        last_code = "rate_limited" if status == 429 else "upstream_unavailable"
                        try:
                            retry_after = float(response.headers.get("retry-after", ""))
                        except ValueError:
                            pass
                    elif status != 200:
                        raise OrdersFetchError("invalid_response")
                    else:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 8_000_000:
                                raise OrdersFetchError("invalid_response")
                        try:
                            value = json.loads(raw)
                        except (ValueError, UnicodeError):
                            raise OrdersFetchError("invalid_response") from None
                        if not isinstance(value, dict):
                            raise OrdersFetchError("invalid_response")
                        return value
            except httpx.TimeoutException:
                last_code = "timeout"
            except httpx.RequestError:
                last_code = "upstream_unavailable"
            if attempt + 1 < self.max_attempts:
                delay = (
                    min(30, 2**attempt)
                    if last_code == "rate_limited"
                    else min(12, 0.5 * 2**attempt)
                )
                if retry_after is not None and math.isfinite(retry_after) and retry_after >= 0:
                    delay = min(120, max(0.5, retry_after))
                await self.pause(delay + random.uniform(0.02, 0.15))
        raise OrdersFetchError(last_code)

    async def fetch_orders(
        self,
        marketplace: str,
        credentials: dict,
        *,
        environment: str,
        maximum: int | None,
        include_details: bool,
        on_batch: BatchCallback,
        on_progress: ProgressCallback = _noop,
        before_request: GuardCallback = _noop,
    ) -> dict:
        if marketplace not in {"kaufland", "worten"}:
            raise OrdersFetchError("unsupported_marketplace")
        if (
            environment not in {"live", "playground"}
            or (marketplace == "worten" and environment != "live")
            or (maximum is not None and (isinstance(maximum, bool) or maximum < 1))
        ):
            raise OrdersFetchError("invalid_configuration")
        try:
            credentials = normalize_credentials(marketplace, credentials)
        except (ConnectionProbeError, AttributeError, TypeError):
            raise OrdersFetchError("invalid_configuration") from None
        # No proxy inheritance or redirects: credentials only reach the fixed provider host.
        async with httpx.AsyncClient(
            transport=self.transport, timeout=45, follow_redirects=False, trust_env=False
        ) as client:
            await before_request()
            fx = await fetch_ecb_snapshot(client)
            if marketplace == "kaufland":
                return await self._kaufland(
                    client,
                    credentials,
                    environment,
                    maximum,
                    include_details,
                    fx,
                    on_batch,
                    on_progress,
                    before_request,
                )
            return await self._worten(
                client, credentials, maximum, fx, on_batch, on_progress, before_request
            )

    async def _kaufland(
        self,
        client,
        credentials,
        environment,
        maximum,
        include_details,
        fx,
        on_batch,
        on_progress,
        before_request,
    ) -> dict:
        base = KAUFLAND_PLAYGROUND_URL if environment == "playground" else KAUFLAND_URL
        by_id = {}
        for status in ORDER_STATUSES:
            offset, fingerprints = 0, set()
            while maximum is None or offset < maximum:
                limit = 100 if maximum is None else min(100, maximum - offset)
                url = f"{base}/order-units?" + urlencode(
                    {
                        "limit": limit,
                        "offset": offset,
                        "status": status,
                    }
                )
                payload = await self._request(client, "kaufland", credentials, url, before_request)
                page = _collection(payload, "data")
                if not page:
                    break
                fingerprint = _fingerprint(page)
                if fingerprint in fingerprints:
                    raise OrdersFetchError("invalid_response")
                fingerprints.add(fingerprint)
                for raw in page:
                    identity = _text(raw.get("id_order_unit"))
                    if not identity:
                        raise OrdersFetchError("invalid_response")
                    by_id.setdefault(identity, raw)
                offset += len(page)
                await on_progress(f"Download ordini {status}: {offset}")
                pagination = payload.get("pagination")
                total = _total(pagination.get("total")) if isinstance(pagination, dict) else None
                if len(page) < limit or (total is not None and offset >= total):
                    break
        rows = sorted(
            by_id.values(),
            key=lambda item: (
                _text(item.get("ts_created_iso")),
                _text(item.get("ts_updated_iso")),
                _text(item.get("id_order_unit")),
            ),
            reverse=True,
        )
        if maximum is not None:
            rows = rows[:maximum]
        batch, order_details = [], {}
        warnings, checked = 0, 0
        for index, raw in enumerate(rows, start=1):
            merged = dict(raw)
            detail_checked_at = None
            unit_id = _text(raw.get("id_order_unit"))
            if include_details and _text(raw.get("status")).lower() in SHIPPED_STATUSES:
                try:
                    url = f"{base}/order-units/{quote(unit_id, safe='')}"
                    detail = _detail(
                        await self._request(client, "kaufland", credentials, url, before_request)
                    )
                    if _text(detail.get("id_order_unit")) != unit_id:
                        raise OrdersFetchError("invalid_response")
                    merged = merge_order_unit(raw, detail)
                    detail_checked_at = datetime.fromtimestamp(self.timestamp(), UTC).isoformat()
                    checked += 1
                    order_id = _text(merged.get("id_order"))
                    if not extract_tracking(merged)[1] and order_id:
                        if order_id not in order_details:
                            url = f"{base}/orders/{quote(order_id, safe='')}"
                            order_details[order_id] = _detail(
                                await self._request(
                                    client,
                                    "kaufland",
                                    credentials,
                                    url,
                                    before_request,
                                )
                            )
                        found = find_order_unit(order_details[order_id], unit_id)
                        if found:
                            merged = merge_order_unit(merged, found)
                except OrdersFetchError as error:
                    if error.code in {"invalid_credentials", "permission_denied"}:
                        raise
                    merged["_detail_warning"] = "details_unavailable"
                    warnings += 1
            merged["_fx"] = fx
            normalized = normalize_order_line("kaufland", merged, fx_rates=fx["rates"])
            if detail_checked_at:
                normalized["details"]["detail_checked_at"] = detail_checked_at
            normalized["raw"] = merged
            batch.append(normalized)
            if len(batch) == 100 or index == len(rows):
                await on_batch(batch, index, len(rows))
                batch = []
        if not rows:
            await on_batch([], 0, 0)
        return {"warning_count": warnings, "details_checked": checked}

    async def _worten(
        self, client, credentials, maximum, fx, on_batch, on_progress, before_request
    ) -> dict:
        offset, processed, seen_orders, seen_lines, fingerprints = 0, 0, set(), set(), set()
        while maximum is None or processed < maximum:
            url = f"{WORTEN_URL}/orders?" + urlencode(
                {
                    "offset": offset,
                    "max": 100,
                    "shop_id": credentials["shop_id"],
                    # OR11 defaults to creation date (Mirakl's official SDK contract).
                    # Do not guess a sort field enum used by a different endpoint.
                    "order": "desc",
                }
            )
            payload = await self._request(client, "worten", credentials, url, before_request)
            orders = _collection(payload, "orders")
            if not orders:
                break
            fingerprint = _fingerprint(orders)
            if fingerprint in fingerprints:
                raise OrdersFetchError("invalid_response")
            fingerprints.add(fingerprint)
            batch = []
            for order in orders:
                if (order.get("shop_id") is not None
                        and _text(order["shop_id"]) != credentials["shop_id"]):
                    raise OrdersFetchError("permission_denied")
                order_id = _text(
                    order.get("order_id") or order.get("commercial_id") or order.get("id")
                )
                if not order_id:
                    raise OrdersFetchError("invalid_response")
                if order_id in seen_orders:
                    continue
                seen_orders.add(order_id)
                lines = order.get("order_lines", order.get("lines"))
                if not isinstance(lines, list) or any(not isinstance(line, dict) for line in lines):
                    raise OrdersFetchError("invalid_response")
                for line_index, line in enumerate(lines):
                    line = dict(line)
                    line_id = (
                        _text(line.get("order_line_id") or line.get("line_id") or line.get("id"))
                        or f"{order_id}-{line_index + 1}"
                    )
                    identity = (order_id, line_id)
                    if identity in seen_lines:
                        continue
                    seen_lines.add(identity)
                    line["order_line_id"] = line_id
                    line["_order"] = {
                        key: value
                        for key, value in order.items()
                        if key not in {"order_lines", "lines"}
                    }
                    line["_fx"] = fx
                    normalized = normalize_order_line("worten", line, fx_rates=fx["rates"])
                    normalized["raw"] = line
                    batch.append(normalized)
                    processed += 1
                    if maximum is not None and processed >= maximum:
                        break
                if maximum is not None and processed >= maximum:
                    break
            offset += len(orders)
            await on_progress(f"Download ordini Worten: {offset} ordini, {processed} righe")
            await on_batch(batch, processed, None)
            total_orders = _total(payload.get("total_count"))
            if len(orders) < 100 or (total_orders is not None and offset >= total_orders):
                break
        await on_batch([], processed, processed)
        return {"warning_count": 0, "details_checked": 0}


async def fetch_orders(marketplace: str, credentials: dict, **options) -> dict:
    return await OrdersConnector().fetch_orders(marketplace, credentials, **options)
