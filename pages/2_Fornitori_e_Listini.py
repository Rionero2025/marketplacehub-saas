from __future__ import annotations

import json
from pathlib import Path
import pandas as pd
import streamlit as st

from marketplace_core.catalogs import CatalogCore
from marketplace_core.jobs import JobsCore

from services.abonline import (ABOnlineClient, ABOnlineError,
                               DEFAULT_GATEWAY as ABONLINE_GATEWAY,
                               download_abonline_catalog, refresh_abonline_prices_stock)
from services.activeshop import (DEFAULT_HOST as ACTIVESHOP_API_HOST,
                                 DEFAULT_STORE_CODE as ACTIVESHOP_STORE_CODE,
                                 validate_credentials as validate_activeshop_api)
from services.db import (accessible_lists, delete_price_list, delete_supplier,
                         execute, now_iso, pending_deletion_paths, rows, sellers)
from services.lists import (download_activeshop_combined, download_cecotec_combined,
                            download_forcetop_combined, download_hurtel_combined,
                            download_url, hurtel_feed_urls, materialize_price_list, normalize, read_list,
                            refresh_forcetop_inventory, save_cecotec_monthly, save_uploaded)
from services.security import decrypt_dict, encrypt_dict
from services.session import bootstrap, seller_selector

bootstrap(); st.title("Fornitori e Listini")
seller_id=seller_selector()
if seller_id is None: st.stop()


def _ab_credentials_payload(client_code: str,login: str,password: str,gateway_url: str,
                            previous: dict | None=None,check: dict | None=None,
                            error: Exception | None=None) -> dict:
    previous=dict(previous or {})
    check=dict(check or {})
    detected=(check.get("public_ip") or
              (getattr(error,"detected_public_ip","") if error else "") or
              (getattr(error,"ip_address","") if error else "") or
              previous.get("last_public_ip","") or "")
    gateway_ip=(check.get("gateway_reported_ip") or
                (getattr(error,"ip_address","") if error else "") or
                previous.get("last_gateway_ip","") or "")
    payload={
        "provider":"abonline","client_code":client_code.strip(),
        "login":login.strip(),"password":password,
        "gateway_url":gateway_url.strip() or ABONLINE_GATEWAY,
        "price_currency":"PLN",
        "last_public_ip":detected,
        "last_gateway_ip":gateway_ip,
        "last_ip_checked_at":check.get("ip_checked_at") or now_iso(),
    }
    if check.get("previous_public_ip"):
        payload["previous_public_ip"]=check["previous_public_ip"]
    elif previous.get("last_public_ip") and detected and previous.get("last_public_ip")!=detected:
        payload["previous_public_ip"]=previous.get("last_public_ip")
    return payload


def _save_ab_credentials(price_list_id: int,credentials: dict) -> None:
    execute(
        "UPDATE price_lists SET source_url=?,source_credentials_encrypted=? WHERE id=?",
        (credentials["gateway_url"],encrypt_dict(credentials),price_list_id)
    )


def _ab_client(client_code: str,login: str,password: str,gateway_url: str,
               previous: dict | None=None) -> ABOnlineClient:
    previous=previous or {}
    return ABOnlineClient(
        client_code,login,password,gateway_url,
        previous_public_ip=previous.get("last_public_ip","")
    )


def _remember_ab_ip_failure(price_list_id: int,client_code: str,login: str,password: str,
                            gateway_url: str,previous: dict,error: Exception) -> None:
    if not isinstance(error,ABOnlineError) or error.code!="59":
        return
    credentials=_ab_credentials_payload(
        client_code,login,password,gateway_url,previous=previous,error=error
    )
    _save_ab_credentials(price_list_id,credentials)


def show_abonline_error(error: Exception,prefix: str="Errore AB Online") -> None:
    st.error(f"{prefix}: {error}")
    if isinstance(error,ABOnlineError) and error.code=="59":
        current=(error.detected_public_ip or error.ip_address or
                 "indicato nel messaggio del Gateway")
        st.warning(
            f"IP pubblico corrente da autorizzare: {current}. Accedi ad AB Online e apri "
            "Administration → XML Gateway IPs; aggiungi questo indirizzo, salva e poi "
            "ripeti l'operazione. Marketplace Hub aggiorna l'IP a ogni verifica e non "
            "riutilizza quello della sessione precedente."
        )
        if error.previous_public_ip and error.detected_public_ip and error.ip_changed:
            st.info(
                f"Cambio IP rilevato automaticamente: {error.previous_public_ip} → "
                f"{error.detected_public_ip}."
            )
        if (error.ip_address and error.detected_public_ip and
                error.ip_address!=error.detected_public_ip):
            st.warning(
                "L'IP visto da AB Online è diverso da quello pubblico rilevato dal PC. "
                "Controlla che non siano attivi VPN o proxy."
            )


delete_notice=st.session_state.pop("_price_list_delete_notice",None)
if delete_notice:
    if delete_notice["pending"]:st.warning(delete_notice["message"])
    else:st.success(delete_notice["message"])

tab1,tab2,tab3=st.tabs(["Registra fornitore/listino","Listini disponibili","Condivisioni"])

with tab1:
    existing=rows("SELECT * FROM suppliers WHERE owner_seller_id=? ORDER BY name",(seller_id,))
    mode=st.radio("Fornitore",("Usa fornitore esistente","Crea nuovo fornitore"),horizontal=True)
    if mode=="Crea nuovo fornitore" or not existing:
        supplier_name=st.text_input("Nome fornitore")
        notes=st.text_area("Note fornitore")
        if st.button("Registra fornitore"):
            if supplier_name.strip():
                execute("INSERT INTO suppliers(owner_seller_id,name,notes,created_at) VALUES(?,?,?,?)",
                        (seller_id,supplier_name.strip(),notes.strip(),now_iso()))
                st.success("Fornitore registrato.");st.rerun()
    else:
        supplier_map={x["name"]:x["id"] for x in existing}
        supplier_choice=st.selectbox("Fornitore",list(supplier_map))
        st.divider();st.subheader("Nuovo listino")
        list_name=st.text_input("Nome listino")
        visibility=st.radio("Visibilità",("Solo questo Seller","Condiviso con Seller selezionati","Globale"))
        vis_code={"Solo questo Seller":"private","Condiviso con Seller selezionati":"shared","Globale":"global"}[visibility]
        other=[x for x in sellers() if x["id"]!=seller_id]
        shared_names=st.multiselect("Seller autorizzati",[x["name"] for x in other],disabled=vis_code!="shared")
        supplier_token=supplier_choice.strip().lower().replace(" ","")
        is_activeshop_supplier="activeshop" in supplier_token
        is_cecotec_supplier=("cecotec" in supplier_token or "ecotech" in supplier_token)
        is_hurtel_supplier="hurtel" in supplier_token
        is_forcetop_supplier=("forcetop" in supplier_token or "focetop" in supplier_token)
        is_abonline_supplier=("abonline" in supplier_token or supplier_token in {"ab.pl","abpl","ab"})
        if is_abonline_supplier:
            source_options=("API XML AB Online",)
        elif is_cecotec_supplier:
            source_options=("Carica file","URL feed","Excel mensile + URL stock")
        else:
            source_options=("Carica file","URL feed")
        source=st.radio("Origine",source_options,horizontal=True,key=f"source_{supplier_map[supplier_choice]}")
        upload=None;url="";stock_url="";username="";password=""
        api_username="";api_password="";api_host=ACTIVESHOP_API_HOST;api_store_code=ACTIVESHOP_STORE_CODE
        ab_client_code="";ab_login="";ab_password="";ab_gateway=ABONLINE_GATEWAY;ab_price_currency="PLN"
        if source=="API XML AB Online":
            st.markdown("#### Accesso XML Gateway AB Online")
            st.caption(
                "Inserisci codice cliente AB, login e password. Prima del download verifica "
                "l'accesso; se il Gateway restituisce l'errore 59, autorizza l'IP mostrato in "
                "AB Online → Administration → XML Gateway IPs."
            )
            ab1,ab2=st.columns(2)
            ab_client_code=ab1.text_input("Codice cliente AB *",
                                          help="ID cliente AB, completo degli eventuali zeri iniziali.")
            ab_login=ab2.text_input("Login AB Online *")
            ab_password=st.text_input("Password AB Online *",type="password")
            st.info(
                "AB Online restituisce i prezzi in PLN. Il programma leggerà il cambio dal "
                "Gateway e salverà il prezzo di acquisto convertito in EUR."
            )
            ab_gateway=st.text_input("URL XML Gateway",value=ABONLINE_GATEWAY)
            if st.button("Verifica connessione AB Online",key="verify_abonline_new"):
                if not ab_client_code.strip() or not ab_login.strip() or not ab_password:
                    st.error("Inserisci codice cliente AB, login e password.")
                else:
                    try:
                        with st.spinner("Verifica XML Gateway AB Online…"):
                            check=_ab_client(
                                ab_client_code,ab_login,ab_password,ab_gateway
                            ).validate(ab_price_currency)
                        st.success(
                            check["message"]+
                            f" Cambio rilevato: 1 EUR = {check['detected_pln_per_eur']:.4f} PLN."
                        )
                    except Exception as error:
                        show_abonline_error(error,"Verifica non riuscita")
        elif source in ("Carica file","Excel mensile + URL stock"):
            upload=st.file_uploader("CSV, XLSX, XML o PKL",type=["csv","xlsx","xls","xml","pkl","pickle"])
            if source=="Excel mensile + URL stock":
                stock_url=st.text_input("URL stock Cecobi",value="https://cecobi.cecotec.cloud/ws/getstockfeedb2c.php?etiqueta=DROPIT")
        else:
            url=st.text_input("URL del feed",value="https://b2b.activeshop.com.pl/media/productsfeed/b2b-it.xml" if is_activeshop_supplier else "")
            secondary_label=("URL feed LIGHT — prezzo ingrosso e stock"
                             if is_hurtel_supplier else
                             "URL prezzi e stock Force Top" if is_forcetop_supplier else
                             "URL secondario prezzo/stock (facoltativo)")
            secondary_help=("Hurtel: il FULL contiene anagrafica e prezzo retail; il LIGHT contiene il tuo prezzo ingrosso. "
                            "Il LIGHT deve essere autorizzato da Hurtel e può avere un token diverso dal FULL. "
                            "Se lasci vuoto, il programma prova a ricavarlo, ma il download funzionerà solo se lo stesso token è abilitato anche per LIGHT."
                            if is_hurtel_supplier else
                            "Force Top: incolla InventoryReport; contiene prezzo di acquisto e disponibilità correnti."
                            if is_forcetop_supplier else
                            "ActiveShop: XML stock-b2b da unire al catalogo localizzato. Cecotec: feed Cecobi stock B2C.")
            stock_url=st.text_input(secondary_label,
                                    value="https://b2b.activeshop.com.pl/media/productsfeed/stock-b2b.xml" if is_activeshop_supplier else "",
                                    help=secondary_help)
            if is_hurtel_supplier:
                st.info(
                    "Hurtel: il programma userà il FULL per descrizioni, immagini, EAN e SKU; "
                    "il costo d’acquisto e la disponibilità saranno presi esclusivamente dal LIGHT. "
                    "Non incollare il collegamento FULL anche nel campo LIGHT: usa l’URL LIGHT fornito o generato da Hurtel."
                )
            if is_forcetop_supplier:
                st.info(
                    "Force Top usa due file BTP: ProductCatalogue per anagrafica, immagini, EAN e SKU; "
                    "InventoryReport per prezzo di acquisto e disponibilità. Marketplace Hub li unirà "
                    "automaticamente e userà l'archivio certificati del sistema senza disattivare SSL."
                )
            if is_activeshop_supplier:
                st.markdown("#### Prezzo Diamond ActiveShop")
                a1,a2=st.columns(2)
                api_username=a1.text_input("Username/e-mail Catalog API ActiveShop")
                api_password=a2.text_input("Password Catalog API ActiveShop",type="password")
                a3,a4=st.columns(2)
                api_host=a3.text_input("Host API ActiveShop",value=ACTIVESHOP_API_HOST)
                api_store_code=a4.text_input("Store code ActiveShop",value=ACTIVESHOP_STORE_CODE)
                st.caption("Le credenziali vengono cifrate. Il prezzo di acquisto sarà `final_price` del tuo livello Diamond.")
            else:
                c1,c2=st.columns(2);username=c1.text_input("Username feed (facoltativo)");password=c2.text_input("Password feed",type="password")
        register_label=("Registra e scarica listino"
                        if source=="API XML AB Online" else "Registra listino")
        if st.button(register_label,type="primary"):
            missing_standard_source=(source not in ("URL feed","API XML AB Online") and upload is None)
            missing_ab_credentials=(source=="API XML AB Online" and
                                    (not ab_client_code.strip() or not ab_login.strip() or not ab_password))
            if (not list_name.strip() or missing_standard_source or
                    (source=="URL feed" and not url.strip()) or
                    (source=="URL feed" and is_forcetop_supplier and not stock_url.strip()) or
                    (source=="Excel mensile + URL stock" and not stock_url.strip())):
                st.error("Compila nome e origine del listino.")
            elif missing_ab_credentials:
                st.error("Inserisci codice cliente AB, login e password.")
            elif is_activeshop_supplier and source=="URL feed" and (not api_username.strip() or not api_password):
                st.error("Inserisci username e password del Catalog API ActiveShop per leggere il prezzo Diamond.")
            else:
                lid=None
                try:
                    if source=="URL feed" and is_hurtel_supplier:
                        url,stock_url=hurtel_feed_urls(url,stock_url)
                    if source=="URL feed" and stock_url.strip() and ("activeshop" in supplier_token or "activeshop.com.pl" in url.lower() or "activeshop.com.pl" in stock_url.lower()):
                        if "stock-b2b" in url.lower() and "stock-b2b" not in stock_url.lower():
                            url,stock_url=stock_url,url
                    source_credentials={
                        "username":username,"password":password,"stock_url":stock_url.strip(),
                        "api_username":api_username.strip(),"api_password":api_password,
                        "api_host":api_host.strip(),"api_store_code":api_store_code.strip(),
                        "provider":"forcetop" if is_forcetop_supplier else ""
                    }
                    if source=="API XML AB Online":
                        with st.spinner("Aggiornamento IP pubblico e verifica AB Online…"):
                            ab_check=_ab_client(
                                ab_client_code,ab_login,ab_password,ab_gateway
                            ).validate(ab_price_currency)
                        source_credentials=_ab_credentials_payload(
                            ab_client_code,ab_login,ab_password,ab_gateway,check=ab_check
                        )
                    lid=execute("""INSERT INTO price_lists
                    (supplier_id,owner_seller_id,name,visibility,source_type,source_url,source_credentials_encrypted,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",(supplier_map[supplier_choice],seller_id,list_name.strip(),vis_code,
                    "url" if source in ("URL feed","API XML AB Online") else "upload",
                    (ab_gateway.strip() or ABONLINE_GATEWAY) if source=="API XML AB Online" else url.strip(),
                    encrypt_dict(source_credentials) if any(source_credentials.values()) else "",now_iso()))
                    if source=="API XML AB Online":
                        ab_bar=st.progress(0,text="Preparazione catalogo AB Online…")
                        def ab_progress(stage,current,total):
                            if stage=="download":
                                ratio=(current/total) if total else 0.05
                                text=(f"Download catalogo AB Online: {ratio:.0%}"
                                      if total else f"Download catalogo AB Online: {current/1048576:.1f} MB")
                                ab_bar.progress(min(0.70,max(0.02,ratio*0.70)),text=text)
                            else:
                                ab_bar.progress(min(0.98,0.70+current/100000),
                                                text=f"Elaborazione catalogo: {current:,} prodotti")
                        download_abonline_catalog(
                            lid,ab_client_code,ab_login,ab_password,ab_gateway,
                            ab_price_currency,ab_progress
                        )
                        ab_bar.progress(1.0,text="Catalogo AB Online importato.")
                    elif source=="Excel mensile + URL stock": save_cecotec_monthly(lid,upload.name,upload.getvalue(),stock_url.strip())
                    elif upload: save_uploaded(lid,upload.name,upload.getvalue())
                    elif is_hurtel_supplier:
                        download_hurtel_combined(lid,url.strip(),stock_url.strip(),username,password)
                    elif is_forcetop_supplier or "ext.btp.link" in url.lower() or "ext.btp.link" in stock_url.lower():
                        download_forcetop_combined(lid,url.strip(),stock_url.strip(),username,password)
                    elif stock_url.strip() and ("activeshop" in supplier_token or "activeshop.com.pl" in url.lower() or "activeshop.com.pl" in stock_url.lower()):
                        api_bar=st.progress(0,text="Lettura prezzi Diamond ActiveShop…")
                        def api_progress(page,total,count):
                            api_bar.progress(min(1.0,page/max(1,total)),text=f"Prezzi Diamond: pagina {page}/{total} · {count} prodotti")
                        download_activeshop_combined(lid,url.strip(),stock_url.strip(),username,password,
                            api_username,api_password,api_host.strip(),api_store_code.strip(),api_progress)
                    elif stock_url.strip():download_cecotec_combined(lid,url.strip(),stock_url.strip(),username,password)
                    else: download_url(lid,url.strip(),username,password)
                    for x in other:
                        if x["name"] in shared_names:
                            execute("INSERT INTO price_list_access(price_list_id,seller_id,permission) VALUES(?,?,'use') ON CONFLICT DO NOTHING",(lid,x["id"]))
                    st.success("Listino registrato e importato.");st.rerun()
                except Exception as e:
                    if lid is not None:
                        delete_price_list(lid,seller_id)
                    if source=="API XML AB Online":
                        show_abonline_error(e,"Errore registrazione")
                    else:
                        st.error(f"Errore registrazione: {e}")

with tab2:
    data=accessible_lists(seller_id)
    if not data: st.info("Nessun listino disponibile per questo Seller.")
    else:
        st.dataframe([{"ID":x["id"],"Fornitore":x["supplier_name"],"Listino":x["name"],"Proprietario":x["owner_name"],"Visibilità":x["visibility"],"Aggiornato":x["last_download_at"] or "—"} for x in data],use_container_width=True,hide_index=True)
        lmap={f"{x['supplier_name']} · {x['name']} (ID {x['id']})":x for x in data}
        choice=st.selectbox("Apri listino",list(lmap));item=dict(lmap[choice])
        if (not item.get("local_path") or not Path(str(item.get("local_path") or "")).exists()) and item.get("storage_key"):
            try:
                recovered=materialize_price_list(int(item["id"]),item.get("local_path"))
                if recovered:item["local_path"]=str(recovered)
            except Exception as storage_error:
                st.caption(f"Storage listino non materializzato: {storage_error}")
        item_supplier_token=str(item.get("supplier_name","")).strip().lower().replace(" ","")
        is_activeshop_item=("activeshop" in item_supplier_token or "activeshop.com.pl" in str(item.get("source_url","")).lower())
        is_hurtel_item=("hurtel" in item_supplier_token or "hurtel.com" in str(item.get("source_url","")).lower())
        is_forcetop_item=("forcetop" in item_supplier_token or "focetop" in item_supplier_token or
                          "ext.btp.link" in str(item.get("source_url","")).lower())
        current_cred=(decrypt_dict(item["source_credentials_encrypted"])
                      if item["owner_seller_id"]==seller_id and item["source_credentials_encrypted"] else {})
        is_abonline_item=(current_cred.get("provider")=="abonline" or
                          "abonline" in item_supplier_token or
                          str(item.get("source_url","")).rstrip("/")==ABONLINE_GATEWAY.rstrip("/"))
        if item["local_path"] and Path(item["local_path"]).exists():
            catalog_core=CatalogCore(); jobs_core=JobsCore()
            catalog_status=catalog_core.status(item["id"],item["local_path"])
            st.markdown("#### Catalogo veloce")
            if catalog_status.ready:
                st.success(
                    f"Catalogo indicizzato: {catalog_status.row_count:,} prodotti · "
                    f"aggiornato {catalog_status.materialized_at or '—'}."
                )
                try:
                    preview=catalog_core.preview(item["id"],200)
                    df=pd.DataFrame.from_records(preview.rows)
                    if not df.empty:
                        if "shipping_cost" not in df:df["shipping_cost"]=0.0
                        if "total_cost" not in df:df["total_cost"]=(pd.to_numeric(df.get("cost",0),errors="coerce").fillna(0)+pd.to_numeric(df.get("shipping_cost",0),errors="coerce").fillna(0)).round(2)
                        front=[c for c in ["ean","sku","name","cost","shipping_cost","total_cost","quantity"] if c in df]
                        remaining=[c for c in df.columns if c not in front][:12]
                        st.dataframe(df[front+remaining],use_container_width=True,hide_index=True)
                    st.caption("Anteprima server-side: vengono lette solo 200 righe, non l'intero listino.")
                except Exception as e:st.error(f"Anteprima catalogo: {e}")
            else:
                size_mb=Path(item["local_path"]).stat().st_size/(1024*1024)
                st.info(
                    f"File sorgente disponibile ({size_mb:.1f} MB), ma non ancora indicizzato. "
                    "La normalizzazione può essere eseguita in background senza bloccare la pagina."
                )
                job_key=f"catalog_materialize_job_{item['id']}"
                if st.button("Prepara catalogo veloce in background",key=f"materialize_catalog_{item['id']}",type="primary"):
                    receipt=jobs_core.submit(catalog_core.build_materialize_job(seller_id,item["id"]))
                    jobs_core.start_local(receipt.job_id)
                    st.session_state[job_key]=receipt.job_id
                    st.rerun()
                job_id=st.session_state.get(job_key)
                if job_id:
                    snap=jobs_core.snapshot(job_id)
                    if snap:
                        st.progress(min(1.0,max(0.0,snap.progress_pct/100.0)),text=snap.message or snap.status)
                        if snap.terminal:
                            if snap.status=="done":
                                st.success("Catalogo indicizzato. Aggiorna la pagina per usare l'anteprima veloce.")
                            elif snap.status=="error":st.error(snap.error or "Errore indicizzazione catalogo")
                        elif st.button("Aggiorna stato catalogo",key=f"refresh_catalog_job_{item['id']}"):
                            st.rerun()
        is_cecotec=bool(current_cred.get("stock_url")) and item["source_type"]=="upload"
        if is_abonline_item and item["owner_seller_id"]==seller_id:
            st.markdown("#### API XML AB Online")
            st.caption(
                "Il catalogo completo aggiorna anagrafica, peso, dimensioni, prezzi e stock. "
                "L'aggiornamento rapido modifica soltanto prezzi e disponibilità. Prima di ogni "
                "verifica o download il programma rileva nuovamente l'IP pubblico e apre una "
                "connessione nuova, senza riutilizzare l'IP salvato in precedenza."
            )
            last_ip=current_cred.get("last_public_ip","")
            last_check=current_cred.get("last_ip_checked_at","")
            if last_ip:
                st.info(
                    f"Ultimo IP pubblico rilevato: **{last_ip}**"+
                    (f" · verifica: {last_check}" if last_check else "")
                )
            ab1,ab2=st.columns(2)
            ab_client_code=ab1.text_input(
                "Codice cliente AB",value=current_cred.get("client_code",""),
                key=f"ab_client_{item['id']}"
            )
            ab_login=ab2.text_input(
                "Login AB Online",value=current_cred.get("login",""),
                key=f"ab_login_{item['id']}"
            )
            ab_password_saved=current_cred.get("password","")
            ab_password_input=st.text_input(
                "Nuova password AB Online",type="password",
                key=f"ab_password_{item['id']}",
                placeholder="Già salvata" if ab_password_saved else "Inserisci password"
            )
            ab_price_currency="PLN"
            st.info(
                "Prezzi sorgente: PLN → costo utilizzato dal programma: EUR, "
                "convertito automaticamente con il cambio AB."
            )
            ab_gateway=st.text_input(
                "URL XML Gateway",
                value=current_cred.get("gateway_url",item["source_url"]) or ABONLINE_GATEWAY,
                key=f"ab_gateway_{item['id']}"
            )
            ab_password=ab_password_input or ab_password_saved
            ab_connection_col,ab_save_col=st.columns(2)
            if ab_connection_col.button(
                "Aggiorna IP e verifica AB Online",key=f"verify_ab_{item['id']}"
            ):
                if not ab_client_code.strip() or not ab_login.strip() or not ab_password:
                    st.error("Inserisci codice cliente AB, login e password.")
                else:
                    try:
                        with st.spinner("Rilevamento nuovo IP e verifica XML Gateway AB Online…"):
                            check=_ab_client(
                                ab_client_code,ab_login,ab_password,ab_gateway,current_cred
                            ).validate(ab_price_currency)
                        saved_ab=_ab_credentials_payload(
                            ab_client_code,ab_login,ab_password,ab_gateway,
                            previous=current_cred,check=check
                        )
                        _save_ab_credentials(item["id"],saved_ab)
                        ip_message=(f" IP pubblico corrente: {check['public_ip']}."
                                    if check.get("public_ip") else "")
                        changed_message=(
                            f" Cambio IP rilevato: {check['previous_public_ip']} → {check['public_ip']}."
                            if check.get("ip_changed") else ""
                        )
                        st.success(
                            check["message"]+
                            f" Cambio rilevato: 1 EUR = {check['detected_pln_per_eur']:.4f} PLN."+
                            ip_message+changed_message
                        )
                        st.rerun()
                    except Exception as error:
                        _remember_ab_ip_failure(
                            item["id"],ab_client_code,ab_login,ab_password,ab_gateway,
                            current_cred,error
                        )
                        show_abonline_error(error,"Verifica non riuscita")
            if ab_save_col.button("Salva credenziali AB Online",key=f"save_ab_{item['id']}"):
                if not ab_client_code.strip() or not ab_login.strip() or not ab_password:
                    st.error("Inserisci codice cliente AB, login e password.")
                else:
                    saved_ab=_ab_credentials_payload(
                        ab_client_code,ab_login,ab_password,ab_gateway,previous=current_cred
                    )
                    _save_ab_credentials(item["id"],saved_ab)
                    st.success("Credenziali AB Online salvate in forma cifrata.");st.rerun()

            full_col,quick_col=st.columns(2)
            if full_col.button("Aggiorna catalogo completo (include peso)",key=f"full_ab_{item['id']}"):
                if not ab_client_code.strip() or not ab_login.strip() or not ab_password:
                    st.error("Configura prima codice cliente AB, login e password.")
                else:
                    try:
                        with st.spinner("Aggiornamento automatico IP e verifica AB Online…"):
                            check=_ab_client(
                                ab_client_code,ab_login,ab_password,ab_gateway,current_cred
                            ).validate(ab_price_currency)
                        saved_ab=_ab_credentials_payload(
                            ab_client_code,ab_login,ab_password,ab_gateway,
                            previous=current_cred,check=check
                        )
                        _save_ab_credentials(item["id"],saved_ab)
                        ab_bar=st.progress(0,text="Preparazione catalogo AB Online…")
                        def ab_full_progress(stage,current,total):
                            if stage=="download":
                                ratio=(current/total) if total else 0.05
                                text=(f"Download catalogo: {ratio:.0%}"
                                      if total else f"Download catalogo: {current/1048576:.1f} MB")
                                ab_bar.progress(min(0.45,max(0.02,ratio*0.45)),text=text)
                            elif stage=="details":
                                ratio=(current/total) if total else 0
                                ab_bar.progress(
                                    min(0.98,0.55+ratio*0.43),
                                    text=f"Recupero peso e dimensioni: {current:,}/{total:,} prodotti"
                                )
                            else:
                                ab_bar.progress(min(0.54,0.45+current/400000),
                                                text=f"Elaborazione: {current:,} prodotti")
                        updated_ab_path=download_abonline_catalog(
                            item["id"],ab_client_code,ab_login,ab_password,ab_gateway,
                            ab_price_currency,ab_full_progress
                        )
                        updated_ab=pd.read_pickle(updated_ab_path)
                        known_weights=int(
                            pd.to_numeric(updated_ab.get("weight_kg",0),errors="coerce")
                            .fillna(0).gt(0).sum()
                        )
                        ab_bar.progress(1.0,text="Catalogo AB Online aggiornato.")
                        if known_weights:
                            st.success(
                                f"Catalogo completo aggiornato: peso acquisito per "
                                f"{known_weights:,} prodotti."
                            )
                        else:
                            st.warning(
                                "Catalogo aggiornato, ma AB Online non ha restituito un peso "
                                "valido per nessun prodotto."
                            )
                        st.rerun()
                    except Exception as e:
                        _remember_ab_ip_failure(
                            item["id"],ab_client_code,ab_login,ab_password,ab_gateway,
                            current_cred,e
                        )
                        show_abonline_error(e,"Errore aggiornamento AB Online")
            if quick_col.button("Aggiorna solo prezzi e stock",key=f"quick_ab_{item['id']}"):
                if not item["local_path"] or not Path(item["local_path"]).exists():
                    st.error("Prima importa il catalogo completo AB Online.")
                elif not ab_client_code.strip() or not ab_login.strip() or not ab_password:
                    st.error("Configura prima codice cliente AB, login e password.")
                else:
                    try:
                        with st.spinner("Aggiornamento automatico IP e verifica AB Online…"):
                            check=_ab_client(
                                ab_client_code,ab_login,ab_password,ab_gateway,current_cred
                            ).validate(ab_price_currency)
                        saved_ab=_ab_credentials_payload(
                            ab_client_code,ab_login,ab_password,ab_gateway,
                            previous=current_cred,check=check
                        )
                        _save_ab_credentials(item["id"],saved_ab)
                        quick_bar=st.progress(0,text="Download prezzi e stock AB Online…")
                        def ab_quick_progress(stage,current,total):
                            ratio=(current/total) if total else 0.20
                            quick_bar.progress(min(0.85,max(0.05,ratio*0.85)),
                                               text=(f"Download prezzi e stock: {ratio:.0%}"
                                                     if total else f"Download: {current/1048576:.1f} MB"))
                        refresh_abonline_prices_stock(
                            item["id"],item["local_path"],ab_client_code,ab_login,
                            ab_password,ab_gateway,ab_price_currency,ab_quick_progress
                        )
                        quick_bar.progress(1.0,text="Prezzi e stock aggiornati.")
                        st.success("Prezzi e stock AB Online aggiornati.");st.rerun()
                    except Exception as e:
                        _remember_ab_ip_failure(
                            item["id"],ab_client_code,ab_login,ab_password,ab_gateway,
                            current_cred,e
                        )
                        show_abonline_error(e,"Errore aggiornamento AB Online")
        elif (item["source_type"]=="url" or is_cecotec) and item["owner_seller_id"]==seller_id:
            primary_url=item["source_url"]
            if item["source_type"]=="url":
                primary_url=st.text_input("URL catalogo principale",value=item["source_url"],key=f"primary_url_{item['id']}",
                                          help=("Hurtel: usa il feed FULL." if is_hurtel_item else
                                                "ActiveShop: usa b2b-it.xml. Il feed stock-b2b.xml va nel campo secondario."))
            companion_url=st.text_input(
                "URL feed LIGHT — prezzo ingrosso e stock" if is_hurtel_item else
                "URL prezzi e stock Force Top" if is_forcetop_item else "URL secondario prezzo/stock",
                value=current_cred.get("stock_url",""),key=f"stock_url_{item['id']}",
                help=("Hurtel: se vuoto, viene ricavato automaticamente dal FULL."
                      if is_hurtel_item else
                      "Force Top: InventoryReport Excel con prezzo e disponibilità correnti."
                      if is_forcetop_item else
                      "ActiveShop: XML stock-b2b. Cecotec: feed Cecobi stock da unire al catalogo.")
            )
            api_username=current_cred.get("api_username","")
            api_password_saved=current_cred.get("api_password","")
            api_password_input=""
            api_host=current_cred.get("api_host",ACTIVESHOP_API_HOST) or ACTIVESHOP_API_HOST
            api_store_code=current_cred.get("api_store_code",ACTIVESHOP_STORE_CODE) or ACTIVESHOP_STORE_CODE
            if is_activeshop_item:
                st.markdown("#### Catalog API ActiveShop — prezzo Diamond")
                a1,a2=st.columns(2)
                api_username=a1.text_input("Username/e-mail API",value=api_username,key=f"active_api_user_{item['id']}")
                api_password_input=a2.text_input("Nuova password API",type="password",key=f"active_api_password_{item['id']}",
                                                 placeholder="Già salvata" if api_password_saved else "Inserisci password")
                a3,a4=st.columns(2)
                api_host=a3.text_input("Host API",value=api_host,key=f"active_api_host_{item['id']}")
                api_store_code=a4.text_input("Store code",value=api_store_code,key=f"active_api_store_{item['id']}")
                api_password=api_password_input or api_password_saved
                if st.button("Verifica connessione e prezzo Diamond",key=f"verify_active_api_{item['id']}"):
                    with st.spinner("Verifica Catalog API ActiveShop…"):
                        check=validate_activeshop_api(api_username,api_password,api_host,api_store_code)
                    if check["ok"]:st.success(check["message"])
                    else:st.error(check["message"])
            if st.button("Salva URL feed",key=f"save_stock_url_{item['id']}"):
                current_cred["stock_url"]=companion_url.strip()
                if is_activeshop_item:
                    current_cred.update({"api_username":api_username.strip(),"api_host":api_host.strip(),
                                         "api_store_code":api_store_code.strip()})
                    if api_password_input:current_cred["api_password"]=api_password_input
                if item["source_type"]=="url":
                    execute("UPDATE price_lists SET source_url=? WHERE id=?",(primary_url.strip(),item["id"]))
                execute("UPDATE price_lists SET source_credentials_encrypted=? WHERE id=?",
                        (encrypt_dict(current_cred),item["id"]))
                st.success("URL dei feed salvati.");st.rerun()
            if is_cecotec:
                monthly_file=st.file_uploader("Sostituisci Excel mensile Cecotec",type=["xlsx","xls"],key=f"monthly_{item['id']}")
                if st.button("Importa Excel e aggiorna stock",key=f"monthly_save_{item['id']}"):
                    if monthly_file is None: st.error("Seleziona il nuovo file Excel Cecotec.")
                    elif not companion_url.strip(): st.error("Inserisci prima l’URL stock Cecobi.")
                    else:
                        try:
                            save_cecotec_monthly(item["id"],monthly_file.name,monthly_file.getvalue(),companion_url.strip())
                            st.success("Excel mensile importato e stock Cecobi aggiornato.");st.rerun()
                        except Exception as e: st.error(f"Errore importazione Cecotec: {e}")
            if item["source_type"]=="url" and st.button("Aggiorna ora dal feed"):
                try:
                    cred=current_cred;stock_feed=companion_url.strip()
                    supplier_token=str(item.get("supplier_name","")).strip().lower().replace(" ","")
                    is_activeshop=("activeshop" in supplier_token or "activeshop.com.pl" in primary_url.lower() or "activeshop.com.pl" in stock_feed.lower())
                    is_hurtel=("hurtel" in supplier_token or "hurtel.com" in primary_url.lower())
                    if is_hurtel:
                        catalog_feed,stock_feed=hurtel_feed_urls(primary_url.strip(),stock_feed)
                        download_hurtel_combined(item["id"],catalog_feed,stock_feed,
                                                 cred.get("username",""),cred.get("password",""))
                        cred["stock_url"]=stock_feed
                        execute("UPDATE price_lists SET source_url=?,source_credentials_encrypted=? WHERE id=?",
                                (catalog_feed,encrypt_dict(cred),item["id"]))
                    elif is_forcetop_item or "ext.btp.link" in primary_url.lower() or "ext.btp.link" in stock_feed.lower():
                        if not stock_feed:
                            raise ValueError("Inserisci l'URL prezzi e stock Force Top (InventoryReport).")
                        download_forcetop_combined(item["id"],primary_url.strip(),stock_feed,
                                                   cred.get("username",""),cred.get("password",""))
                        cred.update({"stock_url":stock_feed,"provider":"forcetop"})
                        execute("UPDATE price_lists SET source_url=?,source_credentials_encrypted=? WHERE id=?",
                                (primary_url.strip(),encrypt_dict(cred),item["id"]))
                    elif stock_feed and is_activeshop:
                        api_password=api_password_input or current_cred.get("api_password","")
                        if not api_username.strip() or not api_password:
                            raise ValueError("Configura e salva le credenziali del Catalog API ActiveShop per leggere il prezzo Diamond.")
                        catalog_feed=primary_url.strip()
                        if "stock-b2b" in catalog_feed.lower() and "stock-b2b" not in stock_feed.lower():
                            catalog_feed,stock_feed=stock_feed,catalog_feed
                        api_bar=st.progress(0,text="Lettura prezzi Diamond ActiveShop…")
                        def api_progress(page,total,count):
                            api_bar.progress(min(1.0,page/max(1,total)),text=f"Prezzi Diamond: pagina {page}/{total} · {count} prodotti")
                        download_activeshop_combined(item["id"],catalog_feed,stock_feed,cred.get("username",""),cred.get("password",""),
                            api_username,api_password,api_host.strip(),api_store_code.strip(),api_progress)
                        cred.update({"stock_url":stock_feed,"api_username":api_username.strip(),"api_password":api_password,
                                     "api_host":api_host.strip(),"api_store_code":api_store_code.strip()})
                        execute("UPDATE price_lists SET source_url=?,source_credentials_encrypted=? WHERE id=?",
                                (catalog_feed,encrypt_dict(cred),item["id"]))
                    elif stock_feed:download_cecotec_combined(item["id"],primary_url.strip(),stock_feed,cred.get("username",""),cred.get("password",""))
                    else:download_url(item["id"],primary_url.strip(),cred.get("username",""),cred.get("password",""))
                    st.success("Listino aggiornato.");st.rerun()
                except Exception as e:st.error(str(e))
            if is_forcetop_item and st.button("Aggiorna solo prezzi e stock Force Top",key=f"forcetop_stock_only_{item['id']}"):
                try:
                    if not companion_url.strip():raise ValueError("Configura l'URL InventoryReport Force Top.")
                    refresh_forcetop_inventory(item["id"],item["local_path"],companion_url.strip(),
                                               current_cred.get("username",""),current_cred.get("password",""))
                    st.success("Prezzi e stock Force Top aggiornati senza riscaricare il catalogo completo.");st.rerun()
                except Exception as e:st.error(f"Errore aggiornamento Force Top: {e}")
            if is_cecotec and st.button("Aggiorna solo stock Cecotec",key=f"stock_only_{item['id']}"):
                try:
                    if not companion_url.strip(): raise ValueError("Configura l’URL stock Cecobi.")
                    save_path=Path(item["local_path"])
                    # The current PKL already contains monthly prices and can be safely enriched again.
                    from services.lists import combine_cecotec_stock
                    combine_cecotec_stock(item["id"],save_path,companion_url.strip())
                    st.success("Stock Cecotec aggiornato senza modificare prezzi e anagrafica.");st.rerun()
                except Exception as e: st.error(f"Errore aggiornamento stock: {e}")
        if item["owner_seller_id"]==seller_id:
            st.divider(); st.subheader("Elimina listino")
            st.warning("Saranno eliminate anche le viste salvate, le regole commerciali e lo storico collegati al listino.")
            confirm_list=st.text_input('Digita ELIMINA per confermare',key=f"confirm_list_{item['id']}")
            if st.button("Elimina definitivamente il listino",key=f"delete_list_{item['id']}"):
                if confirm_list != "ELIMINA": st.error("Conferma non valida: digita ELIMINA.")
                elif delete_price_list(item["id"],seller_id):
                    pending=pending_deletion_paths()
                    st.session_state["_price_list_delete_notice"]={
                        "pending":bool(pending),
                        "message":("Listino eliminato. Alcuni file erano ancora occupati da Windows e saranno rimossi automaticamente "
                                   "appena il processo che li utilizza li rilascia.") if pending else "Listino eliminato definitivamente."
                    }
                    st.rerun()
                else: st.error("Puoi eliminare soltanto un listino di tua proprietà.")

with tab3:
    owned=rows("SELECT * FROM price_lists WHERE owner_seller_id=? ORDER BY name",(seller_id,))
    if not owned:st.info("Questo Seller non possiede listini.")
    else:
        omap={f"{x['name']} · ID {x['id']}":x for x in owned};sel=st.selectbox("Listino di proprietà",list(omap));pl=omap[sel]
        visibility=st.selectbox("Visibilità",("private","shared","global"),index=("private","shared","global").index(pl["visibility"]))
        all_other=[x for x in sellers() if x["id"]!=seller_id]
        current={x["seller_id"] for x in rows("SELECT seller_id FROM price_list_access WHERE price_list_id=?",(pl["id"],))}
        selected=st.multiselect("Condividi con",[x["name"] for x in all_other],default=[x["name"] for x in all_other if x["id"] in current],disabled=visibility!="shared")
        if st.button("Salva condivisione"):
            execute("UPDATE price_lists SET visibility=? WHERE id=?",(visibility,pl["id"]))
            execute("DELETE FROM price_list_access WHERE price_list_id=?",(pl["id"],))
            if visibility=="shared":
                for x in all_other:
                    if x["name"] in selected: execute("INSERT INTO price_list_access(price_list_id,seller_id,permission) VALUES(?,?,'use')",(pl["id"],x["id"]))
            st.success("Condivisione aggiornata.");st.rerun()

st.divider(); st.subheader("Elimina fornitore")
owned_suppliers=rows("SELECT * FROM suppliers WHERE owner_seller_id=? ORDER BY name",(seller_id,))
if not owned_suppliers:
    st.info("Nessun fornitore di proprietà da eliminare.")
else:
    supplier_delete_map={f"{x['name']} · ID {x['id']}":x for x in owned_suppliers}
    supplier_delete_label=st.selectbox("Fornitore da eliminare",list(supplier_delete_map),key="supplier_to_delete")
    supplier_delete=supplier_delete_map[supplier_delete_label]
    count=rows("SELECT COUNT(*) AS total FROM price_lists WHERE supplier_id=?",(supplier_delete["id"],))[0]["total"]
    st.warning(f"Il fornitore contiene {count} listini. Saranno eliminati insieme a tutti i dati collegati.")
    confirm_supplier=st.text_input(f'Digita il nome esatto "{supplier_delete["name"]}" per confermare',key="confirm_delete_supplier")
    if st.button("Elimina definitivamente il fornitore",key="delete_supplier"):
        if confirm_supplier != supplier_delete["name"]: st.error("Il nome inserito non corrisponde.")
        elif delete_supplier(supplier_delete["id"],seller_id):
            pending=pending_deletion_paths()
            st.session_state["_price_list_delete_notice"]={
                "pending":bool(pending),
                "message":("Fornitore e listini eliminati. Alcuni file occupati da Windows saranno rimossi automaticamente.") if pending
                          else "Fornitore e relativi listini eliminati definitivamente."
            }
            st.rerun()
        else: st.error("Fornitore non trovato o non appartenente al Seller.")
