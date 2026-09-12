import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx

from marketplace_hub_core.publication.pricing import worten_csv


class RemoteFailure(Exception):
    def __init__(self, code, *, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


class PublicationConnector:
    def __init__(self, transport=None):
        self.transport = transport

    def request(
        self,
        marketplace,
        credentials,
        method,
        path,
        *,
        playground=False,
        params=None,
        payload=None,
        csv_rows=None,
    ):
        # Neither URLs nor authorization headers are supplied by the browser.
        required = ("client_key", "secret_key") if marketplace == "kaufland" else ("api_key",)
        if marketplace not in {"kaufland", "worten"} or any(
            not isinstance(credentials.get(key), str) or not credentials[key].strip()
            for key in required
        ):
            raise RemoteFailure("credentials_unavailable")
        base = (
            (
                "https://sellerapi-playground.kaufland.com/v2"
                if playground
                else "https://sellerapi.kaufland.com/v2"
            )
            if marketplace == "kaufland"
            else "https://marketplace.worten.pt/api"
        )
        url = base + path + ("?" + urlencode(params) if params else "")
        body = (
            ""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        headers = {"Accept": "application/json", "User-Agent": "MarketplaceHub/1.0"}
        if marketplace == "kaufland":
            stamp = str(int(time.time()))
            signature = hmac.new(
                credentials["secret_key"].encode(),
                f"{method}\n{url}\n{body}\n{stamp}".encode(),
                hashlib.sha256,
            ).hexdigest()
            headers.update(
                {
                    "Shop-Client-Key": credentials["client_key"],
                    "Shop-Timestamp": stamp,
                    "Shop-Signature": signature,
                    "Content-Type": "application/json",
                }
            )
        else:
            headers["Authorization"] = credentials["api_key"]
        options = {"content": body.encode()} if payload is not None else {}
        if csv_rows is not None:
            options = {"files": {"file": ("offers.csv", worten_csv(csv_rows), "text/csv")}}
        try:
            with httpx.Client(
                timeout=30, follow_redirects=False, trust_env=False, transport=self.transport
            ) as client:
                with client.stream(method, url, headers=headers, **options) as response:
                    status = response.status_code
                    if not 200 <= status < 300:
                        raise RemoteFailure(
                            f"HTTP_{status}", uncertain=method != "GET" and status >= 500
                        )
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1_000_000:
                            raise RemoteFailure("response_invalid", uncertain=method != "GET")
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (httpx.RequestError, ValueError, KeyError):
            raise RemoteFailure(
                "response_uncertain" if method != "GET" else "metadata_unavailable",
                uncertain=method != "GET",
            ) from None

    def options(self, marketplace, credentials, storefront, playground):
        if marketplace == "kaufland":
            result = {}
            for name, path, key in [
                ("shipping_groups", "/shipping-groups/", "id_shipping_group"),
                ("warehouses", "/warehouses/", "id_warehouse"),
            ]:
                values = self.request(
                    marketplace,
                    credentials,
                    "GET",
                    path,
                    playground=playground,
                    params={"storefront": storefront, "limit": 30},
                ).get("data", [])
                result[name] = [
                    {"id": str(v[key]), "name": str(v.get("name") or v[key])[:200]}
                    for v in values
                    if isinstance(v, dict) and key in v
                ][:100]
            return result
        result = {}
        for name, path, key in [
            ("logistic_classes", "/shipping/logistic_classes", "logistic_classes"),
            ("states", "/offers/states", "offer_states"),
        ]:
            values = self.request(marketplace, credentials, "GET", path).get(key, [])
            result[name] = [
                {"id": str(v["code"]), "name": str(v.get("label") or v["code"])[:200]}
                for v in values
                if isinstance(v, dict) and "code" in v
            ][:100]
        return result

    def send(self, marketplace, credentials, rules, payloads):
        if marketplace == "kaufland":
            data = self.request(
                marketplace,
                credentials,
                "POST",
                "/units/",
                playground=rules.playground,
                params={"storefront": rules.storefront},
                payload=payloads[0],
            )
            return "accepted", "accepted_by_api"
        data = self.request(
            marketplace,
            credentials,
            "POST",
            "/offers/imports",
            params={"import_mode": "NORMAL", "shop_id": credentials["shop_id"]},
            csv_rows=payloads,
        )
        import_id = data.get("import_id") or data.get("importId") or data.get("id")
        if not import_id or not str(import_id).isascii() or not str(import_id).isalnum():
            raise RemoteFailure("import_receipt_missing", uncertain=True)
        return "submitted", f"import_{str(import_id)[:80]}"
