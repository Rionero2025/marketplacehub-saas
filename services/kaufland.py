from __future__ import annotations

import hashlib,hmac,json,random,threading,time,urllib.error,urllib.parse,urllib.request
from dataclasses import dataclass
from typing import Callable


class KauflandError(RuntimeError): pass


_RATE_LOCK = threading.Lock()
_RATE_NEXT_REQUEST: dict[str, float] = {}


def _shared_rate_limit(client_key: str, playground: bool, requests_per_second: float) -> None:
    """Spread Kaufland requests across the process instead of sending bursts.

    Kaufland applies one rate limit across all endpoints of the same seller.
    Streamlit can create multiple client instances during reruns, therefore the
    limiter must be shared by every instance and not stored on the client only.
    """
    rate = max(1.0, float(requests_per_second or 1.0))
    interval = 1.0 / rate
    key = f"{'playground' if playground else 'production'}:{client_key}"
    with _RATE_LOCK:
        now = time.monotonic()
        slot = max(now, _RATE_NEXT_REQUEST.get(key, now))
        _RATE_NEXT_REQUEST[key] = slot + interval
    delay = slot - time.monotonic()
    if delay > 0:
        time.sleep(delay)


@dataclass
class KauflandClient:
    client_key:str
    secret_key:str
    playground:bool=False
    before_request:Callable[[],None]|None=None
    request_timeout:float=45.0
    max_attempts:int=7
    requests_per_second:float=15.0

    @property
    def base_url(self):
        return "https://sellerapi-playground.kaufland.com/v2" if self.playground else "https://sellerapi.kaufland.com/v2"

    def request(self,method,path,params=None,payload=None):
        method=method.upper();url=f"{self.base_url}/{path.lstrip('/')}"
        # Kaufland uses repeated query parameters for multi-value options, for
        # example:
        #   ?embedded=optional_attributes&embedded=required_attributes
        # ``doseq=True`` is therefore mandatory.  Without it a list would be
        # encoded as its Python representation and the API would reject the
        # request with HTTP 400.
        if params:url=f"{url}?{urllib.parse.urlencode(params,doseq=True)}"
        body="" if payload is None else json.dumps(payload,separators=(",",":"),ensure_ascii=False)
        last_error=None
        attempts=max(1,int(self.max_attempts))
        timeout=max(3.0,float(self.request_timeout))
        for attempt in range(attempts):
            _shared_rate_limit(self.client_key,self.playground,self.requests_per_second)
            if self.before_request:self.before_request()
            ts=str(int(time.time()));signed="\n".join((method,url,body,ts))
            sig=hmac.new(self.secret_key.encode(),signed.encode(),hashlib.sha256).hexdigest()
            headers={"Shop-Client-Key":self.client_key,"Shop-Timestamp":ts,"Shop-Signature":sig,
                     "User-Agent":"MarketplaceHub/1.0","Accept":"application/json","Content-Type":"application/json"}
            req=urllib.request.Request(url,data=body.encode() if body else None,headers=headers,method=method)
            try:
                with urllib.request.urlopen(req,timeout=timeout) as res:content=res.read()
                return json.loads(content.decode()) if content else None
            except urllib.error.HTTPError as error:
                detail=error.read().decode(errors="replace")[:2000]
                last_error=KauflandError(f"HTTP {error.code}: {detail}")
                if error.code not in (429,500,502,503,504):raise last_error from error
                retry_after=error.headers.get("Retry-After","")
                if retry_after.replace(".","",1).isdigit():
                    delay=max(0.5,float(retry_after))
                elif error.code==429:
                    # A 429 means the shared seller-wide quota is saturated.
                    # Back off more aggressively than for an ordinary 5xx.
                    delay=min(30.0,1.0*(2**attempt))+random.uniform(0.05,0.35)
                else:
                    delay=min(12.0,0.5*(2**attempt))+random.uniform(0.02,0.15)
            except (urllib.error.URLError,TimeoutError) as error:
                last_error=KauflandError(f"Errore temporaneo di rete: {error}")
                delay=min(8.0,0.5*(2**attempt))
            if attempt<attempts-1:time.sleep(delay)
        raise last_error or KauflandError(f"Richiesta Kaufland non riuscita dopo {attempts} tentativi.")

    def ping(self):
        return self.request("GET","/status/ping")

    def shipping_groups(self,storefront):
        r=self.request("GET","/shipping-groups/",params={"storefront":storefront,"limit":30}) or {}
        return r.get("data",[])

    def warehouses(self,storefront):
        r=self.request("GET","/warehouses/",params={"storefront":storefront,"limit":30}) or {}
        return r.get("data",[])

    def vat_indicators(self,storefront):
        r=self.request(
            "GET","/vat-indicators/",
            params={"storefront":str(storefront or "").strip().lower(),"limit":100},
        ) or {}
        data=r.get("data",r) if isinstance(r,dict) else r
        return data if isinstance(data,list) else []


    def product_by_ean(self,ean,storefront):
        encoded_ean=urllib.parse.quote(str(ean).strip(),safe="")
        return self.request("GET",f"/products/ean/{encoded_ean}",params={"storefront":storefront})

    def product_by_ean_or_none(self,ean,storefront):
        try:
            return self.product_by_ean(ean,storefront)
        except KauflandError as error:
            if "HTTP 404:" in str(error):
                return None
            raise


    def buybox(self,id_product,storefront,condition="new",limit=10):
        return self.request("GET","/buybox",params={
            "id_product":int(id_product),
            "storefront":str(storefront).strip().lower(),
            "condition":condition,
            "limit":max(1,min(10,int(limit))),
        })

    def commission_rates(self,eans,storefront):
        values=list(dict.fromkeys(
            str(value or "").strip() for value in eans if str(value or "").strip()
        ))
        if not values:
            return {"data":[]}
        if len(values)>50:
            raise KauflandError(
                "La ricerca commissioni Kaufland accetta al massimo 50 EAN per richiesta."
            )
        return self.request(
            "POST","/info/commission-rates/lookup",
            params={"storefront":str(storefront).strip().lower()},
            payload={"eans":values},
        )

    def upsert(self,item,storefront):
        return self.request("POST","/units/",params={"storefront":storefront},payload=item)

    def patch_unit(self,id_unit,item,storefront):
        unit_id=int(id_unit)
        if unit_id<=0:
            raise KauflandError("L'identificativo unità Kaufland non è valido.")
        payload=dict(item or {})
        # Kaufland does not allow changing the product or id_offer of an
        # existing unit.  Those identifiers are used only for lookup.
        for key in ("id_product","ean","id_offer","storefront"):
            payload.pop(key,None)
        if not payload:
            raise KauflandError("Nessun campo aggiornabile fornito per la unità Kaufland.")
        return self.request(
            "PATCH",f"/units/{unit_id}/",
            params={"storefront":str(storefront or "").strip().lower()},
            payload=payload,
        )

    def _unit_for_offer(self,id_offer,storefront):
        expected=str(id_offer or "").strip()
        units=self.units(expected,storefront)
        exact=[
            unit for unit in units
            if str(unit.get("id_offer") or "").strip()==expected
            and unit.get("id_unit") not in (None,"")
        ]
        new_units=[
            unit for unit in exact
            if str(unit.get("condition") or "").strip().upper() in ("NEW","100")
        ]
        candidates=new_units or exact
        if not candidates:
            raise KauflandError(
                f"Nessuna unità Kaufland trovata per lo SKU {expected} in {storefront.upper()}."
            )
        if len(candidates)>1:
            raise KauflandError(
                f"Più unità Kaufland corrispondono allo SKU {expected} in "
                f"{storefront.upper()}; aggiornamento bloccato per sicurezza."
            )
        return int(candidates[0]["id_unit"])

    def update_offer_price(self,id_offer,storefront,listing_price):
        price=float(listing_price)
        if price<=0:
            raise KauflandError("Il nuovo prezzo deve essere maggiore di zero.")
        return self.update_unit_price(
            self._unit_for_offer(id_offer,storefront),storefront,price
        )

    def update_unit_price(self,id_unit,storefront,listing_price):
        price=float(listing_price)
        if price<=0:
            raise KauflandError("Il nuovo prezzo deve essere maggiore di zero.")
        unit_id=int(id_unit)
        if unit_id<=0:
            raise KauflandError("L'identificativo unità Kaufland non è valido.")
        result=self.request(
            "PATCH",f"/units/{unit_id}/",
            params={"storefront":str(storefront).strip().lower()},
            payload={"listing_price":int(round(price*100))},
        )
        return {"id_unit":unit_id,"result":result}

    def update_offer_minimum_price(self,id_offer,storefront,minimum_price):
        price=float(minimum_price)
        if price<=0:
            raise KauflandError(
                "Il nuovo prezzo più basso deve essere maggiore di zero."
            )
        return self.update_unit_minimum_price(
            self._unit_for_offer(id_offer,storefront),storefront,price
        )

    def update_unit_minimum_price(self,id_unit,storefront,minimum_price):
        price=float(minimum_price)
        if price<=0:
            raise KauflandError(
                "Il nuovo prezzo più basso deve essere maggiore di zero."
            )
        unit_id=int(id_unit)
        if unit_id<=0:
            raise KauflandError("L'identificativo unità Kaufland non è valido.")
        result=self.request(
            "PATCH",f"/units/{unit_id}/",
            params={"storefront":str(storefront).strip().lower()},
            payload={"minimum_price":int(round(price*100))},
        )
        return {"id_unit":unit_id,"result":result}

    def update_offer_handling_time(self,id_offer,storefront,handling_time):
        return self.update_unit_handling_time(
            self._unit_for_offer(id_offer,storefront),storefront,handling_time
        )

    def update_unit_handling_time(self,id_unit,storefront,handling_time):
        try:
            days=int(handling_time)
        except (TypeError,ValueError) as error:
            raise KauflandError(
                "I giorni di gestione devono essere un numero intero."
            ) from error
        if days<0:
            raise KauflandError("I giorni di gestione non possono essere negativi.")
        unit_id=int(id_unit)
        if unit_id<=0:
            raise KauflandError("L'identificativo unità Kaufland non è valido.")
        result=self.request(
            "PATCH",f"/units/{unit_id}/",
            params={"storefront":str(storefront).strip().lower()},
            payload={"handling_time":days},
        )
        return {"id_unit":unit_id,"result":result}

    def units(self,id_offer,storefront,embedded=""):
        params={"id_offer":id_offer,"storefront":storefront,"limit":100}
        if embedded:params["embedded"]=embedded
        r=self.request("GET","/units/",params=params) or {}
        return r.get("data",[])

    def unit(self,id_unit,storefront,embedded=""):
        unit_id=int(id_unit)
        if unit_id<=0:
            raise KauflandError("L'identificativo unità Kaufland non è valido.")
        params={"storefront":str(storefront).strip().lower()}
        embedded_value=str(embedded or "").strip().lower()
        if embedded_value=="product":embedded_value="products"
        if embedded_value:params["embedded"]=embedded_value
        response=self.request("GET",f"/units/{unit_id}/",params=params) or {}
        data=response.get("data",response) if isinstance(response,dict) else response
        if isinstance(data,list):
            data=next(
                (
                    item for item in data
                    if isinstance(item,dict)
                    and str(item.get("id_unit") or "")==str(unit_id)
                ),
                data[0] if data and isinstance(data[0],dict) else {},
            )
        if not isinstance(data,dict):
            return {}
        result=dict(data)
        embedded_payload=response.get("embedded",{}) if isinstance(response,dict) else {}
        products=embedded_payload.get("products") if isinstance(embedded_payload,dict) else None
        product_rows=[]
        if isinstance(products,list):
            product_rows=[item for item in products if isinstance(item,dict)]
        elif isinstance(products,dict):
            product_data=products.get("data")
            if isinstance(product_data,list):product_rows=[item for item in product_data if isinstance(item,dict)]
            elif products.get("id_product") not in (None,""):product_rows=[products]
            else:product_rows=[item for item in products.values() if isinstance(item,dict)]
        product_by_id={str(item.get("id_product") or "").strip():item for item in product_rows}
        product=product_by_id.get(str(result.get("id_product") or "").strip())
        if product is None and len(product_rows)==1:product=product_rows[0]
        if isinstance(product,dict):result["product"]=dict(product)
        return result

    def delete_offer(self,id_offer,storefront):
        units=self.units(id_offer,storefront);deleted=0
        for unit in units:
            self.request("DELETE",f"/units/{unit['id_unit']}/",params={"storefront":storefront});deleted+=1
        return deleted


    def storefronts(self):
        response=self.request("GET","/info/storefront") or {}
        data=response.get("data",response) if isinstance(response,dict) else response
        values=[]
        if isinstance(data,dict):
            nested=data.get("data")
            if isinstance(nested,list):
                data=nested
            else:
                data=list(data.values())
        if isinstance(data,list):
            for item in data:
                if isinstance(item,dict):
                    value=(item.get("storefront") or item.get("code") or item.get("id")
                           or item.get("value") or item.get("name"))
                else:
                    value=item
                code=str(value or "").strip().lower()
                if code and len(code)<=5:
                    values.append(code)
        return list(dict.fromkeys(values))

    def locales(self):
        """Return the product-data locales enabled for this seller account."""
        response=self.request("GET","/info/locale") or {}
        data=response.get("data",response) if isinstance(response,dict) else response
        if isinstance(data,dict):
            data=data.get("data") if isinstance(data.get("data"),list) else list(data.values())
        values=[]
        for item in data if isinstance(data,list) else []:
            if isinstance(item,dict):
                code=(item.get("locale") or item.get("code") or item.get("id")
                      or item.get("value") or item.get("name"))
                label=(item.get("label") or item.get("title") or item.get("name") or code)
                storefront=(item.get("storefront") or item.get("country") or "")
                raw=dict(item)
            else:
                code=item;label=item;storefront="";raw={"locale":item}
            token=str(code or "").strip()
            if token:
                values.append({"code":token,"label":str(label or token).strip(),
                               "storefront":str(storefront or "").strip().lower(),"raw":raw})
        unique={item["code"]:item for item in values}
        return list(unique.values())

    def categories_page(self,storefront,*,parent_id=None,query="",locale="",limit=100,offset=0):
        params={
            "storefront":str(storefront or "").strip().lower(),
            "limit":max(1,min(100,int(limit))),
            "offset":max(0,int(offset)),
        }
        if parent_id not in (None,""):
            params["id_parent"]=int(parent_id)
        if str(query or "").strip():
            params["q"]=str(query).strip()
        if str(locale or "").strip():
            params["locale"]=str(locale).strip()
        return self.request("GET","/categories/",params=params) or {}

    def all_categories(self,storefront,*,locale="",progress=None,max_categories=10000):
        """Download the official category catalogue with pagination.

        Current Kaufland API versions return the complete category collection
        when ``id_parent`` is omitted.  A tree-walk fallback is retained for
        accounts/versions that only expose child listings.
        """
        seen={};offset=0;limit=100;global_collection=False
        while len(seen)<max(1,int(max_categories)):
            response=self.categories_page(
                storefront,parent_id=None,locale=locale,limit=limit,offset=offset
            )
            page=response.get("data",[]) if isinstance(response,dict) else []
            if not isinstance(page,list):page=[]
            for item in page:
                if not isinstance(item,dict):continue
                category_id=(item.get("id_category") or item.get("id") or item.get("code"))
                token=str(category_id or "").strip()
                if token:seen[token]=dict(item)
            pagination=response.get("pagination",{}) if isinstance(response,dict) else {}
            try:total=int(pagination.get("total"))
            except (TypeError,ValueError):total=None
            if progress:progress(len(seen),total)
            offset+=len(page)
            if total is not None and total>len(page):global_collection=True
            if not page or (total is not None and offset>=total) or (total is None and len(page)<limit):break
        if not global_collection and len(seen)<=20:
            # Legacy/fallback behaviour: the unfiltered collection may expose
            # only top-level nodes. Walk every child list from root #1.
            seen={};queue=[1];queued={1}
            while queue and len(seen)<max(1,int(max_categories)):
                parent=queue.pop(0);offset=0
                while True:
                    response=self.categories_page(
                        storefront,parent_id=parent,locale=locale,limit=100,offset=offset
                    )
                    page=response.get("data",[]) if isinstance(response,dict) else []
                    if not isinstance(page,list):page=[]
                    for item in page:
                        if not isinstance(item,dict):continue
                        raw=dict(item)
                        category_id=(raw.get("id_category") or raw.get("id") or raw.get("code"))
                        token=str(category_id or "").strip()
                        if not token:continue
                        raw.setdefault("id_parent_category",parent)
                        seen[token]=raw
                        try:child_id=int(category_id)
                        except (TypeError,ValueError):continue
                        if child_id not in queued:
                            queue.append(child_id);queued.add(child_id)
                    pagination=response.get("pagination",{}) if isinstance(response,dict) else {}
                    try:total=int(pagination.get("total"))
                    except (TypeError,ValueError):total=None
                    offset+=len(page)
                    if progress:progress(len(seen),None)
                    if not page or (total is not None and offset>=total) or (total is None and len(page)<100):break
        seen.setdefault("1",{
            "id_category":1,"id_parent_category":0,"name":"root","title_singular":"Root",
            "level":0,"is_root":True,
        })
        return list(seen.values())

    def category(self,id_category,storefront,*,locale="",include_attributes=False):
        """Return a Kaufland category and, optionally, its official schema.

        The category endpoint expects every ``embedded`` value as a separate
        query parameter.  It is storefront-scoped; the product-data locale is
        not required to retrieve the category schema and is intentionally not
        sent here, avoiding validation errors on accounts where that parameter
        is not accepted by this endpoint.

        ``conditional_attributes`` was added after the original required and
        optional lists.  A compatibility retry without it is retained for
        playground/older endpoint variants, while preserving the original
        error if the fallback also fails.
        """
        params={"storefront":str(storefront or "").strip().lower()}
        if include_attributes:
            params["embedded"]=[
                "optional_attributes",
                "required_attributes",
                "conditional_attributes",
            ]
        try:
            return self.request("GET",f"/categories/{int(id_category)}/",params=params) or {}
        except KauflandError as error:
            if not include_attributes or "HTTP 400:" not in str(error):
                raise
            fallback=dict(params)
            fallback["embedded"]=["optional_attributes","required_attributes"]
            return self.request(
                "GET",f"/categories/{int(id_category)}/",params=fallback
            ) or {}

    def attributes(self,storefront,*,locale="",limit=100,offset=0):
        params={
            "storefront":str(storefront or "").strip().lower(),
            "limit":max(1,min(100,int(limit))),
            "offset":max(0,int(offset)),
        }
        if str(locale or "").strip():params["locale"]=str(locale).strip()
        return self.request("GET","/attributes/",params=params) or {}

    def all_attributes(self,storefront,*,locale=""):
        result=[];offset=0
        while True:
            response=self.attributes(storefront,locale=locale,limit=100,offset=offset)
            page=response.get("data",[]) if isinstance(response,dict) else []
            if not isinstance(page,list):page=[]
            result.extend(item for item in page if isinstance(item,dict))
            pagination=response.get("pagination",{}) if isinstance(response,dict) else {}
            try:total=int(pagination.get("total"))
            except (TypeError,ValueError):total=None
            offset+=len(page)
            if not page or (total is not None and offset>=total) or (total is None and len(page)<100):break
        return result

    def decide_category(self,item,storefront,*,locale=""):
        """Ask Kaufland to rank the five most likely product categories.

        The official guide documents ``POST /categories/decide/`` with a JSON
        body containing ``item.title``, ``item.description``,
        ``item.manufacturer`` and optional ``price``.  Storefront is sent first
        for country-specific classification; on a validation HTTP 400 the client
        retries once without query parameters, matching the bare documented
        example instead of failing the entire classification batch.
        """
        storefront_value=str(storefront or "").strip().lower()
        payload=item if isinstance(item,dict) and "item" in item else {"item":dict(item or {})}
        item_payload=payload.get("item") if isinstance(payload,dict) else None
        if not isinstance(item_payload,dict) or not any(str(value or "").strip() for value in item_payload.values()):
            raise KauflandError("Titolo o descrizione prodotto necessari per determinare la categoria Kaufland.")
        attempts=[{"storefront":storefront_value}] if storefront_value else []
        attempts.append({})
        last_error=None
        for params in attempts:
            try:
                return self.request(
                    "POST","/categories/decide/",params=params,payload=payload
                ) or {}
            except KauflandError as error:
                last_error=error
                if "HTTP 400:" not in str(error):
                    raise
        if last_error is not None:
            raise last_error
        return {}

    def put_product_data(self,item,storefront,*,locale=""):
        params={"storefront":str(storefront or "").strip().lower()}
        if str(locale or "").strip():params["locale"]=str(locale).strip()
        return self.request("PUT","/product-data/",params=params,payload=dict(item or {}))

    def product_data_status(self,ean,storefront,*,locale=""):
        params={"storefront":str(storefront or "").strip().lower()}
        if str(locale or "").strip():params["locale"]=str(locale).strip()
        encoded=urllib.parse.quote(str(ean or "").strip(),safe="")
        return self.request("GET",f"/product-data/status/{encoded}",params=params) or {}

    def units_page(self,storefront,limit=100,offset=0,embedded="products"):
        params={
            "storefront":str(storefront).strip().lower(),
            "limit":max(1,min(100,int(limit))),
            "offset":max(0,int(offset)),
        }
        embedded_value=str(embedded or "").strip().lower()
        if embedded_value=="product":
            embedded_value="products"
        if embedded_value:
            params["embedded"]=embedded_value
        return self.request("GET","/units/",params=params) or {}

    def all_units(self,storefront,embedded="products",progress=None):
        items=[];offset=0;limit=100;total=None
        while True:
            response=self.units_page(storefront,limit=limit,offset=offset,embedded=embedded)
            page=response.get("data",[]) if isinstance(response,dict) else []
            if not isinstance(page,list):page=[]
            product_rows=[]
            embedded_payload=response.get("embedded",{}) if isinstance(response,dict) else {}
            products=embedded_payload.get("products") if isinstance(embedded_payload,dict) else None
            if isinstance(products,list):
                product_rows=[item for item in products if isinstance(item,dict)]
            elif isinstance(products,dict):
                product_data=products.get("data")
                if isinstance(product_data,list):
                    product_rows=[item for item in product_data if isinstance(item,dict)]
                elif products.get("id_product") not in (None,""):
                    product_rows=[products]
                else:
                    product_rows=[item for item in products.values() if isinstance(item,dict)]
            product_by_id={
                str(item.get("id_product") or "").strip():item
                for item in product_rows if str(item.get("id_product") or "").strip()
            }
            only_product=product_rows[0] if len(product_rows)==1 else None
            for raw_item in page:
                if not isinstance(raw_item,dict):
                    continue
                item=dict(raw_item)
                if not isinstance(item.get("product"),dict):
                    product=product_by_id.get(str(item.get("id_product") or "").strip()) or only_product
                    if isinstance(product,dict):
                        item["product"]=dict(product)
                items.append(item)
            pagination=response.get("pagination",{}) if isinstance(response,dict) else {}
            try:total=int(pagination.get("total"))
            except (TypeError,ValueError):total=None
            if progress:
                progress(len(items),total)
            if not page:
                break
            if total is not None:
                if len(items)>=total:
                    break
            elif len(page)<limit:
                break
            offset+=len(page)
        return items

    def delete_unit(self,id_unit,storefront):
        unit_id=int(id_unit)
        if unit_id<=0:
            raise KauflandError("L'identificativo unità Kaufland non è valido.")
        return self.request(
            "DELETE",f"/units/{unit_id}/",
            params={"storefront":str(storefront).strip().lower()},
        )

    def tickets(self,status="",limit=30,offset=0,sort=""):
        params={
            "limit":max(1,min(30,int(limit))),
            "offset":max(0,int(offset)),
        }
        if str(status or "").strip():
            params["status"]=str(status).strip().lower()
        if str(sort or "").strip():
            params["sort"]=str(sort).strip()
        return self.request("GET","/tickets",params=params) or {}

    def ticket(self,id_ticket):
        ticket_id=urllib.parse.quote(str(id_ticket).strip(),safe="")
        return self.request("GET",f"/tickets/{ticket_id}") or {}

    def ticket_messages(self,id_ticket="",limit=30,offset=0,sort=""):
        params={
            "limit":max(1,min(30,int(limit))),
            "offset":max(0,int(offset)),
        }
        if str(id_ticket or "").strip():
            params["id_ticket"]=str(id_ticket).strip()
        if str(sort or "").strip():
            params["sort"]=str(sort).strip()
        return self.request("GET","/tickets/messages",params=params) or {}

    def order_unit(self,id_order_unit,embedded=""):
        unit_id=int(id_order_unit)
        if unit_id<=0:
            raise KauflandError("L'identificativo dell'unità ordine non è valido.")
        params={}
        if str(embedded or "").strip():
            params["embedded"]=str(embedded).strip()
        return self.request(
            "GET",f"/order-units/{unit_id}",params=params or None
        ) or {}

    def order_units(self,limit=100,offset=0,status=""):
        params={
            "limit":max(1,min(100,int(limit))),
            "offset":max(0,int(offset)),
        }
        if str(status or "").strip():
            params["status"]=str(status).strip()
        return self.request("GET","/order-units",params=params) or {}

    def orders(self,limit=100,offset=0):
        """Return the lightweight order manifest in API pages.

        Each item contains ``ts_created_iso`` and ``ts_units_updated_iso``.  The
        accounting synchronizer uses this manifest to request full details only
        for orders created or changed since the last successful run.
        """
        params={
            "limit":max(1,min(100,int(limit))),
            "offset":max(0,int(offset)),
        }
        return self.request("GET","/orders",params=params) or {}

    def order(self,id_order,embedded=""):
        order_id=urllib.parse.quote(str(id_order).strip(),safe="")
        params={}
        if str(embedded or "").strip():
            params["embedded"]=str(embedded).strip()
        return self.request("GET",f"/orders/{order_id}",params=params or None) or {}

    def shipping_addresses(self,*,order_ids=None,order_unit_ids=None):
        """Retrieve Kaufland shipping addresses by order or order-unit IDs.

        The official endpoint accepts comma-separated IDs and returns one address
        entry per order unit. Exactly one lookup strategy is sent per request.
        """
        orders=[str(value).strip() for value in (order_ids or []) if str(value).strip()]
        units=[str(value).strip() for value in (order_unit_ids or []) if str(value).strip()]
        if bool(orders)==bool(units):
            raise KauflandError(
                "Indica esclusivamente order_ids oppure order_unit_ids per gli indirizzi."
            )
        params={"order_ids":",".join(dict.fromkeys(orders))} if orders else {
            "order_unit_ids":",".join(dict.fromkeys(units))
        }
        return self.request("GET","/shipping-addresses",params=params) or {}

    def mark_order_unit_sent(self,id_order_unit,carrier_code,tracking_numbers):
        unit_id=int(id_order_unit)
        if unit_id<=0:
            raise KauflandError("L'identificativo dell'unità ordine non è valido.")
        carrier=str(carrier_code or "").strip()
        tracking=str(tracking_numbers or "").strip()
        if not carrier:
            raise KauflandError("Il codice corriere è obbligatorio.")
        payload={"carrier_code":carrier,"tracking_numbers":tracking}
        return self.request(
            "PATCH",f"/order-units/{unit_id}/send",payload=payload
        ) or {}

    def send_ticket_message(
        self,id_ticket,text,interim_notice=False,ticket_message_files=None
    ):
        ticket_id=urllib.parse.quote(str(id_ticket).strip(),safe="")
        payload={"text":str(text)}
        if interim_notice:
            payload["interim_notice"]=True
        if ticket_message_files:
            payload["ticket_message_files"]=list(ticket_message_files)
        return self.request(
            "POST",f"/tickets/{ticket_id}/messages",payload=payload
        ) or {}

    def close_ticket(self,id_ticket):
        ticket_id=urllib.parse.quote(str(id_ticket).strip(),safe="")
        return self.request("PATCH",f"/tickets/{ticket_id}/close") or {}

    def open_ticket(self,id_order_units,reason,message):
        unit_ids=[int(value) for value in id_order_units]
        if not unit_ids:
            raise KauflandError("Seleziona almeno un'unità ordine.")
        return self.request(
            "POST","/tickets",
            payload={
                "id_order_unit":unit_ids,
                "reason":str(reason).strip(),
                "message":str(message),
            },
        ) or {}
