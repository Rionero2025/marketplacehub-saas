from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

import pandas as pd

DEFAULT_HOST = "https://b2b.activeshop.com.pl"
DEFAULT_STORE_CODE = "B2B_PL_pl"
PRODUCT_LIST_INTERVAL = 6.2  # Official limit: 10 product-list requests / 60 seconds.


class ActiveShopAPIError(RuntimeError):
    pass


def _request_json(url: str, method: str = "GET", payload: dict | None = None,
                  token: str = "", timeout: int = 120):
    body=json.dumps(payload).encode("utf-8") if payload is not None else None
    headers={"Accept":"application/json","Content-Type":"application/json",
             "User-Agent":"MarketplaceHub/1.0 (+ActiveShop Diamond prices)"}
    if token:headers["Authorization"]=f"Bearer {token}"
    raw=b""
    for attempt in range(4):
        request=urllib.request.Request(url,data=body,headers=headers,method=method)
        try:
            with urllib.request.urlopen(request,timeout=timeout) as response:
                raw=response.read()
            break
        except urllib.error.HTTPError as error:
            detail=error.read().decode("utf-8",errors="replace")[:500]
            retryable=error.code==429 or 500<=error.code<600
            if retryable and attempt<3:
                try:wait=float(error.headers.get("Retry-After",0) or 0)
                except Exception:wait=0
                time.sleep(min(30.0,max(wait,2.0**attempt)))
                continue
            raise ActiveShopAPIError(f"ActiveShop HTTP {error.code}: {detail or error.reason}") from error
        except Exception as error:
            if attempt<3:
                time.sleep(2.0**attempt);continue
            raise ActiveShopAPIError(f"Connessione ActiveShop non riuscita: {error}") from error
    try:return json.loads(raw.decode("utf-8"))
    except Exception as error:raise ActiveShopAPIError("ActiveShop ha restituito una risposta non JSON.") from error


def create_token(username: str, password: str, host: str = DEFAULT_HOST,
                 store_code: str = DEFAULT_STORE_CODE) -> str:
    if not username.strip() or not password:
        raise ActiveShopAPIError("Inserisci username e password del Catalog API ActiveShop.")
    endpoint=f"{host.rstrip('/')}/rest/{urllib.parse.quote(store_code.strip())}/V1/integration/customer/token"
    result=_request_json(endpoint,"POST",{"username":username.strip(),"password":password})
    token=str(result or "").strip().strip('"')
    if not token:raise ActiveShopAPIError("ActiveShop non ha restituito il token cliente.")
    return token


def _product_list_url(host: str, store_code: str, page: int, page_size: int) -> str:
    query=urllib.parse.urlencode({
        "searchCriteria[current_page]":int(page),
        "searchCriteria[page_size]":int(page_size),
    })
    return f"{host.rstrip('/')}/rest/{urllib.parse.quote(store_code.strip())}/V1/catalogProducts/?{query}"


def validate_credentials(username: str, password: str, host: str = DEFAULT_HOST,
                         store_code: str = DEFAULT_STORE_CODE) -> dict:
    try:
        token=create_token(username,password,host,store_code)
        result=_request_json(_product_list_url(host,store_code,1,1),token=token)
        total=int(result.get("total_count",0)) if isinstance(result,dict) else 0
        items=result.get("items",[]) if isinstance(result,dict) else []
        currency=""
        if items:
            currency=str((items[0].get("extension_attributes") or {}).get("currency","")).upper()
        return {"ok":True,"message":f"Connessione ActiveShop riuscita: {total} prodotti disponibili"+
                (f", valuta {currency}." if currency else "."),"total_count":total,"currency":currency}
    except Exception as error:
        return {"ok":False,"message":str(error),"total_count":0,"currency":""}


def _custom_value(item: dict, attribute_code: str) -> str:
    for attribute in item.get("custom_attributes") or []:
        if str(attribute.get("attribute_code","")).lower()==attribute_code.lower():
            return str(attribute.get("value","") or "").strip()
    return ""


def fetch_diamond_prices(username: str, password: str, host: str = DEFAULT_HOST,
                         store_code: str = DEFAULT_STORE_CODE,
                         progress: Callable[[int,int,int],None] | None = None,
                         page_size: int = 100, min_interval: float = PRODUCT_LIST_INTERVAL) -> pd.DataFrame:
    """Fetch every customer-specific final price while respecting ActiveShop's list rate limit."""
    token=create_token(username,password,host,store_code)
    records=[];page=1;total_pages=1;last_request=0.0
    while page<=total_pages:
        elapsed=time.monotonic()-last_request
        if last_request and elapsed<min_interval:time.sleep(min_interval-elapsed)
        result=_request_json(_product_list_url(host,store_code,page,page_size),token=token)
        last_request=time.monotonic()
        if not isinstance(result,dict):raise ActiveShopAPIError("Formato lista prodotti ActiveShop non valido.")
        items=result.get("items") or []
        total_count=max(0,int(result.get("total_count",len(items)) or 0))
        total_pages=max(1,math.ceil(total_count/page_size))
        for item in items:
            extension=item.get("extension_attributes") or {}
            try:final_price=float(extension.get("final_price"))
            except (TypeError,ValueError):final_price=float("nan")
            records.append({
                "sku":str(item.get("sku","") or "").strip(),
                "ean":_custom_value(item,"ean"),
                "diamond_price":final_price,
                "diamond_currency":str(extension.get("currency","") or "").strip().upper(),
                "diamond_status":bool(item.get("status",True)),
                "diamond_updated_at":str(item.get("updated_at","") or ""),
            })
        if progress:progress(page,total_pages,len(records))
        if not items or page>=total_pages:break
        page+=1
    frame=pd.DataFrame.from_records(records)
    if frame.empty:raise ActiveShopAPIError("Il Catalog API ActiveShop non ha restituito prodotti.")
    return frame
