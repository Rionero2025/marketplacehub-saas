from __future__ import annotations

import io
import json
import os
import re
import ssl
import subprocess
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from services.db import DATA_DIR, execute, now_iso

LIST_DIR = DATA_DIR / "price_lists"


def safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    return value.strip("._") or "listino"


def save_uploaded(price_list_id: int, name: str, content: bytes) -> Path:
    folder = LIST_DIR / str(price_list_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / safe_name(name)
    path.write_bytes(content)
    execute("UPDATE price_lists SET local_path=?,file_format=?,last_download_at=? WHERE id=?",
            (str(path), path.suffix.lower().lstrip("."), now_iso(), price_list_id))
    return path


def _safe_error_text(error: BaseException) -> str:
    text=str(error)
    # Feed URLs often contain account tokens in the query string. Never echo
    # those values in Streamlit error messages or operation logs.
    text=re.sub(r"([?&](?:u|uc|token|key|api_key|apikey)=)[^&\s'\"]+",r"\1***",text,flags=re.I)
    return text


def _certificate_error(error: BaseException) -> bool:
    """Return True when an exception chain contains an SSL trust failure."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        text=f"{type(current).__name__}: {current}".lower()
        if ("certificate_verify_failed" in text or
                "certificate verify failed" in text or
                "unable to get local issuer certificate" in text):
            return True
        reason=getattr(current,"reason",None)
        cause=getattr(current,"__cause__",None) or getattr(current,"__context__",None)
        current=reason if isinstance(reason,BaseException) else cause
    return False


def _open_url(request: urllib.request.Request, timeout: int = 120):
    """Open HTTPS using verified TLS, retrying with the operating-system trust store.

    Force Top's btp.link exports can expose a certificate chain that the Python
    OpenSSL bundle cannot complete even though Windows trusts it.  Verification is
    never disabled: the retry uses ``truststore`` (Windows/macOS/Linux system CA)
    and, on Windows, the native Schannel-backed ``curl.exe`` client.
    """
    try:
        return urllib.request.urlopen(request,timeout=timeout),"python"
    except Exception as first_error:
        if not _certificate_error(first_error):
            raise

        truststore_error: Exception | None=None
        try:
            import truststore
            context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            return urllib.request.urlopen(request,timeout=timeout,context=context),"system-trust"
        except Exception as error:
            truststore_error=error

        if os.name=="nt":
            try:
                return _open_url_windows_curl(request,timeout),"windows-schannel"
            except Exception as curl_error:
                raise ValueError(
                    "Il certificato HTTPS del feed non è verificabile neppure tramite "
                    "l'archivio certificati di Windows. Non è stata disattivata la "
                    "sicurezza SSL. Chiedi al fornitore di pubblicare la catena completa "
                    "del certificato (certificato intermedio incluso). "
                    f"Dettaglio Python: {_safe_error_text(first_error)}. "
                    f"Dettaglio sistema: {_safe_error_text(truststore_error)}. "
                    f"Dettaglio Windows: {_safe_error_text(curl_error)}"
                ) from first_error
        raise ValueError(
            "Il certificato HTTPS del feed non è verificabile. Marketplace Hub ha "
            "provato sia il bundle Python sia l'archivio certificati del sistema, "
            "senza disattivare la verifica SSL. Il fornitore deve correggere o "
            "completare la catena del certificato. "
            f"Dettaglio: {_safe_error_text(first_error)}; "
            f"fallback: {_safe_error_text(truststore_error)}"
        ) from first_error


class _MemoryResponse:
    """Small response adapter used by the verified Windows curl fallback."""
    def __init__(self,content: bytes,headers,final_url: str):
        self._content=content;self.headers=headers;self._final_url=final_url
    def read(self) -> bytes:return self._content
    def geturl(self) -> str:return self._final_url
    def __enter__(self):return self
    def __exit__(self,*_):return False


def _open_url_windows_curl(request: urllib.request.Request, timeout: int = 120):
    import email.parser
    curl=os.path.join(os.environ.get("SystemRoot",r"C:\Windows"),"System32","curl.exe")
    if not Path(curl).exists():
        raise FileNotFoundError("curl.exe di Windows non trovato")
    with tempfile.TemporaryDirectory(prefix="marketplace_hub_tls_") as folder:
        body=Path(folder)/"body.bin";headers_file=Path(folder)/"headers.txt"
        command=[curl,"--fail","--location","--silent","--show-error",
                 "--connect-timeout","30","--max-time",str(max(30,int(timeout))),
                 "--user-agent","MarketplaceHub/1.0","--dump-header",str(headers_file),
                 "--output",str(body)]
        auth=request.get_header("Authorization")
        if auth:command.extend(["--header",f"Authorization: {auth}"])
        command.append(request.full_url)
        completed=subprocess.run(command,capture_output=True,text=True,check=False)
        if completed.returncode!=0:
            raise RuntimeError(completed.stderr.strip() or f"curl.exe codice {completed.returncode}")
        raw_headers=headers_file.read_text(encoding="iso-8859-1",errors="replace")
        # Keep only the last response block after redirects.
        blocks=[block for block in re.split(r"\r?\n\r?\n",raw_headers) if block.strip()]
        last=blocks[-1] if blocks else ""
        lines=last.splitlines()
        header_text="\n".join(lines[1:]) if lines and lines[0].startswith("HTTP/") else last
        headers=email.parser.Parser().parsestr(header_text)
        final_url=headers.get("Content-Location") or request.full_url
        return _MemoryResponse(body.read_bytes(),headers,final_url)


def download_url(price_list_id: int, url: str, username="", password="") -> Path:
    def fetch(target_url: str):
        request=urllib.request.Request(target_url,headers={"User-Agent":"MarketplaceHub/1.0"})
        if username:
            import base64
            token=base64.b64encode(f"{username}:{password}".encode()).decode()
            request.add_header("Authorization",f"Basic {token}")
        response,tls_mode=_open_url(request,timeout=120)
        with response:
            headers=response.headers
            try:headers["X-MarketplaceHub-TLS"]=tls_mode
            except Exception:pass
            return response.read(),headers,response.geturl()

    content,headers,resolved_url=fetch(url)
    content_type=headers.get("Content-Type","").lower()
    # Alcuni fornitori (es. Originalqu) restituiscono una pagina HTML con i veri feed.
    if "text/html" in content_type or content.lstrip().lower().startswith((b"<!doctype html",b"<html")):
        html=content.decode("utf-8",errors="replace")
        plain_text=re.sub(r"<[^>]+>"," ",html)
        plain_text=re.sub(r"\s+"," ",plain_text).strip()
        if "access denied" in plain_text.lower():
            query=urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            requested_type=str(query.get("type",[""])[0]).strip().upper()
            if requested_type=="LIGHT":
                raise ValueError(
                    "Hurtel ha rifiutato il feed LIGHT (Access denied). Il token del FULL "
                    "non autorizza automaticamente il LIGHT: genera o richiedi a Hurtel "
                    "l’URL LIGHT completo, normalmente dotato di un token dedicato, e "
                    "incollalo nel campo «URL feed LIGHT — prezzo ingrosso e stock»."
                )
            raise ValueError(
                f"Il fornitore ha rifiutato l’accesso al feed {requested_type or 'richiesto'} "
                "(Access denied). Controlla URL, token e autorizzazioni."
            )
        links=re.findall(r'href=["\']([^"\']+)["\']',html,flags=re.I)
        feed_links=[urllib.parse.urljoin(resolved_url,x) for x in links if "/api/feed/" in x]
        xml_links=[x for x in feed_links if not x.rstrip("/").endswith(("/txt","/json"))]
        chosen=(xml_links or feed_links)
        if not chosen:
            raise ValueError("L’URL restituisce una pagina HTML ma non contiene un collegamento a un feed leggibile.")
        content,headers,resolved_url=fetch(chosen[0])
        content_type=headers.get("Content-Type","").lower()

    disposition=headers.get("Content-Disposition","")
    filename=Path(urllib.parse.urlparse(resolved_url).path).name or "listino"
    match=re.search(r'filename="?([^";]+)',disposition)
    if match: filename=match.group(1)
    suffix=Path(filename).suffix.lower()
    sample=content.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")[:200].lower()
    if suffix not in (".xml",".csv",".txt",".tsv",".xlsx",".xls",".json",".pkl",".pickle"):
        if sample.startswith((b"<?xml",b"<products",b"<offer")) or "xml" in content_type:filename += ".xml"
        elif sample.startswith((b"{",b"[")) or "json" in content_type:filename += ".json"
        elif sample.startswith(b"pk\x03\x04") or "spreadsheetml" in content_type:filename += ".xlsx"
        elif content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or "ms-excel" in content_type:filename += ".xls"
        elif "csv" in content_type or "text/plain" in content_type:filename += ".csv"
        else:raise ValueError(f"Formato del feed non riconosciuto (Content-Type: {content_type or 'assente'}).")
    return save_uploaded(price_list_id,filename,content)


def hurtel_feed_urls(primary_url: str, secondary_url: str = "") -> tuple[str,str]:
    """Return the Hurtel full and light URLs, deriving the missing counterpart."""
    def feed_type(url: str) -> str:
        query=urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return str(query.get("type",[""])[0]).strip().lower()

    def with_type(url: str, value: str) -> str:
        parsed=urllib.parse.urlparse(url)
        query=urllib.parse.parse_qsl(parsed.query,keep_blank_values=True)
        replaced=False;updated=[]
        for key,current in query:
            if key.lower()=="type":
                updated.append((key,value));replaced=True
            else:
                updated.append((key,current))
        if not replaced:updated.append(("type",value))
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(updated)))

    first=str(primary_url or "").strip();second=str(secondary_url or "").strip()
    if not first:
        raise ValueError("Inserisci l’URL Hurtel del feed full.")
    if feed_type(first)=="light" and second and feed_type(second)=="full":
        first,second=(second or with_type(first,"full")),first
    if second and feed_type(second)=="full":
        raise ValueError(
            "Nel campo LIGHT è stato inserito nuovamente un URL FULL. Incolla l’URL "
            "Hurtel LIGHT completo (`type=light`), con il token autorizzato per il "
            "prezzo ingrosso e lo stock."
        )
    full_url=first if feed_type(first)=="full" else with_type(first,"full")
    light_url=second if second else with_type(full_url,"light")
    if feed_type(light_url)!="light":
        light_url=with_type(light_url,"light")
    return full_url,light_url


def combine_hurtel_feeds(price_list_id: int, full_path: str | Path,
                         light_path: str | Path) -> Path:
    """Merge Hurtel's full catalogue with wholesale prices from the light feed."""
    full=normalize(read_list(full_path));light=normalize(read_list(light_path))

    def clean_key(series: pd.Series) -> pd.Series:
        return series.fillna("").astype(str).str.strip().str.replace(r"\.0$","",regex=True)

    for frame in (full,light):
        frame["ean"]=clean_key(frame["ean"])
        frame["sku"]=clean_key(frame["sku"])
        frame["cost"]=pd.to_numeric(frame["cost"],errors="coerce").fillna(0)
        frame["quantity"]=pd.to_numeric(frame["quantity"],errors="coerce").fillna(0).clip(lower=0).astype(int)

    valid=light[(light["sku"]!="")|(light["ean"]!="")].copy()
    by_sku=valid[valid["sku"]!=""].drop_duplicates("sku").set_index("sku")
    by_ean=valid[valid["ean"]!=""].drop_duplicates("ean").set_index("ean")
    wholesale=full["sku"].map(by_sku["cost"] if not by_sku.empty else pd.Series(dtype=float))
    wholesale=wholesale.fillna(full["ean"].map(by_ean["cost"] if not by_ean.empty else pd.Series(dtype=float)))
    quantity=full["sku"].map(by_sku["quantity"] if not by_sku.empty else pd.Series(dtype=float))
    quantity=quantity.fillna(full["ean"].map(by_ean["quantity"] if not by_ean.empty else pd.Series(dtype=float)))
    matched=wholesale.fillna(0).gt(0)
    if not matched.any():
        raise ValueError(
            "I feed Hurtel full e light non hanno prodotti abbinabili con un prezzo ingrosso valido."
        )

    full=full[matched].copy()
    full["retail_price"]=pd.to_numeric(full["cost"],errors="coerce").fillna(0).round(2)
    full["wholesale_price"]=wholesale.loc[full.index].round(2)
    full["cost"]=full["wholesale_price"]
    full["quantity"]=quantity.loc[full.index].fillna(0).clip(lower=0).astype(int)
    full["shipping_cost"]=0.0
    full["total_cost"]=full["cost"]
    full["price_source"]="hurtel_light"
    folder=LIST_DIR/str(price_list_id);folder.mkdir(parents=True,exist_ok=True)
    path=folder/"hurtel_catalogo_full_prezzi_light.pkl";full.to_pickle(path)
    execute("UPDATE price_lists SET local_path=?,file_format=?,last_download_at=? WHERE id=?",
            (str(path),"pkl",now_iso(),price_list_id))
    return path


def download_hurtel_combined(price_list_id: int, primary_url: str,
                             secondary_url: str = "", username: str = "",
                             password: str = "") -> Path:
    """Download Hurtel full metadata and light wholesale prices, then merge them."""
    full_url,light_url=hurtel_feed_urls(primary_url,secondary_url)
    full_path=download_url(price_list_id,full_url,username,password)
    # Parse before the second download because both PHP endpoints can expose
    # the same output filename and therefore overwrite the first local file.
    full_frame=normalize(read_list(full_path))
    temporary=LIST_DIR/str(price_list_id)/"hurtel_full_temporaneo.pkl"
    full_frame.to_pickle(temporary)
    light_path=download_url(price_list_id,light_url,username,password)
    return combine_hurtel_feeds(price_list_id,temporary,light_path)


def combine_cecotec_stock(price_list_id: int, catalog_path: str | Path, stock_url: str) -> Path:
    """Combine a Cecotec monthly price file with the permanent Cecobi stock feed."""
    catalog=normalize(read_list(catalog_path))
    request=urllib.request.Request(stock_url,headers={"User-Agent":"MarketplaceHub/1.0"})
    with urllib.request.urlopen(request,timeout=120) as response:
        stock_content=response.read()
    stock=pd.read_csv(io.BytesIO(stock_content),sep=";",dtype=str,encoding_errors="replace")
    required={"public_id","stock","barcodes_ean_13"}
    if not required.issubset(stock.columns):
        raise ValueError(f"Il feed stock Cecotec non contiene le colonne richieste: {', '.join(sorted(required))}.")
    stock["_ean"]=stock["barcodes_ean_13"].fillna("").astype(str).str.strip().str.replace(r"\.0$","",regex=True)
    stock["_sku"]=stock["public_id"].fillna("").astype(str).str.strip().str.replace(r"\.0$","",regex=True)
    stock["_quantity"]=pd.to_numeric(stock["stock"],errors="coerce").fillna(0).clip(lower=0).astype(int)
    by_ean=stock[stock["_ean"]!=""].drop_duplicates("_ean").set_index("_ean")["_quantity"]
    by_sku=stock[stock["_sku"]!=""].drop_duplicates("_sku").set_index("_sku")["_quantity"]
    quantity=catalog["ean"].map(by_ean)
    quantity=quantity.fillna(catalog["sku"].map(by_sku)).fillna(0).astype(int)
    catalog["quantity"]=quantity
    catalog["stock_cecotec"]=quantity
    title_map=stock[stock["_ean"]!=""].drop_duplicates("_ean").set_index("_ean").get("title")
    if title_map is not None:catalog["stock_feed_title"]=catalog["ean"].map(title_map).fillna("")
    manual_map=stock[stock["_ean"]!=""].drop_duplicates("_ean").set_index("_ean").get("manual")
    if manual_map is not None:catalog["manual_url"]=catalog["ean"].map(manual_map).fillna("")
    folder=LIST_DIR/str(price_list_id);folder.mkdir(parents=True,exist_ok=True)
    path=folder/"cecotec_catalogo_stock.pkl";catalog.to_pickle(path)
    execute("UPDATE price_lists SET local_path=?,file_format=?,last_download_at=? WHERE id=?",
            (str(path),"pkl",now_iso(),price_list_id))
    return path


def download_cecotec_combined(price_list_id: int, catalog_url: str, stock_url: str,
                               username: str = "", password: str = "") -> Path:
    """Download a Cecotec catalog URL and enrich it with Cecobi B2C stock."""
    catalog_path=download_url(price_list_id,catalog_url,username,password)
    return combine_cecotec_stock(price_list_id,catalog_path,stock_url)


def combine_activeshop_stock(price_list_id: int, catalog_path: str | Path, stock_url: str,
                             diamond_prices: pd.DataFrame | None = None) -> Path:
    """Join the localized ActiveShop catalogue with its live B2B price/stock XML."""
    catalog_source=read_list(catalog_path)
    catalog_columns={str(column).strip().lower() for column in catalog_source.columns}
    if not {"sku","name","ean"}.issubset(catalog_columns):
        raise ValueError("L’URL principale ActiveShop non è il catalogo prodotti. Usa b2b-it.xml come URL principale e stock-b2b.xml come URL secondario.")
    catalog=normalize(catalog_source)
    request=urllib.request.Request(stock_url,headers={"User-Agent":"MarketplaceHub/1.0"})
    with urllib.request.urlopen(request,timeout=120) as response:
        stock_content=response.read()
    try:
        stock=pd.read_xml(io.BytesIO(stock_content))
    except Exception as error:
        raise ValueError(f"Il feed stock ActiveShop non è un XML leggibile: {error}") from error
    lookup={str(column).strip().lower():column for column in stock.columns}
    required={"indeks","stock","price","ean"}
    if not required.issubset(lookup):
        missing=", ".join(sorted(required-set(lookup)))
        raise ValueError(f"Il feed stock ActiveShop non contiene i campi richiesti: {missing}.")

    def clean_key(series: pd.Series) -> pd.Series:
        return series.fillna("").astype(str).str.strip().str.replace(r"\.0$","",regex=True)

    stock["_sku"]=clean_key(stock[lookup["indeks"]])
    stock["_ean"]=clean_key(stock[lookup["ean"]])
    stock["_feed_price"]=pd.to_numeric(stock[lookup["price"]].astype(str).str.replace(",",".",regex=False),errors="coerce")
    stock["_quantity"]=pd.to_numeric(stock[lookup["stock"]],errors="coerce").fillna(0).clip(lower=0).astype(int)
    valid_stock=stock[(stock["_sku"]!="")|(stock["_ean"]!="")].copy()
    by_sku=valid_stock[valid_stock["_sku"]!=""].drop_duplicates("_sku").set_index("_sku")
    by_ean=valid_stock[valid_stock["_ean"]!=""].drop_duplicates("_ean").set_index("_ean")

    feed_price=catalog["sku"].map(by_sku["_feed_price"] if not by_sku.empty else pd.Series(dtype=float))
    feed_price=feed_price.fillna(catalog["ean"].map(by_ean["_feed_price"] if not by_ean.empty else pd.Series(dtype=float)))
    quantity=catalog["sku"].map(by_sku["_quantity"] if not by_sku.empty else pd.Series(dtype=float))
    quantity=quantity.fillna(catalog["ean"].map(by_ean["_quantity"] if not by_ean.empty else pd.Series(dtype=float)))
    valid_catalog=(~catalog["sku"].str.lower().isin(("","nan","none")))|(~catalog["ean"].str.lower().isin(("","nan","none")))
    catalog=catalog[valid_catalog].copy()
    catalog["stock_feed_price"]=feed_price.loc[catalog.index].fillna(0).round(2)
    catalog["quantity"]=quantity.loc[catalog.index].fillna(0).astype(int)
    # The public XML prices are not the customer's Diamond purchase prices.
    # Only extension_attributes.final_price from the authenticated Catalog API is a valid cost.
    catalog["cost"]=0.0;catalog["diamond_currency"]="";catalog["diamond_price_available"]=False
    if diamond_prices is not None and not diamond_prices.empty:
        prices=diamond_prices.copy()
        for key in ("sku","ean"):
            if key not in prices:prices[key]=""
            prices[key]=prices[key].fillna("").astype(str).str.strip().str.replace(r"\.0$","",regex=True)
        if "diamond_price" not in prices:prices["diamond_price"]=float("nan")
        if "diamond_currency" not in prices:prices["diamond_currency"]=""
        prices["diamond_price"]=pd.to_numeric(prices["diamond_price"],errors="coerce")
        prices["diamond_currency"]=prices["diamond_currency"].fillna("").astype(str).str.upper().str.strip()
        price_sku=prices[prices["sku"]!=""].drop_duplicates("sku").set_index("sku")
        price_ean=prices[prices["ean"]!=""].drop_duplicates("ean").set_index("ean")
        diamond=catalog["sku"].map(price_sku["diamond_price"] if not price_sku.empty else pd.Series(dtype=float))
        diamond=diamond.fillna(catalog["ean"].map(price_ean["diamond_price"] if not price_ean.empty else pd.Series(dtype=float)))
        currency=catalog["sku"].map(price_sku["diamond_currency"] if not price_sku.empty else pd.Series(dtype=str))
        currency=currency.fillna(catalog["ean"].map(price_ean["diamond_currency"] if not price_ean.empty else pd.Series(dtype=str))).fillna("")
        diamond_eur=diamond.copy()
        pln=currency.eq("PLN") & diamond.gt(0)
        if pln.any():
            from services.fx import get_ecb_rates
            pln_rate=float(get_ecb_rates()["rates"]["PLN"])
            diamond_eur.loc[pln]=diamond.loc[pln]/pln_rate
        supported=currency.isin(("EUR","PLN")) & diamond_eur.gt(0)
        catalog["cost"]=diamond_eur.where(supported,0).fillna(0).round(2)
        catalog["diamond_currency"]=currency
        catalog["diamond_price_available"]=supported.fillna(False)
    # Contractual ActiveShop shipping bands supplied by the seller.
    shipping_by_pack={"package":10.0,"pallet":100.0,"oversized pallet":200.0}
    if "pack_type" in catalog:
        normalized_pack=catalog["pack_type"].fillna("").astype(str).str.strip().str.lower()
        catalog["shipping_cost"]=normalized_pack.map(shipping_by_pack).fillna(0).round(2)
    else:
        catalog["shipping_cost"]=0.0
    catalog["total_cost"]=(catalog["cost"]+catalog["shipping_cost"]).where(catalog["cost"].gt(0),0).round(2)
    catalog["stock_activeshop"]=catalog["quantity"]
    catalog["active_price_updated"]=catalog["cost"]
    folder=LIST_DIR/str(price_list_id);folder.mkdir(parents=True,exist_ok=True)
    path=folder/"activeshop_catalogo_stock.pkl";catalog.to_pickle(path)
    execute("UPDATE price_lists SET local_path=?,file_format=?,last_download_at=? WHERE id=?",
            (str(path),"pkl",now_iso(),price_list_id))
    return path


def download_activeshop_combined(price_list_id: int, catalog_url: str, stock_url: str,
                                 username: str = "", password: str = "",
                                 api_username: str = "", api_password: str = "",
                                 api_host: str = "https://b2b.activeshop.com.pl",
                                 api_store_code: str = "B2B_PL_pl", progress=None) -> Path:
    """Download the localized ActiveShop catalogue and enrich it with live B2B data."""
    # Accept the two URLs even when the user pastes them in reverse order.
    if "stock-b2b" in catalog_url.lower() and "stock-b2b" not in stock_url.lower():
        catalog_url,stock_url=stock_url,catalog_url
    if not api_username.strip() or not api_password:
        raise ValueError("Configura username e password del Catalog API ActiveShop per leggere il prezzo Diamond.")
    from services.activeshop import fetch_diamond_prices
    diamond_prices=fetch_diamond_prices(api_username,api_password,api_host,api_store_code,progress=progress)
    catalog_path=download_url(price_list_id,catalog_url,username,password)
    return combine_activeshop_stock(price_list_id,catalog_path,stock_url,diamond_prices=diamond_prices)


def _column_token(value: object) -> str:
    text=unicodedata.normalize("NFKD",str(value or ""))
    text="".join(char for char in text if not unicodedata.combining(char))
    # Preserve the word boundaries used by BTP/Force Top camel-case headers:
    # ItemPartNumber -> item_part_number, AvailableQty -> available_qty.
    text=re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])","_",text)
    text=re.sub(r"(?<=[a-z0-9])(?=[A-Z])","_",text)
    text=text.lower()
    return re.sub(r"[^a-z0-9]+","_",text).strip("_")


def _find_column(frame: pd.DataFrame, candidates: tuple[str,...]) -> str | None:
    lookup={_column_token(column):str(column) for column in frame.columns}
    normalized=[_column_token(candidate) for candidate in candidates]
    for candidate in normalized:
        if candidate in lookup:return lookup[candidate]
    for candidate in normalized:
        for token,column in lookup.items():
            if candidate and (token.startswith(candidate+"_") or token.endswith("_"+candidate)):
                return column
    return None


def _clean_product_key(series: pd.Series) -> pd.Series:
    return (series.fillna("").astype(str).str.strip()
            .str.replace(r"\.0$","",regex=True).str.upper())


FORCETOP_SKU_COLUMNS=(
    "sku","product_code","productcode","code","item_code","itemcode",
    "item_part_number","itempartnumber","part_number","partnumber",
    "erp_id","erpid","item_number","itemnumber",
    "symbol","index","indeks","kod","kod_towaru","catalog_number",
    "catalogue_number","supplier_code","manufacturer_code","id_product"
)
FORCETOP_EAN_COLUMNS=(
    "ean","ean13","gtin","barcode","bar_code","kod_ean",
    "item_ean","itemean"
)
FORCETOP_NAME_COLUMNS=(
    "name","product_name","productname","title","description","nazwa",
    "nazwa_towaru","nazwa_produktu"
)
FORCETOP_PRICE_COLUMNS=(
    "cost","net_price","netprice","price_net","price_nett","pricenett",
    "purchase_price","purchaseprice","wholesale_price","wholesaleprice",
    "your_price","customer_price","cena_netto","cena_zakupu",
    "cena_hurtowa","price"
)
FORCETOP_QTY_COLUMNS=(
    # AvailableQty is the sellable quantity exposed by the Force Top BTP report.
    # StockQty and ScheduledQty are retained only as fallbacks when AvailableQty
    # is absent; scheduled inbound stock must not be added to current availability.
    "available_qty","availableqty","available_quantity","availablequantity",
    "quantity","qty","stock_qty","stockqty","stock","on_hand","onhand",
    "availability","stan","stan_magazynowy","ilosc","dostepnosc"
)


def _forcetop_inventory_values(inventory: pd.DataFrame) -> pd.DataFrame:
    sku_col=_find_column(inventory,FORCETOP_SKU_COLUMNS)
    ean_col=_find_column(inventory,FORCETOP_EAN_COLUMNS)
    price_col=_find_column(inventory,FORCETOP_PRICE_COLUMNS)
    qty_col=_find_column(inventory,FORCETOP_QTY_COLUMNS)
    if not sku_col and not ean_col:
        raise ValueError(
            "Il file prezzi/stock Force Top non contiene una colonna prodotto riconoscibile. "
            f"Colonne ricevute: {', '.join(map(str,inventory.columns))}"
        )
    if not price_col and not qty_col:
        raise ValueError(
            "Il file secondario Force Top non contiene né prezzo né disponibilità riconoscibili. "
            f"Colonne ricevute: {', '.join(map(str,inventory.columns))}"
        )
    result=pd.DataFrame(index=inventory.index)
    result["_ft_sku"]=_clean_product_key(inventory[sku_col]) if sku_col else ""
    result["_ft_ean"]=_clean_product_key(inventory[ean_col]) if ean_col else ""
    result["_ft_cost"]=(pd.to_numeric(inventory[price_col].astype(str).str.replace(" ","",regex=False)
                                      .str.replace(",",".",regex=False),errors="coerce").fillna(0)
                         if price_col else 0.0)
    result["_ft_quantity"]=(pd.to_numeric(inventory[qty_col].astype(str).str.replace(" ","",regex=False)
                                          .str.replace(",",".",regex=False),errors="coerce").fillna(0)
                             if qty_col else 0.0)
    result["_ft_quantity"]=result["_ft_quantity"].clip(lower=0)
    return result


def combine_forcetop_feeds(price_list_id: int, catalog_path: str | Path,
                            inventory_path: str | Path) -> Path:
    """Merge Force Top's full catalogue with its lightweight price/stock report."""
    raw_catalog=read_list(catalog_path)
    raw_inventory=read_list(inventory_path)
    catalog=normalize(raw_catalog)
    inventory=_forcetop_inventory_values(raw_inventory)

    catalog["_ft_sku"]=_clean_product_key(catalog["sku"])
    catalog["_ft_ean"]=_clean_product_key(catalog["ean"])
    valid=inventory[(inventory["_ft_sku"]!="")|(inventory["_ft_ean"]!="")].copy()
    if valid.empty:
        raise ValueError("Il file prezzi/stock Force Top non contiene righe prodotto utilizzabili.")

    def aggregate_key(column: str) -> pd.DataFrame:
        block=valid[valid[column]!=""].copy()
        if block.empty:return pd.DataFrame(columns=["_ft_cost","_ft_quantity"])
        # Multiple warehouse rows are summed; the first positive price is kept.
        block["_positive_cost"]=block["_ft_cost"].where(block["_ft_cost"].gt(0))
        grouped=block.groupby(column,sort=False).agg(
            _ft_cost=("_positive_cost","first"),
            _ft_quantity=("_ft_quantity","sum"),
        )
        grouped["_ft_cost"]=grouped["_ft_cost"].fillna(0)
        return grouped

    by_sku=aggregate_key("_ft_sku");by_ean=aggregate_key("_ft_ean")
    sku_cost=catalog["_ft_sku"].map(by_sku["_ft_cost"] if not by_sku.empty else pd.Series(dtype=float))
    ean_cost=catalog["_ft_ean"].map(by_ean["_ft_cost"] if not by_ean.empty else pd.Series(dtype=float))
    sku_qty=catalog["_ft_sku"].map(by_sku["_ft_quantity"] if not by_sku.empty else pd.Series(dtype=float))
    ean_qty=catalog["_ft_ean"].map(by_ean["_ft_quantity"] if not by_ean.empty else pd.Series(dtype=float))
    matched_sku=sku_cost.notna()|sku_qty.notna()
    matched_ean=ean_cost.notna()|ean_qty.notna()
    matched=matched_sku|matched_ean
    if not matched.any():
        raise ValueError(
            "Catalogo e file prezzi/stock Force Top non hanno SKU o EAN abbinabili. "
            "Controlla che i due URL appartengano allo stesso account BTP."
        )

    inventory_cost=sku_cost.fillna(ean_cost).fillna(0)
    inventory_qty=sku_qty.fillna(ean_qty).fillna(0)
    original_cost=pd.to_numeric(catalog["cost"],errors="coerce").fillna(0)
    catalog["catalog_cost_before_inventory"]=original_cost
    catalog["cost"]=inventory_cost.where(inventory_cost.gt(0),original_cost).round(4)
    catalog["quantity"]=inventory_qty.clip(lower=0).round().astype(int)
    catalog["forcetop_inventory_matched"]=matched
    catalog["price_source"]="forcetop_inventory_report"
    catalog["stock_source"]="forcetop_inventory_report"
    shipping=(catalog["shipping_cost"] if "shipping_cost" in catalog
              else pd.Series(0.0,index=catalog.index))
    catalog["shipping_cost"]=pd.to_numeric(shipping,errors="coerce").fillna(0)
    catalog["total_cost"]=(catalog["cost"]+catalog["shipping_cost"]).round(4)
    catalog=catalog.drop(columns=["_ft_sku","_ft_ean"],errors="ignore")

    folder=LIST_DIR/str(price_list_id);folder.mkdir(parents=True,exist_ok=True)
    path=folder/"forcetop_catalogo_prezzi_stock.pkl"
    catalog.to_pickle(path)
    execute("UPDATE price_lists SET local_path=?,file_format=?,last_download_at=? WHERE id=?",
            (str(path),"pkl",now_iso(),price_list_id))
    return path


def download_forcetop_combined(price_list_id: int, catalog_url: str,
                                inventory_url: str, username: str = "",
                                password: str = "") -> Path:
    if not catalog_url.strip() or not inventory_url.strip():
        raise ValueError("Force Top richiede sia l'URL catalogo prodotti sia l'URL prezzi/stock.")
    catalog_path=download_url(price_list_id,catalog_url,username,password)
    # Preserve the catalogue because downloading the second endpoint updates the
    # same price-list record and some providers reuse generic filenames.
    catalog_frame=read_list(catalog_path)
    temporary=LIST_DIR/str(price_list_id)/"forcetop_catalogo_temporaneo.pkl"
    catalog_frame.to_pickle(temporary)
    inventory_path=download_url(price_list_id,inventory_url,username,password)
    return combine_forcetop_feeds(price_list_id,temporary,inventory_path)


def refresh_forcetop_inventory(price_list_id: int, current_catalog_path: str | Path,
                                inventory_url: str, username: str = "",
                                password: str = "") -> Path:
    if not Path(current_catalog_path).exists():
        raise ValueError("Il catalogo Force Top locale non è disponibile: esegui un aggiornamento completo.")
    inventory_path=download_url(price_list_id,inventory_url,username,password)
    return combine_forcetop_feeds(price_list_id,current_catalog_path,inventory_path)


def save_cecotec_monthly(price_list_id: int, name: str, content: bytes, stock_url: str) -> Path:
    """Save the new monthly Excel and immediately refresh quantities from Cecobi."""
    catalog_path=save_uploaded(price_list_id,name,content)
    return combine_cecotec_stock(price_list_id,catalog_path,stock_url)


def read_list(path: str | Path) -> pd.DataFrame:
    path=Path(path)
    suffix=path.suffix.lower()
    if suffix in (".xlsx",".xls"):
        df=pd.read_excel(path)
        # Cecotec monthly files have a decorative first row and headers on row 2.
        unnamed=sum(str(c).lower().startswith("unnamed") for c in df.columns)
        if unnamed > len(df.columns)/2:
            df=pd.read_excel(path,header=1)
        return df
    if suffix in (".csv",".txt",".tsv"):
        for sep in (None,";",",","\t","|"):
            try:
                df=pd.read_csv(path,sep=sep,engine="python",encoding_errors="replace")
                if len(df.columns)>1:return df
            except Exception: pass
        raise ValueError("Formato CSV non riconosciuto.")
    if suffix==".xml":
        with path.open("rb") as handle:
            header=handle.read(2048).lower()
        if b"file_format=\"iof\"" in header or b"file_format='iof'" in header:
            return _read_innpro_iof(path)
        try:
            return pd.read_xml(path)
        except Exception as error:
            # Some Plytix exports contain invalid XML QNames such as
            # <Foto_Image_Gallery_16:9_JPG_01>. lxml's recovery parser keeps
            # the element and its value while tolerating that malformed name.
            if "QName" not in str(error) and "XMLSyntaxError" not in type(error).__name__:
                raise
            return _read_recovering_product_xml(path)
    if suffix==".json":
        return pd.read_json(path)
    if suffix in (".pkl",".pickle"):
        return pd.read_pickle(path)
    raise ValueError(f"Formato non supportato: {suffix}")


def _attr_ending(element: ET.Element, ending: str, default=""):
    for key,value in element.attrib.items():
        if key==ending or key.endswith("}"+ending):
            return value
    return default


def _xml_local_name(value: object) -> str:
    """Return an XML local name while tolerating default/prefixed namespaces."""
    tag = value.tag if isinstance(value, ET.Element) else value
    return str(tag or "").rsplit("}", 1)[-1].split(":")[-1]


def _xml_children(element: ET.Element | None, name: str) -> list[ET.Element]:
    if element is None:
        return []
    return [child for child in list(element) if _xml_local_name(child) == name]


def _xml_child(element: ET.Element | None, name: str) -> ET.Element | None:
    children = _xml_children(element, name)
    return children[0] if children else None


def _xml_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


_IOF_LANGUAGE_PRIORITY = (
    "eng", "en", "en-us", "en-gb", "ita", "it", "deu", "de",
    "por", "pt", "spa", "es", "fra", "fr", "pol", "pl",
)


def _iof_language(element: ET.Element) -> str:
    return str(_attr_ending(element, "lang", "") or "").strip().lower().replace("_", "-")


def _iof_multilingual_text(parent: ET.Element | None, child_name: str) -> str:
    """Choose a stable localized value, preferring English then common EU locales."""
    candidates: list[tuple[int, int, str]] = []
    priorities = {language: index for index, language in enumerate(_IOF_LANGUAGE_PRIORITY)}
    for order, node in enumerate(_xml_children(parent, child_name)):
        value = _xml_text(node)
        if not value:
            continue
        language = _iof_language(node)
        rank = priorities.get(language, len(priorities) + order)
        candidates.append((rank, order, value))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _iof_ordered_urls(parent: ET.Element | None, node_name: str) -> list[str]:
    """Collect unique URL attributes in IOF priority order."""
    if parent is None:
        return []
    items: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, node in enumerate(parent.iter()):
        if _xml_local_name(node) != node_name:
            continue
        url = str(node.attrib.get("url") or _attr_ending(node, "url", "") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        raw_priority = _attr_ending(node, "priority", "") or node.attrib.get("priority")
        try:
            priority = int(float(str(raw_priority)))
        except (TypeError, ValueError):
            priority = 10_000 + order
        items.append((priority, order, url))
    items.sort(key=lambda item: (item[0], item[1]))
    return [url for _, _, url in items]


def _iof_product_images(product: ET.Element) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Read every real product image while keeping supplier icons separate."""
    images_node = _xml_child(product, "images")
    image_urls = _iof_ordered_urls(images_node, "image")
    icon_urls: list[str] = []
    for icon_name in ("icon", "auction_icon", "group_icon"):
        for url in _iof_ordered_urls(images_node, icon_name):
            if url not in icon_urls:
                icon_urls.append(url)
    metadata: list[dict[str, str]] = []
    if images_node is not None:
        for node in images_node.iter():
            if _xml_local_name(node) != "image":
                continue
            url = str(node.attrib.get("url") or _attr_ending(node, "url", "") or "").strip()
            if url:
                metadata.append({
                    "url": url,
                    "priority": str(_attr_ending(node, "priority", "") or "").strip(),
                    "width": str(node.attrib.get("width") or "").strip(),
                    "height": str(node.attrib.get("height") or "").strip(),
                    "hash": str(node.attrib.get("hash") or "").strip(),
                    "date_changed": str(node.attrib.get("date_changed") or "").strip(),
                })
    return image_urls, icon_urls, metadata


def _iof_product_attachments(product: ET.Element) -> tuple[list[str], list[dict[str, str]]]:
    """Read every attachment URL and its IOF metadata."""
    attachments_node = _xml_child(product, "attachments")
    document_urls = _iof_ordered_urls(attachments_node, "file")
    metadata: list[dict[str, str]] = []
    if attachments_node is not None:
        for node in attachments_node.iter():
            if _xml_local_name(node) != "file":
                continue
            url = str(node.attrib.get("url") or _attr_ending(node, "url", "") or "").strip()
            if not url:
                continue
            metadata.append({
                "url": url,
                "name": _iof_multilingual_text(node, "name"),
                "type": str(node.attrib.get("attachment_file_type") or _attr_ending(node, "attachment_file_type", "") or "").strip(),
                "extension": str(node.attrib.get("attachment_file_extension") or _attr_ending(node, "attachment_file_extension", "") or "").strip(),
                "version": str(node.attrib.get("version") or "").strip(),
                "priority": str(node.attrib.get("priority") or _attr_ending(node, "priority", "") or "").strip(),
            })
    return document_urls, metadata


def _iof_product_parameters(product: ET.Element) -> dict[str, object]:
    """Read the complete product parameter set exposed by an IOF full feed."""
    container = _xml_child(product, "parameters")
    result: dict[str, object] = {}
    if container is None:
        return result
    for parameter in _xml_children(container, "parameter"):
        if str(parameter.attrib.get("type") or "parameter").lower() == "section":
            continue
        parameter_id = str(parameter.attrib.get("id") or "").strip()
        name = str(parameter.attrib.get("name") or "").strip()
        if not name:
            name = _iof_multilingual_text(parameter, "name")
        if not name:
            name = f"parameter_{parameter_id}" if parameter_id else "parameter"
        values: list[str] = []
        for value_node in _xml_children(parameter, "value"):
            value = str(value_node.attrib.get("name") or "").strip()
            if not value:
                value = _iof_multilingual_text(value_node, "name") or _xml_text(value_node)
            if value and value not in values:
                values.append(value)
        if not values:
            direct = str(parameter.attrib.get("value") or "").strip()
            if direct:
                values.append(direct)
        if not values:
            continue
        current = result.get(name)
        combined: list[str] = []
        if isinstance(current, list):
            combined.extend(str(item) for item in current if str(item).strip())
        elif current not in (None, ""):
            combined.append(str(current))
        for value in values:
            if value not in combined:
                combined.append(value)
        result[name] = combined[0] if len(combined) == 1 else combined
    return result


def _gtin_from_iof_values(*values) -> str:
    """Choose a GTIN-shaped value without treating an alphanumeric SKU as EAN."""
    for value in values:
        text=str(value or "").strip()
        match=re.fullmatch(r"(\d{8}|\d{12,14})(?:\.0+)?",text)
        if match:return match.group(1)
    return ""


def _read_innpro_iof(path: Path) -> pd.DataFrame:
    """Parse an Innpro/IdoSell IOF full or light feed without losing product content.

    Besides price and stock, the full feed contains product names, long and short
    descriptions, the commercial producer/brand, every large image URL,
    attachments, product-card URL and technical parameters.  These fields are
    deliberately retained in the resulting DataFrame so Catalog Intelligence can
    classify and build a complete marketplace product feed instead of working
    only with SKU/EAN.
    """
    records: list[dict[str, object]] = []
    for _, product in ET.iterparse(path, events=("end",)):
        if _xml_local_name(product) != "product":
            continue

        product_id = str(product.attrib.get("id") or "").strip()
        producer = _xml_child(product, "producer")
        category = _xml_child(product, "category")
        unit = _xml_child(product, "unit")
        series = _xml_child(product, "series")
        warranty = _xml_child(product, "warranty")
        description_node = _xml_child(product, "description")

        producer_name = str((producer.attrib.get("name") if producer is not None else "") or "").strip()
        category_name = str((category.attrib.get("name") if category is not None else "") or "").strip()
        name = _iof_multilingual_text(description_node, "name")
        long_description = _iof_multilingual_text(description_node, "long_desc")
        short_description = _iof_multilingual_text(description_node, "short_desc")
        version_node = _xml_child(description_node, "version")
        version_name = (
            str((version_node.attrib.get("name") if version_node is not None else "") or "").strip()
            or _iof_multilingual_text(version_node, "name")
        )

        card = _xml_child(product, "card")
        product_url = str((card.attrib.get("url") if card is not None else "") or "").strip()
        image_urls, icon_urls, image_metadata = _iof_product_images(product)
        document_urls, attachment_metadata = _iof_product_attachments(product)
        parameters = _iof_product_parameters(product)
        direct_price = _xml_child(product, "price")
        direct_srp = _xml_child(product, "srp")
        product_cost = direct_price.attrib.get("net", "") if direct_price is not None else ""
        product_srp = direct_srp.attrib.get("net", "") if direct_srp is not None else ""
        sizes_container = _xml_child(product, "sizes")
        sizes = _xml_children(sizes_container, "size")
        if not sizes:
            sizes = [product]

        base_record: dict[str, object] = {
            "product_id": product_id,
            "name": name,
            "title": name,
            "description": long_description or short_description,
            "long_description": long_description,
            "short_description": short_description,
            "version_name": version_name,
            "producer": producer_name,
            "producer_id": str((producer.attrib.get("id") if producer is not None else "") or "").strip(),
            "category": category_name,
            "category_id": str((category.attrib.get("id") if category is not None else "") or "").strip(),
            "unit": str((unit.attrib.get("name") if unit is not None else "") or "").strip(),
            "series": str((series.attrib.get("name") if series is not None else "") or "").strip(),
            "warranty": str((warranty.attrib.get("name") if warranty is not None else "") or "").strip(),
            "product_url": product_url,
            "card_url": product_url,
            "images": image_urls,
            "image_urls": image_urls,
            "image_url": image_urls[0] if image_urls else "",
            "image_count": len(image_urls),
            "image_metadata": image_metadata,
            "icon_urls": icon_urls,
            "documents": document_urls,
            "document_urls": document_urls,
            "attachment_urls": document_urls,
            "attachment_metadata": attachment_metadata,
            "parameters": parameters,
            "currency": str(product.attrib.get("currency") or "").strip(),
            "vat": str(product.attrib.get("vat") or "").strip(),
            "product_type": str(product.attrib.get("type") or "").strip(),
            "code_on_card": str(product.attrib.get("code_on_card") or "").strip(),
            "producer_code_standard": str(product.attrib.get("producer_code_standard") or "").strip(),
        }
        # Keep technical parameters as a compact dictionary. Catalog
        # Intelligence searches this dictionary by the official marketplace
        # attribute code/label, avoiding thousands of sparse DataFrame columns
        # while preserving every supplier parameter and its exact name.

        for size in sizes:
            size_price = _xml_child(size, "price")
            size_srp = _xml_child(size, "srp")
            cost = size_price.attrib.get("net", product_cost) if size_price is not None else product_cost
            srp = size_srp.attrib.get("net", product_srp) if size_srp is not None else product_srp
            stocks = _xml_children(size, "stock")
            quantities: list[int] = []
            for stock in stocks:
                raw_quantity = (
                    stock.attrib.get("available_stock_quantity")
                    or stock.attrib.get("quantity")
                    or stock.attrib.get("stock_quantity")
                    or "0"
                )
                try:
                    quantities.append(max(0, int(float(str(raw_quantity).replace(",", ".")))))
                except (TypeError, ValueError):
                    pass
            quantity = sum(quantities)
            code_producer = str(size.attrib.get("code_producer") or "").strip()
            code_external = str(_attr_ending(size, "code_external", "") or "").strip()
            size_code = str(size.attrib.get("code") or "").strip()
            ean = _gtin_from_iof_values(code_producer, code_external, size_code)
            if not ean:
                ean = code_external
            sku = code_producer or size_code or product_id
            weight_g_raw = size.attrib.get("weight") or _attr_ending(size, "weight_net", "")
            try:
                weight_g = max(0.0, float(str(weight_g_raw).replace(",", ".")))
            except (TypeError, ValueError):
                weight_g = 0.0
            record = dict(base_record)
            record.update(
                {
                    "sku": sku,
                    "ean": ean,
                    "code_producer": code_producer,
                    "code_external": code_external,
                    "size_code": size_code,
                    "cost": cost,
                    "srp": srp,
                    "quantity": quantity,
                    "weight_g": round(weight_g, 3),
                    "weight_kg": round(weight_g / 1000, 3) if weight_g else 0.0,
                    "variant": (
                        str(size.attrib.get("name") or "").strip()
                        if size is not product
                        else version_name
                    ),
                }
            )
            records.append(record)
        product.clear()
    if not records:
        raise ValueError("Il file IOF Innpro non contiene prodotti leggibili.")
    return pd.DataFrame.from_records(records)


ALIASES={
    "ean":["ean","ean13","ean_13","barcode","bar_code","gtin","codice_ean","kod_ean",
           "item_ean","itemean"],
    "sku":["sku","tag_sku","seller_sku","reference","referencia","ref","codice","product_id","ref_proveedor",
           "product_code","item_code","item_part_number","itempartnumber","erp_id","erpid",
           "symbol","index","indeks","kod","kod_towaru","catalog_number","catalogue_number"],
    "name":["name","title","nome","prodotto","product_name","description","titulo","nazwa","nazwa_towaru","nazwa_produktu",
            "nombre_completo","nombre_producto___modelo__it_","nombre_producto___modelo"],
    "cost":["neto_italia_(zona_2)","neto_italia","neto_pt","cost","costo","net_price","wholesale_price","price_net",
            "price_nett","pricenett","pricelist_eur","purchase_price","pvd",
            "cena_netto","cena_zakupu","cena_hurtowa","your_price","customer_price"],
    "quantity":["quantity","qty","stock_prod","stock_mwg","stock","availability","available_qty","availableqty","available_quantity","qta",
                "stan","stan_magazynowy","ilosc","dostepnosc"],
    "weight_kg":["weight_kg","peso_kg","peso_(kg)","weight_(kg)","package_weight_kg",
                 "gross_weight_kg","peso","weight"],
}

CECOTEC_NET_COLUMNS={
    "cost_pt":["neto_portugal","neto_pt"],
    "cost_it":["neto_italia_(zona_2)","neto_italia"],
    "cost_fr":["neto_francia_(zona_1)","neto_francia"],
    "cost_de":["neto_alemania_(zona_1)","neto_alemania"],
    "cost_zone3":["neto_zona_3_(monaco_austria_belgica_luxemburgo_holanda_reino_unido)"],
    "cost_zone4":["neto_zona_4_(eslovaquia_eslovenia_polonia_rep._checa_bulgaria_croacia_estonia_grecia_estonia_hungria_letonia_lituania_rumania)"],
    "cost_zone5":["neto_zona_5_(dinamarca_finlandia_irlanda_suecia)"],
    "cost_zone6":["neto_zona_6_(suiza_noruega_san_marino)"],
}


def country_cost(df: pd.DataFrame, country: str) -> pd.Series:
    """Choose the Cecotec purchase cost for the destination country."""
    key={"pt":"cost_pt","it":"cost_it","fr":"cost_fr","de":"cost_de",
         "at":"cost_zone3","be":"cost_zone3","lu":"cost_zone3","nl":"cost_zone3","gb":"cost_zone3",
         "sk":"cost_zone4","si":"cost_zone4","pl":"cost_zone4","cz":"cost_zone4","bg":"cost_zone4",
         "hr":"cost_zone4","ee":"cost_zone4","gr":"cost_zone4","hu":"cost_zone4","lv":"cost_zone4",
         "lt":"cost_zone4","ro":"cost_zone4","dk":"cost_zone5","fi":"cost_zone5","ie":"cost_zone5",
         "se":"cost_zone5","ch":"cost_zone6","no":"cost_zone6","sm":"cost_zone6"}.get((country or "it").lower())
    if "cost" in df:
        fallback=pd.to_numeric(df["cost"],errors="coerce").fillna(0)
    else:
        fallback=pd.Series(0.0,index=df.index)
    if not key or key not in df:return fallback
    selected=pd.to_numeric(df[key],errors="coerce")
    return selected.where(selected.gt(0),fallback).fillna(fallback)


def destination_country_codes(df: pd.DataFrame) -> list[str]:
    """Read the destination-country metadata stored in a saved-view snapshot."""
    raw=None
    if "destination_countries" in df and not df.empty:
        raw=df["destination_countries"].iloc[0]
    if (raw is None or (isinstance(raw,float) and pd.isna(raw)) or not str(raw).strip()) \
            and "destination_country" in df and not df.empty:
        raw=df["destination_country"].iloc[0]
    if raw is None or (not isinstance(raw,(list,tuple,set)) and pd.isna(raw)):
        return []
    if isinstance(raw,(list,tuple,set)):
        values=list(raw)
    else:
        text=str(raw).strip()
        if text.startswith("["):
            try:values=json.loads(text)
            except Exception:values=re.split(r"[,;|]+",text)
        else:
            values=re.split(r"[,;|]+",text)
    result=[]
    for value in values:
        code=str(value).strip().lower()
        if re.fullmatch(r"[a-z]{2}",code) and code not in result:result.append(code)
    return result


def _ean_like_ratio(series: pd.Series) -> float:
    """Share of populated values shaped like GTIN-8/12/13/14 codes."""
    values=series.fillna("").astype(str).str.strip().str.replace(r"\.0$","",regex=True)
    populated=values.ne("") & ~values.str.lower().isin(("nan","none"))
    if not populated.any():return 0.0
    return float(values[populated].str.fullmatch(r"\d{8}|\d{12,14}").mean())


def _read_recovering_product_xml(path: Path) -> pd.DataFrame:
    try:
        from lxml import etree
    except ImportError as error:
        raise ValueError("Il feed XML contiene nomi non validi e richiede la libreria lxml.") from error
    records=[]
    context=etree.iterparse(str(path),events=("end",),tag="product",recover=True,huge_tree=True)
    for _,product in context:
        record={}
        for child in product:
            tag=str(child.tag)
            # Make the recovered malformed QName safe as a dataframe column.
            tag=re.sub(r":(?=\d)","_",tag)
            value="".join(child.itertext()).strip() if len(child) else (child.text or "").strip()
            if tag in record and value:
                record[tag]=f"{record[tag]} | {value}" if record[tag] else value
            else:
                record[tag]=value
        records.append(record)
        product.clear()
        parent=product.getparent()
        while parent is not None and product.getprevious() is not None:
            del parent[0]
    if not records:
        raise ValueError("Il feed XML non contiene elementi <product> leggibili.")
    return pd.DataFrame.from_records(records)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    out=df.copy()
    if out.columns.duplicated().any():
        repaired=[]
        seen=set()
        for position,column in enumerate(out.columns):
            if column in seen:continue
            seen.add(column)
            positions=[i for i,name in enumerate(out.columns) if name==column]
            block=out.iloc[:,positions]
            if len(positions)==1:
                series=block.iloc[:,0]
            else:
                # Repair previously generated PKLs: take the first populated
                # value across every duplicate copy of the same column.
                series=block.replace(r"^\s*$",pd.NA,regex=True).bfill(axis=1).iloc[:,0]
            repaired.append(series.rename(column))
        out=pd.concat(repaired,axis=1)
    lookup={_column_token(c):c for c in out.columns}
    for target,names in CECOTEC_NET_COLUMNS.items():
        if target in out:continue
        for name in names:
            if name in lookup:
                out[target]=pd.to_numeric(out[lookup[name]].astype(str).str.replace(",",".",regex=False),errors="coerce").fillna(0)
                break
    ren={}
    for target,names in ALIASES.items():
        # A normalized PKL can be opened again. Never rename another alias to
        # an already existing canonical column, otherwise pandas creates
        # duplicate names and out[col] becomes a DataFrame instead of a Series.
        if target in out.columns:
            continue
        for name in names:
            if name in lookup:
                ren[lookup[name]]=target;break
    out=out.rename(columns=ren)
    for col in ("ean","sku","name"):
        if col not in out:out[col]=""
        out[col]=out[col].astype(str).str.strip().str.replace(r"\.0$","",regex=True)
    # Some Hurtel exports label the supplier reference as EAN and the real
    # 13-digit barcode as SKU. Repair the whole feed when the evidence is clear.
    ean_ratio=_ean_like_ratio(out["ean"])
    sku_ratio=_ean_like_ratio(out["sku"])
    if ean_ratio < 0.25 and sku_ratio > 0.75:
        original_ean=out["ean"].copy()
        out["ean"]=out["sku"]
        out["sku"]=original_ean
    for col in ("cost","quantity","weight_kg"):
        if col not in out:out[col]=0
        out[col]=pd.to_numeric(out[col].astype(str).str.replace(",",".",regex=False),errors="coerce").fillna(0)
    return out


def apply_weight_exclusion(df: pd.DataFrame, mode: str = "none",
                           weight_from: float = 0.0,
                           weight_to: float = 0.0) -> pd.DataFrame:
    """Exclude products by weight in kilograms while retaining unknown weights.

    Supported modes are ``none``, ``above``, ``below`` and ``between``.
    Boundaries are inclusive for ``between`` and exclusive for above/below:
    "above 10" removes values greater than 10 while keeping exactly 10.
    """
    if mode=="none" or "weight_kg" not in df:
        return df.copy()
    if mode not in {"above","below","between"}:
        raise ValueError("Modalità di esclusione peso non valida.")
    lower=max(0.0,float(weight_from))
    upper=max(0.0,float(weight_to))
    if mode=="between" and lower>upper:
        raise ValueError("Nel filtro peso il valore Da non può superare il valore A.")
    weight=pd.to_numeric(df["weight_kg"],errors="coerce")
    known=weight.gt(0)
    if mode=="above":
        excluded=known & weight.gt(lower)
    elif mode=="below":
        excluded=known & weight.lt(lower)
    else:
        excluded=known & weight.between(lower,upper,inclusive="both")
    return df.loc[~excluded].copy()
