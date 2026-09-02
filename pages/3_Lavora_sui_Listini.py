from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from marketplace_core.catalogs import CatalogCore

from services.db import (DATA_DIR, accessible_lists, delete_saved_view, execute,
                         json_text, now_iso, row, rows)
from services.lists import (apply_weight_exclusion,country_cost,
                            destination_country_codes,normalize,read_list,
                            safe_name)
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
pl=lmap[st.selectbox("Listino da lavorare",list(lmap))]
if not pl["local_path"] or not Path(pl["local_path"]).exists():st.error("Il listino non è stato ancora scaricato.");st.stop()

try:
    catalog_core=CatalogCore()
    catalog_status=catalog_core.status(pl["id"],pl["local_path"])
    if catalog_status.ready:
        base=catalog_core.load_working_frame(pl["id"],catalog_status.source_fingerprint)
        st.caption(
            f"Catalogo indicizzato v310: {catalog_status.row_count:,} prodotti. "
            "La normalizzazione non viene ripetuta a ogni filtro/rerun della pagina."
        )
    else:
        base=normalize(read_list(pl["local_path"]))
        st.info(
            "Questo listino non è ancora indicizzato. In Fornitori e Listini usa "
            "«Prepara catalogo veloce in background» per eliminare i parsing ripetuti."
        )
except Exception as e:st.error(f"Impossibile leggere il listino: {e}");st.stop()

destination_country=""
destination_countries=[]
supplier_token=str(pl.get("supplier_name","")).strip().lower().replace(" ","")
if "activeshop" in supplier_token:
    diamond_valid=pd.to_numeric(base["cost"],errors="coerce").fillna(0).gt(0)
    missing_diamond=int((~diamond_valid).sum())
    if missing_diamond:
        st.warning(f"{missing_diamond} prodotti senza prezzo Diamond valido sono esclusi dalla lavorazione.")
    base=base[diamond_valid].copy()
    st.info("ActiveShop: costo prodotto = prezzo Diamond API; spedizione = regola pack_type; costo totale = somma dei due valori.")
if "cecotec" in supplier_token or "ecotech" in supplier_token:
    country_options=[
        ("🇵🇹 Portogallo","pt"),("🇮🇹 Italia","it"),("🇩🇪 Germania","de"),
        ("🇫🇷 Francia","fr"),("🇦🇹 Austria","at"),("🇵🇱 Polonia","pl"),
        ("🇨🇿 Repubblica Ceca","cz"),("🇸🇰 Slovacchia","sk"),
        ("🇳🇱 Paesi Bassi","nl"),
    ]
    country_key_prefix=f"cecotec_destination_{pl['id']}"
    country_keys={code:f"{country_key_prefix}_{code}" for _,code in country_options}
    if not any(key in st.session_state for key in country_keys.values()):
        st.session_state[country_keys["pt"]]=True

    st.markdown("**Paesi di destinazione Cecotec**")
    st.caption(
        "Seleziona uno o più Paesi. La scelta viene salvata nella vista e sarà riconosciuta "
        "automaticamente durante la pubblicazione sui marketplace."
    )
    select_countries_col,deselect_countries_col=st.columns(2)
    if select_countries_col.button(
        "☑ Seleziona tutti i Paesi",key=f"{country_key_prefix}_select_all",
        use_container_width=True,
    ):
        for key in country_keys.values():st.session_state[key]=True
        st.rerun()
    if deselect_countries_col.button(
        "☐ Deseleziona tutti i Paesi",key=f"{country_key_prefix}_deselect_all",
        use_container_width=True,
    ):
        for key in country_keys.values():st.session_state[key]=False
        st.rerun()
    country_columns=st.columns(5)
    for position,(label,code) in enumerate(country_options):
        country_columns[position%5].checkbox(
            f"{label} ({code.upper()})",key=country_keys[code],
        )
    destination_countries=[
        code for _,code in country_options if st.session_state.get(country_keys[code],False)
    ]
    if not destination_countries:
        st.warning("Seleziona almeno un Paese di destinazione Cecotec.");st.stop()
    destination_country=destination_countries[0]
    selected_country_label=next(label for label,code in country_options if code==destination_country)
    base["cost"]=country_cost(base,destination_country).round(2)
    base["destination_countries"]=",".join(destination_countries)
    base["destination_country"]=destination_country if len(destination_countries)==1 else ""
    selected_labels=[
        label for label,code in country_options if code in destination_countries
    ]
    st.info(
        f"Paesi Cecotec abilitati: {', '.join(selected_labels)}. "
        f"La tabella usa {selected_country_label} come riferimento; in pubblicazione "
        "i costi saranno ricalcolati automaticamente per ogni Paese."
    )

st.subheader("1. Filtra e prepara la vista")
f1,f2,f3,f4=st.columns(4)
search=f1.text_input("Cerca",placeholder="EAN, SKU, nome…")
min_qty=f2.number_input("Quantità minima",0,999999,0)
min_cost=f3.number_input("Costo minimo",0.0,999999.0,0.0,1.0)
max_cost=f4.number_input("Costo massimo (0 = nessun limite)",0.0,999999.0,0.0,1.0)

weight_modes={
    "Nessuna esclusione":"none",
    "Escludi peso superiore a":"above",
    "Escludi peso inferiore a":"below",
    "Escludi peso compreso tra Da e A":"between",
}
weight_values=pd.to_numeric(base.get("weight_kg",pd.Series(0.0,index=base.index)),errors="coerce").fillna(0)
weight_available=weight_values.gt(0).any()
st.markdown("**Esclusione per peso**")
w1,w2,w3=st.columns([2,1,1])
weight_label=w1.selectbox(
    "Modalità filtro peso (kg)",list(weight_modes),
    disabled=not weight_available,
    help="Il peso è sempre espresso in chilogrammi. I prodotti senza peso restano inclusi.",
)
weight_mode=weight_modes[weight_label] if weight_available else "none"
weight_from=0.0;weight_to=0.0
if weight_mode in ("above","below"):
    weight_from=w2.number_input("Peso di riferimento (kg)",0.0,999999.0,0.0,0.1,format="%.3f")
elif weight_mode=="between":
    weight_from=w2.number_input("Da kg",0.0,999999.0,0.0,0.1,format="%.3f")
    weight_to=w3.number_input("A kg",0.0,999999.0,0.0,0.1,format="%.3f")
if weight_available:
    st.caption(
        f"Peso disponibile per {int(weight_values.gt(0).sum()):,} prodotti; "
        f"{int(weight_values.le(0).sum()):,} prodotti senza peso resteranno inclusi."
    )
else:
    if "abonline" in supplier_token or supplier_token in {"ab.pl","abpl","ab"}:
        weight_hint=(
            "Per AB Online apri Fornitori e Listini e usa "
            "«Aggiorna catalogo completo (include peso)»."
        )
    elif "hurtel" in supplier_token:
        weight_hint="Per Hurtel aggiorna nuovamente il feed con la versione corrente."
    else:
        weight_hint="Aggiorna il listino verificando che il feed sorgente contenga il peso."
    st.warning(
        "Questo listino non contiene ancora pesi utilizzabili. La colonna Peso (kg) "
        f"sarà comunque mostrata. {weight_hint}"
    )

df=base.copy()
if search:
    patt=re.escape(search)
    mask=df["ean"].str.contains(patt,case=False,na=False)|df["sku"].str.contains(patt,case=False,na=False)|df["name"].str.contains(patt,case=False,na=False)
    df=df[mask]
df=df[(df["quantity"]>=min_qty)&(df["cost"]>=min_cost)]
if max_cost>0:df=df[df["cost"]<=max_cost]
before_weight_filter=len(df)
try:
    df=apply_weight_exclusion(df,weight_mode,weight_from,weight_to)
except ValueError as error:
    st.error(str(error));st.stop()
if weight_mode!="none":
    st.info(f"Prodotti esclusi dal filtro peso: {before_weight_filter-len(df):,}.")

c1,c2,c3=st.columns(3)
shipping=c1.number_input("Spedizione aggiuntiva",0.0,9999.0,0.0,0.10,
                         help="Si aggiunge all'eventuale spedizione già presente nel feed. ActiveShop applica automaticamente 10/100/200 € secondo pack_type.")
margin=c2.number_input("Ricarico iniziale %",0.0,500.0,35.0,1.0)
minimum_margin=c3.number_input("Ricarico prezzo minimo %",0.0,500.0,10.0,1.0)
df=df.copy()
feed_shipping=(pd.to_numeric(df["shipping_cost"],errors="coerce").fillna(0)
               if "shipping_cost" in df else pd.Series(0.0,index=df.index))
df["shipping_cost"]=(feed_shipping+float(shipping)).round(2)
df["total_cost"]=(df["cost"]+df["shipping_cost"]).round(2)
df["price"]=(df["total_cost"]*(1+margin/100)).round(2);df["minimum_price"]=(df["total_cost"]*(1+minimum_margin/100)).round(2)

select_all=st.checkbox("Seleziona tutti i prodotti filtrati",value=True)
df.insert(0,"Seleziona",select_all)
visible=["Seleziona","ean","sku","name","weight_kg","cost","shipping_cost","total_cost","quantity","price","minimum_price"]
st.caption(f"Prodotti nella vista: {len(df):,}")
edited=st.data_editor(df[visible],use_container_width=True,height=520,hide_index=True,
    column_config={"Seleziona":st.column_config.CheckboxColumn(),
                   "weight_kg":st.column_config.NumberColumn("Peso (kg)",format="%.3f"),
                   "cost":st.column_config.NumberColumn("Costo",format="%.2f"),
                   "shipping_cost":st.column_config.NumberColumn("Spedizione",format="%.2f"),
                   "total_cost":st.column_config.NumberColumn("Costo totale",format="%.2f"),
                   "price":st.column_config.NumberColumn("Prezzo",format="%.2f"),
                   "minimum_price":st.column_config.NumberColumn("Prezzo minimo",format="%.2f")},
    disabled=["ean","sku","name","weight_kg"])
selected_indexes=edited.index[edited["Seleziona"]==True]
# Keep the hidden national Cecotec price columns in the saved snapshot.
selected=df.loc[selected_indexes].drop(columns=["Seleziona","Mantieni"],errors="ignore").copy()
for column in visible:
    if column!="Seleziona":selected[column]=edited.loc[selected_indexes,column]
if destination_countries:
    selected["destination_countries"]=",".join(destination_countries)
    selected["destination_country"]=destination_country if len(destination_countries)==1 else ""
st.metric("Prodotti che saranno salvati",len(selected))

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
    if selected.empty:st.error("La vista non contiene prodotti selezionati.")
    elif not chosen_accounts:st.error("Seleziona almeno un marketplace di destinazione.")
    elif not overwrite and not view_name.strip():st.error("Inserisci il nome della vista.")
    else:
        try:
            folder=DATA_DIR/"saved_views"/str(seller_id);folder.mkdir(parents=True,exist_ok=True)
            if overwrite:
                target=existing_map[existing_choice];vid=target["id"];name=target["name"]
            else:
                name=view_name.strip();vid=execute("""INSERT INTO saved_views
                (seller_id,price_list_id,name,snapshot_path,filters_json,row_count,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",(seller_id,pl["id"],name,"pending",json_text({}),len(selected),now_iso(),now_iso()))
            path=folder/f"{vid}_{safe_name(name)}.pkl";selected.to_pickle(path)
            filters={"search":search,"min_qty":min_qty,"min_cost":min_cost,"max_cost":max_cost,
                     "shipping":shipping,"margin":margin,"minimum_margin":minimum_margin,
                     "weight_exclusion_mode":weight_mode,"weight_from":weight_from,
                     "weight_to":weight_to,
                     "destination_countries":destination_countries}
            execute("UPDATE saved_views SET price_list_id=?,snapshot_path=?,filters_json=?,row_count=?,updated_at=? WHERE id=?",
                    (pl["id"],str(path),json_text(filters),len(selected),now_iso(),vid))
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
    st.dataframe([{"ID":x["id"],"Vista":x["name"],"Listino":x["price_list_name"],"Prodotti":x["row_count"],"Marketplace":x["destinations"] or "—","Aggiornata":x["updated_at"]} for x in saved],use_container_width=True,hide_index=True)

    st.subheader("Apri e modifica una vista salvata")
    saved_map={f"{x['name']} · {x['row_count']} prodotti · ID {x['id']}":x for x in saved}
    saved_label=st.selectbox("Vista da lavorare",list(saved_map),key="saved_view_to_edit")
    saved_view=saved_map[saved_label]
    snapshot=Path(saved_view["snapshot_path"])
    if not snapshot.exists():
        st.error("Il file della vista non è disponibile. Puoi eliminare la registrazione e ricrearla.")
    else:
        try:
            view_df=normalize(pd.read_pickle(snapshot))
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
                        managed.to_pickle(snapshot)
                        execute("UPDATE saved_views SET name=?,row_count=?,updated_at=? WHERE id=? AND seller_id=?",
                                (new_name.strip(),len(managed),now_iso(),saved_view["id"],seller_id))
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
