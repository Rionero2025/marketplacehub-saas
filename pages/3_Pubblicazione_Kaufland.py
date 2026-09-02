from __future__ import annotations

import json
import hashlib
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import pandas as pd
import streamlit as st

from services.db import DATA_DIR, execute, json_text, now_iso, row, rows
from services.batch_memory import (attach_product_keys, frame_records, load_state,
                                   progress_summary, record_result, reset_state,
                                   select_range)
from services.fx import get_ecb_rates
from services.kaufland import KauflandClient
from services.kaufland_offer import (commercial_values, composed_sku,
                                     has_valid_ean, price_fields)
from services.lists import (apply_weight_exclusion,country_cost,
                            destination_country_codes,normalize)
from services.security import decrypt_dict
from services.saved_view_storage import load_saved_view_frame
from services.session import bootstrap, seller_selector

embedded=bool(st.session_state.get("_embedded_marketplace_publication"))
if not embedded:
    bootstrap();st.title("Pubblicazione Kaufland")
seller_id=st.session_state.get("active_seller_id") if embedded else seller_selector()
if seller_id is None:st.stop()

accounts=rows("SELECT * FROM marketplace_accounts WHERE seller_id=? AND marketplace='kaufland' AND active=1 ORDER BY account_name",(seller_id,))
if not accounts:st.error("Configura un account Kaufland per questo Seller.");st.stop()

amap={f"{x['account_name']} · ID {x['id']}":x for x in accounts};account=amap[st.selectbox("Account Kaufland",list(amap))]
cred=decrypt_dict(account["credentials_encrypted"])
playground=st.checkbox("Playground (test)",value=True)
client=KauflandClient(cred.get("client_key",""),cred.get("secret_key",""),playground)

country_names={"de":"Germania","it":"Italia","fr":"Francia","at":"Austria","pl":"Polonia","cz":"Rep. Ceca","sk":"Slovacchia","es":"Spagna","nl":"Paesi Bassi"}

views=rows("""SELECT sv.*,pl.name price_list_name,pl.id price_list_id,s.name supplier_name
FROM saved_views sv JOIN price_lists pl ON pl.id=sv.price_list_id
JOIN suppliers s ON s.id=pl.supplier_id
JOIN saved_view_marketplaces svm ON svm.saved_view_id=sv.id
WHERE sv.seller_id=? AND svm.marketplace_account_id=? ORDER BY sv.updated_at DESC""",(seller_id,account["id"]))
if not views:st.error("Non ci sono viste salvate destinate a questo account Kaufland. Creane una in Lavora sui listini.");st.stop()
vmap={f"{x['name']} · {x['row_count']} prodotti · ID {x['id']}":x for x in views};view=vmap[st.selectbox("Vista salvata",list(vmap))]
pl={"id":view["price_list_id"],"name":view["price_list_name"]}
try:df=normalize(load_saved_view_frame(view))
except Exception as e:st.error(f"Impossibile leggere la vista salvata: {e}");st.stop()
saved_destination_countries=destination_country_codes(df)
compatible_saved=[
    code for code in country_names if code in saved_destination_countries
]
if saved_destination_countries and not compatible_saved:
    st.error(
        "La vista non contiene Paesi compatibili con Kaufland. "
        f"Paesi salvati: {', '.join(code.upper() for code in saved_destination_countries)}."
    )
    st.stop()

st.subheader("Paesi Kaufland")
if compatible_saved:
    st.info(
        "Paesi riconosciuti automaticamente dalla vista Cecotec: "
        + ", ".join(country_names[code] for code in compatible_saved)
        + "."
    )
country_cols=st.columns(5)
selected_storefronts=[]
for i,(code,label) in enumerate(country_names.items()):
    allowed=not saved_destination_countries or code in compatible_saved
    default_selected=code in compatible_saved if saved_destination_countries else code=="de"
    if country_cols[i%5].checkbox(
        label,key=f"country_{view['id']}_{view.get('updated_at','')}_{code}",
        value=default_selected,disabled=not allowed,
    ):
        selected_storefronts.append(code)
if not selected_storefronts:
    st.warning("Seleziona almeno un Paese Kaufland.");st.stop()
primary_storefront=selected_storefronts[0]

fx_data=None
if any(sf in selected_storefronts for sf in ("pl","cz")):
    @st.cache_data(ttl=3600,show_spinner=False)
    def cached_ecb_rates():
        return get_ecb_rates()
    fx_data=cached_ecb_rates()
    if fx_data.get("online"):
        st.success(
            f"Cambio BCE aggiornato al {fx_data['date']}: "
            f"1 EUR = {float(fx_data['rates']['PLN']):.4f} PLN · "
            f"1 EUR = {float(fx_data['rates']['CZK']):.4f} CZK"
        )
    else:
        st.warning(
            f"BCE momentaneamente non raggiungibile. Uso l'ultimo cambio salvato del {fx_data['date']}: "
            f"PLN {float(fx_data['rates']['PLN']):.4f} · CZK {float(fx_data['rates']['CZK']):.4f}."
        )

rule=row("SELECT * FROM commercial_rules WHERE seller_id=? AND price_list_id=? AND marketplace='kaufland' AND storefront=?",(seller_id,pl["id"],primary_storefront)) or {}
try:saved_rule_settings=json.loads(rule.get("settings_json") or "{}")
except Exception:saved_rule_settings={}
r1,r2,r3,r4,r5=st.columns(5)
margin=r1.number_input("Margine %",0.0,500.0,float(rule.get("margin_pct",35)),1.0)
commission=r2.number_input("Commissione %",0.0,100.0,float(rule.get("commission_pct",15)),0.5)
minimum_margin=r3.number_input("Prezzo minimo: ricarico %",0.0,500.0,float(rule.get("minimum_margin_pct",10)),1.0)
minimum_qty=r4.number_input("Quantità minima",0,99999,int(rule.get("minimum_qty",1)))
minimum_cost=r5.number_input("Costo minimo",0.0,999999.0,float(rule.get("minimum_cost",0)),1.0)

weight_modes={
    "Nessuna esclusione":"none",
    "Escludi peso superiore a":"above",
    "Escludi peso inferiore a":"below",
    "Escludi peso compreso tra Da e A":"between",
}
weight_available=("weight_kg" in df and pd.to_numeric(df["weight_kg"],errors="coerce").fillna(0).gt(0).any())
weight_mode="none";weight_from=0.0;weight_to=0.0
if weight_available:
    st.subheader("Esclusione per peso")
    saved_weight_mode=str(saved_rule_settings.get("weight_exclusion_mode","none"))
    weight_index=next((index for index,value in enumerate(weight_modes.values()) if value==saved_weight_mode),0)
    weight_columns=st.columns([2,1,1])
    weight_label=weight_columns[0].selectbox("Filtro peso (kg)",list(weight_modes),index=weight_index)
    weight_mode=weight_modes[weight_label]
    if weight_mode in ("above","below"):
        weight_from=weight_columns[1].number_input(
            "Peso di riferimento (kg)",min_value=0.0,value=float(saved_rule_settings.get("weight_from",0)),
            step=0.1,format="%.3f",
        )
    elif weight_mode=="between":
        weight_from=weight_columns[1].number_input(
            "Da kg",min_value=0.0,value=float(saved_rule_settings.get("weight_from",0)),
            step=0.1,format="%.3f",
        )
        weight_to=weight_columns[2].number_input(
            "A kg",min_value=0.0,value=float(saved_rule_settings.get("weight_to",0)),
            step=0.1,format="%.3f",
        )
        if weight_from>weight_to:st.error("Il peso «Da» non può essere maggiore del peso «A».")
    valid_weights=pd.to_numeric(df["weight_kg"],errors="coerce").fillna(0).gt(0)
    st.caption(
        f"Peso disponibile per {int(valid_weights.sum()):,} prodotti; "
        f"{int((~valid_weights).sum()):,} prodotti senza peso resteranno inclusi."
    )
else:
    st.caption("Filtro peso non disponibile: questa vista non contiene valori di peso utilizzabili.")

country_config={}
for sf in selected_storefronts:
    with st.expander(f"Configurazione {country_names[sf]} ({sf.upper()})",expanded=True):
        try:groups=client.shipping_groups(sf)
        except Exception as e:groups=[];st.warning(f"Gruppi non caricati per {sf.upper()}: {e}")
        try:warehouses=client.warehouses(sf)
        except Exception as e:warehouses=[];st.warning(f"Magazzini non caricati per {sf.upper()}: {e}")
        k1,k2,k3,k4,k5=st.columns(5)
        if groups:
            gmap={f"{x.get('name','Gruppo')} · ID {x['id_shipping_group']}":str(x["id_shipping_group"]) for x in groups}
            shipping_group=gmap[k1.selectbox("Gruppo spedizione",list(gmap),key=f"group_{sf}")]
        else:shipping_group=k1.text_input("ID gruppo spedizione",key=f"group_manual_{sf}")
        if warehouses:
            wmap={f"{x.get('name','Magazzino')} · ID {x['id_warehouse']}":str(x["id_warehouse"]) for x in warehouses}
            warehouse=wmap[k2.selectbox("Magazzino",list(wmap),key=f"warehouse_{sf}")]
        else:warehouse=k2.text_input("ID magazzino",key=f"warehouse_manual_{sf}")
        handling=k3.number_input("Giorni gestione",0,30,1,key=f"handling_{sf}")
        vat=k4.selectbox("Indicatore IVA",("standard_rate","reduced_rate_1","reduced_rate_2","super_reduced_rate","zero_rate"),key=f"vat_{sf}")
        if sf in ("pl","cz"):
            currency="PLN" if sf=="pl" else "CZK"
            automatic=k5.checkbox("Cambio BCE automatico",value=True,key=f"auto_fx_{sf}")
            auto_rate=float(fx_data["rates"][currency])
            multiplier=k5.number_input(
                f"1 EUR in {currency}",0.0001,1000.0,auto_rate,0.0001,
                key=f"multiplier_{sf}_{fx_data['date']}",disabled=automatic,format="%.4f",
            )
            if automatic:
                multiplier=auto_rate
            st.caption(
                f"Conversione {'automatica' if automatic else 'manuale'}: prezzi in EUR × {multiplier:.4f} = {currency}. "
                f"Fonte: {fx_data['source']}, data {fx_data['date']}."
            )
        else:
            multiplier=k5.number_input("Moltiplicatore prezzo",0.0001,1000.0,1.0,0.01,key=f"multiplier_{sf}")
        country_config[sf]={"shipping_group":shipping_group,"warehouse":warehouse,"handling":handling,"vat":vat,
                            "multiplier":multiplier,"currency":"PLN" if sf=="pl" else ("CZK" if sf=="cz" else "EUR"),
                            "fx_date":fx_data["date"] if sf in ("pl","cz") else "",
                            "fx_source":fx_data["source"] if sf in ("pl","cz") else "",
                            "weight_exclusion_mode":weight_mode,"weight_from":weight_from,
                            "weight_to":weight_to}

invalid_weight_range=(weight_mode=="between" and weight_from>weight_to)
if st.button("Salva regole commerciali",disabled=invalid_weight_range):
    for sf in selected_storefronts:
        execute("""INSERT INTO commercial_rules(seller_id,price_list_id,marketplace,storefront,margin_pct,commission_pct,minimum_margin_pct,minimum_qty,minimum_cost,settings_json,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(seller_id,price_list_id,marketplace,storefront) DO UPDATE SET
        margin_pct=excluded.margin_pct,commission_pct=excluded.commission_pct,minimum_margin_pct=excluded.minimum_margin_pct,
        minimum_qty=excluded.minimum_qty,minimum_cost=excluded.minimum_cost,settings_json=excluded.settings_json,updated_at=excluded.updated_at""",
        (seller_id,pl["id"],"kaufland",sf,margin,commission,minimum_margin,minimum_qty,minimum_cost,json_text(country_config[sf]),now_iso()))
    st.success(f"Regole salvate per {len(selected_storefronts)} Paesi.")

source_purchase_cost=pd.to_numeric(df["cost"],errors="coerce").fillna(0)
national_cost=country_cost(df,primary_storefront)
shipping=(
    pd.to_numeric(df["total_cost"],errors="coerce").fillna(0)-source_purchase_cost
).clip(lower=0) if "total_cost" in df else pd.Series(0.0,index=df.index)
base_cost=national_cost+shipping
df=df[(base_cost>=minimum_cost)&(df["quantity"]>=minimum_qty)].copy();base_cost=base_cost.loc[df.index]
before_weight_filter=len(df)
try:
    df=apply_weight_exclusion(df,weight_mode,weight_from,weight_to)
except ValueError as error:
    st.error(str(error));st.stop()
base_cost=base_cost.loc[df.index]
excluded_by_weight=before_weight_filter-len(df)
if weight_mode!="none":
    st.info(f"Prodotti esclusi dal filtro peso: {excluded_by_weight:,}.")
df["_source_purchase_cost"]=source_purchase_cost.loc[df.index]
df["_destination_shipping"]=shipping.loc[df.index]
primary_values=[
    commercial_values(purchase,delivery,margin,minimum_margin,commission)
    for purchase,delivery in zip(national_cost.loc[df.index],shipping.loc[df.index])
]
for column in ("cost","price","minimum_price","commission_eur","profit"):
    df[column]=[values[column] for values in primary_values]
if len(selected_storefronts)>1:
    st.caption(
        f"La tabella mostra i valori di {country_names[primary_storefront]}. "
        "Durante l’invio costo, prezzo, prezzo minimo e SKU composto saranno "
        "ricalcolati separatamente per ogni Paese selezionato."
    )
df=attach_product_keys(df)
selection_scope=f"{seller_id}_{account['id']}_{view['id']}_{'-'.join(selected_storefronts)}"
selection_state_key=f"kaufland_selected_keys_{selection_scope}"
grid_key=(f"kaufland_grid_{view['id']}_{'-'.join(selected_storefronts)}_{margin:.2f}_{commission:.2f}_"
          f"{minimum_margin:.2f}_{minimum_qty}_{minimum_cost:.2f}_{weight_mode}_{weight_from:.3f}_{weight_to:.3f}")

use_composite_sku=st.checkbox("Usa SKU composto: NOMEFORNITORE_EAN_PREZZOACQUISTO_PREZZOMINIMO",value=False)
operation="CREA/AGGIORNA"
st.caption("Operazione: CREA/AGGIORNA. Per rimuovere offerte usa la voce separata Cancellazione dai Marketplace.")
batch_scope={"marketplace":"kaufland","seller_id":int(seller_id),"account_id":int(account["id"]),
             "view_id":int(view["id"]),"view_updated_at":str(view.get("updated_at", "")),
             "storefronts":sorted(selected_storefronts),"operation":operation,
             "sku_mode":"composito" if use_composite_sku else "originale","environment":"test" if playground else "live",
             "weight_filter":{"mode":weight_mode,"from":weight_from,"to":weight_to}}
all_batch_records=frame_records(df);batch_state=load_state(batch_scope);batch_summary=progress_summary(batch_state,all_batch_records)

select_col,deselect_col,start_col,end_col,range_col=st.columns([1,1,0.7,0.7,1])
if select_col.button("☑ Seleziona tutti",key=f"select_all_{selection_scope}",use_container_width=True):
    st.session_state[selection_state_key]=[item["key"] for item in all_batch_records]
    st.session_state.pop(grid_key,None)
    st.rerun()
if deselect_col.button("☐ Deseleziona tutti",key=f"deselect_all_{selection_scope}",use_container_width=True):
    st.session_state[selection_state_key]=[]
    st.session_state.pop(grid_key,None)
    st.rerun()
maximum_position=max(1,len(all_batch_records))
range_start=start_col.number_input(
    "Da posizione",min_value=1,max_value=maximum_position,value=1,step=1,
    key=f"batch_start_{selection_scope}",
)
range_end=end_col.number_input(
    "A posizione",min_value=1,max_value=maximum_position,
    value=min(100,maximum_position),step=1,key=f"batch_end_{selection_scope}",
)
if range_col.button("Seleziona intervallo",key=f"select_range_{selection_scope}",use_container_width=True):
    try:
        chosen_keys,_,_=select_range(
            batch_scope,all_batch_records,int(range_start),int(range_end)
        )
        st.session_state[selection_state_key]=chosen_keys
        st.session_state.pop(grid_key,None);st.rerun()
    except ValueError as error:
        st.error(str(error))

st.caption(f"Le posizioni Da/A si riferiscono ai {len(all_batch_records):,} prodotti attualmente filtrati e comprendono entrambi gli estremi.")
st.caption(f"Memoria intervalli: inviati {batch_summary['completed']:,} · rimanenti {batch_summary['remaining']:,} · totale {batch_summary['total']:,}.")
if batch_summary.get("active"):
    active=batch_summary["active"]
    selected_count=int(active.get("selected_count",0) or 0)
    skipped=int(active.get("already_completed_count",0) or 0)
    position_text=(f"posizioni {active['requested_start']:,}–{active['requested_end']:,} · "
                   if active.get("requested_start") else "")
    if selected_count:
        skipped_text=f" · {skipped:,} già inviati esclusi" if skipped else ""
        st.info(
            f"Intervallo {active['number']} selezionato: {position_text}"
            f"{selected_count:,} prodotti · da SKU {active['first_sku']} a SKU {active['last_sku']}"
            f"{skipped_text}."
        )
    elif skipped:
        st.warning(
            f"Le posizioni {active['requested_start']:,}–{active['requested_end']:,} "
            "risultano già inviate: nessun prodotto è stato riselezionato."
        )
if batch_summary.get("history"):
    with st.expander("Storico intervalli inviati"):
        st.dataframe(pd.DataFrame(batch_summary["history"][-20:]).drop(columns=["metadata"],errors="ignore"),use_container_width=True,hide_index=True)
with st.expander("Azzera memoria intervalli"):
    reset_confirm=st.checkbox("Confermo di voler ricominciare dal primo prodotto",key=f"reset_batch_confirm_{selection_scope}")
    if st.button("Azzera memoria",key=f"reset_batch_{selection_scope}",disabled=not reset_confirm):
        reset_state(batch_scope);st.session_state[selection_state_key]=[];st.session_state.pop(grid_key,None);st.rerun()

df=df.drop(columns=["Seleziona","Mantieni"],errors="ignore")
stored_selection=st.session_state.get(selection_state_key,[])
selected_key_defaults=({item["key"] for item in all_batch_records} if stored_selection is True else
                       set(stored_selection) if isinstance(stored_selection,(list,tuple,set)) else set())
df.insert(0,"Seleziona",df["_batch_key"].isin(selected_key_defaults))
view_cols=["Seleziona","ean","sku","name"]
if weight_available:view_cols.append("weight_kg")
view_cols+=["cost","quantity","price","minimum_price","commission_eur","profit"]
edited=st.data_editor(df[view_cols],use_container_width=True,height=500,hide_index=True,
    key=grid_key,
    column_config={
        "Seleziona":st.column_config.CheckboxColumn(required=True),
        "cost":st.column_config.NumberColumn("Costo acquisto",format="%.2f"),
        "price":st.column_config.NumberColumn("Prezzo vendita",format="%.2f"),
        "minimum_price":st.column_config.NumberColumn("Prezzo minimo",format="%.2f"),
        "commission_eur":st.column_config.NumberColumn("Commissione €",format="%.2f"),
        "profit":st.column_config.NumberColumn("Guadagno €",format="%.2f"),
        "weight_kg":st.column_config.NumberColumn("Peso (kg)",format="%.3f"),
    },disabled=["ean","sku","name","weight_kg","cost","quantity","price","minimum_price","commission_eur","profit"])
selected_indexes=edited.index[edited["Seleziona"]==True]
selected=df.loc[selected_indexes].drop(columns=["Seleziona","Mantieni"],errors="ignore").copy()
selected_total=len(selected)
excluded_missing_ean=pd.DataFrame()
if use_composite_sku and not selected.empty:
    valid_ean_mask=selected.apply(has_valid_ean,axis=1)
    excluded_missing_ean=selected[~valid_ean_mask].copy()
    selected=selected[valid_ean_mask].copy()

metric_1,metric_2=st.columns(2)
metric_1.metric("Prodotti selezionati",selected_total)
metric_2.metric("Prodotti pronti all’invio",len(selected))
if not excluded_missing_ean.empty:
    st.warning(
        f"{len(excluded_missing_ean):,} prodotti senza EAN sono stati esclusi dallo SKU composto. "
        f"Gli altri {len(selected):,} prodotti possono essere inviati normalmente."
    )
    with st.expander("Prodotti esclusi perché senza EAN"):
        st.dataframe(
            excluded_missing_ean[["ean","sku","name"]],use_container_width=True,hide_index=True
        )

def effective_sku(product) -> str:
    if not use_composite_sku:
        return str(product["sku"]).strip()
    return composed_sku(str(view.get("supplier_name","")),product)

def product_for_storefront(product, storefront: str) -> dict:
    """Return one product recalculated with the destination-country Cecotec cost."""
    data=product.to_dict() if hasattr(product,"to_dict") else dict(product)
    source_cost=float(data.get("_source_purchase_cost",data.get("cost",0)) or 0)
    delivery=float(data.get("_destination_shipping",0) or 0)
    source_row=pd.DataFrame([data])
    source_row["cost"]=source_cost
    purchase=float(country_cost(source_row,storefront).iloc[0])
    data.update(
        commercial_values(purchase,delivery,margin,minimum_margin,commission)
    )
    data["_storefront"]=storefront
    return data

if use_composite_sku and not selected.empty:
    preview=effective_sku(selected.iloc[0])
    st.caption(
        f"Anteprima SKU composto per {country_names[primary_storefront]}: `{preview}`"
    )
    generated=selected.apply(effective_sku,axis=1)
    duplicate_count=int(generated.duplicated(keep=False).sum())
    if duplicate_count:st.warning(f"Attenzione: {duplicate_count} prodotti hanno lo stesso EAN e generano SKU duplicati.")

checkpoint_dir=DATA_DIR/"checkpoints"
batch_signature=hashlib.sha256("|".join(selected.get("_batch_key",pd.Series(dtype=str)).astype(str)).encode("utf-8")).hexdigest()[:12]
checkpoint_name=re.sub(r"[^A-Za-z0-9_.-]+","_",f"kaufland_{seller_id}_{account['id']}_{view['id']}_{operation}_{'-'.join(selected_storefronts)}_{'composto' if use_composite_sku else 'originale'}_{'test' if playground else 'live'}_{batch_signature}.json")
checkpoint_path=checkpoint_dir/checkpoint_name
resume_previous=False
if checkpoint_path.exists():
    try:
        saved_checkpoint=json.loads(checkpoint_path.read_text(encoding="utf-8"));saved_count=len(saved_checkpoint.get("completed",[]))
    except Exception:saved_count=0
    if saved_count:
        st.info(f"Trovato invio interrotto: {saved_count} operazioni già completate.")
        resume_previous=st.checkbox("Riprendi dall’ultimo punto salvato",value=True)

if st.button("Esegui su Kaufland",type="primary"):
    if selected.empty:st.warning("Seleziona almeno un prodotto.")
    elif operation=="CREA/AGGIORNA" and any(not country_config[sf]["shipping_group"] or not country_config[sf]["warehouse"] for sf in selected_storefronts):
        st.warning("Seleziona gruppo spedizione e magazzino per tutti i Paesi scelti.")
    else:
        tasks=[];skipped_during_task_build=[]
        for _,product in selected.iterrows():
            original_product=product.to_dict()
            if use_composite_sku and not has_valid_ean(original_product):
                skipped_during_task_build.append({
                    "ean":original_product.get("ean",""),
                    "sku":original_product.get("sku",""),
                    "name":original_product.get("name",""),
                })
                continue
            for sf in selected_storefronts:
                storefront_product=product_for_storefront(original_product,sf)
                try:
                    offer_sku=effective_sku(storefront_product)
                except ValueError as error:
                    skipped_during_task_build.append({
                        "ean":storefront_product.get("ean",""),
                        "sku":storefront_product.get("sku",""),
                        "name":storefront_product.get("name",""),
                        "paese":sf,
                        "errore":str(error),
                    })
                    continue
                tasks.append({
                    "key":f"{sf}|{offer_sku}",
                    "batch_key":str(product["_batch_key"]),
                    "sf":sf,
                    "offer_sku":offer_sku,
                    "product":storefront_product,
                })
        if skipped_during_task_build:
            st.warning(
                f"{len(skipped_during_task_build):,} prodotti non validi sono stati ignorati "
                "durante la preparazione finale; l’invio degli altri prodotti prosegue."
            )
        if not tasks:
            st.warning("Nessun prodotto valido da inviare a Kaufland.")
            st.stop()
        total=len(tasks);completed=set();details=[]
        if resume_previous and checkpoint_path.exists():
            try:completed=set(json.loads(checkpoint_path.read_text(encoding="utf-8")).get("completed",[]))
            except Exception:completed=set()
        pending=[task for task in tasks if task["key"] not in completed]
        checkpoint_dir.mkdir(parents=True,exist_ok=True)

        rate_lock=threading.Lock();request_times=deque()
        def throttle():
            while True:
                with rate_lock:
                    now=time.monotonic()
                    while request_times and now-request_times[0]>=1.0:request_times.popleft()
                    if len(request_times)<40:
                        request_times.append(now);return
                    delay=max(0.01,1.0-(now-request_times[0]))
                time.sleep(delay)
        client.before_request=throttle

        def execute_task(task):
            x=task["product"];sf=task["sf"];cfg=country_config[sf];offer_sku=task["offer_sku"]
            try:
                if operation=="ELIMINA":result=client.delete_offer(offer_sku,sf)
                else:
                    # price/minimum_price already include the destination purchase
                    # cost and shipping. Recalculating here used to add shipping a
                    # second time, making the API minimum differ from the SKU.
                    payload={"ean":str(x["ean"]),"condition":"NEW",
                             "amount":min(99999,int(x["quantity"])),"id_offer":offer_sku,"handling_time":int(cfg["handling"]),
                             "id_shipping_group":str(cfg["shipping_group"]),"id_warehouse":str(cfg["warehouse"]),"vat_indicator":cfg["vat"]}
                    payload.update(price_fields(x,float(cfg["multiplier"])))
                    result=client.upsert(payload,sf)
                return {"key":task["key"],"paese":sf,"valuta":cfg["currency"],"tasso_eur":cfg["multiplier"],
                        "batch_key":task["batch_key"],
                        "data_cambio":cfg["fx_date"],"ean":str(x["ean"]),"sku_originale":str(x["sku"]),
                        "sku_inviato":offer_sku,"ok":True,"result":result}
            except Exception as error:
                return {"key":task["key"],"batch_key":task["batch_key"],"paese":sf,"ean":str(x.get("ean","")),"sku_originale":str(x.get("sku","")),
                        "sku_inviato":offer_sku,"ok":False,"error":str(error)}

        st.caption("Ogni riga indicata come accettata è già stata trasmessa a Kaufland; la visualizzazione nel Seller Portal può richiedere alcuni minuti.")
        progress=st.progress(len(completed)/total if total else 1.0);progress_text=st.empty()
        st.markdown("#### Ultime offerte accettate da Kaufland")
        accepted_table=st.empty();recent_accepted=deque(maxlen=12);started=time.monotonic();processed=0
        with st.status("Invio parallelo in corso…",expanded=True) as status:
            with ThreadPoolExecutor(max_workers=15) as executor:
                futures=[executor.submit(execute_task,task) for task in pending]
                for future in as_completed(futures):
                    result=future.result();details.append(result);processed+=1
                    if result["ok"]:
                        completed.add(result["key"])
                        recent_accepted.appendleft({"Stato":"Accettata","Paese":result["paese"].upper(),"EAN":result["ean"],"SKU inviato":result["sku_inviato"]})
                    done=len(completed);attempted=processed+total-len(pending);elapsed=max(0.001,time.monotonic()-started)
                    speed=processed/elapsed;remaining=max(0,total-attempted);eta=int(remaining/speed) if speed else 0
                    progress.progress(min(1.0,attempted/total if total else 1.0))
                    progress_text.caption(f"Completate {attempted:,} / {total:,} · OK {done:,} · Velocità {speed:.1f}/s · Tempo stimato {eta//60}m {eta%60}s")
                    if recent_accepted and (processed%5==0 or processed==len(pending)):
                        accepted_table.dataframe(list(recent_accepted),use_container_width=True,hide_index=True)
                    if processed%100==0 or processed==len(pending):
                        temp_path=checkpoint_path.with_suffix(".tmp")
                        temp_path.write_text(json.dumps({"completed":sorted(completed),"total":total,"updated_at":now_iso()}),encoding="utf-8")
                        temp_path.replace(checkpoint_path)
            status.update(label="Operazione completata",state="complete")
        ok=len(completed);fail=total-ok
        if fail==0:checkpoint_path.unlink(missing_ok=True)
        selected_records=frame_records(selected)
        expected_by_product={item["key"]:{task["key"] for task in tasks if task["batch_key"]==item["key"]} for item in selected_records}
        successful_products={key for key,expected in expected_by_product.items() if expected and expected.issubset(completed)}
        failed_products={item["key"] for item in selected_records}-successful_products
        interval_entry=record_result(batch_scope,selected_records,successful_products,failed_products,
                                     "success" if not failed_products else "partial",
                                     {"task_success":ok,"task_failed":fail,"storefronts":selected_storefronts})
        execute("""INSERT INTO operations(seller_id,marketplace_account_id,price_list_id,marketplace,storefront,operation_type,status,total_rows,success_rows,failed_rows,details_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(seller_id,account["id"],pl["id"],"kaufland",",".join(selected_storefronts),operation,"success" if fail==0 else "partial",total,ok,fail,
        json_text({"environment":"test" if playground else "live","saved_view_id":view["id"],
                   "saved_view_name":view["name"],"rows":details}),now_iso()))
        if fail:st.warning(f"Intervallo {interval_entry['number']} da SKU {interval_entry['first_sku']} a SKU {interval_entry['last_sku']}: riuscite {ok} · fallite {fail}.")
        else:st.success(f"Intervallo {interval_entry['number']} inviato: {len(selected)} prodotti, da SKU {interval_entry['first_sku']} a SKU {interval_entry['last_sku']}, su {len(selected_storefronts)} Paesi ({ok} operazioni).")
        with st.expander("Dettaglio risultati"):st.json(details)
