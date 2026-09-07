import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

KAUFLAND_URL = "https://sellerapi.kaufland.com/v2"
WORTEN_URL = "https://marketplace.worten.pt/api"

ERRORS = {
    "invalid_credentials": (422, "Il marketplace non ha accettato le credenziali API."),
    "permission_denied": (422, "Le credenziali non autorizzano questo accesso al marketplace."),
    "invalid_configuration": (422, "Controlla i campi richiesti per questo marketplace."),
    "unexpected_response": (502, "Il marketplace ha restituito una risposta non riconoscibile."),
    "upstream_unavailable": (502, "Il marketplace è temporaneamente non disponibile. Riprova."),
    "timeout": (504, "La verifica del marketplace è scaduta. Riprova."),
    "credentials_unavailable": (503, "Credenziali salvate non disponibili per la verifica."),
}


class ConnectionProbeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        self.status_code, self.message = ERRORS[code]
        super().__init__(self.message)


@dataclass(frozen=True)
class VerifiedMetadata:
    public_name: str | None = None
    external_shop_id: str | None = None
    storefronts: list[str] = field(default_factory=list)


JsonTransport = Callable[[str, dict[str, str]], Awaitable[dict]]


async def remote_json(url: str, headers: dict[str, str]) -> dict:
    """Fixed connector URLs, no redirects/proxies, bounded JSON and no body in errors."""
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=False, trust_env=False,
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 401:
                    raise ConnectionProbeError("invalid_credentials")
                if response.status_code == 403:
                    raise ConnectionProbeError("permission_denied")
                if response.status_code != 200:
                    raise ConnectionProbeError("upstream_unavailable")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 500_000:
                        raise ConnectionProbeError("unexpected_response")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ConnectionProbeError("unexpected_response")
        return value
    except httpx.TimeoutException:
        raise ConnectionProbeError("timeout") from None
    except httpx.RequestError:
        raise ConnectionProbeError("upstream_unavailable") from None
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ConnectionProbeError):
            raise
        raise ConnectionProbeError("unexpected_response") from None


def normalize_credentials(marketplace: str, values: dict[str, str]) -> dict[str, str]:
    values = {key: value.strip() for key, value in values.items()}
    if marketplace == "kaufland":
        if set(values) - {"client_key", "secret_key"} or not all(
            values.get(key) for key in ("client_key", "secret_key")
        ):
            raise ConnectionProbeError("invalid_configuration")
        return {key: values[key] for key in ("client_key", "secret_key")}
    if marketplace == "worten":
        if set(values) - {"api_key", "shop_id", "api_url", "country"} or not all(
            values.get(key) for key in ("api_key", "shop_id")
        ):
            raise ConnectionProbeError("invalid_configuration")
        if values.get("api_url", WORTEN_URL).rstrip("/") != WORTEN_URL:
            raise ConnectionProbeError("invalid_configuration")
        if values.get("country", "pt").lower() != "pt":
            raise ConnectionProbeError("invalid_configuration")
        return {"api_key": values["api_key"], "shop_id": values["shop_id"],
                "api_url": WORTEN_URL, "country": "pt"}
    raise ConnectionProbeError("invalid_configuration")


def kaufland_headers(url: str, credentials: dict[str, str], timestamp: str) -> dict[str, str]:
    message = f"GET\n{url}\n\n{timestamp}"
    signature = hmac.new(
        credentials["secret_key"].encode("utf-8"), message.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return {
        "Shop-Client-Key": credentials["client_key"], "Shop-Timestamp": timestamp,
        "Shop-Signature": signature, "Accept": "application/json",
        "User-Agent": "MarketplaceHub/1.0",
    }


class MarketplaceConnector:
    def __init__(self, transport: JsonTransport = remote_json) -> None:
        self.transport = transport

    async def verify(self, marketplace: str, values: dict[str, str]) -> VerifiedMetadata:
        credentials = normalize_credentials(marketplace, values)
        try:
            # Covers every request, body read and any optional metadata lookup together.
            async with asyncio.timeout(18):
                if marketplace == "kaufland":
                    return await self._kaufland(credentials)
                return await self._worten(credentials)
        except TimeoutError:
            raise ConnectionProbeError("timeout") from None

    async def _kaufland(self, credentials: dict[str, str]) -> VerifiedMetadata:
        url = f"{KAUFLAND_URL}/info/storefront"
        headers = kaufland_headers(url, credentials, str(int(time.time())))
        result = await self.transport(url, headers)
        raw = result.get("data")
        if not isinstance(raw, list):
            raise ConnectionProbeError("unexpected_response")
        storefronts = []
        for item in raw:
            if isinstance(item, dict):
                item = next((item[key] for key in ("storefront", "code", "id", "value", "name")
                             if item.get(key)), None)
            if not isinstance(item, str):
                raise ConnectionProbeError("unexpected_response")
            item = item.strip().lower()
            if not item or len(item) > 5 or not item.isascii() or not item.isalpha():
                raise ConnectionProbeError("unexpected_response")
            storefronts.append(item)
        # API says created storefronts regardless of status; do not label these enabled.
        return VerifiedMetadata(storefronts=list(dict.fromkeys(storefronts)))

    async def _worten(self, credentials: dict[str, str]) -> VerifiedMetadata:
        url = f"{WORTEN_URL}/offers?" + urlencode({"shop_id": credentials["shop_id"], "max": 1})
        headers = {
            "Authorization": credentials["api_key"], "Accept": "application/json",
            "User-Agent": "MarketplaceHub/1.0 (Worten credential check)",
        }
        result = await self.transport(url, headers)
        offers = result.get("offers")
        if (not isinstance(offers, list) or any(not isinstance(item, dict) for item in offers)
                or result.get("error") or result.get("errors")):
            raise ConnectionProbeError("unexpected_response")
        # A01 supplies the real identity. OF21 alone verifies access but supplies no
        # public shop name; unavailable optional metadata must never invent one.
        try:
            async with asyncio.timeout(4):
                account = await self.transport(
                    f"{WORTEN_URL}/account?" + urlencode({"shop_id": credentials["shop_id"]}),
                    headers,
                )
        except (ConnectionProbeError, TimeoutError):
            return VerifiedMetadata()
        shop_id = account.get("shop_id")
        if isinstance(shop_id, bool) or not isinstance(shop_id, str | int):
            return VerifiedMetadata()
        if str(shop_id) != credentials["shop_id"]:
            raise ConnectionProbeError("invalid_configuration")
        name = account.get("shop_name")
        if not isinstance(name, str) or not name.strip() or credentials["api_key"] in name:
            name = None
        return VerifiedMetadata(
            public_name=name.strip()[:200] if name else None, external_shop_id=str(shop_id),
        )
