from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pandas as pd
import streamlit as st

from marketplace_core.catalogs import CatalogCore
from marketplace_core.jobs import JobsCore

from services.db import (DATA_DIR, accessible_lists, delete_saved_view, execute,
                         json_text, now_iso, rows)
from services.lists import destination_country_codes, materialize_price_list, normalize
from services.object_storage import storage_status
from services.saved_view_storage import (load_saved_view_frame, migrate_saved_views_to_storage,
                                         save_saved_view_frame)
from services.session import bootstrap, seller_selector

bootstrap();st.title("Lavora sui listini")
st.caption("Crea e salva viste prodotti destinate a uno o più marketplace abilitati.")
seller_id=seller_selector()
if seller_id is None:st.stop()

lists=accessible_lists(seller_id)
accounts=rows("SELECT * FROM marketplace_accounts WHERE seller_id=? AND active=1 ORDER BY marketplace,account_name",(seller_id,))
if not lists:st.error("Nessun listino disponibile per questo Seller.");st.stop()
if not accounts:st.error("Abilita prima almeno un account marketplace in Gestione Seller.");st.stop()

lmap={f"{x['supplier_name']} · {x['name']} · ID {x['id']}":x for x in lists}
pl=dict(lmap[st.selectbox("Listino da lavorare",list(lmap))])
if not pl.get("local_path") or not Path(str(pl.get("local_path") or "")).exists():
    try:
        recovered=materialize_price_list(int(pl["id"]),pl.get("local_path"))
        if recovered:pl["local_path"]=str(recovered)
    except Exception as storage_error:
        st.error(f"Il listino non è disponibile nello storage: {storage_error}");st.stop()
if not pl.get("local_path") or not Path(str(pl.get("local_path") or "")).exists():st.error("Il listino non è stato ancora scaricato o archiviato.");st.stop()

catalog_core=CatalogCore();jobs_core=JobsCore()
try:catalog_status=catalog_core.status(pl["id"],pl["local_path"])
except Exception as e:st.error(f"Impossibile verificare il catalogo: {e}");st.stop()

if not catalog_status.ready:
    st.info(
        "Per usare la tabella server-side v311 questo listino deve essere indicizzato con "
        "il nuovo schema. L'operazione avviene in background e viene eseguita solo quando "
        "il file sorgente cambia."
    )
    job_key=f"catalog_materialize_v311_{pl['id']}"
    if st.button("Prepara / aggiorna catalogo veloce",type="primary",key=f"materialize_v311_{pl['id']}"):
        receipt=jobs_core.submit(catalog_core.build_materialize_job(seller_id,pl["id"]))
        jobs_core.start_local(receipt.job_id)
        st.session_state[job_key]=receipt.job_id
        st.rerun()
    job_id=st.session_state.get(job_key)
    if job_id:
        snap=jobs_core.snapshot(job_id)
        if snap:
            st.progress(min(1.0,max(0.0,snap.progress_pct/100.0)),text=snap.message or snap.status)
            if snap.status=="done":
                st.success("Catalogo pronto. Premi Aggiorna pagina.")
            elif snap.status=="error":st.error(snap.error or "Errore indicizzazione catalogo")
            elif st.button("Aggiorna stato",key=f"refresh_v311_{pl['id']}"):st.rerun()
    st.stop()

st.caption(
    f"Catalogo server-side v311: {catalog_status.row_count:,} prodotti indicizzati. "
    "Filtri e paginazione vengono eseguiti direttamente sul database: la pagina non carica più "
    "l'intero listino in memoria."
)

supplier_token=str(pl.get("supplier_name","")).strip().lower().replace(" ","")
is_activeshop="activeshop" in supplier_token
is_cecotec="cecotec" in supplier_token or "ecotech" in supplier_token

destination_country="";destination_countries=[]
if is_cecotec:
    country_options=[
        ("🇵🇹 Portogallo","pt"),("🇮🇹 Italia","it"),("🇩🇪 Germania","de"),
        ("🇫🇷 Francia","fr"),("🇦🇹 Austria","at"),("🇵🇱 Polonia","pl"),
        ("🇨🇿 Repubblica Ceca","cz"),("🇸🇰 Slovacchia","sk"),("🇳🇱 Paesi Bassi","nl"),
    ]
    country_key_prefix=f"cecotec_destination_{pl['id']}"
    country_keys={code:f"{country_key_prefix}_{code}" for _,code in country_options}
    if not any(key in st.session_state for key in country_keys.values()):
        st.session_state[country_keys["pt"]]=True
    st.markdown("**Paesi di destinazione Cecotec**")
    st.caption("Il costo della tabella viene risolto lato server per il primo Paese selezionato; la vista conserva tutti i Paesi scelti.")
    a,b=st.columns(2)
    if a.button("☑ Seleziona tutti i Paesi",key=f"{country_key_prefix}_all",use_container_width=True):
        for key in country_keys.values():st.session_state[key]=True
        st.rerun()
    if b.button("☐ Deseleziona tutti i Paesi",key=f"{country_key_prefix}_none",use_container_width=True):
        for key in country_keys.values():st.session_state[key]=False
        st.rerun()
    cols=st.columns(5)
    for pos,(label,code) in enumerate(country_options):
        cols[pos%5].checkbox(f"{label} ({code.upper()})",key=country_keys[code])
    destination_countries=[code for _,code in country_options if st.session_state.get(country_keys[code],False)]
    if not destination_countries:st.warning("Seleziona almeno un Paese di destinazione Cecotec.");st.stop()
    destination_country=destination_countries[0]

st.subheader("1. Filtra e prepara la vista")
f1,f2,f3,f4=st.columns(4)
search=f1.text_input("Cerca",placeholder="EAN, SKU, nome…")
min_qty=f2.number_input("Quantità minima",0,999999,0)
min_cost=f3.number_input("Costo minimo",0.0,999999.0,0.0,1.0)
max_cost=f4.number_input("Costo massimo (0 = nessun limite)",0.0,999999.0,0.0,1.0)

known_weight,unknown_weight=catalog_core.weight_stats(pl["id"])
weight_available=known_weight>0
weight_modes={
    "Nessuna esclusione":"none","Escludi peso superiore a":"above",
    "Escludi peso inferiore a":"below","Escludi peso compreso tra Da e A":"between",
}
st.markdown("**Esclusione per peso**")
w1,w2,w3=st.columns([2,1,1])
weight_label=w1.selectbox("Modalità filtro peso (kg)",list(weight_modes),disabled=not weight_available)
weight_mode=weight_modes[weight_label] if weight_available else "none"
weight_from=0.0;weight_to=0.0
if weight_mode in ("above","below"):
    weight_from=w2.number_input("Peso di riferimento (kg)",0.0,999999.0,0.0,0.1,format="%.3f")
elif weight_mode=="between":
    weight_from=w2.number_input("Da kg",0.0,999999.0,0.0,0.1,format="%.3f")
    weight_to=w3.number_input("A kg",0.0,999999.0,0.0,0.1,format="%.3f")
if weight_available:
    st.caption(f"Peso disponibile per {known_weight:,} prodotti; {unknown_weight:,} senza peso restano inclusi.")
else:
    st.caption("Nessun peso utilizzabile nel catalogo indicizzato.")

c1,c2,c3=st.columns(3)
shipping=c1.number_input("Spedizione aggiuntiva",0.0,9999.0,0.0,0.10)
margin=c2.number_input("Ricarico iniziale %",0.0,500.0,35.0,1.0)
minimum_margin=c3.number_input("Ricarico prezzo minimo %",0.0,500.0,10.0,1.0)

query_filters={
    "search":search,"min_qty":float(min_qty),"min_cost":float(min_cost),"max_cost":float(max_cost),
    "weight_mode":weight_mode,"weight_from":float(weight_from),"weight_to":float(weight_to),
    "destination_country":destination_country,"positive_cost_only":is_activeshop,
}
filter_signature=hashlib.sha1(json.dumps(query_filters,sort_keys=True).encode()).hexdigest()[:12]
state_prefix=f"catalog_select_v311_{pl['id']}"
if st.session_state.get(f"{state_prefix}_sig")!=filter_signature:
    st.session_state[f"{state_prefix}_sig"]=filter_signature
    st.session_state[f"{state_prefix}_included"]=[]
    st.session_state[f"{state_prefix}_excluded"]=[]
    st.session_state[f"{state_prefix}_overrides"]={}

page_size=int(st.selectbox("Prodotti per pagina",[100,250,500],index=1,key=f"catalog_page_size_{pl['id']}"))
try:
    first_page=catalog_core.query(pl["id"],offset=0,limit=page_size,**query_filters)
except ValueError as e:st.error(str(e));st.stop()
total=first_page.total
page_count=max(1,math.ceil(total/page_size))
page_number=int(st.number_input("Pagina",min_value=1,max_value=page_count,value=1,step=1,key=f"catalog_page_{pl['id']}_{filter_signature}"))
page=first_page if page_number==1 else catalog_core.query(pl["id"],offset=(page_number-1)*page_size,limit=page_size,**query_filters)

select_key=f"{state_prefix}_all"
if select_key not in st.session_state:st.session_state[select_key]=True
select_all=st.checkbox("Seleziona tutti i prodotti filtrati",key=select_key)
mode_key=f"{state_prefix}_mode"
if st.session_state.get(mode_key) is None:st.session_state[mode_key]=select_all
if bool(st.session_state.get(mode_key))!=bool(select_all):
    st.session_state[mode_key]=select_all
    st.session_state[f"{state_prefix}_included"]=[]
    st.session_state[f"{state_prefix}_excluded"]=[]
    st.session_state[f"{state_prefix}_overrides"]={}

included={int(x) for x in st.session_state.get(f"{state_prefix}_included",[])}
excluded={int(x) for x in st.session_state.get(f"{state_prefix}_excluded",[])}
overrides=dict(st.session_state.get(f"{state_prefix}_overrides",{}))

frame=pd.DataFrame.from_records(page.rows)
if frame.empty:
    st.info("Nessun prodotto corrisponde ai filtri.")
    selected_count=0
else:
    for col,default in (("ean",""),("sku",""),("name",""),("weight_kg",0.0),("cost",0.0),("shipping_cost",0.0),("quantity",0.0)):
        if col not in frame:frame[col]=default
    frame["_feed_shipping"]=pd.to_numeric(frame["shipping_cost"],errors="coerce").fillna(0)
    frame["shipping_cost"]=(frame["_feed_shipping"]+float(shipping)).round(2)
    frame["cost"]=pd.to_numeric(frame["cost"],errors="coerce").fillna(0).round(2)
    frame["total_cost"]=(frame["cost"]+frame["shipping_cost"]).round(2)
    frame["price"]=(frame["total_cost"]*(1+float(margin)/100)).round(2)
    frame["minimum_price"]=(frame["total_cost"]*(1+float(minimum_margin)/100)).round(2)
    for idx,record in frame.iterrows():
        row_no=int(record.get("_row_no") or 0); saved=overrides.get(str(row_no)) or {}
        for col in ("cost","shipping_cost","total_cost","quantity","price","minimum_price"):
            if col in saved:frame.at[idx,col]=saved[col]
    row_nos=[int(v) for v in frame["_row_no"].tolist()]
    frame.insert(0,"Seleziona",[(rn not in excluded) if select_all else (rn in included) for rn in row_nos])
    visible=["Seleziona","ean","sku","name","weight_kg","cost","shipping_cost","total_cost","quantity","price","minimum_price"]
    edited=st.data_editor(
        frame[visible],use_container_width=True,height=520,hide_index=True,key=f"catalog_editor_{pl['id']}_{filter_signature}_{page_number}",
        column_config={"Seleziona":st.column_config.CheckboxColumn(),"weight_kg":st.column_config.NumberColumn("Peso (kg)",format="%.3f"),
                       "cost":st.column_config.NumberColumn("Costo",format="%.2f"),"shipping_cost":st.column_config.NumberColumn("Spedizione",format="%.2f"),
                       "total_cost":st.column_config.NumberColumn("Costo totale",format="%.2f"),"price":st.column_config.NumberColumn("Prezzo",format="%.2f"),
                       "minimum_price":st.column_config.NumberColumn("Prezzo minimo",format="%.2f")},
        disabled=["ean","sku","name","weight_kg"],
    )
    for pos,row_no in enumerate(row_nos):
        checked=bool(edited.iloc[pos]["Seleziona"])
        if select_all:
            if checked:excluded.discard(row_no)
            else:excluded.add(row_no)
        else:
            if checked:included.add(row_no)
            else:included.discard(row_no)
        overrides[str(row_no)]={
            col:float(edited.iloc[pos][col] or 0) for col in ("cost","shipping_cost","total_cost","quantity","price","minimum_price")
        }
    st.session_state[f"{state_prefix}_included"]=sorted(included)
    st.session_state[f"{state_prefix}_excluded"]=sorted(excluded)
    st.session_state[f"{state_prefix}_overrides"]=overrides
    selected_count=max(0,total-len(excluded)) if select_all else len(included)
    st.caption(f"Prodotti filtrati: {total:,} · pagina {page_number}/{page_count} · letti ora: {len(frame):,}")

st.metric("Prodotti che saranno salvati",selected_count)
if selected_count>50_000:
    st.info("La tabella resta leggera; soltanto quando premi Salva verranno materializzati i prodotti selezionati per creare lo snapshot della vista.")

st.subheader("2. Scegli i marketplace di destinazione")
st.caption("Sono mostrati soltanto gli account abilitati per il Seller selezionato.")
chosen_accounts=[]
cols=st.columns(min(4,len(accounts)))
for i,a in enumerate(accounts):
    label=f"{a['marketplace'].title()} — {a['account_name']}"
    if cols[i%len(cols)].checkbox(label,key=f"target_{a['id']}"):chosen_accounts.append(a["id"])

st.subheader("3. Salva la vista")
view_name=st.text_input("Nome della vista",placeholder="Es. 3MK accessori disponibili luglio")
existing=rows("SELECT * FROM saved_views WHERE seller_id=? ORDER BY updated_at DESC",(seller_id,))
overwrite=False
if existing:
    overwrite=st.checkbox("Sovrascrivi una vista esistente")
    existing_map={f"{x['name']} · {x['row_count']} prodotti":x for x in existing}
    existing_choice=st.selectbox("Vista da sovrascrivere",list(existing_map),disabled=not overwrite)

if st.button("Salva vista e destinazioni",type="primary"):
    if selected_count<=0:st.error("La vista non contiene prodotti selezionati.")
    elif not chosen_accounts:st.error("Seleziona almeno un marketplace di destinazione.")
    elif not overwrite and not view_name.strip():st.error("Inserisci il nome della vista.")
    else:
        try:
            with st.spinner(f"Preparazione snapshot di {selected_count:,} prodotti…"):
                selected=catalog_core.export_filtered_frame(
                    pl["id"],excluded_row_nos=excluded if select_all else (),
                    selected_row_nos=None if select_all else included,**query_filters,
                )
                if selected.empty:raise ValueError("La selezione non contiene prodotti.")
                selected["_feed_shipping"]=pd.to_numeric(selected.get("shipping_cost",0),errors="coerce").fillna(0)
                selected["shipping_cost"]=(selected["_feed_shipping"]+float(shipping)).round(2)
                selected["cost"]=pd.to_numeric(selected.get("cost",0),errors="coerce").fillna(0).round(2)
                selected["quantity"]=pd.to_numeric(selected.get("quantity",0),errors="coerce").fillna(0)
                selected["total_cost"]=(selected["cost"]+selected["shipping_cost"]).round(2)
                selected["price"]=(selected["total_cost"]*(1+float(margin)/100)).round(2)
                selected["minimum_price"]=(selected["total_cost"]*(1+float(minimum_margin)/100)).round(2)
                for idx,record in selected.iterrows():
                    rn=int(record.get("_row_no") or 0); saved=overrides.get(str(rn)) or {}
                    for col,value in saved.items():
                        if col in selected.columns:selected.at[idx,col]=value
                if destination_countries:
                    selected["destination_countries"]=",".join(destination_countries)
                    selected["destination_country"]=destination_country if len(destination_countries)==1 else ""
                selected=selected.drop(columns=["_row_no","_feed_shipping"],errors="ignore")
                if overwrite:
                    target=existing_map[existing_choice];vid=target["id"];name=target["name"]
                else:
                    name=view_name.strip();vid=execute("""INSERT INTO saved_views
                    (seller_id,price_list_id,name,snapshot_path,filters_json,row_count,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)""",(seller_id,pl["id"],name,"pending",json_text({}),len(selected),now_iso(),now_iso()))
                filters={"search":search,"min_qty":min_qty,"min_cost":min_cost,"max_cost":max_cost,
                         "shipping":shipping,"margin":margin,"minimum_margin":minimum_margin,
                         "weight_exclusion_mode":weight_mode,"weight_from":weight_from,"weight_to":weight_to,
                         "destination_countries":destination_countries}
                execute("UPDATE saved_views SET price_list_id=?,filters_json=?,row_count=?,updated_at=? WHERE id=?",
                        (pl["id"],json_text(filters),len(selected),now_iso(),vid))
                saved_artifact=save_saved_view_frame(
                    view_id=vid,seller_id=seller_id,name=name,frame=selected,
                )
                execute("DELETE FROM saved_view_marketplaces WHERE saved_view_id=?",(vid,))
                for aid in chosen_accounts:execute("INSERT INTO saved_view_marketplaces(saved_view_id,marketplace_account_id) VALUES(?,?)",(vid,aid))
            st.success(f"Vista '{name}' salvata con {len(selected)} prodotti e {len(chosen_accounts)} destinazioni.");st.rerun()
        except Exception as e:st.error(f"Errore salvataggio: {e}")

st.divider();st.subheader("Viste salvate")
saved=rows("""SELECT sv.*,pl.name price_list_name,GROUP_CONCAT(ma.marketplace || ' — ' || ma.account_name, ', ') destinations
FROM saved_views sv JOIN price_lists pl ON pl.id=sv.price_list_id
LEFT JOIN saved_view_marketplaces svm ON svm.saved_view_id=sv.id
LEFT JOIN marketplace_accounts ma ON ma.id=svm.marketplace_account_id
WHERE sv.seller_id=? GROUP BY sv.id ORDER BY sv.updated_at DESC""",(seller_id,))
if saved:
    storage=storage_status()
    backend_label=str(storage.get("backend") or "local").upper()
    st.caption(
        f"Storage snapshot v312: {backend_label}. Le viste nuove vengono salvate tramite il layer object storage; "
        "la copia locale resta solo come cache/compatibilità durante la migrazione."
    )
    missing_remote=sum(1 for item in saved if not str(item.get("snapshot_storage_key") or "").strip())
    if missing_remote:
        m1,m2=st.columns([2,1])
        m1.info(f"{missing_remote} viste legacy non hanno ancora una copia object storage.")
        if m2.button("Migra viste legacy",key=f"migrate_saved_views_storage_{seller_id}",use_container_width=True):
            try:
                result=migrate_saved_views_to_storage(seller_id)
                if result["failed"]:
                    st.warning(f"Migrate {result['migrated']} viste; {len(result['failed'])} non disponibili localmente.")
                else:
                    st.success(f"Migrazione completata: {result['migrated']} viste copiate nello storage.")
                st.rerun()
            except Exception as e:st.error(f"Migrazione storage non riuscita: {e}")
    st.dataframe([{"ID":x["id"],"Vista":x["name"],"Listino":x["price_list_name"],"Prodotti":x["row_count"],"Marketplace":x["destinations"] or "—","Aggiornata":x["updated_at"]} for x in saved],use_container_width=True,hide_index=True)

    st.subheader("Apri e modifica una vista salvata")
    saved_map={f"{x['name']} · {x['row_count']} prodotti · ID {x['id']}":x for x in saved}
    saved_label=st.selectbox("Vista da lavorare",list(saved_map),key="saved_view_to_edit")
    saved_view=saved_map[saved_label]
    try:
        view_df=normalize(load_saved_view_frame(saved_view))
        saved_destinations=destination_country_codes(view_df)
        if saved_destinations:
            destination_names={"pt":"Portogallo","it":"Italia","de":"Germania","fr":"Francia","at":"Austria",
                               "pl":"Polonia","cz":"Repubblica Ceca","sk":"Slovacchia","nl":"Paesi Bassi"}
            saved_labels=[
                destination_names.get(code,code.upper()) for code in saved_destinations
            ]
            st.info(f"Paesi di destinazione salvati: {', '.join(saved_labels)}")
        view_df.insert(0,"Mantieni",True)
        edit_columns=["Mantieni","ean","sku","name","weight_kg","cost","shipping_cost","total_cost","quantity","price","minimum_price"]
        for column in edit_columns:
            if column not in view_df.columns:
                view_df[column]=True if column=="Mantieni" else ("" if column in ("ean","sku","name") else 0.0)
        st.caption("Modifica i valori, aggiungi nuove righe oppure togli la spunta Mantieni per rimuovere un prodotto dalla vista.")
        managed=st.data_editor(
            view_df[edit_columns],use_container_width=True,height=520,hide_index=True,
            num_rows="dynamic",key=f"saved_editor_{saved_view['id']}",
            column_config={
                "Mantieni":st.column_config.CheckboxColumn(default=True),
                "weight_kg":st.column_config.NumberColumn("Peso (kg)",format="%.3f",min_value=0.0),
                "cost":st.column_config.NumberColumn("Costo",format="%.2f"),
                "shipping_cost":st.column_config.NumberColumn("Spedizione",format="%.2f"),
                "total_cost":st.column_config.NumberColumn("Costo totale",format="%.2f"),
                "quantity":st.column_config.NumberColumn("Quantità",min_value=0,step=1),
                "price":st.column_config.NumberColumn("Prezzo",format="%.2f"),
                "minimum_price":st.column_config.NumberColumn("Prezzo minimo",format="%.2f"),
            },
        )
        managed=managed[managed["Mantieni"].fillna(True)].drop(columns=["Mantieni"])
        managed=managed[
            managed["ean"].fillna("").astype(str).str.strip().ne("") |
            managed["sku"].fillna("").astype(str).str.strip().ne("")
        ].copy()

        new_name=st.text_input("Nome vista",value=saved_view["name"],key=f"saved_name_{saved_view['id']}")
        current_accounts={x["marketplace_account_id"] for x in rows(
            "SELECT marketplace_account_id FROM saved_view_marketplaces WHERE saved_view_id=?",
            (saved_view["id"],),
        )}
        st.caption("Destinazioni marketplace della vista")
        managed_accounts=[]
        account_cols=st.columns(min(4,len(accounts)))
        for i,account in enumerate(accounts):
            checked=account_cols[i%len(account_cols)].checkbox(
                f"{account['marketplace'].title()} — {account['account_name']}",
                value=account["id"] in current_accounts,
                key=f"saved_target_{saved_view['id']}_{account['id']}",
            )
            if checked: managed_accounts.append(account["id"])

        b1,b2=st.columns([1,1])
        if b1.button("Aggiorna vista salvata",type="primary",key=f"update_saved_{saved_view['id']}"):
            if not new_name.strip(): st.error("Inserisci il nome della vista.")
            elif managed.empty: st.error("La vista deve contenere almeno un prodotto.")
            elif not managed_accounts: st.error("Seleziona almeno un marketplace di destinazione.")
            else:
                try:
                    # normalize validates and standardizes manually added rows too.
                    managed=normalize(managed).drop(columns=["Seleziona","Mantieni"],errors="ignore")
                    if saved_destinations:
                        managed["destination_countries"]=",".join(saved_destinations)
                        managed["destination_country"]=(
                            saved_destinations[0] if len(saved_destinations)==1 else ""
                        )
                    execute("UPDATE saved_views SET name=?,row_count=?,updated_at=? WHERE id=? AND seller_id=?",
                            (new_name.strip(),len(managed),now_iso(),saved_view["id"],seller_id))
                    save_saved_view_frame(
                        view_id=saved_view["id"],seller_id=seller_id,
                        name=new_name.strip(),frame=managed,
                    )
                    execute("DELETE FROM saved_view_marketplaces WHERE saved_view_id=?",(saved_view["id"],))
                    for account_id in managed_accounts:
                        execute("INSERT INTO saved_view_marketplaces(saved_view_id,marketplace_account_id) VALUES(?,?)",
                                (saved_view["id"],account_id))
                    st.success(f"Vista aggiornata: {len(managed)} prodotti e {len(managed_accounts)} destinazioni.")
                    st.rerun()
                except Exception as e: st.error(f"Impossibile aggiornare la vista: {e}")

        with b2.popover("Elimina vista"):
            st.warning("Questa operazione elimina la vista salvata, ma non il listino originale.")
            confirm_view=st.text_input("Digita ELIMINA",key=f"confirm_delete_view_{saved_view['id']}")
            if st.button("Elimina definitivamente",key=f"delete_view_{saved_view['id']}"):
                if confirm_view!="ELIMINA": st.error("Conferma non valida.")
                elif delete_saved_view(saved_view["id"],seller_id):
                    st.success("Vista eliminata."); st.rerun()
                else: st.error("Vista non trovata.")
    except Exception as e:
        st.error(f"Impossibile aprire la vista: {e}")
else:st.info("Nessuna vista salvata.")
