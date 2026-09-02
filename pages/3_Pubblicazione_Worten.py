from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from services.batch_memory import (attach_product_keys, frame_records, load_state,
                                   progress_summary, record_result, reset_state,
                                   select_next)
from services.db import execute, json_text, now_iso, row, rows
from services.lists import country_cost, normalize
from services.security import decrypt_dict
from services.saved_view_storage import load_saved_view_frame
from services.session import bootstrap, seller_selector
from services.worten import (DEFAULT_API_URL, WORTEN_OFFER_COLUMNS, get_logistic_classes,
                             get_offer_states, normalize_offer_state, upload_offer_csv,
                             validate_credentials)

embedded=bool(st.session_state.get("_embedded_marketplace_publication"))
if not embedded:
    bootstrap(); st.title("Pubblicazione Worten Portogallo")
seller_id=st.session_state.get("active_seller_id") if embedded else seller_selector()
if seller_id is None: st.stop()

accounts=rows("SELECT * FROM marketplace_accounts WHERE seller_id=? AND marketplace='worten' AND active=1 ORDER BY account_name",(seller_id,))
if not accounts: st.error("Configura prima un account Worten in Gestione Seller."); st.stop()
c1,c2=st.columns(2)
amap={f"{x['account_name']} · ID {x['id']}":x for x in accounts}; account=amap[c1.selectbox("Account Worten",list(amap))]
views=rows("""SELECT sv.*,pl.name price_list_name,pl.id price_list_id,s.name supplier_name
FROM saved_views sv JOIN price_lists pl ON pl.id=sv.price_list_id
JOIN suppliers s ON s.id=pl.supplier_id
JOIN saved_view_marketplaces svm ON svm.saved_view_id=sv.id
WHERE sv.seller_id=? AND svm.marketplace_account_id=? ORDER BY sv.updated_at DESC""",(seller_id,account["id"]))
if not views: st.error("Nessuna vista salvata destinata a questo account Worten."); st.stop()
vmap={f"{x['name']} · {x['row_count']} prodotti · ID {x['id']}":x for x in views}; view=vmap[c2.selectbox("Vista salvata",list(vmap))]

cred=decrypt_dict(account["credentials_encrypted"]); api_key=cred.get("api_key",""); shop_id=cred.get("shop_id",""); api_url=cred.get("api_url",DEFAULT_API_URL)
if st.button("Verifica connessione Worten",key="verify_worten_publication"):
    check=validate_credentials(api_key,shop_id,api_url)
    if check["ok"]: st.success(check["message"])
    else: st.error(f"{check['message']} (HTTP {check['status'] or '—'})")

rule=row("SELECT * FROM commercial_rules WHERE seller_id=? AND price_list_id=? AND marketplace='worten' AND storefront='pt'",(seller_id,view["price_list_id"])) or {}
try: saved_settings=json.loads(rule.get("settings_json") or "{}")
except Exception: saved_settings={}
r1,r2,r3,r4,r5,r6=st.columns(6)
margin=r1.number_input("Margine %",0.0,500.0,float(rule.get("margin_pct",35)),1.0)
commission=r2.number_input("Commissione %",0.0,100.0,float(rule.get("commission_pct",15)),0.5)
minimum_margin=r3.number_input("Prezzo minimo: ricarico %",0.0,500.0,float(rule.get("minimum_margin_pct",10)),1.0)
minimum_qty=r4.number_input("Quantità minima",0,999999,int(rule.get("minimum_qty",1)))
minimum_cost=r5.number_input("Costo minimo",0.0,999999.0,float(rule.get("minimum_cost",0)),1.0)
minimum_profit=r6.number_input("Guadagno minimo €",0.0,999999.0,float(saved_settings.get("minimum_profit",0)),0.5,
                               help="Mostra e consente di inviare solo prodotti con un guadagno almeno pari a questa cifra, dopo la commissione Worten.")
s1,s2,s3,s4=st.columns(4)
leadtime=s1.number_input("Giorni preparazione",0,44,int(saved_settings.get("leadtime",2)))
@st.cache_data(ttl=3600,show_spinner=False)
def cached_logistic_classes(key:str,url:str):
    try:
        values=get_logistic_classes(key,api_url=url)
        return values,"api" if values else "empty"
    except Exception:return [],"fallback"
logistic_classes,logistic_source=cached_logistic_classes(api_key,api_url)
logistic_labels={"Usa la famiglia predefinita Worten":""}
for item in logistic_classes:
    logistic_labels[f"{item['label']} · codice {item['code']}"]=item["code"]
saved_logistic=str(saved_settings.get("logistic_class","")).strip()
logistic_index=next((i for i,value in enumerate(logistic_labels.values()) if value==saved_logistic),0)
logistic_label=s2.selectbox("Famiglia logistica",list(logistic_labels),index=logistic_index)
logistic_class=logistic_labels[logistic_label]
if logistic_source!="api":
    s2.caption("Elenco API non disponibile: sarà usata la famiglia predefinita del vostro account.")
else:
    s2.caption(f"Nel CSV sarà scritto il codice: {logistic_class or '(vuoto/predefinito)'}")
ship_from=s3.text_input("Paese di spedizione",value=saved_settings.get("ship_from","IT|Italy"))
@st.cache_data(ttl=3600,show_spinner=False)
def cached_worten_states(key:str,url:str):
    try:return get_offer_states(key,api_url=url),"api"
    except Exception:return [{"code":"11","label":"New"}],"fallback"
offer_states,state_source=cached_worten_states(api_key,api_url)
state_labels={f"{x['label']} · codice {x['code']}":x["code"] for x in offer_states}
saved_state=str(saved_settings.get("state_code","11"))
state_index=next((i for i,x in enumerate(state_labels.values()) if x==saved_state),0)
state_label=s4.selectbox("Stato prodotto",list(state_labels),index=state_index)
state_code=normalize_offer_state(state_labels[state_label],state_label)
if state_source=="fallback":s4.caption("API stati non raggiungibile: uso il codice Mirakl 11 = New.")
s4.caption(f"Valore che sarà scritto nel CSV: {state_code}")
if st.button("Salva regole Worten"):
    execute("""INSERT INTO commercial_rules(seller_id,price_list_id,marketplace,storefront,margin_pct,commission_pct,minimum_margin_pct,minimum_qty,minimum_cost,settings_json,updated_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(seller_id,price_list_id,marketplace,storefront) DO UPDATE SET margin_pct=excluded.margin_pct,
    commission_pct=excluded.commission_pct,minimum_margin_pct=excluded.minimum_margin_pct,minimum_qty=excluded.minimum_qty,
    minimum_cost=excluded.minimum_cost,settings_json=excluded.settings_json,updated_at=excluded.updated_at""",
    (seller_id,view["price_list_id"],"worten","pt",margin,commission,minimum_margin,minimum_qty,minimum_cost,
     json_text({"leadtime":leadtime,"logistic_class":logistic_class,"ship_from":ship_from,"state_code":state_code,
                "minimum_profit":minimum_profit}),now_iso()))
    st.success("Regole Worten salvate.")

try:df=normalize(load_saved_view_frame(view))
except Exception as e:st.error(f"Impossibile leggere la vista salvata: {e}");st.stop()
if "destination_country" in df and not df.empty:
    saved_country=str(df["destination_country"].iloc[0] or "").lower()
    if saved_country and saved_country!="pt":
        st.error(f"Questa vista Cecotec è destinata a {saved_country.upper()}, non al Portogallo. Crea una vista con Paese Portogallo.");st.stop()
national_cost=country_cost(df,"pt")
shipping=(pd.to_numeric(df["total_cost"],errors="coerce").fillna(0)-pd.to_numeric(df["cost"],errors="coerce").fillna(0)).clip(lower=0) if "total_cost" in df else 0
base_cost=national_cost+shipping
df=df[(base_cost>=minimum_cost)&(df["quantity"]>=minimum_qty)].copy(); base_cost=base_cost.loc[df.index]
df["cost"]=base_cost.round(2); df["price"]=(base_cost*(1+margin/100)).round(2); df["minimum_price"]=(base_cost*(1+minimum_margin/100)).round(2)
df["commission_eur"]=(df["price"]*commission/100).round(2); df["profit"]=(df["price"]-df["commission_eur"]-base_cost).round(2)
df=df[df["profit"]>=minimum_profit].copy()
df=attach_product_keys(df)
missing_ean=int(df["ean"].fillna("").astype(str).str.strip().isin(("","nan","none")).sum())
if missing_ean: st.warning(f"{missing_ean} prodotti senza EAN non potranno essere inviati a Worten.")

scope=f"{seller_id}_{account['id']}_{view['id']}"; select_key=f"worten_select_{scope}"; grid_key=f"worten_grid_{scope}_{margin}_{commission}_{minimum_margin}_{minimum_qty}_{minimum_cost}_{minimum_profit}"
sku_mode=st.selectbox("Formato SKU offerta",("SKU originale","NOMEFORNITORE_EAN_PREZZOACQUISTO_PREZZOMINIMO"))
operation="CREA/AGGIORNA"
st.caption("Operazione: CREA/AGGIORNA. Per rimuovere offerte usa la voce separata «Cancellazione dai Marketplace».")
batch_scope={"marketplace":"worten","seller_id":int(seller_id),"account_id":int(account["id"]),
             "view_id":int(view["id"]),"view_updated_at":str(view.get("updated_at","")),"storefronts":["pt"],
             "operation":operation,"sku_mode":sku_mode}
all_batch_records=frame_records(df);batch_state=load_state(batch_scope);batch_summary=progress_summary(batch_state,all_batch_records)
b1,b2,count_col,next_col=st.columns([1,1,1,1])
if b1.button("☑ Seleziona tutti",key=f"worten_all_{scope}",use_container_width=True):
    st.session_state[select_key]=[item["key"] for item in all_batch_records]; st.session_state.pop(grid_key,None); st.rerun()
if b2.button("☐ Deseleziona tutti",key=f"worten_none_{scope}",use_container_width=True):
    st.session_state[select_key]=[]; st.session_state.pop(grid_key,None); st.rerun()
interval_count=count_col.number_input("Prodotti per intervallo",min_value=1,value=min(100,max(1,len(df))),step=1,key=f"worten_batch_count_{scope}")
if next_col.button("Seleziona prossimo intervallo",key=f"worten_next_batch_{scope}",use_container_width=True):
    chosen_keys,_,_=select_next(batch_scope,all_batch_records,int(interval_count))
    st.session_state[select_key]=chosen_keys;st.session_state.pop(grid_key,None);st.rerun()
st.caption(f"Memoria intervalli: inviati {batch_summary['completed']:,} · rimanenti {batch_summary['remaining']:,} · totale {batch_summary['total']:,}.")
if batch_summary.get("active") and batch_summary["active"].get("selected_count"):
    active=batch_summary["active"]
    st.info(f"Intervallo {active['number']} selezionato: {active['selected_count']:,} prodotti · da SKU {active['first_sku']} a SKU {active['last_sku']}.")
if batch_summary.get("history"):
    with st.expander("Storico intervalli inviati"):
        st.dataframe(pd.DataFrame(batch_summary["history"][-20:]).drop(columns=["metadata"],errors="ignore"),use_container_width=True,hide_index=True)
with st.expander("Azzera memoria intervalli"):
    reset_confirm=st.checkbox("Confermo di voler ricominciare dal primo prodotto",key=f"worten_reset_confirm_{scope}")
    if st.button("Azzera memoria",key=f"worten_reset_{scope}",disabled=not reset_confirm):
        reset_state(batch_scope);st.session_state[select_key]=[];st.session_state.pop(grid_key,None);st.rerun()
df=df.drop(columns=["Seleziona","Mantieni"],errors="ignore")
stored_selection=st.session_state.get(select_key,[])
selected_key_defaults=({item["key"] for item in all_batch_records} if stored_selection is True else
                       set(stored_selection) if isinstance(stored_selection,(list,tuple,set)) else set())
df.insert(0,"Seleziona",df["_batch_key"].isin(selected_key_defaults))
cols=["Seleziona","ean","sku","name","cost","quantity","price","minimum_price","commission_eur","profit"]
edited=st.data_editor(df[cols],use_container_width=True,height=520,hide_index=True,key=grid_key,
    column_config={"Seleziona":st.column_config.CheckboxColumn(required=True),"cost":st.column_config.NumberColumn("Costo",format="%.2f"),
    "price":st.column_config.NumberColumn("Prezzo",format="%.2f"),"minimum_price":st.column_config.NumberColumn("Prezzo minimo",format="%.2f"),
    "commission_eur":st.column_config.NumberColumn("Commissione €",format="%.2f"),"profit":st.column_config.NumberColumn("Guadagno €",format="%.2f")},
    disabled=["ean","sku","name","cost","quantity","price","minimum_price","commission_eur","profit"])
selected_indexes=edited.index[edited["Seleziona"]==True]
selected=df.loc[selected_indexes].drop(columns=["Seleziona","Mantieni"],errors="ignore").copy(); st.metric("Prodotti selezionati",len(selected))

def text(value):
    return "" if pd.isna(value) else str(value).strip()
def money(value): return f"{float(value):.2f}"
def offer_sku(item):
    if sku_mode=="SKU originale": value=text(item.get("sku",""))
    else:
        supplier=re.sub(r"[^A-Za-z0-9-]+","-",str(view.get("supplier_name","")).strip()).strip("-") or "FORNITORE"
        ean=text(item.get("ean","")).removesuffix(".0")
        if not ean or ean.lower() in ("nan","none"):return ""
        suffix=f"_{ean}_{money(item.get('cost',0))}_{money(item.get('minimum_price',0))}"
        value=f"{supplier[:max(1,40-len(suffix))]}{suffix}"
    return value.replace("/","-")[:40]

if sku_mode!="SKU originale" and not selected.empty:
    generated=selected.apply(offer_sku,axis=1)
    st.caption(f"Anteprima SKU composto: `{generated.iloc[0]}`")
    duplicate_count=int(generated.duplicated(keep=False).sum())
    if duplicate_count:st.warning(f"Attenzione: {duplicate_count} prodotti hanno lo stesso EAN e generano SKU duplicati.")
def build_csv(frame:pd.DataFrame)->bytes:
    output=io.StringIO(newline=""); writer=csv.DictWriter(output,fieldnames=WORTEN_OFFER_COLUMNS,delimiter=";",lineterminator="\n")
    writer.writeheader()
    for _,item in frame.iterrows():
        ean=text(item.get("ean","")); sku=offer_sku(item)
        if not ean or ean.lower() in ("nan","none"): continue
        record={column:"" for column in WORTEN_OFFER_COLUMNS}
        record.update({"sku":sku,"product-id":ean,"product-id-type":"EAN","description":text(item.get("name","")),
            "internal-description":text(item.get("name","")),"price":money(item.get("price",0)),"quantity":str(max(0,int(item.get("quantity",0)))),
            # CSV Mirakl requires the technical condition code. Never write the Excel label "New".
            "state":normalize_offer_state(state_code,state_label),"leadtime-to-ship":str(int(leadtime)),"logistic-class":logistic_class,
            "update-delete":"update","price[channel=WRT_PT_ONLINE]":money(item.get("price",0)),
            "description-pt":text(item.get("name","")),"ship-from-country-offer":ship_from})
        writer.writerow(record)
    return output.getvalue().encode("utf-8-sig")

csv_bytes=build_csv(selected)
if not selected.empty:
    with st.expander("Controlla il CSV che sarà inviato"):
        preview_lines=csv_bytes.decode("utf-8-sig",errors="replace").splitlines()[:3]
        st.code("\n".join(preview_lines),language="text")
        st.caption("Nella decima colonna `state` deve comparire il valore tecnico `11`, mai `New`.")
st.download_button("Scarica CSV ufficiale Worten",csv_bytes,file_name=f"worten_pt_{view['id']}.csv",mime="text/csv",disabled=selected.empty)
confirm=st.checkbox("Confermo l'invio delle offerte selezionate a Worten Portogallo")
if st.button("Invia selezionate a Worten",type="primary"):
    valid_count=len(selected)-int(selected["ean"].fillna("").astype(str).str.strip().isin(("","nan","none")).sum()) if not selected.empty else 0
    if selected.empty: st.warning("Seleziona almeno un prodotto.")
    elif not confirm: st.warning("Conferma esplicitamente l'invio.")
    elif valid_count==0: st.error("Nessun prodotto selezionato possiede un EAN valido.")
    else:
        try:
            result=upload_offer_csv(api_key,csv_bytes,api_url=api_url,shop_id=shop_id,import_mode="NORMAL")
            import_id=result.get("import_id") or result.get("importId") or result.get("id")
            selected_records=frame_records(selected)
            valid_mask=~selected["ean"].fillna("").astype(str).str.strip().str.lower().isin(("","nan","none"))
            successful_keys=set(selected.loc[valid_mask,"_batch_key"].astype(str))
            failed_keys=set(selected["_batch_key"].astype(str))-successful_keys
            interval_entry=record_result(batch_scope,selected_records,successful_keys,failed_keys,"submitted",
                                         {"import_id":import_id,"marketplace":"worten","storefront":"pt"})
            operation_rows=[{
                "ok":True,"sku_inviato":offer_sku(item),"sku_originale":text(item.get("sku","")),
                "ean":text(item.get("ean","")),"name":text(item.get("name","")),"status":"submitted",
            } for _,item in selected.loc[valid_mask].iterrows()]
            execute("""INSERT INTO operations(seller_id,marketplace_account_id,price_list_id,marketplace,storefront,operation_type,status,total_rows,success_rows,failed_rows,details_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(seller_id,account["id"],view["price_list_id"],"worten","pt",operation,"submitted",valid_count,0,0,
            json_text({"saved_view_id":view["id"],"import_id":import_id,"response":result,
                       "interval":interval_entry,"rows":operation_rows}),now_iso()))
            st.success(f"Intervallo {interval_entry['number']} inviato a Worten: {valid_count} prodotti, da SKU {interval_entry['first_sku']} a SKU {interval_entry['last_sku']}. ID import: {import_id or 'restituito nella risposta'}")
            with st.expander("Risposta API"): st.json(result)
        except Exception as error:
            if not selected.empty:
                selected_records=frame_records(selected);all_failed={item["key"] for item in selected_records}
                record_result(batch_scope,selected_records,set(),all_failed,"failed",{"error":str(error)[:500]})
            st.error(f"Invio Worten non riuscito: {error}")
