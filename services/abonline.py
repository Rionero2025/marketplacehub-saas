from __future__ import annotations

import ipaddress
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from services.db import DATA_DIR,execute,now_iso

DEFAULT_GATEWAY="https://xml.ab.pl/gateway.php"
LIST_DIR=DATA_DIR/"price_lists"
PRODUCT_DETAIL_BATCH_SIZE=100
PUBLIC_IP_ENDPOINTS=(
    "https://api.ipify.org?format=json",
    "https://checkip.amazonaws.com/",
    "https://icanhazip.com/",
)
PUBLIC_IP_TIMEOUT=4
IP_RETRY_DELAY_SECONDS=1.0


class ABOnlineError(RuntimeError):
    def __init__(self,message: str,code: str="",ip_address: str="",
                 detected_public_ip: str="",previous_public_ip: str=""):
        super().__init__(message)
        self.code=str(code or "")
        self.ip_address=str(ip_address or "")
        self.detected_public_ip=str(detected_public_ip or "")
        self.previous_public_ip=str(previous_public_ip or "")
        self.ip_changed=bool(
            self.detected_public_ip and self.previous_public_ip and
            self.detected_public_ip!=self.previous_public_ip
        )



def _valid_public_ipv4(value: str) -> str:
    text=str(value or "").strip()
    try:
        address=ipaddress.ip_address(text)
    except ValueError:
        return ""
    if address.version!=4 or not address.is_global:
        return ""
    return str(address)


def _public_ip_from_response(content: bytes) -> str:
    text=content.decode("utf-8",errors="replace").strip()
    try:
        parsed=json.loads(text)
    except (json.JSONDecodeError,TypeError):
        parsed=None
    if isinstance(parsed,dict):
        for key in ("ip","query","address"):
            ip=_valid_public_ipv4(parsed.get(key,""))
            if ip:
                return ip
    match=re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b",text)
    return _valid_public_ipv4(match.group(0) if match else "")


def _fresh_url_opener(*,direct: bool=True):
    # A new opener avoids reusing stale proxy/session state after a network/IP change.
    # Direct mode is preferred for AB Online; system proxy is used only as fallback
    # when a direct connection cannot be established.
    handlers=[urllib.request.ProxyHandler({})] if direct else []
    return urllib.request.build_opener(*handlers)


def detect_public_ipv4(timeout: int=PUBLIC_IP_TIMEOUT, endpoints=PUBLIC_IP_ENDPOINTS) -> str:
    """Detect the public IPv4 currently used by this PC without persistent caching."""
    for endpoint in endpoints:
        separator="&" if "?" in endpoint else "?"
        request=urllib.request.Request(
            f"{endpoint}{separator}_mh={time.time_ns()}",
            headers={
                "User-Agent":"MarketplaceHub/1.0",
                "Accept":"application/json,text/plain,*/*",
                "Cache-Control":"no-cache, no-store, max-age=0",
                "Pragma":"no-cache",
                "Connection":"close",
            },
            method="GET",
        )
        for direct in (True,False):
            try:
                with _fresh_url_opener(direct=direct).open(request,timeout=timeout) as response:
                    ip=_public_ip_from_response(response.read(4096))
                if ip:
                    return ip
            except Exception:  # best effort: AB itself remains authoritative
                pass
    return ""

def _local_name(tag: str) -> str:
    return str(tag).split("}")[-1]


def _child(element: ET.Element,name: str) -> ET.Element | None:
    return next((item for item in element if _local_name(item.tag)==name),None)


def _text(element: ET.Element,name: str,default: str="") -> str:
    item=_child(element,name)
    return (item.text or "").strip() if item is not None else default


def _nested_text(element: ET.Element,parent: str,name: str,default: str="") -> str:
    container=_child(element,parent)
    return _text(container,name,default) if container is not None else default


def _node_value(element: ET.Element | None,name: str,default: str="") -> str:
    if element is None:
        return default
    child=_child(element,name)
    if child is not None and (child.text or "").strip():
        return (child.text or "").strip()
    return str(element.attrib.get(name,default) or "").strip()


def _normalized_name(value: str) -> str:
    ascii_value=unicodedata.normalize("NFKD",str(value or "")).encode("ascii","ignore").decode()
    return re.sub(r"[^a-z0-9]+","_",ascii_value.lower()).strip("_")


def _value_and_unit(value: str,unit: str="") -> tuple[float,str]:
    text=str(value or "").strip()
    detected_unit=str(unit or "").strip().lower()
    if not detected_unit:
        match=re.search(r"\b(kg|g|gram(?:s|mi)?|kilogram(?:s|mi)?|mm|cm|m)\b",text,re.I)
        if match:
            detected_unit=match.group(1).lower()
    number_match=re.search(r"[-+]?\d+(?:[.,]\d+)?",text.replace("\xa0"," "))
    return (_number(number_match.group(0)) if number_match else 0.0,detected_unit)


def _parameter_measurements(product: ET.Element) -> dict[str,tuple[float,str]]:
    """Extract logistical measures also when AB exposes them as parameters.

    Depending on account/language, the ``product`` response can represent
    measures as explicit tags, attributes, or generic parameter nodes whose
    name is for example ``Weight``, ``Waga`` or ``Package weight``.
    """
    aliases={
        "weight":{
            "weight","weight_g","weight_kg","gross_weight","gross_weight_kg",
            "net_weight","package_weight","package_weight_kg","waga","waga_kg",
            "waga_produktu","waga_brutto","peso","peso_kg",
        },
        "width":{"width","width_mm","package_width","szerokosc","szerokosc_mm"},
        "height":{"height","height_mm","package_height","wysokosc","wysokosc_mm"},
        "depth":{"depth","depth_mm","package_depth","length","length_mm",
                 "glebokosc","glebokosc_mm","dlugosc","dlugosc_mm"},
    }
    generic_tags={
        "parameter","param","attribute","attr","feature","property","spec",
        "specification","technical_parameter","technicalparam","item",
    }
    result={}
    for node in product.iter():
        tag=_normalized_name(_local_name(node.tag))
        labels=[tag]
        if tag in generic_tags:
            for key in ("name","label","key","code","title"):
                if node.attrib.get(key):
                    labels.append(_normalized_name(node.attrib[key]))
            for child_name in ("name","label","key","code","title"):
                label=_text(node,child_name)
                if label:
                    labels.append(_normalized_name(label))
        measure=next((
            kind for kind,names in aliases.items()
            if any(label in names for label in labels)
        ),None)
        if not measure:
            continue
        raw=(node.attrib.get("value") or node.attrib.get("data") or
             _text(node,"value") or _text(node,"data") or _text(node,"content") or
             (node.text or ""))
        unit=(node.attrib.get("unit") or node.attrib.get("uom") or
              node.attrib.get("measure") or _text(node,"unit") or _text(node,"uom"))
        value,detected_unit=_value_and_unit(raw,unit)
        if value>0 and measure not in result:
            result[measure]=(value,detected_unit or tag)
    return result


def _dimension_value(product: ET.Element,name: str) -> float:
    """Read an AB dimension represented either as a child or an attribute."""
    dimensions=_child(product,"dimensions")
    value=_node_value(dimensions,name)
    if not value:
        value=_node_value(product,name)
    parsed,unit=_value_and_unit(value)
    if parsed<=0:
        parsed,unit=_parameter_measurements(product).get(name,(0.0,""))
    if unit=="cm":
        return parsed*10
    if unit=="m":
        return parsed*1000
    return parsed


def _product_weight(product: ET.Element) -> tuple[float,float]:
    """Return AB product weight as ``(grams, kilograms)``.

    The documented ``dimensions/weight`` value is expressed in grams.  Older
    and alternate Gateway responses may expose the same value as an attribute,
    as a direct product child, or with an explicit kg/g field name.
    """
    dimensions=_child(product,"dimensions")
    candidates=[]
    if dimensions is not None:
        candidates.extend(
            item for item in dimensions.iter()
            if _local_name(item.tag).lower().replace("-","_") in {
                "weight","weight_g","weight_kg","gross_weight","gross_weight_kg"
            }
        )
        for name in ("weight","weight_g","weight_kg","gross_weight","gross_weight_kg"):
            if name in dimensions.attrib:
                candidates.append((name,dimensions.attrib[name],""))
    for item in product:
        normalized=_local_name(item.tag).lower().replace("-","_")
        if normalized in {"weight","weight_g","weight_kg","gross_weight","gross_weight_kg",
                          "package_weight","package_weight_kg"}:
            candidates.append(item)
    for name in ("weight","weight_g","weight_kg","gross_weight","gross_weight_kg",
                 "package_weight","package_weight_kg"):
        if name in product.attrib:
            candidates.append((name,product.attrib[name],""))

    for candidate in candidates:
        if isinstance(candidate,tuple):
            name,value,unit=candidate
        else:
            name=_local_name(candidate.tag).lower().replace("-","_")
            value=(candidate.text or candidate.attrib.get("value","")).strip()
            unit=(candidate.attrib.get("unit") or candidate.attrib.get("uom") or "").strip().lower()
        parsed=_number(value)
        if parsed<=0:
            continue
        kilograms=(name.endswith("_kg") or unit in {"kg","kilogram","kilograms"})
        weight_kg=parsed if kilograms else parsed/1000
        return round(weight_kg*1000,3),round(weight_kg,3)
    parsed,unit=_parameter_measurements(product).get("weight",(0.0,""))
    if parsed>0:
        kilograms=("_kg" in unit or unit in {"kg","kilogram","kilograms","kilogrammi"})
        weight_kg=parsed if kilograms else parsed/1000
        return round(weight_kg*1000,3),round(weight_kg,3)
    return 0.0,0.0


def _number(value,default: float=0.0) -> float:
    try:return float(str(value).strip().replace(",","."))
    except (TypeError,ValueError):return default


def _integer(value,default: int=0) -> int:
    return max(0,int(_number(value,float(default))))


def _safe_gateway_error(code: str,message: str) -> ABOnlineError:
    if str(code)=="3":
        return ABOnlineError(
            "AB Online: accesso negato. Verifica codice cliente AB, login, password, "
            "abilitazione del servizio XML Gateway.",
            code="3",
        )
    if str(code)=="59":
        ip_match=re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b",message or "")
        ip_address=ip_match.group(0) if ip_match else ""
        suffix=f" ({ip_address})" if ip_address else ""
        return ABOnlineError(
            f"AB Online non consente l'accesso XML dall'indirizzo corrente{suffix}.",
            code="59",ip_address=ip_address,
        )
    sanitized=re.sub(r"\[[^\]]*(?:p|pass)=[^\]]*\]","",message or "",flags=re.I).strip()
    return ABOnlineError(f"AB Online errore {code}: {sanitized or 'richiesta non riuscita'}",
                         code=str(code))


def _raise_if_gateway_error(content: bytes) -> None:
    sample=content[:131072]
    if b"<error" not in sample.lower():
        return
    try:
        root=ET.fromstring(content)
        error=next((item for item in root.iter() if _local_name(item.tag)=="error"),None)
        if error is not None:
            raise _safe_gateway_error(_text(error,"code"),_text(error,"message"))
    except ET.ParseError as parse_error:
        raise ABOnlineError(f"Risposta XML AB Online non valida: {parse_error}") from parse_error


def _raise_if_gateway_error_file(path: Path) -> None:
    with path.open("rb") as handle:
        sample=handle.read(131072)
    if b"<error" in sample.lower():
        _raise_if_gateway_error(path.read_bytes())


@dataclass
class ABOnlineClient:
    client_code: str
    login: str
    password: str
    gateway_url: str=DEFAULT_GATEWAY
    previous_public_ip: str=""

    def __post_init__(self):
        self.previous_public_ip=_valid_public_ipv4(self.previous_public_ip)
        self.current_public_ip=""
        self.gateway_reported_ip=""
        self.ip_checked_at=""

    @property
    def ip_changed(self) -> bool:
        return bool(
            self.current_public_ip and self.previous_public_ip and
            self.current_public_ip!=self.previous_public_ip
        )

    def refresh_public_ip(self) -> dict:
        # Never trust a value saved during an earlier session: recalculate it
        # from the active network before each AB verification/download.
        self.current_public_ip=detect_public_ipv4()
        self.ip_checked_at=now_iso()
        return {
            "public_ip":self.current_public_ip,
            "previous_public_ip":self.previous_public_ip,
            "ip_changed":self.ip_changed,
            "ip_checked_at":self.ip_checked_at,
        }

    def _payload(self,request_name: str,**parameters) -> bytes:
        if not self.client_code.strip() or not self.login.strip() or not self.password:
            raise ABOnlineError("AB Online richiede codice cliente, login e password.")
        data={"client":self.client_code.strip(),"login":self.login.strip(),"pass":self.password,
              "req":request_name}
        data.update({key:value for key,value in parameters.items() if value is not None})
        return urllib.parse.urlencode(data,doseq=True).encode("utf-8")

    def _request(self,request_name: str,**parameters) -> urllib.request.Request:
        return urllib.request.Request(
            self.gateway_url.strip() or DEFAULT_GATEWAY,
            data=self._payload(request_name,**parameters),
            headers={
                "User-Agent":"MarketplaceHub/1.0",
                "Content-Type":"application/x-www-form-urlencoded",
                "Accept":"application/xml,text/xml,*/*",
                "Cache-Control":"no-cache, no-store, max-age=0",
                "Pragma":"no-cache",
                "Connection":"close",
            },
            method="POST",
        )

    def _open(self,request_name: str,timeout: int=240,**parameters):
        request=self._request(request_name,**parameters)
        last_error=None
        # A fresh direct opener prevents urllib/environment proxy state from
        # keeping an obsolete outbound IP after the PC changes network.
        # The system-proxy opener is only a connectivity fallback.
        for direct in (True,False):
            try:
                return _fresh_url_opener(direct=direct).open(request,timeout=timeout)
            except urllib.error.HTTPError as error:
                detail=error.read().decode("utf-8",errors="replace")[:500]
                raise ABOnlineError(f"AB Online HTTP {error.code}: {detail}") from error
            except (urllib.error.URLError,TimeoutError,OSError) as error:
                last_error=error
        raise ABOnlineError(f"AB Online non raggiungibile: {last_error}") from last_error

    def _enrich_ip_error(self,error: ABOnlineError) -> ABOnlineError:
        self.gateway_reported_ip=error.ip_address
        if not self.ip_checked_at:
            self.refresh_public_ip()
        gateway_ip=error.ip_address
        detected=self.current_public_ip
        details=[]
        if gateway_ip:
            details.append(f"IP visto da AB Online: {gateway_ip}")
        if detected:
            details.append(f"IP pubblico rilevato ora dal PC: {detected}")
        if self.previous_public_ip and detected and self.previous_public_ip!=detected:
            details.append(f"IP precedente sostituito: {self.previous_public_ip}")
        if gateway_ip and detected and gateway_ip!=detected:
            details.append("Il collegamento sta passando attraverso un proxy o una VPN con un IP diverso")
        suffix=(" "+". ".join(details)+".") if details else ""
        return ABOnlineError(
            "AB Online non consente ancora l'accesso XML dall'indirizzo corrente."+suffix,
            code="59",ip_address=gateway_ip or detected,
            detected_public_ip=detected,previous_public_ip=self.previous_public_ip,
        )

    def request_bytes(self,request_name: str,**parameters) -> bytes:
        if not self.ip_checked_at:
            self.refresh_public_ip()
        for attempt in range(2):
            with self._open(request_name,**parameters) as response:
                content=response.read()
            if not content.strip():
                raise ABOnlineError("AB Online ha restituito una risposta vuota.")
            try:
                _raise_if_gateway_error(content)
            except ABOnlineError as error:
                if error.code=="59" and attempt==0:
                    self.gateway_reported_ip=error.ip_address
                    self.refresh_public_ip()
                    time.sleep(IP_RETRY_DELAY_SECONDS)
                    continue
                if error.code=="59":
                    raise self._enrich_ip_error(error) from error
                raise
            return content
        raise ABOnlineError("AB Online: richiesta non completata dopo il rinnovo IP.")

    def request_file(self,request_name: str,target: Path,progress=None,**parameters) -> Path:
        if not self.ip_checked_at:
            self.refresh_public_ip()
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_suffix(target.suffix+".part")
        try:
            for attempt in range(2):
                temporary.unlink(missing_ok=True)
                with self._open(request_name,**parameters) as response,temporary.open("wb") as output:
                    total=int(response.headers.get("Content-Length") or 0);downloaded=0
                    while True:
                        chunk=response.read(1024*1024)
                        if not chunk:break
                        output.write(chunk);downloaded+=len(chunk)
                        if progress:progress("download",downloaded,total)
                try:
                    _raise_if_gateway_error_file(temporary)
                except ABOnlineError as error:
                    if error.code=="59" and attempt==0:
                        self.gateway_reported_ip=error.ip_address
                        self.refresh_public_ip()
                        time.sleep(IP_RETRY_DELAY_SECONDS)
                        continue
                    if error.code=="59":
                        raise self._enrich_ip_error(error) from error
                    raise
                temporary.replace(target)
                return target
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        raise ABOnlineError("AB Online: download non completato dopo il rinnovo IP.")

    def exchange_rate(self,price_currency: str="PLN") -> float:
        # AB returns catalogue prices in PLN. The argument is retained only for
        # backward compatibility with credentials saved by earlier versions.
        return self._request_eur_exchange_rate()

    def _request_eur_exchange_rate(self) -> float:
        content=self.request_bytes("exchange_curr_rate",curr="EUR")
        root=ET.fromstring(content)
        rate=next((item.text for item in root.iter() if _local_name(item.tag)=="rate"),None)
        parsed=_number(rate)
        if parsed<=0:raise ABOnlineError("AB Online non ha restituito un cambio PLN/EUR valido.")
        return parsed

    def validate(self,price_currency: str="PLN") -> dict:
        # Refresh the network identity every time; no saved IP is reused.
        identity=self.refresh_public_ip()
        detected_rate=self._request_eur_exchange_rate()
        return {
            "ok":True,
            "message":"Connessione riuscita. I prezzi PLN saranno convertiti automaticamente in EUR.",
            "pln_per_eur":detected_rate,
            "detected_pln_per_eur":detected_rate,
            "gateway_reported_ip":self.gateway_reported_ip,
            **identity,
        }

    def download_catalog(self,target: Path,progress=None) -> Path:
        # ``mode=pricedata`` is the reduced price-oriented response and can
        # omit logistical fields.  products_all without that restriction
        # returns the full product record, including dimensions and weight.
        # Do not request the server-side cache: a new combination of parameters
        # is not necessarily cached yet and AB then returns error 22
        # ("request not cached") instead of generating the catalogue.
        return self.request_file(
            "products_all",target,progress=progress,withdesc="0",
            rels="0",
        )

    def download_prices_stocks(self,target: Path,progress=None) -> Path:
        return self.request_file("prices_stocks",target,progress=progress)

    def product_details(self,product_skus: list[str]) -> bytes:
        clean=[str(item).strip() for item in product_skus if str(item).strip()]
        if not clean:
            return b"<gateway><products/></gateway>"
        return self.request_bytes(
            "product",pid=";".join(clean),withdesc="1",ignore_missing="1"
        )


def parse_full_catalog(path: str | Path,price_currency: str="PLN",
                       pln_per_eur: float=1.0,progress=None) -> pd.DataFrame:
    if pln_per_eur<=0:
        raise ABOnlineError("Cambio PLN/EUR non valido: impossibile convertire i prezzi AB Online.")
    source=Path(path);records=[];processed=0
    for _,product in ET.iterparse(source,events=("end",)):
        if _local_name(product.tag)!="product" or _child(product,"abpn") is None:
            continue
        sku=_text(product,"abpn");ean=_text(product,"ean")
        names=[(item.text or "").strip() for item in product if _local_name(item.tag)=="name" and (item.text or "").strip()]
        image_nodes=[item for item in product.iter() if _local_name(item.tag)=="imageid"]
        main_image=next(((item.text or "").strip() for item in image_nodes if item.attrib.get("is_main")=="1"),"")
        if not main_image and image_nodes:main_image=(image_nodes[0].text or "").strip()
        original_price=_number(_text(product,"price"))
        cost=original_price/pln_per_eur
        dimension_fee_pln=_number(_text(product,"gab_fee"))
        weight_g,weight_kg=_product_weight(product)
        records.append({
            "product_id":_text(product,"id"),"sku":sku,"ean":ean,
            "name":names[0] if names else (_text(product,"vendpn") or sku),
            "manufacturer_sku":_text(product,"vendpn"),"producer_id":_text(product,"producer_id"),
            "group_id":_text(product,"groupid") or _text(product,"group_id"),
            "cost":round(cost,2),"cost_eur":round(cost,2),
            "cost_original":round(original_price,2),"cost_pln":round(original_price,2),
            "source_currency":"PLN","target_currency":"EUR","pln_per_eur":pln_per_eur,
            "quantity":_integer(_text(product,"instock")),"stock_abonline":_integer(_text(product,"instock")),
            "vat":_number(_text(product,"vat")),"size_class":_text(product,"size_class"),
            "dimension_fee_original":round(dimension_fee_pln,2),
            "dimension_fee_pln":round(dimension_fee_pln,2),
            "dimension_fee_eur":round(dimension_fee_pln/pln_per_eur,2),
            "width_mm":_dimension_value(product,"width"),
            "height_mm":_dimension_value(product,"height"),
            "depth_mm":_dimension_value(product,"depth"),
            "weight_g":weight_g,"weight_kg":weight_kg,
            "image_id":main_image,"energy_label_url":_text(product,"energy_label_url"),
            "shipping_cost":0.0,"total_cost":round(cost,2),
        })
        processed+=1
        if progress and processed%1000==0:progress("parse",processed,0)
        product.clear()
    if not records:raise ABOnlineError("Il catalogo AB Online non contiene prodotti leggibili.")
    return pd.DataFrame(records)


def parse_prices_stocks(path: str | Path) -> pd.DataFrame:
    records=[]
    for _,product in ET.iterparse(Path(path),events=("end",)):
        if _local_name(product.tag)!="product" or "abpn" not in product.attrib:
            continue
        records.append({
            "product_id":(product.text or "").strip(),"sku":product.attrib.get("abpn","").strip(),
            "ean":product.attrib.get("ean","").strip(),"manufacturer_sku":product.attrib.get("vend_pn","").strip(),
            "cost_original":_number(product.attrib.get("price")),"quantity":_integer(product.attrib.get("stock")),
            "vat":_number(product.attrib.get("vat")),"group_id":product.attrib.get("groupid","").strip(),
        })
        product.clear()
    if not records:raise ABOnlineError("AB Online non ha restituito prezzi e stock leggibili.")
    return pd.DataFrame(records)


def parse_product_measurements(content: bytes) -> pd.DataFrame:
    """Parse product detail responses used to enrich the bulk catalogue."""
    root=ET.fromstring(content);records=[]
    for product in root.iter():
        if _local_name(product.tag)!="product":
            continue
        sku=_text(product,"abpn") or str(product.attrib.get("abpn","")).strip()
        product_id=_text(product,"id") or str(product.attrib.get("id","")).strip()
        if not sku and not product_id:
            continue
        weight_g,weight_kg=_product_weight(product)
        records.append({
            "sku":sku,"product_id":product_id,
            "width_mm":_dimension_value(product,"width"),
            "height_mm":_dimension_value(product,"height"),
            "depth_mm":_dimension_value(product,"depth"),
            "weight_g":weight_g,"weight_kg":weight_kg,
        })
    return pd.DataFrame(records)


def enrich_catalog_measurements(client: ABOnlineClient,catalog: pd.DataFrame,
                                progress=None,batch_size: int=PRODUCT_DETAIL_BATCH_SIZE) -> pd.DataFrame:
    """Join weights/dimensions returned by AB's detailed ``product`` request."""
    result=catalog.copy()
    skus=result.get("sku",pd.Series("",index=result.index)).fillna("").astype(str).str.strip()
    pending=result.loc[skus.ne("")].copy()
    if "weight_kg" in pending:
        pending=pending[pd.to_numeric(pending["weight_kg"],errors="coerce").fillna(0).le(0)]
    requested=pending.get("sku",pd.Series(dtype=str)).drop_duplicates().tolist()
    total=len(requested);detail_frames=[]
    for start in range(0,total,max(1,int(batch_size))):
        batch=requested[start:start+max(1,int(batch_size))]
        parsed=parse_product_measurements(client.product_details(batch))
        if not parsed.empty:
            detail_frames.append(parsed)
        if progress:
            progress("details",min(start+len(batch),total),total)
    if not detail_frames:
        return result
    details=pd.concat(detail_frames,ignore_index=True)
    details["sku"]=details["sku"].fillna("").astype(str).str.strip()
    details=details[details["sku"].ne("")].drop_duplicates("sku",keep="last").set_index("sku")
    for column in ("width_mm","height_mm","depth_mm","weight_g","weight_kg"):
        if column not in result:
            result[column]=0.0
        mapped=skus.map(details[column])
        valid=pd.to_numeric(mapped,errors="coerce").fillna(0).gt(0)
        result.loc[valid,column]=mapped.loc[valid].astype(float)
    return result


def download_abonline_catalog(price_list_id: int,client_code: str,login: str,password: str,
                              gateway_url: str=DEFAULT_GATEWAY,price_currency: str="PLN",
                              progress=None) -> Path:
    client=ABOnlineClient(client_code,login,password,gateway_url)
    rate=client.exchange_rate("PLN")
    folder=LIST_DIR/str(price_list_id);folder.mkdir(parents=True,exist_ok=True)
    xml_path=client.download_catalog(folder/"abonline_products.xml",progress=progress)
    if progress:progress("parse",0,0)
    catalog=parse_full_catalog(xml_path,"PLN",rate,progress)
    catalog=enrich_catalog_measurements(client,catalog,progress)
    target=folder/"abonline_catalog.pkl";catalog.to_pickle(target)
    xml_path.unlink(missing_ok=True)
    execute("UPDATE price_lists SET local_path=?,file_format=?,last_download_at=? WHERE id=?",
            (str(target),"pkl",now_iso(),price_list_id))
    return target


def refresh_abonline_prices_stock(price_list_id: int,catalog_path: str | Path,
                                  client_code: str,login: str,password: str,
                                  gateway_url: str=DEFAULT_GATEWAY,price_currency: str="PLN",
                                  progress=None) -> Path:
    client=ABOnlineClient(client_code,login,password,gateway_url)
    rate=client.exchange_rate("PLN")
    folder=LIST_DIR/str(price_list_id);folder.mkdir(parents=True,exist_ok=True)
    xml_path=client.download_prices_stocks(folder/"abonline_prices_stocks.xml",progress=progress)
    updates=parse_prices_stocks(xml_path);catalog=pd.read_pickle(catalog_path).copy()
    for key in ("sku","ean"):
        catalog[key]=catalog.get(key,"").fillna("").astype(str).str.strip().str.replace(r"\.0$","",regex=True)
        updates[key]=updates[key].fillna("").astype(str).str.strip().str.replace(r"\.0$","",regex=True)
    by_sku=updates[updates["sku"]!=""].drop_duplicates("sku").set_index("sku")
    by_ean=updates[updates["ean"]!=""].drop_duplicates("ean").set_index("ean")
    original=catalog["sku"].map(by_sku["cost_original"] if not by_sku.empty else pd.Series(dtype=float))
    original=original.fillna(catalog["ean"].map(by_ean["cost_original"] if not by_ean.empty else pd.Series(dtype=float)))
    quantity=catalog["sku"].map(by_sku["quantity"] if not by_sku.empty else pd.Series(dtype=float))
    quantity=quantity.fillna(catalog["ean"].map(by_ean["quantity"] if not by_ean.empty else pd.Series(dtype=float)))
    available=original.notna()
    catalog.loc[available,"cost_original"]=original.loc[available]
    catalog.loc[available,"cost_pln"]=original.loc[available]
    converted=original/rate
    catalog.loc[available,"cost"]=converted.loc[available].round(2)
    catalog.loc[available,"cost_eur"]=converted.loc[available].round(2)
    shipping=(pd.to_numeric(catalog["shipping_cost"],errors="coerce").fillna(0)
              if "shipping_cost" in catalog else pd.Series(0.0,index=catalog.index))
    catalog.loc[available,"total_cost"]=(catalog.loc[available,"cost"]+shipping.loc[available]).round(2)
    catalog.loc[quantity.notna(),"quantity"]=quantity.loc[quantity.notna()].astype(int)
    catalog.loc[quantity.notna(),"stock_abonline"]=quantity.loc[quantity.notna()].astype(int)
    catalog["source_currency"]="PLN";catalog["target_currency"]="EUR";catalog["pln_per_eur"]=rate
    target=folder/"abonline_catalog.pkl";catalog.to_pickle(target)
    xml_path.unlink(missing_ok=True)
    execute("UPDATE price_lists SET local_path=?,file_format=?,last_download_at=? WHERE id=?",
            (str(target),"pkl",now_iso(),price_list_id))
    return target
