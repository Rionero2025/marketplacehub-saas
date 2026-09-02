from __future__ import annotations

import hashlib
import json
import runpy
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from marketplace_core.buybox import BuyBoxCore
from marketplace_core.jobs import JobsCore

try:
    from st_aggrid import (
        AgGrid, DataReturnMode, GridOptionsBuilder, GridUpdateMode, JsCode,
    )
except ImportError:  # pragma: no cover - fallback runtime
    AgGrid = None

from services.db import (
    database_storage_status,
    database_write_probe,
    execute,
    execute_many,
    json_text,
    now_iso,
    repair_database_permissions,
    rows,
)
from services.fx import get_ecb_rates
from services.kaufland import KauflandClient
from services.kaufland_buybox import (
    buybox_logistics_analysis,
    commission_rates_from_response,
    ean_lookup_candidates,
    minimum_price_from_composed_sku,
    minimum_price_from_units,
    own_prices_from_units,
    parse_buybox_response,
    resolve_offer_product,
    seller_pseudonyms_from_units,
    unit_id_from_units,
    unit_logistics_from_units,
)
from services.kaufland_buybox_account import (
    catalog_signature,
    clean_identifier,
    commercial_fallback,
    effective_commission,
    ensure_schema as ensure_buybox_account_schema,
    latest_order_commissions,
    load_seller_catalog,
    resolve_latest_order_commission,
    resolve_offer_cost,
)
from services.kaufland_buybox_fast import (
    QuickBuyboxNeedsFullCheck,
    quick_buybox_check,
)
from services.kaufland_live_inventory import (
    cached_summary,
    cached_units,
    ensure_schema as ensure_inventory_schema,
    sync_storefront,
)
from services.kaufland_profit import (
    buybox_financials,
    buybox_margin_tone,
    buybox_row_tone,
    minimum_price_candidate_status,
    price_financials,
)
from services.security import decrypt_dict
from services.session import bootstrap,seller_selector


def display_rome_time(value: str) -> str:
    try:
        parsed=datetime.fromisoformat(str(value or ""))
        return parsed.astimezone(ZoneInfo("Europe/Rome")).strftime(
            "%d/%m/%Y %H:%M:%S"
        )
    except (TypeError,ValueError):
        return str(value or "")


bootstrap()
buybox_core=BuyBoxCore()
jobs_core=JobsCore()
st.title("Controllo Buy Box")
seller_id=seller_selector()
if seller_id is None:st.stop()
enabled_marketplaces={
    str(item["marketplace"]).lower()
    for item in rows(
        """SELECT DISTINCT marketplace FROM marketplace_accounts
        WHERE seller_id=? AND active=1 AND marketplace IN ('kaufland','worten')""",
        (seller_id,),
    )
}
if not enabled_marketplaces:
    st.error("Configura prima un account Kaufland o Worten per questo Seller.");st.stop()
marketplace_labels={
    "Kaufland":"kaufland",
    "Worten":"worten",
}
available_labels=[
    label for label,value in marketplace_labels.items() if value in enabled_marketplaces
]
chosen_marketplace=marketplace_labels[
    st.selectbox(
        "Marketplace da controllare",
        available_labels,
        key=f"buybox_marketplace_{seller_id}",
    )
]
st.divider()
if chosen_marketplace=="worten":
    st.session_state["_embedded_worten_buybox"]=True
    try:
        runpy.run_path(
            str(Path(__file__).with_name("3_Controllo_BuyBox_Worten.py")),
            run_name="marketplace_worten_buybox",
        )
    finally:
        st.session_state.pop("_embedded_worten_buybox",None)
    st.stop()

st.subheader("Buy Box Kaufland")
st.caption(
    "Verifica la posizione delle offerte già pubblicate: vincitore, nostra posizione, "
    "prezzo totale, prezzo necessario per la Buy Box e relativo guadagno o perdita. "
    "Il controllo non modifica le offerte; il prezzo cambia soltanto tramite gli "
    "appositi pulsanti presenti nella gestione per riga."
)
st.info(
    "Calcolo economico: prezzo necessario alla Buy Box − commissione Kaufland − "
    "(costo di acquisto + spedizione del fornitore). La percentuale è calcolata "
    "sul costo totale del prodotto."
)
st.caption(
    "Commissioni: per ogni EAN e Paese il controllo legge la tariffa corrente API "
    "(percentuale variabile ed eventuale quota fissa) e calcola la percentuale "
    "effettiva sul totale dell’offerta. Quando esiste uno storico ordini, mostra "
    "separatamente anche l’ultima commissione realmente addebitata sul prodotto."
)
accounts=rows(
    "SELECT * FROM marketplace_accounts WHERE seller_id=? AND marketplace='kaufland' "
    "AND active=1 ORDER BY account_name",(seller_id,)
)
if not accounts:
    st.error("Configura prima un account Kaufland per questo Seller.");st.stop()
account_map={f"{item['account_name']} · ID {item['id']}":item for item in accounts}
account=account_map[st.selectbox("Account Kaufland",list(account_map),key="buybox_account")]
playground=st.checkbox(
    "Playground (test)",value=True,key=f"buybox_playground_{account['id']}",
    help="I risultati del Playground e della produzione sono memorizzati separatamente.",
)
environment="test" if playground else "live"
st.info(
    f"Ambiente API: {'PLAYGROUND (test)' if playground else 'PRODUZIONE'}. "
    "Il controllo è in sola lettura. Le azioni prezzo, se confermate, aggiornano "
    "esclusivamente l'offerta e il Paese indicati nella riga."
)

credentials=decrypt_dict(account["credentials_encrypted"])
client=KauflandClient(
    credentials.get("client_key",""),credentials.get("secret_key",""),playground
)
try:
    account_settings=json.loads(account.get("settings_json") or "{}")
except (TypeError,ValueError,json.JSONDecodeError):
    account_settings={}
saved_pseudonyms=account_settings.get("buybox_seller_pseudonyms") or []
if isinstance(saved_pseudonyms,str):saved_pseudonyms=[saved_pseudonyms]
legacy_pseudonym=str(account_settings.get("buybox_seller_pseudonym") or "").strip()
if legacy_pseudonym:saved_pseudonyms.append(legacy_pseudonym)
default_pseudonyms=", ".join(dict.fromkeys(
    str(value).strip() for value in saved_pseudonyms if str(value).strip()
))
pseudonym_value=st.text_input(
    "Pseudonimo Seller Kaufland",
    value=default_pseudonyms,
    placeholder="Verrà rilevato automaticamente, ad esempio Ginevra_Store",
    key=f"buybox_pseudonym_{account['id']}",
    help=(
        "Serve a riconoscere la tua offerta quando la graduatoria non espone lo SKU. "
        "Il programma prova a rilevarlo automaticamente dalle tue unità Kaufland."
    ),
)
configured_pseudonyms={
    value.strip() for value in pseudonym_value.split(",") if value.strip()
}
if st.button("Salva pseudonimo Seller",key=f"save_buybox_pseudonym_{account['id']}"):
    account_settings["buybox_seller_pseudonyms"]=sorted(configured_pseudonyms)
    account_settings.pop("buybox_seller_pseudonym",None)
    execute("UPDATE marketplace_accounts SET settings_json=? WHERE id=? AND seller_id=?",
            (json_text(account_settings),account["id"],seller_id))
    st.success("Pseudonimo Seller salvato.")

ensure_inventory_schema()
ensure_buybox_account_schema()

country_names={
    "de":"Germania","it":"Italia","fr":"Francia","at":"Austria","pl":"Polonia",
    "cz":"Rep. Ceca","sk":"Slovacchia","es":"Spagna","nl":"Paesi Bassi",
}
country_currencies={
    "de":"EUR","it":"EUR","fr":"EUR","at":"EUR","sk":"EUR",
    "es":"EUR","nl":"EUR","pl":"PLN","cz":"CZK",
}
fallback_storefronts=["de","at","pl","cz","sk","fr","it","es","nl"]

def country_label(code: str) -> str:
    value=str(code or "").strip().lower()
    return f"{country_names.get(value,value.upper())} ({value.upper()})"

def available_storefronts() -> list[str]:
    state_key=f"kaufland_buybox_storefronts_v242_{account['id']}_{environment}"
    current=st.session_state.get(state_key)
    if isinstance(current,list) and current:
        return current
    # La pagina deve aprirsi dalla cache persistente, senza una chiamata API
    # semplicemente per ricostruire l'elenco Paesi. L'API storefront viene usata
    # solo alla primissima configurazione, quando non esiste ancora alcuna traccia.
    values=[
        str(item.get("storefront") or "").strip().lower()
        for item in cached_summary(int(seller_id),int(account["id"]),environment)
    ]
    values.extend(
        str(item.get("storefront") or "").strip().lower()
        for item in rows(
            """SELECT DISTINCT storefront FROM operations
            WHERE marketplace_account_id=? AND marketplace='kaufland' AND storefront<>''""",
            (account["id"],),
        )
    )
    if not any(values):
        try:
            values.extend(client.storefronts())
        except Exception:
            pass
    values.extend(fallback_storefronts)
    cleaned=[value for value in dict.fromkeys(values) if value]
    st.session_state[state_key]=cleaned
    return cleaned

def sync_selected_storefronts(storefronts: list[str],force_full: bool=False) -> list[dict]:
    overall=st.progress(0.0)
    label=st.empty()
    results=[]
    for position,storefront in enumerate(storefronts,1):
        label.info(f"Scaricamento di tutte le offerte presenti su {country_label(storefront)}…")
        country_progress=st.progress(0.0)
        def page_progress(done,total):
            if total:
                country_progress.progress(min(1.0,done/max(1,total)))
            label.caption(
                f"{country_label(storefront)}: lette {done:,}"
                +(f" di {total:,} offerte" if total else " offerte")
            )
        try:
            result=sync_storefront(
                client,seller_id=int(seller_id),account_id=int(account["id"]),
                environment=environment,storefront=storefront,
                force_full=force_full,progress=page_progress,
            )
            results.append({**result,"ok":True})
            country_progress.progress(1.0)
        except Exception as error:
            results.append({"storefront":storefront,"ok":False,"error":str(error)})
            country_progress.empty()
        overall.progress(position/max(1,len(storefronts)))
    overall.empty();label.empty()
    return results

st.markdown("#### Offerte reali dell’account Kaufland")
st.caption(
    "Questa pagina non dipende più dal singolo listino pubblicato. Scarica tutte le "
    "unità presenti nell’account tramite GET /units, conserva il punto di ripresa e "
    "abbina ogni offerta a tutti i listini accessibili del Seller tramite EAN, SKU, "
    "prefisso fornitore e storico di pubblicazione."
)
storefronts=available_storefronts()
pre_summary=cached_summary(int(seller_id),int(account["id"]),environment)
cached_offer_total=sum(int(item.get("active_count") or 0) for item in pre_summary)
if cached_offer_total:
    latest_inventory=max(
        (str(item.get("last_sync_at") or item.get("last_seen_at") or "") for item in pre_summary),
        default="",
    )
    st.success(
        f"{cached_offer_total:,} offerte sono già memorizzate nel database locale. "
        "La pagina le carica subito senza riscaricarle da Kaufland."
        +(f" · Ultimo inventario: {display_rome_time(latest_inventory)}" if latest_inventory else "")
    )
    st.caption(
        "Per il lavoro quotidiano usa il controllo rapido Buy Box più sotto. "
        "Aggiorna l'inventario completo soltanto quando hai pubblicato/eliminato offerte "
        "o vuoi riallineare quantità e dati statici."
    )

with st.expander(
    "Aggiorna inventario offerte via API (solo quando serve)",
    expanded=not bool(cached_offer_total),
):
    download_columns=st.columns(min(5,max(1,len(storefronts))))
    download_countries=[]
    for index,code in enumerate(storefronts):
        if download_columns[index%len(download_columns)].checkbox(
            country_label(code),value=True,
            key=f"buybox_download_country_v242_{account['id']}_{environment}_{code}",
        ):
            download_countries.append(code)

    sync_col,full_col=st.columns([3,1])
    if sync_col.button(
        "Scarica/aggiorna tutte le offerte via API Kaufland",
        use_container_width=True,disabled=not download_countries,
        key=f"buybox_sync_all_v242_{account['id']}_{environment}",
    ):
        st.session_state[f"buybox_sync_result_v242_{account['id']}_{environment}"] = (
            sync_selected_storefronts(download_countries)
        )
        st.rerun()
    force_full=full_col.checkbox(
        "Risincronizzazione completa",
        key=f"buybox_force_full_v242_{account['id']}_{environment}",
    )
    if full_col.button(
        "Ricostruisci",use_container_width=True,disabled=not download_countries,
        key=f"buybox_sync_full_v242_{account['id']}_{environment}",
    ):
        if not force_full:
            st.warning("Spunta «Risincronizzazione completa» prima di ricostruire il catalogo.")
        else:
            st.session_state[f"buybox_sync_result_v242_{account['id']}_{environment}"] = (
                sync_selected_storefronts(download_countries,force_full=True)
            )
            st.session_state[f"buybox_force_full_v242_{account['id']}_{environment}"]=False
            st.rerun()

sync_result=st.session_state.pop(
    f"buybox_sync_result_v242_{account['id']}_{environment}",None
)
if sync_result:
    sync_frame=pd.DataFrame([{
        "Paese":country_label(item.get("storefront","")),
        "Esito":"OK" if item.get("ok") else "Errore",
        "Offerte presenti":item.get("seen"),
        "Nuove":item.get("inserted"),
        "Aggiornate":item.get("updated"),
        "Invariate":item.get("unchanged"),
        "Non più presenti":item.get("missing"),
        "Ripresa da":item.get("resumed_from"),
        "Errore":item.get("error",""),
    } for item in sync_result])
    st.dataframe(sync_frame,use_container_width=True,hide_index=True)
    if all(item.get("ok") for item in sync_result):
        st.success("Catalogo reale Kaufland aggiornato correttamente.")
    else:
        st.warning("Alcuni Paesi non sono stati aggiornati; i dati già salvati restano disponibili.")

summary=cached_summary(int(seller_id),int(account["id"]),environment)
available_countries=sorted({
    str(item.get("storefront") or "").lower() for item in summary
    if int(item.get("active_count") or 0)>0
})
if not available_countries:
    st.info(
        "Non sono ancora presenti offerte API memorizzate. Seleziona i Paesi e premi "
        "«Scarica/aggiorna tutte le offerte via API Kaufland»."
    )
    st.stop()
summary_frame=pd.DataFrame([{
    "Paese":country_label(item.get("storefront","")),
    "Offerte API presenti":int(item.get("active_count") or 0),
    "Ultimo aggiornamento":item.get("last_sync_at") or item.get("last_seen_at") or "",
    "Stato":item.get("last_status") or "",
    "Ripresa salvata":int(item.get("resume_offset") or 0) or "",
} for item in summary if str(item.get("storefront") or "").lower() in available_countries])
st.dataframe(summary_frame,use_container_width=True,hide_index=True)

st.markdown("#### Paesi da controllare")
country_columns=st.columns(min(4,max(1,len(available_countries))))
chosen_countries=[]
for index,code in enumerate(available_countries):
    if country_columns[index%len(country_columns)].checkbox(
        country_label(code),value=True,
        key=f"buybox_country_v203_{account['id']}_{environment}_{code}",
    ):
        chosen_countries.append(code)
if not chosen_countries:
    st.warning("Seleziona almeno un Paese.");st.stop()

live_units=cached_units(
    int(seller_id),int(account["id"]),environment,chosen_countries,present_only=True
)
inactive_statuses={"inactive","deleted","removed","closed","blocked"}
unique_units={}
for unit in live_units:
    status=str(unit.get("status") or "").strip().lower()
    if status in inactive_statuses:
        continue
    key=(str(unit.get("storefront") or "").lower(),int(unit["id_unit"]))
    unique_units[key]=unit
live_units=list(unique_units.values())
if not live_units:
    st.warning("Nei Paesi selezionati non risultano offerte attive restituite dall’API.")
    st.stop()

# Stato Buy Box già persistito: serve sia per mostrare immediatamente l'ultima
# fotografia sia per evitare di ricalcolare costi/listini ad ogni apertura.
_saved_placeholders=",".join("?" for _ in chosen_countries)
saved_check_rows=rows(
    f"""SELECT * FROM kaufland_buybox_account_checks
    WHERE seller_id=? AND marketplace_account_id=? AND environment=?
      AND storefront IN ({_saved_placeholders})""",
    (seller_id,account["id"],environment,*chosen_countries),
)
saved_checks_by_offer={
    (str(item.get("storefront") or "").strip().lower(),str(item.get("sku") or "").strip()):dict(item)
    for item in saved_check_rows
}
uncached_live_count=sum(
    1 for item in live_units
    if (str(item.get("storefront") or "").strip().lower(),str(item.get("id_offer") or "").strip())
       not in saved_checks_by_offer
)

@st.cache_resource(ttl=300,show_spinner=False)
def cached_seller_catalog_v203(seller: int,account_id: int,env: str,signature: str):
    return load_seller_catalog(seller,account_id,env)

if uncached_live_count:
    with st.spinner(
        f"Indicizzazione listini solo per {uncached_live_count:,} offerte non ancora memorizzate…"
    ):
        catalog=cached_seller_catalog_v203(
            int(seller_id),int(account["id"]),environment,
            catalog_signature(int(seller_id),int(account["id"])),
        )
    if not catalog.get("sources"):
        st.warning(
            "Non è stato possibile leggere alcun listino associato al Seller. Le offerte API "
            "restano visibili, ma i margini delle nuove offerte non saranno calcolabili."
        )
    if catalog.get("unavailable"):
        st.warning(
            f"{len(catalog['unavailable']):,} sorgenti listino non erano leggibili. "
            "Il programma ha continuato con tutte le altre sorgenti disponibili."
        )
else:
    # Tutte le offerte hanno già un controllo persistito: non ricaricare i listini.
    catalog={
        "sources":[],"unavailable":[],
        "list_count":len({item.get("matched_price_list_id") for item in saved_check_rows if item.get("matched_price_list_id")}),
        "source_count":0,
    }
    st.caption(
        "Costi, commissioni e abbinamenti listino sono già memorizzati: nessuna "
        "reindicizzazione dei listini necessaria per questa apertura."
    )

fx_data=None
if any(code in {"pl","cz"} for code in chosen_countries):
    fx_data=get_ecb_rates()
fx_rates=(fx_data or {}).get("rates",{})

records=[];economics_by_offer={}
for item in live_units:
    storefront=str(item.get("storefront") or "").lower()
    offer_id=clean_identifier(item.get("id_offer"))
    ean=clean_identifier(item.get("ean"))
    previous_check=saved_checks_by_offer.get((storefront,offer_id))
    if previous_check:
        match={
            "matched_price_list_id":previous_check.get("matched_price_list_id"),
            "matched_saved_view_id":previous_check.get("matched_saved_view_id"),
            "supplier_name":previous_check.get("supplier_name") or "",
            "price_list_name":previous_check.get("price_list_name") or "",
            "cost_match_source":previous_check.get("cost_match_source") or "Memoria Buy Box",
            "cost_match_count":int(previous_check.get("cost_match_count") or 0),
            "purchase_cost_eur":previous_check.get("purchase_cost_eur"),
            "shipping_cost_eur":previous_check.get("shipping_cost_eur"),
            "total_cost_eur":previous_check.get("total_cost_eur"),
            "original_sku":previous_check.get("original_sku") or "",
            "alternative_matches":[],
        }
        fallback={
            "commission_pct":float(previous_check.get("commission_pct") or 15),
            "source":previous_check.get("commission_source") or "Memoria Buy Box",
            "currency":previous_check.get("currency") or country_currencies.get(storefront,"EUR"),
            "multiplier":1,
        }
    else:
        match=resolve_offer_cost(
            catalog,seller_id=int(seller_id),storefront=storefront,
            id_offer=offer_id,ean=ean,
        )
        fallback=commercial_fallback(
            int(seller_id),price_list_id=match.get("matched_price_list_id"),
            storefront=storefront,
        )
    currency=str(fallback.get("currency") or country_currencies.get(storefront,"EUR")).upper()
    multiplier=float(fallback.get("multiplier") or 1)
    if currency!="EUR" and multiplier<=1:
        multiplier=float(fx_rates.get(currency,1) or 1)
    listing_price=(
        float(item["listing_price_cents"])/100
        if item.get("listing_price_cents") not in (None,"") else None
    )
    minimum_price=(
        float(item["minimum_price_cents"])/100
        if item.get("minimum_price_cents") not in (None,"") else None
    )
    economics={
        "purchase_cost_eur":match.get("purchase_cost_eur"),
        "shipping_cost_eur":match.get("shipping_cost_eur"),
        "total_cost_eur":match.get("total_cost_eur"),
        "commission_pct":float(fallback.get("commission_pct") or 15),
        "commission_source":fallback.get("source",""),
        "publication_currency":currency,
        "eur_multiplier":multiplier,
        "matched_price_list_id":match.get("matched_price_list_id"),
        "matched_saved_view_id":match.get("matched_saved_view_id"),
        "supplier_name":match.get("supplier_name",""),
        "price_list_name":match.get("price_list_name",""),
        "cost_match_source":match.get("cost_match_source",""),
        "cost_match_count":int(match.get("cost_match_count") or 0),
        "alternative_matches":match.get("alternative_matches") or [],
    }
    economics_by_offer[(storefront,offer_id)]=economics
    row_key=hashlib.sha256(
        f"{storefront}\0{item['id_unit']}\0{offer_id}".encode("utf-8")
    ).hexdigest()[:24]
    records.append({
        "_key":row_key,
        "Paese":country_names.get(storefront,storefront.upper()),
        "paese":storefront,
        "EAN":ean,
        "SKU inviato":offer_id,
        "SKU originale":match.get("original_sku",""),
        "Prodotto":str(item.get("title") or ""),
        "Fornitore":match.get("supplier_name") or "Non associato",
        "Listino abbinato":match.get("price_list_name") or "Non associato",
        "Origine costo":match.get("cost_match_source",""),
        "Corrispondenze":int(match.get("cost_match_count") or 0),
        "Costo acquisto €":match.get("purchase_cost_eur"),
        "Spedizione fornitore €":match.get("shipping_cost_eur"),
        "Costo totale €":match.get("total_cost_eur"),
        "Commissione di riserva %":float(fallback.get("commission_pct") or 15),
        "Prezzo API":listing_price,
        "Prezzo minimo API":minimum_price,
        "Quantità":item.get("amount"),
        "Stato API":item.get("status",""),
        "Ultima modifica API":item.get("date_lastchange_iso",""),
        "Ultimo stato Buy Box":(previous_check or {}).get("status") or "Mai controllata",
        "Ultimo controllo":(previous_check or {}).get("checked_at") or "",
        "Ultima posizione":(previous_check or {}).get("our_rank"),
        "_currency":currency,
        "_eur_multiplier":multiplier,
        "_id_product":item.get("id_product"),
        "_id_unit":item.get("id_unit"),
        "_matched_price_list_id":match.get("matched_price_list_id"),
        "_matched_saved_view_id":match.get("matched_saved_view_id"),
        "_alternative_matches":match.get("alternative_matches") or [],
    })

offer_frame=pd.DataFrame(records)
filter1,filter2,filter3=st.columns(3)
search=filter1.text_input(
    "Cerca nelle offerte",placeholder="EAN, SKU, prodotto, fornitore o listino…",
    key=f"buybox_search_v203_{account['id']}_{environment}",
).strip().lower()
supplier_options=sorted(value for value in offer_frame["Fornitore"].dropna().unique() if str(value).strip())
list_options=sorted(value for value in offer_frame["Listino abbinato"].dropna().unique() if str(value).strip())
supplier_filter=filter2.multiselect(
    "Fornitori",supplier_options,key=f"buybox_supplier_filter_v203_{account['id']}_{environment}"
)
list_filter=filter3.multiselect(
    "Listini abbinati",list_options,key=f"buybox_list_filter_v203_{account['id']}_{environment}"
)
if search:
    offer_frame=offer_frame[offer_frame.apply(
        lambda row: search in " ".join(str(row.get(column) or "") for column in (
            "EAN","SKU inviato","SKU originale","Prodotto","Fornitore","Listino abbinato"
        )).lower(),axis=1,
    )].reset_index(drop=True)
if supplier_filter:
    offer_frame=offer_frame[offer_frame["Fornitore"].isin(supplier_filter)].reset_index(drop=True)
if list_filter:
    offer_frame=offer_frame[offer_frame["Listino abbinato"].isin(list_filter)].reset_index(drop=True)
if offer_frame.empty:
    st.warning("Nessuna offerta corrisponde ai filtri.");st.stop()

metrics=st.columns(5)
metrics[0].metric("Offerte memorizzate",len(offer_frame))
metrics[1].metric(
    "Già controllate",
    int((offer_frame["Ultimo stato Buy Box"]!="Mai controllata").sum()),
)
metrics[2].metric("Con costo trovato",int(offer_frame["Costo totale €"].notna().sum()))
metrics[3].metric("Senza costo",int(offer_frame["Costo totale €"].isna().sum()))
metrics[4].metric("Listini consultati ora",int(catalog.get("list_count") or 0) if uncached_live_count else 0)
st.caption(
    f"Sorgenti indicizzate: {int(catalog.get('source_count') or 0):,}. "
    "In caso di più corrispondenze viene privilegiato il listino dello storico di "
    "pubblicazione, poi il prefisso fornitore, l’EAN e infine lo SKU/codice."
)

scope=(
    f"{seller_id}_{account['id']}_{environment}_{'-'.join(chosen_countries)}_"
    f"{hashlib.sha1((search+'|'+','.join(supplier_filter)+'|'+','.join(list_filter)).encode()).hexdigest()[:8]}"
)
selected_key=f"buybox_selected_{scope}"
grid_key=f"buybox_grid_{scope}"
selected_defaults=set(st.session_state.get(selected_key,[]))

st.markdown("#### Offerte da verificare")
b1,b2,b3,b4,b5=st.columns([1.2,1.2,.8,.8,1.4])
if b1.button("☑ Seleziona tutte",key=f"buybox_all_{scope}",use_container_width=True):
    st.session_state[selected_key]=offer_frame["_key"].tolist()
    st.session_state.pop(grid_key,None);st.rerun()
if b2.button("☐ Deseleziona tutte",key=f"buybox_none_{scope}",use_container_width=True):
    st.session_state[selected_key]=[]
    st.session_state.pop(grid_key,None);st.rerun()
maximum=len(offer_frame)
start=b3.number_input(
    "Da posizione",min_value=1,max_value=maximum,value=1,step=1,key=f"buybox_from_{scope}"
)
end=b4.number_input(
    "A posizione",min_value=1,max_value=maximum,value=min(100,maximum),step=1,
    key=f"buybox_to_{scope}"
)
if b5.button("Seleziona intervallo Da/A",key=f"buybox_range_{scope}",use_container_width=True):
    low,high=sorted((int(start),int(end)))
    st.session_state[selected_key]=offer_frame.iloc[low-1:high]["_key"].tolist()
    st.session_state.pop(grid_key,None);st.rerun()

offer_frame.insert(0,"Seleziona",offer_frame["_key"].isin(selected_defaults))
editor_columns=[
    "Seleziona","Paese","EAN","SKU inviato","SKU originale","Prodotto",
    "Fornitore","Listino abbinato","Origine costo","Corrispondenze",
    "Costo acquisto €","Spedizione fornitore €","Costo totale €",
    "Prezzo API","Prezzo minimo API","Quantità","Stato API",
    "Ultimo stato Buy Box","Ultima posizione","Ultimo controllo","Ultima modifica API",
]
edited=st.data_editor(
    offer_frame[editor_columns],use_container_width=True,height=480,hide_index=True,key=grid_key,
    column_config={
        "Seleziona":st.column_config.CheckboxColumn(required=True),
        "Corrispondenze":st.column_config.NumberColumn(format="%d"),
        "Quantità":st.column_config.NumberColumn(format="%d"),
    },
    disabled=[column for column in editor_columns if column!="Seleziona"],
)
chosen_indexes=edited.index[edited["Seleziona"]==True]
chosen=offer_frame.loc[chosen_indexes].copy()
st.session_state[selected_key]=chosen["_key"].tolist()
c1,c2=st.columns(2)
c1.metric("Offerte selezionate",len(chosen))
c2.metric("Offerte disponibili",len(offer_frame))

def buybox_payload(result: dict) -> dict:
    return {
        "seller_id":seller_id,
        "marketplace_account_id":account["id"],
        "matched_price_list_id":result.get("matched_price_list_id"),
        "matched_saved_view_id":result.get("matched_saved_view_id"),
        "supplier_name":result.get("supplier_name",""),
        "price_list_name":result.get("price_list_name",""),
        "cost_match_source":result.get("cost_match_source",""),
        "cost_match_count":result.get("cost_match_count",0),
        "storefront":result["paese"],
        "environment":environment,
        "ean":result["ean"],
        "sku":result["sku"],
        "original_sku":result.get("original_sku",""),
        "product_title":result.get("product_title",""),
        "inventory_status":result.get("inventory_status",""),
        "inventory_amount":result.get("inventory_amount"),
        "id_product":result.get("id_product"),
        "id_unit":result.get("id_unit"),
        "status":result["status"],
        "our_rank":result.get("our_rank"),
        "winner_seller":result.get("winner_seller",""),
        "winner_price":result.get("winner_price"),
        "winner_shipping":result.get("winner_shipping"),
        "winner_total":result.get("winner_total"),
        "our_price":result.get("our_price"),
        "our_shipping":result.get("our_shipping"),
        "our_total":result.get("our_total"),
        "minimum_price":result.get("minimum_price"),
        "minimum_price_source":result.get("minimum_price_source",""),
        "target_price":result.get("target_price"),
        "currency":result.get("currency",""),
        "delivery_min":result.get("delivery_min"),
        "delivery_max":result.get("delivery_max"),
        "own_delivery_min":result.get("own_delivery_min"),
        "own_delivery_max":result.get("own_delivery_max"),
        "own_handling_time":result.get("own_handling_time"),
        "logistics_status":result.get("logistics_status",""),
        "offer_count":result.get("offer_count",0),
        "error_type":result.get("error_type",""),
        "error":result.get("error",""),
        "details_json":json_text(result.get("details",{})),
        "purchase_cost_eur":result.get("purchase_cost_eur"),
        "shipping_cost_eur":result.get("shipping_cost_eur"),
        "total_cost_eur":result.get("total_cost_eur"),
        "commission_pct":result.get("commission_pct"),
        "commission_fixed_eur":result.get("commission_fixed_eur",0),
        "commission_source":result.get("commission_source",""),
        "current_commission_eur":result.get("current_commission_eur"),
        "current_commission_effective_pct":result.get("current_commission_effective_pct"),
        "actual_order_commission_pct":result.get("actual_order_commission_pct"),
        "actual_order_commission_local":result.get("actual_order_commission_local"),
        "actual_order_commission_currency":result.get("actual_order_commission_currency",""),
        "actual_order_id":result.get("actual_order_id",""),
        "target_sales_price":result.get("target_sales_price"),
        "target_sales_price_eur":result.get("target_sales_price_eur"),
        "target_source":result.get("target_source",""),
        "target_commission_eur":result.get("target_commission_eur"),
        "target_commission_effective_pct":result.get("target_commission_effective_pct"),
        "profit_eur":result.get("profit_eur"),
        "profit_pct":result.get("profit_pct"),
        "profit_status":result.get("profit_status",""),
        "checked_at":result["checked_at"],
    }


def save_checks(results: list[dict]) -> int:
    if not results:
        return 0
    payloads=[buybox_payload(result) for result in results]
    columns=list(payloads[0])
    immutable={"seller_id","marketplace_account_id","storefront","environment","sku"}
    mutable=[column for column in columns if column not in immutable]
    return execute_many(
        f"""INSERT INTO kaufland_buybox_account_checks({','.join(columns)})
        VALUES({','.join('?' for _ in columns)})
        ON CONFLICT(marketplace_account_id,storefront,environment,sku)
        DO UPDATE SET {','.join(f'{column}=excluded.{column}' for column in mutable)}""",
        [tuple(payload[column] for column in columns) for payload in payloads],
    )


pending_save_key=f"buybox_pending_save_{scope}"
pending_save=st.session_state.get(pending_save_key,[])
if pending_save:
    st.warning(
        f"Ci sono {len(pending_save):,} risultati Buy Box già controllati ma non ancora "
        "salvati a causa di un precedente problema di accesso al database."
    )
    if st.button(
        "Riprova il salvataggio dei risultati già controllati",
        key=f"retry_{pending_save_key}",
    ):
        try:
            repair_database_permissions(force=True)
            database_write_probe()
            save_checks(list(pending_save))
            st.session_state.pop(pending_save_key,None)
            st.success("Risultati recuperati e salvati correttamente.")
            st.rerun()
        except Exception as error:
            status=database_storage_status()
            st.error(
                "Il database non è ancora scrivibile. Chiudi eventuali copie di "
                "Marketplace Hub, verifica la cartella indicata e riavvia il programma. "
                f"Percorso: {status['database_path']} · Dettaglio: {error}"
            )


st.markdown("#### Controllo rapido dello stato Buy Box")
st.caption(
    "Usa le offerte, i costi e le commissioni già memorizzati. Per ogni offerta fa "
    "soltanto il controllo Buy Box live, senza riscaricare l'inventario, senza "
    "ricaricare listini e senza richiedere di nuovo commissioni e dati prodotto."
)
quick_cols=st.columns([3,2])
if quick_cols[0].button(
    "⚡ Controlla solo se siamo ancora in Buy Box",
    type="primary", use_container_width=True, disabled=chosen.empty,
    key=f"buybox_quick_execute_v305_{scope}",
):
    quick_tasks = tuple(item.to_dict() for _, item in chosen.iterrows())
    request = buybox_core.build_refresh_job(
        BuyBoxScope(int(seller_id), int(account["id"]), "kaufland", environment),
        mode="quick",
        storefronts=tuple(chosen_countries),
        skus=tuple(str(item.get("SKU inviato") or "") for item in quick_tasks),
        tasks=quick_tasks,
        own_seller_pseudonyms=tuple(sorted(configured_pseudonyms)),
        max_workers=20,
    )
    receipt = jobs_core.submit(request)
    jobs_core.start_local(receipt.job_id)
    st.session_state[f"buybox_quick_job_{scope}"] = receipt.job_id
    st.success(
        "Controllo Buy Box avviato in background. Puoi cambiare pagina e continuare a lavorare."
    )

quick_job_id = st.session_state.get(f"buybox_quick_job_{scope}")
if quick_job_id:
    quick_job = jobs_core.snapshot(quick_job_id)
    if quick_job:
        st.progress(
            min(1.0, max(0.0, quick_job.progress_pct / 100.0)),
            text=quick_job.message or quick_job.status,
        )
        qj1, qj2 = st.columns([1, 4])
        if qj1.button("Aggiorna stato", key=f"buybox_job_refresh_{quick_job_id}"):
            st.rerun()
        if quick_job.status == "done":
            result = dict(quick_job.result)
            st.success(
                f"Controllo completato · aggiornate {result.get('successful', 0)} / "
                f"{result.get('total', 0)} · errori {result.get('errors', 0)} · "
                f"controllo completo necessario {result.get('needs_full', 0)}."
            )
        elif quick_job.status == "error":
            st.error(f"Controllo Buy Box non riuscito: {quick_job.error}")
        else:
            qj2.caption(
                f"Job {quick_job.job_id[:8]} · {quick_job.status}. "
                "Il lavoro continua anche se navighi in un'altra sezione."
            )

st.markdown("##### Controllo completo / diagnostica")

if st.button(
    "Controllo completo (costi, commissioni e logistica)",
    disabled=chosen.empty,key=f"buybox_execute_{scope}",
):
    try:
        # Verifica reale prima di iniziare centinaia o migliaia di chiamate API.
        # Se il database o la cartella sono stati copiati con l'attributo
        # Windows "sola lettura", il programma prova prima a ripararli.
        repair_database_permissions(force=True)
        database_write_probe()
    except Exception as error:
        status=database_storage_status()
        st.error(
            "Controllo Buy Box non avviato: il database locale non è scrivibile. "
            "Marketplace Hub ha tentato la riparazione automatica senza modificare "
            "o ricreare i dati. Chiudi tutte le altre copie del programma e riavvialo. "
            f"Database: {status['database_path']} · Dettaglio: {error}"
        )
        st.stop()
    cached_rows=rows("""SELECT storefront,ean,id_product
        FROM kaufland_buybox_account_checks
        WHERE marketplace_account_id=? AND environment=?
          AND id_product IS NOT NULL""",(account["id"],environment))
    cached_ids={(str(item["storefront"]),str(item["ean"])):int(item["id_product"])
                for item in cached_rows}
    rate_lock=threading.Lock();request_times=deque()
    def throttle():
        while True:
            with rate_lock:
                current=time.monotonic()
                while request_times and current-request_times[0]>=1.0:request_times.popleft()
                if len(request_times)<10:
                    request_times.append(current);return
                delay=max(.01,1.0-(current-request_times[0]))
            time.sleep(delay)
    client.before_request=throttle

    tasks=[item.to_dict() for _,item in chosen.iterrows()]
    order_commission_index=latest_order_commissions(
        int(seller_id),int(account["id"]),environment
    )
    commission_by_offer={}
    commission_lookup_errors=[]
    for storefront in sorted({str(item["paese"]) for item in tasks}):
        country_tasks=[item for item in tasks if str(item["paese"])==storefront]
        original_eans=list(dict.fromkeys(
            str(item["EAN"]).strip() for item in country_tasks
            if str(item["EAN"]).strip()
        ))
        api_eans=list(dict.fromkeys(
            candidate
            for original in original_eans
            for candidate in ean_lookup_candidates(original)
        ))
        api_results={}
        for chunk_start in range(0,len(api_eans),50):
            chunk=api_eans[chunk_start:chunk_start+50]
            try:
                api_results.update(commission_rates_from_response(
                    client.commission_rates(chunk,storefront)
                ))
            except Exception as error:
                commission_lookup_errors.append(
                    f"{storefront.upper()}: {error}"
                )
        for original in original_eans:
            candidates=[
                api_results[candidate]
                for candidate in ean_lookup_candidates(original)
                if candidate in api_results
            ]
            selected_rate=next(
                (rate for rate in candidates if rate.get("status")=="OK"),
                candidates[0] if candidates else None,
            )
            if selected_rate is not None:
                commission_by_offer[(storefront,original)]=selected_rate
    if commission_lookup_errors:
        st.warning(
            "Per alcune righe Kaufland non ha restituito la tariffa corrente. "
            "Il programma userà prima l’ultima commissione reale di un ordine dello "
            "stesso prodotto e soltanto dopo la regola commerciale di riserva. Dettagli: "
            +" | ".join(dict.fromkeys(commission_lookup_errors))
        )

    runtime_pseudonyms=set(configured_pseudonyms)
    preflight_detected:set[str]=set()
    if not runtime_pseudonyms:
        label=st.empty()
        label.caption("Rilevamento automatico del pseudonimo Seller Kaufland…")
        for candidate in tasks[:10]:
            try:
                own_units=client.units(
                    str(candidate["SKU inviato"]).strip(),
                    str(candidate["paese"]).strip(),
                    embedded="seller",
                )
                preflight_detected=seller_pseudonyms_from_units(own_units)
                if preflight_detected:
                    runtime_pseudonyms.update(preflight_detected);break
            except Exception:
                continue
        if runtime_pseudonyms:
            label.caption(
                "Pseudonimo Seller riconosciuto: "+", ".join(sorted(runtime_pseudonyms))
            )
        else:
            label.warning(
                "Pseudonimo non rilevato automaticamente. Le offerte senza SKU esposto "
                "resteranno «Da identificare»: inseriscilo nel campo sopra e ripeti il controllo."
            )

    def optional_number(value):
        return None if value is None or pd.isna(value) else float(value)

    def check_one(item: dict) -> dict:
        publication_currency=str(item.get("_currency") or "EUR").upper()
        eur_multiplier=float(item.get("_eur_multiplier",1) or 1)
        configured_commission=float(
            item.get("Commissione di riserva %",15) or 15
        )
        country=str(item["paese"])
        ean=str(item["EAN"]).strip()
        sku=str(item["SKU inviato"]).strip()
        actual_order=resolve_latest_order_commission(
            order_commission_index,country,sku,ean
        )
        commission_lookup=commission_by_offer.get((country,ean))
        commission_status=(
            str(commission_lookup.get("status") or "")
            if commission_lookup else "API_NON_DISPONIBILE"
        )
        if commission_lookup and commission_status=="OK" and (
            commission_lookup.get("variable_fee") is not None
        ):
            effective_commission=max(0.0,float(commission_lookup["variable_fee"]))
            fixed_local=max(
                0.0,float(commission_lookup.get("fixed_fee_minor") or 0)
            )/100
            fixed_eur=(
                fixed_local/eur_multiplier
                if publication_currency!="EUR" and eur_multiplier>0
                else fixed_local
            )
            commission_source="API Kaufland · tariffa corrente per EAN/Paese"
        elif actual_order and float(actual_order.get("rate") or 0)>0:
            effective_commission=float(actual_order["rate"])
            fixed_eur=0.0
            commission_source=(
                f"Ultimo ordine reale Kaufland {actual_order.get('id_order') or ''} "
                f"(fallback tariffa: {commission_status})"
            ).strip()
        else:
            effective_commission=configured_commission
            fixed_eur=0.0
            commission_source=f"Regola commerciale di riserva ({commission_status})"

        base={
            "paese":country,
            "ean":ean,
            "sku":sku,
            "original_sku":str(item.get("SKU originale") or "").strip(),
            "product_title":str(item.get("Prodotto") or "").strip(),
            "inventory_status":str(item.get("Stato API") or "").strip(),
            "inventory_amount":(
                None if item.get("Quantità") is None or pd.isna(item.get("Quantità"))
                else int(item.get("Quantità"))
            ),
            "matched_price_list_id":(
                None if item.get("_matched_price_list_id") is None
                or pd.isna(item.get("_matched_price_list_id"))
                else int(item.get("_matched_price_list_id"))
            ),
            "matched_saved_view_id":(
                None if item.get("_matched_saved_view_id") is None
                or pd.isna(item.get("_matched_saved_view_id"))
                else int(item.get("_matched_saved_view_id"))
            ),
            "supplier_name":str(item.get("Fornitore") or "").strip(),
            "price_list_name":str(item.get("Listino abbinato") or "").strip(),
            "cost_match_source":str(item.get("Origine costo") or "").strip(),
            "cost_match_count":int(item.get("Corrispondenze") or 0),
            "purchase_cost_eur":optional_number(item.get("Costo acquisto €")),
            "shipping_cost_eur":optional_number(item.get("Spedizione fornitore €")),
            "total_cost_eur":optional_number(item.get("Costo totale €")),
            "commission_pct":effective_commission,
            "commission_fixed_eur":round(fixed_eur,2),
            "commission_source":commission_source,
            "commission_status":commission_status,
            "actual_order_commission_pct":(
                float(actual_order.get("rate")) if actual_order else None
            ),
            "actual_order_commission_local":(
                actual_order.get("amount_local") if actual_order else None
            ),
            "actual_order_commission_currency":(
                actual_order.get("currency","") if actual_order else ""
            ),
            "actual_order_id":actual_order.get("id_order","") if actual_order else "",
            "publication_currency":publication_currency,
            "eur_multiplier":eur_multiplier,
            "checked_at":now_iso(),
        }
        live_id_product=(
            None if item.get("_id_product") is None or pd.isna(item.get("_id_product"))
            else int(item.get("_id_product"))
        )
        live_id_unit=(
            None if item.get("_id_unit") is None or pd.isna(item.get("_id_unit"))
            else int(item.get("_id_unit"))
        )
        try:
            if not base["ean"]:
                raise ValueError("EAN mancante nell’offerta reale restituita da Kaufland.")
            lookup_payload=None
            resolution={"lookup_ean":"","unit_error":""}
            unit_payload=[]
            id_product=live_id_product or cached_ids.get((base["paese"],base["ean"]))
            try:
                unit_payload=client.units(
                    base["sku"],base["paese"],embedded="seller"
                )
            except Exception as unit_error:
                resolution["unit_error"]=str(unit_error)
            if id_product is None:
                resolved=resolve_offer_product(
                    client,base["sku"],base["ean"],base["paese"],
                    cached_ids.get((base["paese"],base["ean"])),
                )
                id_product=resolved["id_product"]
                lookup_payload=resolved.get("lookup")
                if not unit_payload:
                    unit_payload=resolved.get("units") or []
                resolution=resolved
            buybox_response=client.buybox(
                id_product,base["paese"],condition="new",limit=10
            )
            detected_pseudonyms=seller_pseudonyms_from_units(unit_payload)
            normalized=parse_buybox_response(
                buybox_response,base["sku"],runtime_pseudonyms|detected_pseudonyms
            )
            id_unit=(
                live_id_unit
                or unit_id_from_units(unit_payload,base["sku"])
                or normalized.get("id_unit")
            )
            unit_detail={}
            unit_detail_error=""
            if id_unit not in (None,""):
                try:
                    unit_detail=client.unit(
                        int(id_unit),base["paese"],embedded="products"
                    )
                except Exception as error:
                    unit_detail_error=str(error)
            unit_sources=[unit_detail,unit_payload] if unit_detail else unit_payload
            minimum_price=optional_number(item.get("Prezzo minimo API"))
            minimum_price_source="Manifest API GET /units" if minimum_price is not None else ""
            if minimum_price is None:
                minimum_price=minimum_price_from_units(
                    unit_sources,base["sku"],expected_unit_id=id_unit
                )
                if minimum_price is not None:
                    minimum_price_source="Dettaglio API Kaufland"
            if minimum_price is None:
                sent_minimum_eur=minimum_price_from_composed_sku(
                    base["sku"],base["ean"]
                )
                if sent_minimum_eur is not None:
                    minimum_price=round(
                        sent_minimum_eur*float(base["eur_multiplier"] or 1),2
                    )
                    minimum_price_source="SKU composto"
            unit_logistics=unit_logistics_from_units(
                unit_sources,base["sku"],expected_unit_id=id_unit
            )
            own_unit_prices=own_prices_from_units(unit_sources)
            manifest_price=optional_number(item.get("Prezzo API"))
            if normalized.get("our_price") is None:
                normalized["our_price"]=own_unit_prices.get("price") or manifest_price
            if normalized.get("our_shipping") is None:
                normalized["our_shipping"]=own_unit_prices.get("shipping")
            if normalized.get("our_total") is None:
                normalized["our_total"]=own_unit_prices.get("total")
            if normalized.get("our_total") is None and normalized.get("our_price") is not None:
                normalized["our_total"]=(
                    float(normalized["our_price"])+float(normalized.get("our_shipping") or 0)
                )
            if not normalized.get("currency"):
                normalized["currency"]=own_unit_prices.get("currency","")
            if normalized.get("own_delivery_min") is None:
                normalized["own_delivery_min"]=unit_logistics.get("delivery_time_min")
            if normalized.get("own_delivery_max") is None:
                normalized["own_delivery_max"]=unit_logistics.get("delivery_time_max")
            if normalized.get("own_delivery_min") is None and (
                unit_logistics.get("handling_time") is not None
                and unit_logistics.get("transport_time_min") is not None
            ):
                normalized["own_delivery_min"]=(
                    int(unit_logistics["handling_time"])+int(unit_logistics["transport_time_min"])
                )
            if normalized.get("own_delivery_max") is None and (
                unit_logistics.get("handling_time") is not None
                and unit_logistics.get("transport_time_max") is not None
            ):
                normalized["own_delivery_max"]=(
                    int(unit_logistics["handling_time"])+int(unit_logistics["transport_time_max"])
                )
            if (normalized["status"]=="Non classificata" and unit_payload
                    and (runtime_pseudonyms or detected_pseudonyms)):
                normalized["status"]="Oltre top 10"
            normalized["currency"]=(
                str(normalized.get("currency") or base["publication_currency"]).upper()
            )
            financial=buybox_financials(
                total_cost_eur=base["total_cost_eur"],
                commission_pct=base["commission_pct"],
                commission_fixed_eur=base["commission_fixed_eur"],
                currency=normalized["currency"],
                eur_multiplier=base["eur_multiplier"],
                status=normalized["status"],
                target_price=normalized.get("target_price"),
                winner_price=normalized.get("winner_price"),
                winner_total=normalized.get("winner_total"),
                our_price=normalized.get("our_price"),
                our_shipping=normalized.get("our_shipping"),
            )
            current_total=normalized.get("our_total")
            current_total_eur=(
                float(current_total)/base["eur_multiplier"]
                if current_total not in (None,"")
                and normalized["currency"]!="EUR" and base["eur_multiplier"]>0
                else current_total
            )
            current_commission=effective_commission(
                current_total_eur,base["commission_pct"],base["commission_fixed_eur"]
            )
            target_total_eur=None
            if financial.get("target_sales_price") is not None:
                target_total=float(financial["target_sales_price"])+float(normalized.get("our_shipping") or 0)
                target_total_eur=(
                    target_total/base["eur_multiplier"]
                    if normalized["currency"]!="EUR" and base["eur_multiplier"]>0
                    else target_total
                )
            target_commission=effective_commission(
                target_total_eur,base["commission_pct"],base["commission_fixed_eur"]
            )
            logistics_analysis=buybox_logistics_analysis(
                status=normalized["status"],
                winner_price=normalized.get("winner_price"),
                winner_total=normalized.get("winner_total"),
                our_price=normalized.get("our_price"),
                our_total=normalized.get("our_total"),
                winner_delivery_min=normalized.get("delivery_min"),
                winner_delivery_max=normalized.get("delivery_max"),
                our_delivery_min=normalized.get("own_delivery_min"),
                our_delivery_max=normalized.get("own_delivery_max"),
            )
            logistics_message=str(logistics_analysis.get("message") or "").strip()
            if not logistics_message and (
                normalized.get("own_delivery_min") is None
                and normalized.get("own_delivery_max") is None
                and unit_logistics.get("handling_time") is None
            ):
                logistics_message=(
                    "Kaufland non ha restituito tempi di consegna o giorni di gestione "
                    "per questa unità. Verifica il gruppo di spedizione associato."
                )
            return {
                **base,**normalized,**financial,
                "id_product":id_product,"id_unit":id_unit,
                "minimum_price":minimum_price,
                "minimum_price_source":minimum_price_source,
                "own_handling_time":unit_logistics.get("handling_time"),
                "logistics_status":logistics_message,
                "current_commission_eur":current_commission.get("commission_eur"),
                "current_commission_effective_pct":current_commission.get("effective_pct"),
                "target_commission_effective_pct":target_commission.get("effective_pct"),
                "ok":True,"error_type":"","error":"",
                "detected_pseudonyms":sorted(detected_pseudonyms),
                "details":{
                    "lookup":lookup_payload,"buybox":buybox_response,
                    "own_units":unit_payload,"own_unit_detail":unit_detail,
                    "lookup_ean":resolution.get("lookup_ean",""),
                    "unit_lookup_error":resolution.get("unit_error",""),
                    "unit_detail_error":unit_detail_error,
                    "cost_alternatives":item.get("_alternative_matches") or [],
                    "commission_lookup":commission_lookup or {},
                    "actual_order_commission":actual_order or {},
                },
            }
        except Exception as error:
            error_type=str(getattr(error,"error_type","Errore tecnico/API Kaufland"))
            error_status=str(getattr(error,"status","Errore"))
            financial=buybox_financials(
                total_cost_eur=base.get("total_cost_eur"),
                commission_pct=base.get("commission_pct"),
                commission_fixed_eur=base.get("commission_fixed_eur",0),
                currency=base.get("publication_currency","EUR"),
                eur_multiplier=base.get("eur_multiplier",1),
                status="Errore",
            )
            return {
                **base,"id_product":live_id_product or cached_ids.get((base["paese"],base["ean"])),
                "id_unit":live_id_unit,
                "minimum_price":optional_number(item.get("Prezzo minimo API")),
                "minimum_price_source":"Manifest API GET /units",
                "own_delivery_min":None,"own_delivery_max":None,
                "own_handling_time":None,"logistics_status":"",
                "status":error_status,"our_rank":None,"winner_seller":"",
                "winner_price":None,"winner_shipping":None,"winner_total":None,
                "our_price":optional_number(item.get("Prezzo API")),
                "our_shipping":None,"our_total":optional_number(item.get("Prezzo API")),
                "target_price":None,"currency":base.get("publication_currency","EUR"),
                "delivery_min":None,"delivery_max":None,"offer_count":0,"ok":False,
                "error_type":error_type,"error":str(error),
                "current_commission_eur":None,
                "current_commission_effective_pct":None,
                "target_commission_effective_pct":None,
                **financial,"detected_pseudonyms":[],
                "details":{
                    "cost_alternatives":item.get("_alternative_matches") or [],
                    "commission_lookup":commission_lookup or {},
                    "actual_order_commission":actual_order or {},
                },
            }

    progress=st.progress(0.0);label=st.empty();live_table=st.empty();results=[]
    pending_batch=[];storage_failure=None;saved_count=0
    with ThreadPoolExecutor(max_workers=min(8,len(tasks))) as executor:
        futures=[executor.submit(check_one,item) for item in tasks]
        for completed,future in enumerate(as_completed(futures),1):
            result=future.result();results.append(result);pending_batch.append(result)
            if len(pending_batch)>=25 or completed==len(tasks):
                try:
                    save_checks(pending_batch)
                    saved_count+=len(pending_batch)
                    pending_batch.clear()
                except Exception as error:
                    storage_failure=error
                    st.session_state[pending_save_key]=list(pending_batch)
                    for queued_future in futures:
                        queued_future.cancel()
                    break
            progress.progress(completed/len(tasks))
            label.caption(
                f"Controllate {completed:,} di {len(tasks):,} offerte · "
                f"salvate {saved_count:,}."
            )
            if completed==1 or completed%5==0 or completed==len(tasks):
                preview=pd.DataFrame([{
                    "Paese":country_names.get(item["paese"],item["paese"].upper()),
                    "EAN":item["ean"],"SKU":item["sku"],"Esito":item["status"],
                    "Tipo anomalia":item.get("error_type"),
                    "Posizione":item.get("our_rank"),"Vincitore":item.get("winner_seller"),
                    "Totale vincente":item.get("winner_total"),
                    "Nostro totale":item.get("our_total"),
                    "Costo totale €":item.get("total_cost_eur"),
                    "Commissione API corrente %":item.get("commission_pct"),
                    "Commissione fissa €":item.get("commission_fixed_eur"),
                    "Fonte commissione":item.get("commission_source"),
                    "Prezzo per Buy Box €":item.get("target_sales_price_eur"),
                    "Esito economico":item.get("profit_status"),
                    "Guadagno/Perdita €":item.get("profit_eur"),
                    "Guadagno/Perdita %":item.get("profit_pct"),
                    "Errore":item.get("error"),
                } for item in results[-20:]])
                live_table.dataframe(preview,use_container_width=True,hide_index=True)
    if storage_failure is not None:
        status=database_storage_status()
        st.error(
            "Il controllo è stato interrotto in sicurezza perché SQLite non riusciva "
            "più a scrivere. I risultati dell'ultimo blocco sono rimasti in memoria e "
            "possono essere recuperati con il pulsante di riprova. "
            f"Già salvati: {saved_count:,} · da recuperare: {len(pending_batch):,} · "
            f"database: {status['database_path']} · dettaglio: {storage_failure}"
        )
    else:
        st.session_state.pop(pending_save_key,None)
        success_count=sum(item["ok"] for item in results)
        detected_names=preflight_detected|{
            name for item in results for name in item.get("detected_pseudonyms",[]) if name
        }
        if detected_names:
            account_settings["buybox_seller_pseudonyms"]=sorted(
                runtime_pseudonyms|detected_names
            )
            account_settings.pop("buybox_seller_pseudonym",None)
            execute("UPDATE marketplace_accounts SET settings_json=? WHERE id=? AND seller_id=?",
                    (json_text(account_settings),account["id"],seller_id))
            st.caption(
                "Pseudonimo Seller rilevato e salvato automaticamente: "
                +", ".join(sorted(detected_names))
            )
        compact_errors=[{
            "storefront":item.get("paese"),"ean":item.get("ean"),
            "sku":item.get("sku"),"error_type":item.get("error_type"),
            "error":item.get("error"),
        } for item in results if item.get("error")][:100]
        execute("""INSERT INTO operations(
            seller_id,marketplace_account_id,price_list_id,marketplace,storefront,
            operation_type,status,total_rows,success_rows,failed_rows,details_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(
            seller_id,account["id"],None,"kaufland",
            ",".join(chosen_countries),"CONTROLLO_BUYBOX",
            "success" if success_count==len(results) else ("partial" if success_count else "failed"),
            len(results),success_count,len(results)-success_count,
            json_text({
                "environment":environment,
                "saved_rows":saved_count,
                "error_count":len(results)-success_count,
                "errors":compact_errors,
                "source":"all_active_units_api",
                "matched_price_list_ids":sorted({
                    int(item["matched_price_list_id"])
                    for item in results if item.get("matched_price_list_id")
                }),
            }),now_iso(),
        ))
        if success_count==len(results):
            st.success(f"Controllo completato: {success_count:,} offerte verificate.")
        else:
            st.warning(
                f"Controllo completato: {success_count:,} riuscite · "
                f"{len(results)-success_count:,} non verificate."
            )

st.divider()
st.markdown("### Stato e visioni Buy Box salvate")
placeholders=",".join("?" for _ in chosen_countries)
current_saved=rows(f"""SELECT id,storefront,ean,sku,original_sku,product_title,
matched_price_list_id,matched_saved_view_id,supplier_name,price_list_name,
cost_match_source,cost_match_count,inventory_status,inventory_amount,id_unit,status,
our_rank,winner_seller,winner_price,winner_shipping,winner_total,our_price,
our_shipping,our_total,target_price,currency,delivery_min,delivery_max,
own_delivery_min,own_delivery_max,own_handling_time,logistics_status,offer_count,
error_type,error,purchase_cost_eur,shipping_cost_eur,total_cost_eur,commission_pct,
commission_fixed_eur,commission_source,current_commission_eur,
current_commission_effective_pct,actual_order_commission_pct,
actual_order_commission_local,actual_order_commission_currency,actual_order_id,
target_sales_price,target_sales_price_eur,target_source,target_commission_eur,
target_commission_effective_pct,profit_eur,profit_pct,profit_status,minimum_price,
minimum_price_source,details_json,checked_at
FROM kaufland_buybox_account_checks
WHERE seller_id=? AND marketplace_account_id=? AND environment=?
  AND storefront IN ({placeholders})
ORDER BY checked_at DESC,storefront,sku""",
    (seller_id,account["id"],environment,*chosen_countries))
if not current_saved:
    st.caption("Nessun controllo Buy Box ancora salvato per questa selezione.")
else:
    latest_checked_at=max(
        str(item.get("checked_at") or "") for item in current_saved
    )
    save_name_col,save_button_col=st.columns([3,1])
    view_name=save_name_col.text_input(
        "Nome della visione da salvare",
        placeholder="Esempio: Controllo completo di tutte le offerte",
        key=f"kaufland_view_name_{scope}",
    ).strip()
    if save_button_col.button(
        "Salva questa visione",
        use_container_width=True,
        key=f"kaufland_view_save_{scope}",
    ):
        automatic_name=f"Visione {display_rome_time(now_iso())}"
        execute(
            """INSERT INTO kaufland_buybox_account_views(
            seller_id,marketplace_account_id,environment,storefronts,name,
            rows_json,row_count,latest_checked_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                seller_id,account["id"],environment,
                ",".join(chosen_countries),view_name or automatic_name,
                json_text(current_saved),len(current_saved),latest_checked_at,now_iso(),
            ),
        )
        st.success("Visione Buy Box Kaufland salvata con data e ora.")
        st.rerun()

    stored_views=rows(
        """SELECT * FROM kaufland_buybox_account_views
        WHERE seller_id=? AND marketplace_account_id=? AND environment=?
        ORDER BY created_at DESC,id DESC""",
        (seller_id,account["id"],environment),
    )
    latest_option=(
        f"Ultima versione · {display_rome_time(latest_checked_at)}"
    )
    view_options={latest_option:None}
    for stored_view in stored_views:
        countries=str(stored_view.get("storefronts") or "").upper() or "—"
        label=(
            f"{stored_view['name']} · "
            f"{display_rome_time(stored_view['created_at'])} · "
            f"{stored_view['row_count']} righe · {countries} · "
            f"ID {stored_view['id']}"
        )
        view_options[label]=stored_view
    selected_view_label=st.selectbox(
        "Scegli la visione Buy Box",
        list(view_options),
        key=f"kaufland_view_selector_{scope}",
    )
    selected_view=view_options[selected_view_label]
    historical_view=selected_view is not None
    active_view_key="latest"
    if selected_view is None:
        saved=current_saved
        st.caption(
            "Visualizzazione dell’ultima versione disponibile, aggiornata il "
            f"{display_rome_time(latest_checked_at)}."
        )
    else:
        try:
            restored=json.loads(selected_view.get("rows_json") or "[]")
            saved=[item for item in restored if isinstance(item,dict)]
        except (TypeError,ValueError,json.JSONDecodeError):
            saved=[]
        active_view_key=f"saved_{selected_view['id']}"
        st.caption(
            f"Visione salvata il "
            f"{display_rome_time(selected_view['created_at'])} · "
            f"{len(saved)} righe. Questa fotografia è in sola lettura."
        )
    scope=f"{scope}_{active_view_key}"

    m1,m2,m3,m4,m5,m6,m7=st.columns(7)
    m1.metric("Controllate",len(saved))
    m2.metric("Buy Box vinte",sum(item["status"]=="Vinta" for item in saved))
    m3.metric("Buy Box perse",sum(item["status"]=="Persa" for item in saved))
    m4.metric("Oltre top 10",sum(item["status"]=="Oltre top 10" for item in saved))
    m5.metric("Nessuna Buy Box",sum(item["status"]=="Nessuna Buy Box" for item in saved))
    m6.metric("Da identificare",sum(item["status"]=="Non classificata" for item in saved))
    m7.metric("Non verificate",sum(bool(item.get("error_type")) for item in saved))
    calculated=[item for item in saved if item.get("profit_eur") is not None]
    gains=[item for item in calculated if float(item["profit_eur"])>0]
    losses=[item for item in calculated if float(item["profit_eur"])<0]
    e1,e2,e3,e4,e5=st.columns(5)
    e1.metric("Righe con guadagno",len(gains))
    e2.metric("Righe in perdita",len(losses))
    e3.metric("Guadagni potenziali €",f"{sum(float(x['profit_eur']) for x in gains):,.2f}")
    e4.metric("Perdite potenziali €",f"{sum(float(x['profit_eur']) for x in losses):,.2f}")
    e5.metric("Risultato netto €",f"{sum(float(x['profit_eur']) for x in calculated):,.2f}")
    if len(calculated)<len(saved):
        st.caption(
            "Le righe controllate prima di questo aggiornamento non hanno ancora il calcolo "
            "economico. Selezionale e ripeti il controllo per valorizzarle."
        )
    filter_col1,filter_col2=st.columns(2)
    status_options=["Tutti"]+sorted({str(item["status"]) for item in saved})
    status_filter=filter_col1.selectbox(
        "Filtra esito Buy Box",status_options,key=f"buybox_status_{scope}"
    )
    profit_options=["Tutti"]+sorted({
        str(item.get("profit_status") or "Non calcolabile") for item in saved
    })
    profit_filter=filter_col2.selectbox(
        "Filtra esito economico",profit_options,key=f"buybox_profit_status_{scope}"
    )

    margin_modes={
        "Nessun filtro":"all",
        "Margine compreso tra X e Y":"range",
        "Margine almeno X":"minimum",
    }
    margin_filter_col,exclude_filter_col=st.columns(2)
    margin_mode_label=margin_filter_col.selectbox(
        "Filtra Margine allineamento %",
        list(margin_modes),
        key=f"buybox_profit_margin_mode_{scope}",
    )
    margin_mode=margin_modes[margin_mode_label]
    margin_from=None
    margin_to=None
    if margin_mode=="range":
        range_from_col,range_to_col=margin_filter_col.columns(2)
        margin_from=range_from_col.number_input(
            "Margine da X %",
            min_value=-100000.0,max_value=100000.0,value=0.0,step=0.10,
            key=f"buybox_profit_margin_from_{scope}",
        )
        margin_to=range_to_col.number_input(
            "Margine a Y %",
            min_value=-100000.0,max_value=100000.0,value=10.0,step=0.10,
            key=f"buybox_profit_margin_to_{scope}",
        )
    elif margin_mode=="minimum":
        margin_from=margin_filter_col.number_input(
            "Margine minimo X %",
            min_value=-100000.0,max_value=100000.0,value=10.0,step=0.10,
            key=f"buybox_profit_margin_minimum_{scope}",
        )

    exclude_below=exclude_filter_col.checkbox(
        "Escludi Margine allineamento % minore di",
        key=f"buybox_profit_margin_exclude_enabled_{scope}",
    )
    exclude_threshold=None
    if exclude_below:
        exclude_threshold=exclude_filter_col.number_input(
            "Soglia di esclusione %",
            min_value=-100000.0,max_value=100000.0,value=0.0,step=0.10,
            key=f"buybox_profit_margin_exclude_value_{scope}",
        )

    visible=saved if status_filter=="Tutti" else [
        item for item in saved if item["status"]==status_filter
    ]
    if profit_filter!="Tutti":
        visible=[
            item for item in visible
            if str(item.get("profit_status") or "Non calcolabile")==profit_filter
        ]

    def numeric_profit_margin(item):
        try:
            value=float(item.get("profit_pct"))
            return value if pd.notna(value) else None
        except (TypeError,ValueError):
            return None

    invalid_margin_range=(
        margin_mode=="range"
        and margin_from is not None and margin_to is not None
        and float(margin_from)>float(margin_to)
    )
    if invalid_margin_range:
        st.warning("Nel filtro margine, il valore X deve essere minore o uguale a Y.")
        visible=[]
    elif margin_mode=="range":
        visible=[
            item for item in visible
            if (
                (value:=numeric_profit_margin(item)) is not None
                and float(margin_from)<=value<=float(margin_to)
            )
        ]
    elif margin_mode=="minimum":
        visible=[
            item for item in visible
            if (
                (value:=numeric_profit_margin(item)) is not None
                and value>=float(margin_from)
            )
        ]
    if exclude_below:
        visible=[
            item for item in visible
            if (
                (value:=numeric_profit_margin(item)) is not None
                and value>=float(exclude_threshold)
            )
        ]
    margin_filter_signature="|".join(map(str,(
        margin_mode,margin_from,margin_to,exclude_below,exclude_threshold
    )))
    st.caption(f"Righe dopo i filtri: {len(visible):,} su {len(saved):,}.")

    def delivery_window(minimum,maximum) -> str:
        if minimum is None and maximum is None:
            return "Non disponibile"
        if minimum is None:
            return f"entro {int(maximum)} gg"
        if maximum is None:
            return f"da {int(minimum)} gg"
        if int(minimum)==int(maximum):
            return f"{int(minimum)} gg"
        return f"{int(minimum)}–{int(maximum)} gg"

    saved_frame=pd.DataFrame([{
        "Paese":country_names.get(item["storefront"],item["storefront"].upper()),
        "EAN":item["ean"],"SKU inviato":item["sku"],
        "Prodotto":item.get("product_title",""),
        "Fornitore":item.get("supplier_name","") or "Non associato",
        "Listino abbinato":item.get("price_list_name","") or "Non associato",
        "Origine costo":item.get("cost_match_source",""),
        "Corrispondenze listini":item.get("cost_match_count",0),
        "Esito":item["status"],
        "Esito economico":item["profit_status"] or "Non calcolabile",
        "Risultato allineamento €":item["profit_eur"],
        "Margine allineamento %":item["profit_pct"],
        "Tipo anomalia":item.get("error_type",""),
        "Nostra posizione":item["our_rank"],"Vincitore":item["winner_seller"],
        "Prezzo più basso concorrente":item["winner_price"],
        "Totale concorrente":item["winner_total"],"Nostro prezzo":item["our_price"],
        "Nostro prezzo più basso":item.get("minimum_price"),
        "Fonte prezzo più basso":item.get("minimum_price_source",""),
        "Nostra spedizione":item["our_shipping"],"Nostro totale":item["our_total"],
        "Prezzo obiettivo API":item["target_price"],"Valuta":item["currency"],
        "Costo acquisto €":item["purchase_cost_eur"],
        "Spedizione fornitore €":item["shipping_cost_eur"],
        "Costo totale €":item["total_cost_eur"],
        "Commissione variabile corrente %":item["commission_pct"],
        "Commissione fissa €":item.get("commission_fixed_eur",0),
        "Commissione corrente €":item.get("current_commission_eur"),
        "Commissione effettiva corrente %":item.get("current_commission_effective_pct"),
        "Fonte commissione":item.get("commission_source",""),
        "Ultima commissione reale ordine %":item.get("actual_order_commission_pct"),
        "Ultima commissione reale ordine":item.get("actual_order_commission_local"),
        "Valuta commissione ordine":item.get("actual_order_commission_currency",""),
        "Ordine commissione reale":item.get("actual_order_id",""),
        "Prezzo per Buy Box":item["target_sales_price"],
        "Prezzo per Buy Box €":item["target_sales_price_eur"],
        "Fonte prezzo Buy Box":item["target_source"],
        "Consegna vincitore":delivery_window(
            item.get("delivery_min"),item.get("delivery_max")
        ),
        "Nostra consegna":delivery_window(
            item.get("own_delivery_min"),item.get("own_delivery_max")
        ),
        "Gestione attuale (gg)":item.get("own_handling_time"),
        "Analisi logistica":item.get("logistics_status",""),
        "Commissione Buy Box €":item["target_commission_eur"],
        "Commissione effettiva Buy Box %":item.get("target_commission_effective_pct"),
        "Offerte confrontate":item["offer_count"],"Controllata":item["checked_at"],
        "Errore":item["error"],
    } for item in visible])
    selected_action_items=[]
    selected_action_item=None
    anomalies=[item for item in saved if item.get("error_type")]
    if anomalies:
        anomaly_counts=pd.DataFrame([
            {"Tipo anomalia":name,"Prodotti":count}
            for name,count in sorted(
                {
                    name:sum(
                        str(item.get("error_type") or "")==name for item in anomalies
                    )
                    for name in {
                        str(item.get("error_type") or "") for item in anomalies
                    }
                }.items(),
                key=lambda pair:(-pair[1],pair[0]),
            )
        ])
        st.markdown("#### Riepilogo prodotti non verificati")
        st.dataframe(
            anomaly_counts,use_container_width=True,hide_index=True,
            column_config={"Prodotti":st.column_config.NumberColumn(format="%d")},
        )
    st.markdown("#### Tabella operativa Buy Box")
    st.caption(
        "Puoi selezionare una o più righe. Con una riga si apre la gestione "
        "dettagliata; con più righe si attiva l’aggiornamento collettivo sicuro. "
        "Verde = Buy Box vinta · rosso = Buy Box persa · "
        "giallo = posizione o margine da controllare. Il Totale concorrente "
        "comprende internamente anche la sua spedizione."
    )
    filtered_block_key=hashlib.sha1(
        f"{status_filter}|{profit_filter}|{margin_filter_signature}".encode()
    ).hexdigest()[:8]
    select_filtered_block=st.checkbox(
        (
            f"Seleziona tutto il blocco filtrato per l’allineamento Buy Box "
            f"({len(visible):,} righe)"
        ),
        value=False,disabled=not visible,
        help=(
            "La selezione non modifica ancora Kaufland. Prima dell’aggiornamento "
            "saranno mostrati il riepilogo economico, le eventuali perdite e le "
            "conferme richieste."
        ),
        key=f"buybox_select_filtered_block_{scope}_{filtered_block_key}",
    )
    if saved_frame.empty:
        st.info("Nessuna riga corrisponde ai filtri selezionati.")
    else:
        row_palette={
            "green":("background-color: #dcfce7;","color: #14532d;"),
            "red":("background-color: #fee2e2;","color: #7f1d1d;"),
            "yellow":("background-color: #fef3c7;","color: #78350f;"),
            "neutral":("",""),
        }

        def style_buybox_row(row):
            tone=buybox_row_tone(
                row.get("Esito"),row.get("Risultato allineamento €"),
                row.get("Margine allineamento %"),
            )
            background,text=row_palette[tone]
            return [f"{background}{text}" for _ in row]

        margin_palette={
            "green":"background-color: #22c55e; color: #ffffff; font-weight: 700;",
            "yellow":"background-color: #facc15; color: #713f12; font-weight: 700;",
            "red":"background-color: #ef4444; color: #ffffff; font-weight: 700;",
            "neutral":"background-color: #e5e7eb; color: #4b5563;",
        }

        def style_economic_columns(row):
            tone=buybox_margin_tone(
                row.get("Risultato allineamento €"),
                row.get("Margine allineamento %"),
            )
            styles=pd.Series("",index=row.index)
            for column in ("Risultato allineamento €","Margine allineamento %"):
                styles[column]=margin_palette[tone]
            return styles

        decimal_columns=(
            "Prezzo più basso concorrente","Totale concorrente",
            "Nostro prezzo","Nostro prezzo più basso","Nostra spedizione",
            "Nostro totale","Prezzo obiettivo API","Costo acquisto €",
            "Spedizione fornitore €","Costo totale €",
            "Commissione variabile corrente %","Commissione fissa €",
            "Commissione corrente €","Commissione effettiva corrente %",
            "Ultima commissione reale ordine %","Ultima commissione reale ordine",
            "Prezzo per Buy Box","Prezzo per Buy Box €",
            "Commissione Buy Box €","Commissione effettiva Buy Box %",
            "Risultato allineamento €",
            "Margine allineamento %",
        )
        table_column_config={
            column:st.column_config.NumberColumn(format="%.2f")
            for column in decimal_columns
        }
        table_column_config.update({
            "Nostra posizione":st.column_config.NumberColumn(format="%d"),
            "Gestione attuale (gg)":st.column_config.NumberColumn(format="%d"),
            "Offerte confrontate":st.column_config.NumberColumn(format="%d"),
            "Corrispondenze listini":st.column_config.NumberColumn(format="%d"),
        })
        # Una tabella Buy Box può contenere decine di migliaia di righe.
        # Pandas Styler genera una cella HTML per ogni valore e Streamlit
        # blocca il rendering oltre il limite configurato. AgGrid usa invece
        # virtualizzazione e paginazione lato browser, mantenendo colori e
        # selezione multipla senza costruire centinaia di migliaia di celle.
        row_lookup={}
        table_row_keys=[]
        for row_position,item in enumerate(visible):
            base_key="|".join((
                str(item.get("id") or ""),
                str(item.get("storefront") or ""),
                str(item.get("sku") or ""),
                str(item.get("ean") or ""),
                str(item.get("checked_at") or ""),
            ))
            row_key=base_key
            if row_key in row_lookup:
                row_key=f"{base_key}|{row_position}"
            row_lookup[row_key]=item
            table_row_keys.append(row_key)

        table_frame=saved_frame.copy()
        table_frame.insert(0,"_row_key",table_row_keys)
        selected_row_keys=[]

        if AgGrid is not None:
            builder=GridOptionsBuilder.from_dataframe(table_frame)
            builder.configure_default_column(
                sortable=True,filter=True,resizable=True,minWidth=100,
            )
            builder.configure_column("_row_key",hide=True)
            builder.configure_column(
                "Paese",checkboxSelection=True,headerCheckboxSelection=True,
                headerCheckboxSelectionFilteredOnly=True,pinned="left",minWidth=145,
            )
            builder.configure_column("EAN",minWidth=145)
            builder.configure_column("SKU inviato",minWidth=250)
            builder.configure_column("Esito",minWidth=135)
            for column in decimal_columns:
                if column in table_frame.columns:
                    builder.configure_column(
                        column,type=["numericColumn"],
                        valueFormatter=(
                            "value == null || value === '' ? '' : "
                            "Number(value).toFixed(2)"
                        ),
                    )
            for column in (
                "Nostra posizione","Gestione attuale (gg)",
                "Offerte confrontate",
            ):
                if column in table_frame.columns:
                    builder.configure_column(
                        column,type=["numericColumn"],
                        valueFormatter=(
                            "value == null || value === '' ? '' : "
                            "Math.trunc(Number(value)).toString()"
                        ),
                    )
            economic_cell_style=JsCode(
                """function(params) {
                    const row = params.data || {};
                    const rawValue = row['Risultato allineamento €'];
                    const rawMargin = row['Margine allineamento %'];
                    if (rawValue == null || rawValue === '' ||
                        rawMargin == null || rawMargin === '') {
                        return {'backgroundColor':'#e5e7eb','color':'#4b5563'};
                    }
                    const value = Number(rawValue);
                    const margin = Number(rawMargin);
                    if (!Number.isFinite(value) || !Number.isFinite(margin)) {
                        return {'backgroundColor':'#e5e7eb','color':'#4b5563'};
                    }
                    if (value < 0 || margin < 0) {
                        return {'backgroundColor':'#ef4444','color':'#ffffff','fontWeight':'700'};
                    }
                    if (margin < 10) {
                        return {'backgroundColor':'#facc15','color':'#713f12','fontWeight':'700'};
                    }
                    return {'backgroundColor':'#22c55e','color':'#ffffff','fontWeight':'700'};
                }"""
            )
            for column in ("Risultato allineamento €","Margine allineamento %"):
                if column in table_frame.columns:
                    builder.configure_column(column,cellStyle=economic_cell_style)
            row_style=JsCode(
                """function(params) {
                    const row = params.data || {};
                    const status = String(row['Esito'] || '').toLowerCase();
                    const rawValue = row['Risultato allineamento €'];
                    const rawMargin = row['Margine allineamento %'];
                    const value = rawValue == null || rawValue === '' ? NaN : Number(rawValue);
                    const margin = rawMargin == null || rawMargin === '' ? NaN : Number(rawMargin);
                    if ((Number.isFinite(value) && value < 0) ||
                        (Number.isFinite(margin) && margin < 0)) {
                        return {'backgroundColor':'#fee2e2','color':'#7f1d1d'};
                    }
                    if (status === 'vinta') {
                        return {'backgroundColor':'#dcfce7','color':'#14532d'};
                    }
                    if (status === 'persa') {
                        return {'backgroundColor':'#fee2e2','color':'#7f1d1d'};
                    }
                    if (status.includes('oltre') || status.includes('non classificata') ||
                        (Number.isFinite(margin) && margin < 10)) {
                        return {'backgroundColor':'#fef3c7','color':'#78350f'};
                    }
                    return null;
                }"""
            )
            builder.configure_selection(selection_mode="multiple",use_checkbox=True)
            builder.configure_grid_options(
                rowMultiSelectWithClick=True,
                suppressRowClickSelection=False,
                enableRangeSelection=True,
                animateRows=False,
                pagination=True,
                paginationPageSize=250,
                cacheQuickFilter=True,
                getRowStyle=row_style,
                getRowId=JsCode(
                    "function(params) { return String(params.data._row_key); }"
                ),
                suppressScrollOnNewData=True,
            )
            manual_mode=getattr(
                GridUpdateMode,"MANUAL",GridUpdateMode.SELECTION_CHANGED
            )
            grid_response=AgGrid(
                table_frame,
                gridOptions=builder.build(),
                data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                update_mode=manual_mode,
                height=560,
                fit_columns_on_grid_load=False,
                theme="streamlit",
                key=(
                    f"buybox_saved_grid_v195_{scope}_"
                    f"{filtered_block_key}"
                ),
                allow_unsafe_jscode=True,
                reload_data=False,
            )
            returned=grid_response.get("selected_rows")
            if isinstance(returned,pd.DataFrame) and "_row_key" in returned.columns:
                selected_row_keys=list(returned["_row_key"].astype(str))
            elif isinstance(returned,list):
                selected_row_keys=[
                    str(item.get("_row_key")) for item in returned
                    if isinstance(item,dict) and item.get("_row_key") is not None
                ]
            st.caption(
                f"Tabella virtualizzata: {len(table_frame):,} righe · "
                "250 righe per pagina. Seleziona le righe e premi **Applica** "
                "nel riquadro della tabella. Il quadratino nell’intestazione "
                "seleziona tutte le righe filtrate nella griglia."
            )
            selected_action_items=[
                row_lookup[row_key] for row_key in selected_row_keys
                if row_key in row_lookup
            ]
        else:
            # Fallback senza st-aggrid: lo Styler viene usato soltanto sotto
            # il limite sicuro. Per tabelle grandi Streamlit riceve il DataFrame
            # Arrow non stilizzato, evitando l'eccezione styler.render.max_elements.
            render_frame=table_frame.drop(columns=["_row_key"])
            cell_count=int(render_frame.shape[0])*int(render_frame.shape[1])
            if cell_count<=250_000:
                rendered_table=(
                    render_frame.style
                    .apply(style_buybox_row,axis=1)
                    .apply(style_economic_columns,axis=1)
                )
            else:
                rendered_table=render_frame
                st.info(
                    f"La tabella contiene {cell_count:,} celle: i colori sono "
                    "temporaneamente disattivati per mantenere il rendering stabile."
                )
            selection_result=st.dataframe(
                rendered_table,use_container_width=True,height=520,hide_index=True,
                on_select="rerun",selection_mode="multi-row",
                column_config=table_column_config,
                key=(
                    f"buybox_saved_table_v195_{scope}_"
                    f"{filtered_block_key}"
                ),
            )
            try:
                selected_positions=list(selection_result.selection.rows)
            except (AttributeError,TypeError):
                selected_positions=list(
                    selection_result.get("selection",{}).get("rows",[])
                    if isinstance(selection_result,dict) else []
                )
            selected_action_items=[
                visible[int(position)] for position in selected_positions
                if 0<=int(position)<len(visible)
            ]
        if select_filtered_block:
            selected_action_items=list(visible)
        if len(selected_action_items)==1 and not select_filtered_block:
            selected_action_item=selected_action_items[0]
        if not selected_action_items:
            st.info("Seleziona una o più righe della tabella per lavorare sui prezzi.")
        elif len(selected_action_items)>1:
            st.info(
                f"Hai selezionato {len(selected_action_items):,} offerte. "
                "La gestione collettiva è disponibile sotto la tabella."
            )
    st.download_button(
        "Scarica controllo Buy Box CSV",
        saved_frame.to_csv(index=False).encode("utf-8-sig"),
        file_name=(
            f"buybox_kaufland_tutte_offerte_account_{account['id']}_"
            f"{environment}_{now_iso()[:10]}.csv"
            .replace(" ","_")
        ),
        mime="text/csv",key=f"buybox_download_{scope}",
    )
    if historical_view:
        st.info(
            "Stai consultando una fotografia storica: filtri, statistiche ed "
            "esportazione restano disponibili, mentre gli aggiornamenti di "
            "prezzo e tempi di gestione sono disattivati."
        )
        st.stop()

    st.divider()
    st.markdown("### Gestione del prezzo più basso per riga")
    st.caption(
        "Le azioni modificano esclusivamente «Il suo prezzo più basso» usato dallo "
        "Smart Pricing Kaufland. Il prezzo principale resta invariato. Una perdita "
        "blocca l'operazione; un margine inferiore al 10% richiede una seconda conferma."
    )
    if not playground:
        st.warning(
            "Ambiente PRODUZIONE: i pulsanti seguenti modificano realmente soltanto "
            "il prezzo più basso dell'offerta Kaufland selezionata."
        )
    flash_key=f"buybox_price_flash_{scope}"
    if st.session_state.get(flash_key):
        st.success(st.session_state.pop(flash_key))

    def row_economics(item: dict) -> dict:
        fallback=economics_by_offer.get(
            (str(item["storefront"]),str(item["sku"])),{}
        )
        return {
            "purchase_cost_eur":(
                item.get("purchase_cost_eur")
                if item.get("purchase_cost_eur") is not None
                else fallback.get("purchase_cost_eur")
            ),
            "shipping_cost_eur":(
                item.get("shipping_cost_eur")
                if item.get("shipping_cost_eur") is not None
                else fallback.get("shipping_cost_eur")
            ),
            "total_cost_eur":(
                item.get("total_cost_eur")
                if item.get("total_cost_eur") is not None
                else fallback.get("total_cost_eur")
            ),
            "commission_pct":float(
                item.get("commission_pct")
                if item.get("commission_pct") is not None
                else fallback.get("commission_pct",15)
            ),
            "commission_fixed_eur":float(
                item.get("commission_fixed_eur") or 0
            ),
            "commission_source":str(
                item.get("commission_source") or "Regola salvata"
            ),
            "currency":str(
                item.get("currency") or fallback.get("publication_currency") or "EUR"
            ).upper(),
            "eur_multiplier":float(fallback.get("eur_multiplier",1) or 1),
        }

    def evaluate_row_price(item: dict,price: float) -> dict:
        economics=row_economics(item)
        return price_financials(
            sales_price=price,
            customer_shipping=item.get("our_shipping") or 0,
            total_cost_eur=economics["total_cost_eur"],
            commission_pct=economics["commission_pct"],
            commission_fixed_eur=economics["commission_fixed_eur"],
            currency=economics["currency"],
            eur_multiplier=economics["eur_multiplier"],
            low_margin_threshold=10,
        )

    def margin_message(label: str,evaluation: dict) -> str:
        if evaluation.get("profit_eur") is None:
            return (
                f"{label}: impossibile calcolare il margine perché manca un costo "
                "totale valido. Aggiornamento bloccato."
            )
        return (
            f"{label}: {evaluation['profit_eur']:+.2f} € · "
            f"{evaluation['profit_pct']:+.2f}% sul costo totale."
        )

    def show_margin_alert(label: str,evaluation: dict) -> None:
        message=margin_message(label,evaluation)
        if evaluation["margin_alert"]=="red":st.error(message)
        elif evaluation["margin_alert"]=="yellow":st.warning(message)
        else:st.success(message)

    def resolve_update_unit_id(item: dict) -> int | None:
        id_unit=item.get("id_unit")
        if id_unit not in (None,""):
            return int(id_unit)
        try:
            details=json.loads(str(item.get("details_json") or "{}"))
        except (TypeError,ValueError,json.JSONDecodeError):
            details={}
        saved_buybox=parse_buybox_response(
            details.get("buybox",{}),str(item["sku"]),configured_pseudonyms,
        )
        id_unit=saved_buybox.get("id_unit")
        if id_unit in (None,""):
            id_unit=unit_id_from_units(
                details.get("own_units",[]),str(item["sku"])
            )
        return int(id_unit) if id_unit not in (None,"") else None

    def perform_minimum_price_update(
        item: dict,price: float,source: str,*,rerun_after: bool=True,
        allow_confirmed_loss: bool=False,
    ) -> tuple[bool,str]:
        evaluation=evaluate_row_price(item,price)
        confirmed_loss=(
            allow_confirmed_loss
            and evaluation.get("profit_eur") is not None
            and float(evaluation["profit_eur"])<0
            and float(price)>0
        )
        if not evaluation["can_update"] and not confirmed_loss:
            message=margin_message("Prezzo non applicato",evaluation)
            if rerun_after:st.error(message)
            return False,message
        listing_price=float(item.get("our_price") or 0)
        if listing_price>0 and float(price)>listing_price:
            message=(
                f"Prezzo più basso non applicato: {float(price):.2f} è superiore "
                f"al prezzo principale invariato di {listing_price:.2f}."
            )
            if rerun_after:st.error(message)
            return False,message
        try:
            id_unit=resolve_update_unit_id(item)
            if id_unit not in (None,""):
                api_result=client.update_unit_minimum_price(
                    int(id_unit),str(item["storefront"]),float(price)
                )
            else:
                api_result=client.update_offer_minimum_price(
                    str(item["sku"]),str(item["storefront"]),float(price)
                )
            economics=row_economics(item)
            matched_price_list_id=(
                int(item["matched_price_list_id"])
                if item.get("matched_price_list_id") not in (None,"") else None
            )
            commission_effective_pct=(
                round(float(evaluation["commission_eur"])
                      /float(evaluation["customer_total_eur"])*100,4)
                if evaluation.get("commission_eur") is not None
                and float(evaluation.get("customer_total_eur") or 0)>0
                else None
            )
            execute("""INSERT INTO kaufland_buybox_account_price_updates(
                seller_id,marketplace_account_id,matched_price_list_id,
                supplier_name,price_list_name,storefront,environment,
                ean,sku,id_unit,source,previous_price,new_price,currency,
                purchase_cost_eur,shipping_cost_eur,total_cost_eur,commission_pct,
                commission_fixed_eur,commission_source,commission_eur,
                commission_effective_pct,profit_eur,profit_pct,margin_status,
                price_field,api_result_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                seller_id,account["id"],matched_price_list_id,
                item.get("supplier_name","") or "",item.get("price_list_name","") or "",
                item["storefront"],
                environment,item["ean"],item["sku"],api_result["id_unit"],source,
                item.get("minimum_price"),float(price),economics["currency"],
                economics["purchase_cost_eur"],economics["shipping_cost_eur"],
                economics["total_cost_eur"],economics["commission_pct"],
                economics["commission_fixed_eur"],economics["commission_source"],
                evaluation["commission_eur"],commission_effective_pct,evaluation["profit_eur"],
                evaluation["profit_pct"],evaluation["profit_status"],"minimum_price",
                json_text(api_result.get("result")),now_iso(),
            ))
            execute("""UPDATE kaufland_buybox_account_checks SET minimum_price=?,
                minimum_price_source='Aggiornato dalla gestione Buy Box'
                WHERE id=? AND seller_id=? AND marketplace_account_id=?""",(
                float(price),item["id"],seller_id,account["id"],
            ))
            execute("""INSERT INTO operations(
                seller_id,marketplace_account_id,price_list_id,marketplace,storefront,
                operation_type,status,total_rows,success_rows,failed_rows,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(
                seller_id,account["id"],matched_price_list_id,"kaufland",
                item["storefront"],"AGGIORNA_PREZZO_MINIMO_BUYBOX","success",1,1,0,
                json_text({
                    "environment":environment,"source":source,"ean":item["ean"],
                    "sku":item["sku"],"id_unit":api_result["id_unit"],
                    "matched_price_list_id":matched_price_list_id,
                    "supplier_name":item.get("supplier_name",""),
                    "price_list_name":item.get("price_list_name",""),
                    "price_field":"minimum_price",
                    "listing_price_unchanged":item.get("our_price"),
                    "previous_minimum_price":item.get("minimum_price"),
                    "new_minimum_price":float(price),
                    "currency":economics["currency"],"profit_eur":evaluation["profit_eur"],
                    "profit_pct":evaluation["profit_pct"],
                    "commission_pct":economics["commission_pct"],
                    "commission_fixed_eur":economics["commission_fixed_eur"],
                    "commission_source":economics["commission_source"],
                }),now_iso(),
            ))
            message=(
                f"Prezzo più basso aggiornato: {item['sku']} · {float(price):.2f} "
                f"{economics['currency']}. Il prezzo principale è rimasto invariato · "
                f"margine {evaluation['profit_pct']:.2f}%."
            )
            if rerun_after:
                st.session_state[flash_key]=message
                st.rerun()
            return True,message
        except Exception as error:
            message=f"Aggiornamento Kaufland non riuscito: {error}"
            if rerun_after:st.error(message)
            return False,message

    def perform_handling_time_update(item: dict,handling_time: int) -> None:
        try:
            id_unit=resolve_update_unit_id(item)
            if id_unit not in (None,""):
                api_result=client.update_unit_handling_time(
                    id_unit,str(item["storefront"]),int(handling_time)
                )
            else:
                api_result=client.update_offer_handling_time(
                    str(item["sku"]),str(item["storefront"]),int(handling_time)
                )
            execute("""UPDATE kaufland_buybox_account_checks SET own_handling_time=?
                WHERE id=? AND seller_id=? AND marketplace_account_id=?""",(
                int(handling_time),item["id"],seller_id,account["id"],
            ))
            matched_price_list_id=(
                int(item["matched_price_list_id"])
                if item.get("matched_price_list_id") not in (None,"") else None
            )
            execute("""INSERT INTO operations(
                seller_id,marketplace_account_id,price_list_id,marketplace,storefront,
                operation_type,status,total_rows,success_rows,failed_rows,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(
                seller_id,account["id"],matched_price_list_id,"kaufland",
                item["storefront"],"AGGIORNA_GIORNI_GESTIONE_BUYBOX",
                "success",1,1,0,json_text({
                    "environment":environment,"ean":item["ean"],"sku":item["sku"],
                    "id_unit":api_result["id_unit"],
                    "matched_price_list_id":matched_price_list_id,
                    "supplier_name":item.get("supplier_name",""),
                    "price_list_name":item.get("price_list_name",""),
                    "previous_handling_time":item.get("own_handling_time"),
                    "new_handling_time":int(handling_time),
                    "api_result":api_result.get("result"),
                }),now_iso(),
            ))
            st.session_state[flash_key]=(
                f"Giorni di gestione aggiornati: {item['sku']} · "
                f"{int(handling_time)} giorni lavorativi. Ripeti il controllo Buy Box "
                "per leggere la nuova finestra di consegna."
            )
            st.rerun()
        except Exception as error:
            st.error(f"Aggiornamento giorni di gestione non riuscito: {error}")

    if select_filtered_block or len(selected_action_items)>1:
        st.markdown("#### Gestione collettiva delle righe selezionate")
        batch_selection_signature=hashlib.sha1(
            "|".join(
                str(item.get("id") or f"{item.get('storefront')}:{item.get('sku')}")
                for item in selected_action_items
            ).encode()
        ).hexdigest()[:10]
        batch_rows=[]
        for selected_item in selected_action_items:
            selected_target=float(selected_item.get("target_sales_price") or 0)
            selected_evaluation=evaluate_row_price(selected_item,selected_target)
            candidate_status=minimum_price_candidate_status(
                target_price=selected_target,
                listing_price=selected_item.get("our_price"),
                profit_eur=selected_evaluation.get("profit_eur"),
                margin_alert=selected_evaluation.get("margin_alert"),
            )
            category=str(candidate_status["category"])
            batch_rows.append({
                "item":selected_item,
                "price":selected_target,
                "evaluation":selected_evaluation,
                "category":category,
                "reason":str(candidate_status["reason"]),
                "is_loss":category=="loss",
                "missing_data":category in ("invalid_target","missing_economics"),
                "blocked_by_listing":category=="above_listing",
            })

        safe_rows=[row for row in batch_rows if row["category"]=="safe"]
        low_margin_rows=[row for row in batch_rows if row["category"]=="low_margin"]
        loss_rows=[row for row in batch_rows if row["category"]=="loss"]
        above_listing_rows=[
            row for row in batch_rows if row["category"]=="above_listing"
        ]
        invalid_rows=[
            row for row in batch_rows
            if row["category"] in ("invalid_target","missing_economics")
        ]
        metric_row1=st.columns(4)
        metric_row1[0].metric("Righe selezionate",len(batch_rows))
        metric_row1[1].metric("Margine ≥ 10%",len(safe_rows))
        metric_row1[2].metric("Margine 0–9,99%",len(low_margin_rows))
        metric_row1[3].metric("Righe in perdita",len(loss_rows))
        metric_row2=st.columns(2)
        metric_row2[0].metric(
            "Obiettivo superiore al prezzo principale",len(above_listing_rows)
        )
        metric_row2[1].metric("Dati realmente mancanti/non validi",len(invalid_rows))

        selected_profit=sum(
            float(row["evaluation"].get("profit_eur") or 0)
            for row in safe_rows+low_margin_rows+loss_rows
        )
        st.caption(
            f"Risultato potenziale complessivo delle righe economicamente "
            f"valutabili: {selected_profit:+,.2f} €. "
            f"Righe escluse perché l’obiettivo supera il prezzo principale: "
            f"{len(above_listing_rows):,}. Righe con dati realmente mancanti o "
            f"prezzo non valido: {len(invalid_rows):,}."
        )
        if above_listing_rows:
            st.info(
                f"Le {len(above_listing_rows):,} righe indicate non hanno dati "
                "mancanti. Il prezzo obiettivo Buy Box è superiore al prezzo "
                "principale dell’offerta. Poiché questa sezione modifica soltanto "
                "il prezzo minimo Smart Pricing e lascia invariato il prezzo "
                "principale, tali righe non possono essere applicate."
            )
            with st.expander(
                "Mostra le righe con obiettivo superiore al prezzo principale"
            ):
                blocked_preview=pd.DataFrame([{
                    "Paese":country_names.get(
                        row["item"]["storefront"],row["item"]["storefront"].upper()
                    ),
                    "EAN":row["item"].get("ean"),
                    "SKU":row["item"].get("sku"),
                    "Prezzo principale":row["item"].get("our_price"),
                    "Obiettivo Buy Box":row["price"],
                    "Motivo esclusione":row["reason"],
                } for row in above_listing_rows[:500]])
                st.dataframe(
                    blocked_preview,use_container_width=True,hide_index=True,
                    column_config={
                        "Prezzo principale":st.column_config.NumberColumn(format="%.2f"),
                        "Obiettivo Buy Box":st.column_config.NumberColumn(format="%.2f"),
                    },
                )
                if len(above_listing_rows)>500:
                    st.caption(
                        f"Mostrate le prime 500 righe su "
                        f"{len(above_listing_rows):,}."
                    )
        if invalid_rows:
            reason_counts={}
            for row in invalid_rows:
                reason_counts[row["reason"]]=reason_counts.get(row["reason"],0)+1
            st.warning(
                "Dati realmente mancanti/non validi: "+" · ".join(
                    f"{reason}: {count:,}"
                    for reason,count in sorted(reason_counts.items())
                )
            )
        if loss_rows:
            st.error(
                f"Attenzione: nel blocco selezionato ci sono {len(loss_rows):,} "
                "prodotti che, allineati alla Buy Box, genererebbero una perdita."
            )
            st.dataframe(pd.DataFrame([{
                "Paese":country_names.get(
                    row["item"]["storefront"],row["item"]["storefront"].upper()
                ),
                "EAN":row["item"]["ean"],
                "SKU":row["item"]["sku"],
                "Prezzo Buy Box":row["price"],
                "Perdita €":row["evaluation"]["profit_eur"],
                "Margine %":row["evaluation"]["profit_pct"],
            } for row in loss_rows]),use_container_width=True,hide_index=True,
                column_config={
                    "Prezzo Buy Box":st.column_config.NumberColumn(format="%.2f"),
                    "Perdita €":st.column_config.NumberColumn(format="%.2f"),
                    "Margine %":st.column_config.NumberColumn(format="%.2f"),
                },
            )
        include_low_margin=st.checkbox(
            "Includi anche le righe gialle con margine inferiore al 10%",
            value=False,
            key=f"buybox_batch_low_margin_{scope}_{batch_selection_signature}",
        )
        include_losses=False
        if loss_rows:
            include_losses=st.checkbox(
                (
                    f"CONFERMO di voler includere anche i {len(loss_rows):,} "
                    "prodotti in perdita elencati sopra"
                ),
                value=False,
                key=f"buybox_batch_losses_{scope}_{batch_selection_signature}",
            )
        rows_to_update=safe_rows+(
            low_margin_rows if include_low_margin else []
        )+(
            loss_rows if include_losses else []
        )
        batch_confirm=st.checkbox(
            (
                f"Confermo l’aggiornamento del solo prezzo più basso per "
                f"{len(rows_to_update):,} offerte"
            ),
            value=False,key=f"buybox_batch_confirm_{scope}_{batch_selection_signature}",
        )
        batch_clicked=st.button(
            "Aggiorna i prezzi minimi selezionati",
            type="primary",use_container_width=True,
            disabled=not rows_to_update or not batch_confirm,
            key=f"buybox_batch_apply_{scope}_{batch_selection_signature}",
        )
        if batch_clicked:
            batch_progress=st.progress(0.0)
            batch_label=st.empty()
            updated_count=0
            failed_messages=[]
            for position,row in enumerate(rows_to_update,1):
                ok,message=perform_minimum_price_update(
                    row["item"],row["price"],"minimo_buybox_multiplo",
                    rerun_after=False,
                    allow_confirmed_loss=bool(row.get("is_loss") and include_losses),
                )
                if ok:
                    updated_count+=1
                else:
                    failed_messages.append(message)
                batch_progress.progress(position/len(rows_to_update))
                batch_label.caption(
                    f"Aggiornate {position:,} di {len(rows_to_update):,} offerte."
                )
            skipped_count=len(batch_rows)-len(rows_to_update)
            flash_message=(
                f"Aggiornamento multiplo completato: {updated_count:,} riuscite · "
                f"{len(failed_messages):,} fallite · {skipped_count:,} escluse. "
                "Il prezzo principale è rimasto invariato."
            )
            if failed_messages:
                flash_message+=f" Primo errore: {failed_messages[0]}"
            st.session_state[flash_key]=flash_message
            st.rerun()

    if selected_action_item is not None:
        item=selected_action_item
        row_key=(
            f"{account['id']}_{environment}_"
            f"{item['storefront']}_{item['id']}"
        )
        economics=row_economics(item)
        currency=economics["currency"]
        with st.container(border=True):
            st.markdown(
                f"**Riga selezionata · "
                f"{country_names.get(item['storefront'],item['storefront'].upper())} "
                f"· EAN {item['ean']} · SKU {item['sku']}**"
            )
            st.caption(
                f"Fornitore: {item.get('supplier_name') or 'Non associato'} · "
                f"Listino: {item.get('price_list_name') or 'Non associato'} · "
                f"Origine costo: {item.get('cost_match_source') or 'Non disponibile'}"
            )
            info1,info2,info3,info4,info5=st.columns(5)
            info1.metric(
                "Prezzo principale (invariato)",
                f"{float(item['our_price']):.2f} {currency}"
                if item.get("our_price") is not None else "Non rilevato",
            )
            info2.metric(
                "Prezzo più basso attuale",
                f"{float(item['minimum_price']):.2f} {currency}"
                if item.get("minimum_price") is not None else "Non impostato",
            )
            info2.caption(
                str(item.get("minimum_price_source") or "Non restituito da Kaufland")
            )
            info3.metric(
                "Obiettivo Buy Box",
                f"{float(item['target_sales_price']):.2f} {currency}"
                if item.get("target_sales_price") is not None else "Non disponibile",
            )
            info4.metric(
                "Costo totale",
                f"{float(economics['total_cost_eur']):.2f} €"
                if economics["total_cost_eur"] is not None else "Non disponibile",
            )
            info5.metric(
                "Tariffa commissione corrente",
                f"{economics['commission_pct']:.2f}%"
                +(
                    f" + {economics['commission_fixed_eur']:.2f} €"
                    if economics["commission_fixed_eur"]>0 else ""
                ),
                help=economics["commission_source"],
            )
            commission_info=st.columns(3)
            commission_info[0].metric(
                "Commissione effettiva sul nostro totale",
                (
                    f"{float(item['current_commission_effective_pct']):.2f}%"
                    if item.get("current_commission_effective_pct") is not None
                    else "Non calcolabile"
                ),
            )
            commission_info[1].metric(
                "Commissione corrente stimata",
                (
                    f"{float(item['current_commission_eur']):.2f} €"
                    if item.get("current_commission_eur") is not None
                    else "Non calcolabile"
                ),
            )
            commission_info[2].metric(
                "Ultima commissione reale ordine",
                (
                    f"{float(item['actual_order_commission_pct']):.2f}%"
                    if item.get("actual_order_commission_pct") is not None
                    else "Non disponibile"
                ),
                help=(
                    f"Ordine {item.get('actual_order_id') or '—'} · "
                    f"Importo {item.get('actual_order_commission_local') or '—'} "
                    f"{item.get('actual_order_commission_currency') or ''}"
                ),
            )

            st.markdown("#### Confronto logistico Buy Box")
            logistics_message=str(item.get("logistics_status") or "").strip()
            if logistics_message:
                st.warning(logistics_message)
            elif str(item.get("status") or "")=="Vinta":
                st.success("Buy Box vinta: nessuna penalizzazione logistica rilevata.")
            else:
                st.info(
                    "Nessuna penalizzazione logistica determinabile dai dati API "
                    "disponibili per questa offerta."
                )
            logistics1,logistics2,logistics3=st.columns(3)
            logistics1.metric(
                "Consegna del vincitore",
                delivery_window(item.get("delivery_min"),item.get("delivery_max")),
            )
            logistics2.metric(
                "Nostra consegna",
                delivery_window(
                    item.get("own_delivery_min"),item.get("own_delivery_max")
                ),
            )
            current_handling=item.get("own_handling_time")
            logistics3.metric(
                "Nostri giorni di gestione",
                (
                    f"{int(current_handling)} gg lavorativi"
                    if current_handling not in (None,"") else "Non disponibili"
                ),
            )
            handling_col,handling_button_col=st.columns([2,1])
            new_handling=int(handling_col.number_input(
                "Nuovi giorni di gestione",
                min_value=0,max_value=365,
                value=int(current_handling or 0),step=1,
                key=f"buybox_handling_{row_key}",
                help=(
                    "Giorni lavorativi necessari prima di affidare il prodotto "
                    "al corriere. Il tempo di trasporto dipende dal gruppo di spedizione."
                ),
            ))
            handling_clicked=handling_button_col.button(
                "Aggiorna giorni di gestione",
                use_container_width=True,
                key=f"buybox_handling_apply_{row_key}",
            )
            if handling_clicked:
                if current_handling not in (None,"") and (
                    int(current_handling)==new_handling
                ):
                    st.info("I giorni di gestione non sono cambiati.")
                else:
                    perform_handling_time_update(item,new_handling)
            st.caption(
                "La modifica riguarda solo i giorni di preparazione dell’offerta. "
                "I tempi del corriere restano quelli del gruppo di spedizione Kaufland."
            )

            st.divider()
            align_col,custom_col=st.columns(2)
            target_value=float(item.get("target_sales_price") or 0)
            with align_col:
                st.markdown("**1. Imposta l’obiettivo Buy Box come prezzo più basso**")
                target_evaluation=evaluate_row_price(item,target_value)
                show_margin_alert("Nuovo prezzo più basso Buy Box",target_evaluation)
                align_clicked=st.button(
                    "Imposta come prezzo più basso",use_container_width=True,
                    key=f"buybox_align_{row_key}",
                )
            default_custom=float(
                item.get("minimum_price") or item.get("target_sales_price")
                or item.get("our_price") or .01
            )
            with custom_col:
                st.markdown("**2. Imposta un prezzo più basso personalizzato**")
                custom_price=float(st.number_input(
                    f"Nuovo prezzo più basso ({currency})",min_value=.01,
                    value=default_custom,
                    step=.01,format="%.2f",key=f"buybox_custom_price_{row_key}",
                ))
                custom_evaluation=evaluate_row_price(item,custom_price)
                show_margin_alert("Prezzo più basso personalizzato",custom_evaluation)
                custom_clicked=st.button(
                    "Applica prezzo più basso personalizzato",use_container_width=True,
                    key=f"buybox_custom_apply_{row_key}",
                )

            pending_key=f"buybox_pending_price_{row_key}"
            if align_clicked:
                if not target_evaluation["can_update"]:
                    st.error(margin_message("Allineamento bloccato",target_evaluation))
                elif target_evaluation["requires_confirmation"]:
                    st.session_state[pending_key]={
                        "price":target_value,"source":"minimo_buybox",
                    }
                else:
                    perform_minimum_price_update(
                        item,target_value,"minimo_buybox"
                    )
            if custom_clicked:
                if not custom_evaluation["can_update"]:
                    st.error(margin_message("Prezzo bloccato",custom_evaluation))
                elif custom_evaluation["requires_confirmation"]:
                    st.session_state[pending_key]={
                        "price":custom_price,"source":"minimo_personalizzato",
                    }
                else:
                    perform_minimum_price_update(
                        item,custom_price,"minimo_personalizzato"
                    )

            pending=st.session_state.get(pending_key)
            if pending:
                pending_evaluation=evaluate_row_price(item,float(pending["price"]))
                st.warning(
                    f"Margine inferiore al 10%: prezzo {float(pending['price']):.2f} "
                    f"{currency}, guadagno {pending_evaluation['profit_eur']:+.2f} € "
                    f"({pending_evaluation['profit_pct']:.2f}%). Confermi l'aggiornamento?"
                )
                confirm_col,cancel_col=st.columns(2)
                if confirm_col.button(
                    "Conferma comunque",type="primary",use_container_width=True,
                    key=f"buybox_confirm_{row_key}",
                ):
                    perform_minimum_price_update(
                        item,float(pending["price"]),str(pending["source"])
                    )
                if cancel_col.button(
                    "Annulla",use_container_width=True,key=f"buybox_cancel_{row_key}"
                ):
                    st.session_state.pop(pending_key,None);st.rerun()

    price_history=rows("""SELECT storefront,ean,sku,supplier_name,price_list_name,
        source,previous_price,new_price,currency,commission_pct,
        commission_fixed_eur,commission_source,commission_eur,
        commission_effective_pct,profit_eur,profit_pct,margin_status,
        price_field,created_at
        FROM kaufland_buybox_account_price_updates
        WHERE seller_id=? AND marketplace_account_id=? AND environment=?
        ORDER BY created_at DESC,id DESC LIMIT 100""",(
        seller_id,account["id"],environment,
    ))
    if price_history:
        with st.expander("Storico aggiornamenti prezzo"):
            st.dataframe(pd.DataFrame([{
                "Paese":country_names.get(x["storefront"],x["storefront"].upper()),
                "EAN":x["ean"],"SKU":x["sku"],
                "Fornitore":x.get("supplier_name","") or "Non associato",
                "Listino":x.get("price_list_name","") or "Non associato",
                "Origine":(
                    "Prezzo più basso Buy Box"
                    if x["source"]=="minimo_buybox"
                    else (
                        "Prezzo più basso personalizzato"
                        if x["source"]=="minimo_personalizzato"
                        else (
                            "Vecchio allineamento prezzo principale"
                            if x["source"]=="allineamento_buybox"
                            else "Vecchio prezzo principale personalizzato"
                        )
                    )
                ),
                "Campo aggiornato":(
                    "Prezzo più basso"
                    if x.get("price_field")=="minimum_price" else "Prezzo principale"
                ),
                "Valore precedente":x["previous_price"],
                "Nuovo valore":x["new_price"],
                "Valuta":x["currency"],
                "Commissione API corrente %":x["commission_pct"],
                "Commissione fissa €":x.get("commission_fixed_eur",0),
                "Commissione €":x.get("commission_eur"),
                "Commissione effettiva %":x.get("commission_effective_pct"),
                "Fonte commissione":x.get("commission_source",""),
                "Guadagno/Perdita €":x["profit_eur"],
                "Margine %":x["profit_pct"],"Esito":x["margin_status"],
                "Aggiornato":x["created_at"],
            } for x in price_history]),use_container_width=True,hide_index=True)
