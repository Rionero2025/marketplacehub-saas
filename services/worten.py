from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable

DEFAULT_API_URL = "https://marketplace.worten.pt/api"

WORTEN_OFFER_COLUMNS = [
    "sku","product-id","product-id-type","description","internal-description","price",
    "price-additional-info","quantity","min-quantity-alert","state","available-start-date",
    "available-end-date","logistic-class","favorite-rank","discount-price","discount-start-date",
    "discount-end-date","leadtime-to-ship","max-order-quantity","package-quantity","update-delete",
    "price[channel=WRT_ES_ONLINE]","discount-price[channel=WRT_ES_ONLINE]",
    "discount-start-date[channel=WRT_ES_ONLINE]","discount-end-date[channel=WRT_ES_ONLINE]",
    "price[channel=WRT_PT_ONLINE]","discount-price[channel=WRT_PT_ONLINE]",
    "discount-start-date[channel=WRT_PT_ONLINE]","discount-end-date[channel=WRT_PT_ONLINE]",
    "description-es","description-pt","ship-from-country-offer","package-length","package-width",
    "package-height","package-weight","package-fragile","unit-measurement","unit-price-es",
    "pvpr-es","pvpr-pt","unit-price-pt",
]


def _request(api_key: str, url: str, *, data: bytes | None = None, headers: dict | None = None) -> dict:
    request_headers={"Authorization":api_key.strip(),"Accept":"application/json",
                     "User-Agent":"MarketplaceHub/1.0 (Worten offers)"}
    request_headers.update(headers or {})
    request=urllib.request.Request(url,data=data,headers=request_headers,method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request,timeout=60) as response:
            raw=response.read().decode("utf-8",errors="replace")
            return json.loads(raw) if raw.strip() else {"http_status":int(response.status)}
    except urllib.error.HTTPError as error:
        detail=error.read(8000).decode("utf-8",errors="replace")
        raise RuntimeError(f"Worten HTTP {error.code}: {detail[:1000]}") from error


def get_offer_states(api_key: str, *, api_url: str = DEFAULT_API_URL) -> list[dict]:
    payload=_request(api_key,f"{api_url.strip().rstrip('/')}/offers/states")
    raw=(payload.get("offer_states") or payload.get("states") or []) if isinstance(payload,dict) else payload
    states=[]
    for item in raw:
        code=str(item.get("code") or item.get("state_code") or item.get("id") or "").strip()
        label=str(item.get("label") or item.get("name") or item.get("description") or code).strip()
        if code: states.append({"code":normalize_offer_state(code,label),"label":label})
    return states or [{"code":"11","label":"New"}]


def normalize_offer_state(code: str = "", label: str = "") -> str:
    """Convert the human Excel value for a new item to the Mirakl CSV code."""
    raw=str(code or "").strip()
    description=f"{raw} {label or ''}".strip().lower()
    if not raw or raw.lower() in ("new","new product","nuovo") or "new" in description:
        return "11"
    return raw


def get_logistic_classes(api_key: str, *, api_url: str = DEFAULT_API_URL) -> list[dict]:
    """SH31: return the tenant-specific logistic class codes and labels."""
    payload=_request(api_key,f"{api_url.strip().rstrip('/')}/shipping/logistic_classes")
    raw=(payload.get("logistic_classes") or payload.get("classes") or []) if isinstance(payload,dict) else payload
    classes=[]
    for item in raw or []:
        code=str(item.get("code") or item.get("id") or "").strip()
        label=str(item.get("label") or item.get("name") or item.get("description") or code).strip()
        if code: classes.append({"code":code,"label":label})
    return classes


def upload_offer_csv(api_key: str, csv_bytes: bytes, *, api_url: str = DEFAULT_API_URL,
                     shop_id: str = "", import_mode: str = "NORMAL") -> dict:
    """Upload an official Mirakl offer CSV through OF01.

    ``NORMAL`` and ``REPLACE`` are the valid Mirakl modes.  The Worten tenant
    currently parses ``import_mode`` as an unquoted request parameter.  Sending
    a JSON string in the multipart body (for example ``"NORMAL"`` including the
    quotation marks) is rejected as a literal invalid value.  Therefore the
    mode is sent as the bare query value ``import_mode=NORMAL`` while the body
    contains only the CSV file.  This is also compatible with older Mirakl OF01
    deployments used by Worten.
    """
    mode=str(import_mode or "NORMAL").strip().upper()
    if mode not in ("NORMAL","REPLACE"):
        raise ValueError(
            "Modalità import Worten non valida: usa NORMAL o REPLACE."
        )
    if not isinstance(csv_bytes,(bytes,bytearray)) or not csv_bytes:
        raise ValueError("Il file CSV Worten da importare è vuoto.")

    boundary=f"----MarketplaceHub{uuid.uuid4().hex}"
    body=b"".join((
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="offers.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode("utf-8"),
        bytes(csv_bytes),
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ))
    query={"import_mode":mode}
    if str(shop_id).strip():
        query["shop_id"]=str(shop_id).strip()
    url=(
        f"{api_url.strip().rstrip('/')}/offers/imports?"
        f"{urllib.parse.urlencode(query)}"
    )
    return _request(
        api_key,
        url,
        data=body,
        headers={"Content-Type":f"multipart/form-data; boundary={boundary}"},
    )


def list_offers(api_key: str, shop_id: str, *, api_url: str = DEFAULT_API_URL,
                page_size: int = 100) -> list[dict]:
    """Read every live shop offer through Mirakl OF21.

    The marketplace caps ``max`` at 100, therefore pagination must continue
    with ``offset`` until ``total_count`` (or an empty page) is reached.
    """
    if not str(api_key).strip() or not str(shop_id).strip():
        raise ValueError("API Key e Shop ID Worten sono obbligatori.")
    base=api_url.strip().rstrip("/")
    limit=max(1,min(100,int(page_size)))
    offset=0
    total_count=None
    result=[]
    seen=set()
    for _ in range(100_000):
        query=urllib.parse.urlencode({"shop_id":str(shop_id).strip(),"max":limit,"offset":offset})
        payload=_request(api_key,f"{base}/offers?{query}")
        page=payload.get("offers",[]) if isinstance(payload,dict) else []
        if total_count is None and isinstance(payload,dict):
            try:total_count=int(payload.get("total_count"))
            except (TypeError,ValueError):total_count=None
        if not page:break
        for item in page:
            sku=str(item.get("shop_sku") or item.get("sku") or "").strip()
            if not sku or sku in seen:continue
            seen.add(sku)
            references = item.get("product_references") or []
            if isinstance(references, dict):
                references = [references]
            ean = str(item.get("product_id") or "").strip()
            if not ean:
                for reference in references:
                    if not isinstance(reference, dict):
                        continue
                    reference_type = str(
                        reference.get("reference_type")
                        or reference.get("type")
                        or ""
                    ).upper()
                    reference_value = str(
                        reference.get("reference")
                        or reference.get("value")
                        or ""
                    ).strip()
                    if reference_value and (
                        not reference_type
                        or "EAN" in reference_type
                        or "GTIN" in reference_type
                    ):
                        ean = reference_value
                        break
            shop = item.get("shop") or {}
            if not isinstance(shop, dict):
                shop = {}
            category = (
                item.get("category")
                or item.get("product_category")
                or item.get("catalog_category")
                or {}
            )
            if not isinstance(category, dict):
                category = {"code": category}
            result.append({
                "sku":sku,
                "ean":ean,
                "name":str(item.get("product_title") or item.get("description") or "").strip(),
                "quantity":item.get("quantity",0),
                "price":item.get("price",0),
                "state":str(item.get("state_code") or item.get("state") or "").strip(),
                "active":item.get("active",True),
                "product_sku":str(item.get("product_sku") or "").strip(),
                "category_code":str(
                    item.get("category_code")
                    or category.get("code")
                    or category.get("category_code")
                    or category.get("id")
                    or ""
                ).strip(),
                "category_label":str(
                    item.get("category_label")
                    or category.get("label")
                    or category.get("category_label")
                    or category.get("name")
                    or ""
                ).strip(),
                "offer_id":_clean_identifier(item.get("offer_id")),
                "shop_id":_clean_identifier(
                    item.get("shop_id")
                    or shop.get("id")
                    or shop.get("shop_id")
                    or shop_id
                ),
                "shop_name":str(
                    item.get("shop_name")
                    or shop.get("name")
                    or shop.get("shop_name")
                    or ""
                ).strip(),
                "channels":item.get("channels") or [],
                "shipping_price":_number(
                    item.get("min_shipping_price")
                    if item.get("min_shipping_price") is not None
                    else item.get("shipping_price")
                ),
                "total_price":_number(item.get("total_price")),
            })
        offset+=len(page)
        if total_count is not None and offset>=total_count:break
        if len(page)<limit:break
    return result


def list_orders(
    api_key: str,
    shop_id: str,
    *,
    offer_ids: Iterable[str],
    api_url: str = DEFAULT_API_URL,
    page_size: int = 100,
) -> list[dict]:
    """OR11: list every order linked to at most 100 Mirakl offer IDs."""
    clean_offer_ids = list(
        dict.fromkeys(
            _clean_identifier(value)
            for value in offer_ids
            if _clean_identifier(value)
        )
    )
    if not clean_offer_ids:
        return []
    if len(clean_offer_ids) > 100:
        raise ValueError("OR11 accetta al massimo 100 offer_id per richiesta.")
    if not str(api_key).strip() or not str(shop_id).strip():
        raise ValueError("API Key e Shop ID Worten sono obbligatori.")
    base = api_url.strip().rstrip("/")
    limit = max(1, min(100, int(page_size)))
    offset = 0
    total_count = None
    result = []
    seen = set()
    for _ in range(100_000):
        query = urllib.parse.urlencode(
            {
                "shop_id": str(shop_id).strip(),
                "offer_ids": ",".join(clean_offer_ids),
                "max": limit,
                "offset": offset,
                "sort": "dateCreated",
                "order": "desc",
            }
        )
        payload = _request(api_key, f"{base}/orders?{query}")
        page = payload.get("orders", []) if isinstance(payload, dict) else []
        if total_count is None and isinstance(payload, dict):
            try:
                total_count = int(payload.get("total_count"))
            except (TypeError, ValueError):
                total_count = None
        if not page:
            break
        for order in page:
            if not isinstance(order, dict):
                continue
            order_id = _clean_identifier(
                _value(order, "order_id", "commercial_id", "id", default="")
            )
            key = order_id or json.dumps(order, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            result.append(order)
        offset += len(page)
        if total_count is not None and offset >= total_count:
            break
        if len(page) < limit:
            break
    return result


def _number(value, default: float | None = None) -> float | None:
    """Return a numeric Mirakl value without depending on one response version."""
    if isinstance(value, dict):
        for key in (
            "amount", "price", "value", "unit_price", "unitPrice",
            "shipping_price", "shippingPrice", "rate", "percentage",
            "percent", "commission_rate",
        ):
            if key in value:
                return _number(value.get(key), default)
        return default
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", ".").strip().rstrip("%").strip())
    except (TypeError, ValueError):
        return default


def _value(item: dict, *keys, default=None):
    """Read either snake_case or kebab-case Mirakl fields."""
    if not isinstance(item, dict):
        return default
    normalized = {
        str(key).lower().replace("-", "_"): value for key, value in item.items()
    }
    for key in keys:
        candidate = normalized.get(str(key).lower().replace("-", "_"))
        if candidate is not None:
            return candidate
    return default


def _commissionable_breakdown_amount(value) -> float | None:
    if not isinstance(value, dict):
        return None
    parts = _value(value, "parts", default=[]) or []
    amounts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if _value(part, "commissionable", default=False) is not True:
            continue
        amount = _number(_value(part, "amount"))
        if amount is not None:
            amounts.append(amount)
    return sum(amounts) if amounts else None


def commission_rate_from_order_line(line: dict) -> dict | None:
    """Extract the effective commission percentage returned by Mirakl OR11."""
    if not isinstance(line, dict):
        return None
    direct_rate = _number(
        _value(
            line,
            "commission_rate",
            "commission_percentage",
            "commission_fee_rate",
        )
    )
    if direct_rate is not None and 0 <= direct_rate <= 100:
        return {
            "rate": round(direct_rate, 4),
            "fee": _number(_value(line, "commission_fee")),
            "base": None,
            "method": "commission_rate",
        }

    fee = _number(_value(line, "commission_fee"))
    if fee is None:
        total_commission = _number(_value(line, "total_commission"))
        commission_vat = _number(_value(line, "commission_vat"), 0.0) or 0.0
        if total_commission is not None:
            fee = total_commission - commission_vat
    if fee is None:
        return None

    price_base = _commissionable_breakdown_amount(
        _value(line, "price_amount_breakdown", default={})
    )
    shipping_base = _commissionable_breakdown_amount(
        _value(line, "shipping_price_amount_breakdown", default={})
    )
    breakdown_values = [
        value for value in (price_base, shipping_base) if value is not None
    ]
    base = sum(breakdown_values) if breakdown_values else None
    if base is None:
        base = _number(_value(line, "total_price"))
    if base is None:
        price = _number(_value(line, "price"), 0.0) or 0.0
        shipping = _number(_value(line, "shipping_price"), 0.0) or 0.0
        base = price + shipping
    if base <= 0:
        return None
    rate = fee / base * 100
    if not 0 <= rate <= 100:
        return None
    return {
        "rate": round(rate, 4),
        "fee": fee,
        "base": base,
        "method": "commission_fee/base",
    }


def build_commission_rate_index(orders: Iterable[dict]) -> dict:
    """Build newest-first commission lookups from OR11 order lines."""
    entries = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        order_date = str(
            _value(
                order,
                "last_updated_date",
                "created_date",
                "date_created",
                default="",
            )
            or ""
        )
        for line in _value(order, "order_lines", default=[]) or []:
            rate = commission_rate_from_order_line(line)
            if rate is None:
                continue
            entries.append(
                {
                    **rate,
                    "offer_id": _clean_identifier(
                        _value(line, "offer_id", default="")
                    ),
                    "offer_sku": _clean_identifier(
                        _value(line, "offer_sku", "shop_sku", default="")
                    ),
                    "category_code": str(
                        _value(line, "category_code", default="") or ""
                    ).strip(),
                    "category_label": str(
                        _value(line, "category_label", default="") or ""
                    ).strip(),
                    "order_id": _clean_identifier(
                        _value(order, "order_id", "commercial_id", default="")
                    ),
                    "observed_at": str(
                        _value(
                            line,
                            "last_updated_date",
                            "created_date",
                            default=order_date,
                        )
                        or order_date
                    ),
                }
            )
    entries.sort(key=lambda item: item.get("observed_at", ""), reverse=True)
    index = {"offer_id": {}, "offer_sku": {}, "category_code": {}}
    for entry in entries:
        for field in index:
            key = _match_identifier(entry.get(field))
            if key:
                index[field].setdefault(key, entry)
    return index


def resolve_order_commission(
    index: dict,
    *,
    offer_id: str = "",
    offer_sku: str = "",
    category_code: str = "",
) -> dict | None:
    """Resolve an API commission by exact offer, SKU, then category."""
    labels = {
        "offer_id": "API ordine Worten · offerta",
        "offer_sku": "API ordine Worten · SKU",
        "category_code": "API ordine Worten · categoria",
    }
    for field, value in (
        ("offer_id", offer_id),
        ("offer_sku", offer_sku),
        ("category_code", category_code),
    ):
        key = _match_identifier(value)
        entry = (index.get(field) or {}).get(key)
        if entry:
            return {**entry, "source": labels[field]}
    return None


def _category_identity(item: dict) -> tuple[str, str]:
    """Read a category code and label from the common Mirakl response shapes."""
    category = _value(
        item,
        "category",
        "product_category",
        "catalog_category",
        default={},
    )
    if isinstance(category, dict):
        code = _clean_identifier(
            _value(
                category,
                "code",
                "category_code",
                "id",
                "category_id",
                default="",
            )
        )
        label = str(
            _value(
                category,
                "label",
                "category_label",
                "name",
                "description",
                default="",
            )
            or ""
        ).strip()
    else:
        code = _clean_identifier(category)
        label = ""

    code = code or _clean_identifier(
        _value(
            item,
            "category_code",
            "category_id",
            "product_category_code",
            default="",
        )
    )
    label = label or str(
        _value(
            item,
            "category_label",
            "category_name",
            "product_category_label",
            default="",
        )
        or ""
    ).strip()
    if not code and _category_commission_rate(item) is not None:
        code = _clean_identifier(_value(item, "code", "id", default=""))
        label = label or str(
            _value(item, "label", "name", "description", default="") or ""
        ).strip()
    return code, label


def _category_commission_rate(item: dict) -> float | None:
    direct = _number(
        _value(
            item,
            "commission_rate",
            "commission_percentage",
            "commission_pct",
            "percentage",
            "rate",
        )
    )
    if direct is None:
        direct = _number(
            _value(
                item,
                "commission",
                "commission_fee",
                "fee",
                default=None,
            )
        )
    if direct is None or not 0 <= direct <= 100:
        return None
    return round(direct, 4)


def normalize_category_commissions(payload) -> list[dict]:
    """Normalize the category commission grid exposed by Worten/Mirakl.

    The platform-setting response differs between marketplace versions.  Some
    tenants return a flat list, while others return a category tree where a
    child inherits the rate configured on its parent.
    """
    resolved: dict[str, dict] = {}

    def visit(value, inherited_rate=None, inherited_path=()):
        if isinstance(value, list):
            for child in value:
                visit(child, inherited_rate, inherited_path)
            return
        if not isinstance(value, dict):
            return

        own_rate = _category_commission_rate(value)
        effective_rate = own_rate if own_rate is not None else inherited_rate
        code, label = _category_identity(value)
        if not code and effective_rate is not None:
            code = _clean_identifier(_value(value, "code", "id", default=""))
            label = label or str(
                _value(value, "label", "name", "description", default="") or ""
            ).strip()
        path = (*inherited_path, label or code) if (label or code) else inherited_path
        if code and effective_rate is not None:
            key = _match_identifier(code)
            entry = {
                "category_code": code,
                "category_label": label,
                "rate": effective_rate,
                "inherited": own_rate is None,
                "category_path": " > ".join(part for part in path if part),
            }
            previous = resolved.get(key)
            if previous is None or (
                previous.get("inherited") and not entry["inherited"]
            ):
                resolved[key] = entry

        child_rate = effective_rate
        child_path = path
        for key, child in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if normalized_key in {
                "commission",
                "commission_fee",
                "fee",
            }:
                continue
            if isinstance(child, (dict, list)):
                visit(child, child_rate, child_path)

    visit(payload)
    return list(resolved.values())


def list_category_commissions(
    api_key: str,
    *,
    api_url: str = DEFAULT_API_URL,
) -> list[dict]:
    """Read current commission percentages from the Worten category grid."""
    if not str(api_key).strip():
        raise ValueError("API Key Worten obbligatoria.")
    base = api_url.strip().rstrip("/")
    payload = _request(
        api_key,
        f"{base}/platform-setting/commission/category",
    )
    return normalize_category_commissions(payload)


def build_category_commission_index(commissions: Iterable[dict]) -> dict:
    index = {"category_code": {}, "category_label": {}}
    for entry in commissions or []:
        if not isinstance(entry, dict):
            continue
        code = _match_identifier(entry.get("category_code"))
        label = _match_name(entry.get("category_label"))
        if code:
            index["category_code"][code] = entry
        if label:
            index["category_label"][label] = entry
    return index


def resolve_category_commission(
    index: dict,
    *,
    category_code: str = "",
    category_label: str = "",
) -> dict | None:
    """Resolve the current commission for the exact OF21 product category."""
    code = _match_identifier(category_code)
    entry = (index.get("category_code") or {}).get(code)
    if entry is None:
        label = _match_name(category_label)
        entry = (index.get("category_label") or {}).get(label)
    if entry is None:
        return None
    return {
        **entry,
        "source": (
            "API Worten · categoria corrente"
            + (" (ereditata)" if entry.get("inherited") else "")
        ),
    }


def _offer_price(offer: dict) -> float | None:
    applicable = _value(offer, "applicable_pricing", default={}) or {}
    return _number(
        _value(
            offer,
            "price",
            "discount_price",
            default=_value(applicable, "price", "discount_price"),
        )
    )


def _offer_shipping(offer: dict) -> float:
    shipping = _value(offer, "shipping", default={}) or {}
    return float(
        _number(
            _value(
                offer,
                "min_shipping_price",
                "shipping_price",
                default=_value(shipping, "price", "shipping_price"),
            ),
            0.0,
        )
        or 0.0
    )


def _offer_total(offer: dict) -> float | None:
    total = _number(_value(offer, "total_price", "total"))
    if total is not None:
        return total
    price = _offer_price(offer)
    return None if price is None else price + _offer_shipping(offer)


def _clean_identifier(value) -> str:
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") else text


def _match_identifier(value) -> str:
    """Normalize Mirakl identifiers without changing their semantic content."""
    return unicodedata.normalize("NFKC", _clean_identifier(value)).casefold()


def _match_name(value) -> str:
    """Normalize a shop label returned with different casing/spacing."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def _product_ean(product: dict, requested: list[str], index: int) -> str:
    references = _value(
        product, "product_references", "references", "product_reference", default=[]
    )
    if isinstance(references, dict):
        references = [references]
    for reference in references or []:
        if not isinstance(reference, dict):
            continue
        kind = str(
            _value(reference, "type", "reference_type", "product_id_type", default="")
        ).upper()
        value = _clean_identifier(
            _value(reference, "value", "reference", "product_id", "id", default="")
        )
        if value and (not kind or "EAN" in kind or "GTIN" in kind):
            return value
    direct = _clean_identifier(
        _value(product, "ean", "product_ean", "product_id", "reference", default="")
    )
    if direct in requested:
        return direct
    # Mirakl normally preserves the order of the requested product references.
    return requested[index] if index < len(requested) else direct


def list_product_offers(
    api_key: str,
    eans: Iterable[str],
    shop_id: str,
    *,
    api_url: str = DEFAULT_API_URL,
    channel_code: str = "WRT_PT_ONLINE",
    all_channels: bool = False,
    include_inactive: bool = False,
) -> dict:
    """P11: read the Buy Box ordering for up to 100 EANs.

    Mirakl sorts P11 by ``bestPrice``: total price, premium information and
    shop grade.  The first visible active offer is therefore the Buy Box
    candidate for the requested channel.
    """
    clean_eans = list(
        dict.fromkeys(
            _clean_identifier(value)
            for value in eans
            if _clean_identifier(value)
        )
    )
    if not clean_eans:
        raise ValueError("Indica almeno un EAN da controllare.")
    if len(clean_eans) > 100:
        raise ValueError("P11 accetta al massimo 100 EAN per richiesta.")
    if not str(api_key).strip() or not str(shop_id).strip():
        raise ValueError("API Key e Shop ID Worten sono obbligatori.")
    if str(channel_code).strip().upper() != "WRT_PT_ONLINE":
        raise ValueError(
            "Worten è configurato esclusivamente per il canale Portogallo "
            "WRT_PT_ONLINE."
        )
    channel_code = "WRT_PT_ONLINE"
    query = {
        "product_references": ",".join(f"EAN|{ean}" for ean in clean_eans),
        "shop_id": str(shop_id).strip(),
        # P11 documents all_offers=false as active offers only.  The diagnostic
        # pass sets it to true so scheduled/inactive own offers can still be
        # identified without treating them as Buy Box candidates.
        "all_offers": "true" if include_inactive else "false",
        "sort": "bestPrice",
        "order": "asc",
        "all_channels": "true" if all_channels else "false",
    }
    if str(channel_code).strip() and not all_channels:
        query.update(
            {
                "channel_codes": str(channel_code).strip(),
                "pricing_channel_code": str(channel_code).strip(),
            }
        )
    elif str(channel_code).strip():
        # Keep channel-aware prices while intentionally disabling eligibility
        # filtering.  This diagnostic request reveals own offers that are active
        # in Mirakl but are not sellable on WRT_PT_ONLINE.
        query["pricing_channel_code"] = str(channel_code).strip()
    url = (
        f"{api_url.strip().rstrip('/')}/products/offers?"
        f"{urllib.parse.urlencode(query)}"
    )
    payload = _request(api_key, url)
    products = payload.get("products", []) if isinstance(payload, dict) else []
    normalized_products = []
    returned_eans = set()
    for product_index, product in enumerate(products or []):
        if not isinstance(product, dict):
            continue
        ean = _product_ean(product, clean_eans, product_index)
        returned_eans.add(ean)
        raw_offers = _value(product, "offers", default=[]) or []
        offers = []
        for rank, offer in enumerate(raw_offers, start=1):
            if not isinstance(offer, dict):
                continue
            shop = _value(offer, "shop", default={}) or {}
            offer_shop_id = _clean_identifier(
                _value(offer, "shop_id", default=_value(shop, "id", "shop_id"))
            )
            shop_name = str(
                _value(
                    offer,
                    "shop_name",
                    default=_value(shop, "name", "shop_name", default=""),
                )
                or ""
            ).strip()
            price = _offer_price(offer)
            shipping = _offer_shipping(offer)
            total = _offer_total(offer)
            offers.append(
                {
                    "rank": rank,
                    "offer_id": _clean_identifier(
                        _value(offer, "offer_id", "id", default="")
                    ),
                    "shop_id": offer_shop_id,
                    "shop_name": shop_name,
                    "shop_sku": _clean_identifier(
                        _value(offer, "shop_sku", "sku", default="")
                    ),
                    "price": price,
                    "shipping": shipping,
                    "total": total,
                    "currency": str(
                        _value(
                            offer,
                            "currency_iso_code",
                            "currency",
                            default="EUR",
                        )
                        or "EUR"
                    ).upper(),
                    "leadtime_to_ship": _number(
                        _value(offer, "leadtime_to_ship"), None
                    ),
                    "state_code": str(
                        _value(offer, "state_code", "offer_state_code", default="")
                        or ""
                    ),
                    "raw": offer,
                }
            )
        normalized_products.append(
            {
                "ean": ean,
                "product_sku": _clean_identifier(
                    _value(product, "product_sku", "sku", "id", default="")
                ),
                "category_code": _clean_identifier(
                    _value(
                        product,
                        "category_code",
                        "product_category_code",
                        default=_value(
                            _value(
                                product,
                                "category",
                                "product_category",
                                default={},
                            )
                            or {},
                            "code",
                            "category_code",
                            "id",
                            default="",
                        ),
                    )
                ),
                "category_label": str(
                    _value(
                        product,
                        "category_label",
                        "product_category_label",
                        default=_value(
                            _value(
                                product,
                                "category",
                                "product_category",
                                default={},
                            )
                            or {},
                            "label",
                            "category_label",
                            "name",
                            default="",
                        ),
                    )
                    or ""
                ).strip(),
                "offers": offers,
                "raw": product,
            }
        )
    # A missing product is meaningful and must be visible in the dashboard.
    for ean in clean_eans:
        if ean not in returned_eans:
            normalized_products.append(
                {
                    "ean": ean,
                    "product_sku": "",
                    "category_code": "",
                    "category_label": "",
                    "offers": [],
                    "raw": {},
                }
            )
    return {
        "products": normalized_products,
        "requested_eans": clean_eans,
        "raw": payload,
    }


def classify_product_buybox(
    product: dict,
    shop_id: str,
    own_sku: str = "",
    *,
    own_offer_id: str = "",
    own_shop_name: str = "",
) -> dict:
    """Classify one normalized P11 product from the seller's perspective."""
    expected_shop = _clean_identifier(shop_id)
    expected_sku = _clean_identifier(own_sku)
    expected_offer = _clean_identifier(own_offer_id)
    expected_name = str(own_shop_name or "").strip()
    expected_shop_match = _match_identifier(expected_shop)
    expected_sku_match = _match_identifier(expected_sku)
    expected_offer_match = _match_identifier(expected_offer)
    expected_name_match = _match_name(expected_name)
    offers = list(product.get("offers") or [])
    winner = offers[0] if offers else None
    own_offer = None
    own_match_source = ""
    for offer in offers:
        offer_shop = _clean_identifier(offer.get("shop_id"))
        offer_sku = _clean_identifier(offer.get("shop_sku"))
        offer_id = _clean_identifier(offer.get("offer_id"))
        offer_name = str(offer.get("shop_name") or "").strip()
        if (
            expected_offer_match
            and _match_identifier(offer_id) == expected_offer_match
        ):
            own_offer = offer
            own_match_source = "offer_id"
            break
        if (
            expected_shop_match
            and _match_identifier(offer_shop) == expected_shop_match
        ):
            own_offer = offer
            own_match_source = "shop_id"
            break
        if (
            expected_sku_match
            and _match_identifier(offer_sku) == expected_sku_match
        ):
            own_offer = offer
            own_match_source = "shop_sku"
            break
        if expected_name_match and _match_name(offer_name) == expected_name_match:
            own_offer = offer
            own_match_source = "shop_name"
            break
    if not offers:
        status = "Prodotto o offerte non trovati"
    elif own_offer is None:
        status = "Offerta propria non trovata"
    elif winner is own_offer:
        status = "Vinta" if len(offers) > 1 else "Vinta / unica visibile"
    else:
        status = "Persa"
    return {
        "ean": _clean_identifier(product.get("ean")),
        "product_sku": _clean_identifier(product.get("product_sku")),
        "status": status,
        "our_rank": own_offer.get("rank") if own_offer else None,
        "winner_shop_id": winner.get("shop_id", "") if winner else "",
        "winner_shop_name": winner.get("shop_name", "") if winner else "",
        "winner_price": winner.get("price") if winner else None,
        "winner_shipping": winner.get("shipping") if winner else None,
        "winner_total": winner.get("total") if winner else None,
        "our_price": own_offer.get("price") if own_offer else None,
        "our_shipping": own_offer.get("shipping") if own_offer else None,
        "our_total": own_offer.get("total") if own_offer else None,
        "currency": (
            (own_offer or winner or {}).get("currency") or "EUR"
        ),
        "offer_count": len(offers),
        "competitor_visible": any(
            offer is not own_offer
            for offer in offers
        ),
        "own_match_source": own_match_source,
        "details": {
            "offers": offers,
            "product_sku": product.get("product_sku", ""),
            "expected_shop_id": expected_shop,
            "expected_shop_sku": expected_sku,
            "expected_offer_id": expected_offer,
            "expected_shop_name": expected_name,
            "own_match_source": own_match_source,
        },
    }


def estimate_offer_price_rank(product: dict, own_total) -> dict:
    """Insert an OF21 own total into the visible P11 price ranking.

    Some Mirakl tenants omit the caller's own offer from P11 even when
    competitors are visible.  In that case the official winner still comes
    from P11, while this deterministic fallback reports the own price position
    among every returned offer total.
    """
    total = _number(own_total)
    if total is None:
        return {
            "rank": None,
            "tie_count": 0,
            "visible_offer_count": 0,
            "own_total": None,
        }
    visible_totals = [
        value
        for value in (
            _number(offer.get("total"))
            for offer in list(product.get("offers") or [])
            if isinstance(offer, dict)
        )
        if value is not None
    ]
    if not visible_totals:
        return {
            "rank": None,
            "tie_count": 0,
            "visible_offer_count": 0,
            "own_total": total,
        }
    tolerance = 0.005
    lower = sum(value < total - tolerance for value in visible_totals)
    ties = sum(abs(value - total) <= tolerance for value in visible_totals)
    return {
        "rank": lower + 1,
        "tie_count": ties,
        "visible_offer_count": len(visible_totals),
        "own_total": total,
    }


def position_cell_style(value) -> str:
    """Return the requested traffic-light style for the Buy Box position."""
    try:
        if value in (None, ""):
            return ""
        rank = int(float(value))
    except (TypeError, ValueError):
        return ""
    if rank == 1:
        return "background-color:#dcfce7;color:#166534;font-weight:700"
    if rank > 1:
        return "background-color:#fee2e2;color:#991b1b;font-weight:700"
    return ""


def position_cell_display(value) -> str:
    """Display a Buy Box rank as an integer without decimal places."""
    try:
        if value in (None, ""):
            return ""
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value or "")


def buybox_outcome(item: dict) -> str:
    """Normalize a saved Worten result into the four UI filter outcomes."""
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


def buybox_alignment_price(item: dict) -> float | None:
    """Return the product price that aligns our total with the winning total."""
    winner_total = _number(item.get("winner_total"))
    if winner_total is None:
        return None
    own_shipping = _number(item.get("our_shipping"), 0.0) or 0.0
    return round(max(0.01, winner_total - own_shipping), 2)


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


def build_price_update_offer_csv(
    updates: Iterable[dict],
    *,
    channel_code: str = "WRT_PT_ONLINE",
) -> bytes:
    """Build a partial OF01 import that updates only existing offer prices."""
    channel = str(channel_code or "").strip()
    column = f"price[channel={channel}]"
    if column not in WORTEN_OFFER_COLUMNS:
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
    written = 0
    for item in updates:
        sku = str(item.get("sku") or "").strip()
        price = _number(item.get("price"))
        if not sku or sku in seen or price is None or price <= 0:
            continue
        seen.add(sku)
        record = {name: "" for name in WORTEN_OFFER_COLUMNS}
        formatted_price = f"{float(price):.2f}"
        record.update(
            {
                "sku": sku,
                "price": formatted_price,
                column: formatted_price,
                "update-delete": "update",
            }
        )
        writer.writerow(record)
        written += 1
    if not written:
        raise ValueError("Nessun prezzo Worten valido da aggiornare.")
    return output.getvalue().encode("utf-8-sig")


def build_delete_offer_csv(offer_skus: list[str]) -> bytes:
    """Build an official Mirakl offer import that deletes exact shop SKUs."""
    output=io.StringIO(newline="")
    writer=csv.DictWriter(output,fieldnames=WORTEN_OFFER_COLUMNS,delimiter=";",lineterminator="\n")
    writer.writeheader()
    seen=set()
    for value in offer_skus:
        sku=str(value or "").strip()
        if not sku or sku in seen:continue
        seen.add(sku)
        record={column:"" for column in WORTEN_OFFER_COLUMNS}
        record.update({"sku":sku,"update-delete":"delete"})
        writer.writerow(record)
    if not seen:
        raise ValueError("Nessuno SKU Worten valido da cancellare.")
    return output.getvalue().encode("utf-8-sig")


def offer_import_status(api_key: str, import_id: str, *, api_url: str = DEFAULT_API_URL,
                        shop_id: str = "") -> dict:
    url=f"{api_url.strip().rstrip('/')}/offers/imports/{urllib.parse.quote(str(import_id))}"
    if str(shop_id).strip():
        url=f"{url}?{urllib.parse.urlencode({'shop_id':str(shop_id).strip()})}"
    return _request(api_key,url)


def validate_credentials(api_key: str, shop_id: str, api_url: str = DEFAULT_API_URL) -> dict:
    """Validate Worten/Mirakl credentials with a read-only offers request."""
    if not api_key.strip() or not shop_id.strip():
        return {"ok": False, "status": 0, "message": "API Key e Shop ID sono obbligatori."}
    base = (api_url or DEFAULT_API_URL).strip().rstrip("/")
    if not base.lower().startswith("https://"):
        return {"ok": False, "status": 0, "message": "L'URL API deve iniziare con https://"}
    query = urllib.parse.urlencode({"shop_id": shop_id.strip(), "max": 1})
    request = urllib.request.Request(
        f"{base}/offers?{query}",
        headers={
            "Authorization": api_key.strip(),
            "Accept": "application/json",
            "User-Agent": "MarketplaceHub/1.0 (Worten credential check)",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(500_000)
            payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            total = payload.get("total_count")
            if total is None and isinstance(payload.get("offers"), list):
                total = len(payload["offers"])
            return {
                "ok": True,
                "status": int(response.status),
                "message": "Credenziali valide e API Worten raggiungibile.",
                "offers_visible": total,
            }
    except urllib.error.HTTPError as error:
        body = error.read(4000).decode("utf-8", errors="replace")
        messages = {
            401: "API Key non valida o scaduta.",
            403: "Chiave riconosciuta ma senza permesso per leggere le offerte.",
            404: "Endpoint API non trovato: controlla l'URL Worten.",
        }
        return {"ok": False, "status": error.code,
                "message": messages.get(error.code, f"Worten ha risposto HTTP {error.code}."),
                "detail": body[:500]}
    except Exception as error:
        return {"ok": False, "status": 0,
                "message": f"Connessione non riuscita: {error}"}
