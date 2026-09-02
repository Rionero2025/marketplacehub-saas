from __future__ import annotations

import streamlit as st

from services.db import (
    delete_marketplace_account,
    delete_seller,
    execute,
    now_iso,
    rows,
    sellers,
)
from services.profit_sharing import normalized_percentages
from services.packlink import (
    PacklinkClient, activate_integration as activate_packlink,
    delete_integration as delete_packlink, ensure_schema as ensure_packlink_schema,
    integration_credentials as packlink_credentials,
    integration_for_seller as packlink_integration_for_seller,
    set_integration_active as set_packlink_active,
    update_connection_status as update_packlink_connection_status,
)
from services.security import decrypt_dict, encrypt_dict, masked
from services.session import bootstrap
from services.entitlements import assert_marketplace_capacity, assert_resource_capacity
from services.catalog_sharing import tenant_for_seller
from services.tenant_db import current_tenant_id
from services.worten import DEFAULT_API_URL, validate_credentials as validate_worten


bootstrap()
ensure_packlink_schema()
st.title("Gestione Seller")

tab1, tab2, tab3, tab4 = st.tabs(
    ["Registra Seller", "Account marketplace", "Seller registrati", "Servizi e integrazioni"]
)

with tab1:
    with st.form("new_seller", clear_on_submit=True):
        name = st.text_input("Nome Seller *")
        legal = st.text_input("Ragione sociale")
        email = st.text_input("E-mail")
        st.markdown("#### Ripartizione del margine utile")
        st.caption(
            "Le percentuali vengono applicate al ricavo netto: vendita meno commissioni, "
            "costo di acquisto e costi extra. Il totale deve essere 100%."
        )
        pct_col1, pct_col2 = st.columns(2)
        our_profit_pct = pct_col1.number_input(
            "% nostro guadagno sul margine utile",
            min_value=0.0,
            max_value=100.0,
            value=35.0,
            step=1.0,
            format="%.2f",
        )
        partner_profit_pct = pct_col2.number_input(
            "% guadagno del partner Seller",
            min_value=0.0,
            max_value=100.0,
            value=65.0,
            step=1.0,
            format="%.2f",
        )
        submit = st.form_submit_button("Registra Seller", type="primary")
    if submit:
        if not name.strip():
            st.error("Inserisci il nome del Seller.")
        elif abs((our_profit_pct + partner_profit_pct) - 100.0) > 0.01:
            st.error("La nostra percentuale e quella del partner devono totalizzare 100%.")
        else:
            try:
                tenant_id = current_tenant_id()
                if tenant_id > 0:
                    assert_resource_capacity(tenant_id, "max_sellers", increment=1)
                execute(
                    """INSERT INTO sellers(
                        name,legal_name,email,our_profit_pct,partner_profit_pct,created_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        name.strip(),
                        legal.strip(),
                        email.strip(),
                        float(our_profit_pct),
                        float(partner_profit_pct),
                        now_iso(),
                    ),
                )
                st.success("Seller registrato.")
                st.rerun()
            except Exception as exc:
                st.error(f"Impossibile registrare il Seller: {exc}")

with tab2:
    all_sellers = sellers(False)
    if not all_sellers:
        st.info("Registra prima un Seller.")
    else:
        seller_map = {item["name"]: item["id"] for item in all_sellers}
        seller_name = st.selectbox("Seller", list(seller_map), key="account_seller")
        marketplace = st.selectbox(
            "Marketplace",
            ["Kaufland", "MediaWorld", "MediaMarkt", "Fnac", "Worten", "BigBang", "Altro"],
        )
        account_name = st.text_input("Nome account", value=f"{marketplace} principale")
        st.caption("Le credenziali vengono cifrate prima del salvataggio.")
        if marketplace == "Kaufland":
            client_key = st.text_input("Client Key", type="password")
            secret_key = st.text_input("Secret Key", type="password")
            credentials = {
                "client_key": client_key.strip(),
                "secret_key": secret_key.strip(),
            }
        elif marketplace == "Worten":
            api_url = st.text_input("URL API Worten", value=DEFAULT_API_URL)
            api_key = st.text_input("API Key Mirakl", type="password")
            shop_id = st.text_input("Shop ID Portogallo")
            credentials = {
                "api_url": api_url.strip(),
                "api_key": api_key.strip(),
                "shop_id": shop_id.strip(),
                "country": "pt",
            }
            if st.button("Verifica credenziali Worten", key="validate_new_worten"):
                check = validate_worten(api_key, shop_id, api_url)
                if check["ok"]:
                    st.success(check["message"])
                else:
                    st.error(f"{check['message']} (HTTP {check['status'] or '—'})")
        else:
            api_key = st.text_input("API Key / Token", type="password")
            shop_id = st.text_input("Shop ID / Account ID")
            credentials = {"api_key": api_key.strip(), "shop_id": shop_id.strip()}

        if st.button("Salva account marketplace", type="primary"):
            missing_credentials = marketplace == "Worten" and (
                not credentials.get("api_key") or not credentials.get("shop_id")
            )
            if not account_name.strip() or not any(credentials.values()) or missing_credentials:
                st.error("Compila nome account e credenziali.")
            else:
                try:
                    selected_seller_id = int(seller_map[seller_name])
                    seller_tenant_id = tenant_for_seller(selected_seller_id)
                    if seller_tenant_id > 0:
                        assert_marketplace_capacity(seller_tenant_id, marketplace)
                    execute(
                        """INSERT INTO marketplace_accounts
                        (seller_id,marketplace,account_name,credentials_encrypted,created_at)
                        VALUES(?,?,?,?,?)""",
                        (
                            seller_map[seller_name],
                            marketplace.lower(),
                            account_name.strip(),
                            encrypt_dict(credentials),
                            now_iso(),
                        ),
                    )
                    st.success("Account marketplace salvato.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

        accounts = rows(
            """SELECT * FROM marketplace_accounts
            WHERE seller_id=? ORDER BY marketplace,account_name""",
            (seller_map[seller_name],),
        )
        if accounts:
            st.subheader("Account configurati")
            display = []
            for account in accounts:
                try:
                    credentials_saved = decrypt_dict(account["credentials_encrypted"])
                except Exception:
                    credentials_saved = {}
                secret = credentials_saved.get("client_key") or credentials_saved.get("api_key") or ""
                display.append(
                    {
                        "ID": account["id"],
                        "Marketplace": account["marketplace"],
                        "Account": account["account_name"],
                        "Chiave": masked(secret),
                        "Attivo": bool(account["active"]),
                    }
                )
            st.dataframe(display, use_container_width=True, hide_index=True)

            worten_accounts = [account for account in accounts if account["marketplace"] == "worten"]
            if worten_accounts:
                st.subheader("Verifica account Worten salvato")
                worten_map = {
                    f"{account['account_name']} · ID {account['id']}": account
                    for account in worten_accounts
                }
                worten_label = st.selectbox(
                    "Account Worten da verificare",
                    list(worten_map),
                    key="saved_worten_check",
                )
                if st.button("Prova connessione Worten", key="validate_saved_worten"):
                    try:
                        saved_credentials = decrypt_dict(
                            worten_map[worten_label]["credentials_encrypted"]
                        )
                        check = validate_worten(
                            saved_credentials.get("api_key", ""),
                            saved_credentials.get("shop_id", ""),
                            saved_credentials.get("api_url", DEFAULT_API_URL),
                        )
                        if check["ok"]:
                            extra = (
                                ""
                                if check.get("offers_visible") is None
                                else f" Offerte rilevate: {check['offers_visible']}."
                            )
                            st.success(check["message"] + extra)
                        else:
                            st.error(f"{check['message']} (HTTP {check['status'] or '—'})")
                    except Exception as exc:
                        st.error(f"Impossibile verificare l'account: {exc}")

            st.divider()
            st.subheader("Elimina account marketplace")
            account_map = {
                f"{account['marketplace'].title()} · {account['account_name']} · ID {account['id']}": account
                for account in accounts
            }
            account_label = st.selectbox(
                "Account da eliminare", list(account_map), key="delete_account"
            )
            account = account_map[account_label]
            confirm_account = st.text_input(
                "Digita ELIMINA per confermare", key="confirm_delete_account"
            )
            if st.button("Elimina account marketplace", type="secondary"):
                if confirm_account != "ELIMINA":
                    st.error("Conferma non valida: digita ELIMINA.")
                elif delete_marketplace_account(account["id"], seller_map[seller_name]):
                    st.success("Account marketplace eliminato.")
                    st.rerun()
                else:
                    st.error("Account non trovato o non appartenente al Seller.")

with tab3:
    data = sellers(False)
    if data:
        st.dataframe(
            [
                {
                    "ID": item["id"],
                    "Nome": item["name"],
                    "Ragione sociale": item["legal_name"],
                    "Email": item["email"],
                    "Nostra quota %": float(item.get("our_profit_pct") or 0),
                    "Quota partner %": float(item.get("partner_profit_pct") or 0),
                    "Attivo": bool(item["active"]),
                }
                for item in data
            ],
            use_container_width=True,
            hide_index=True,
        )
        seller_map = {f"{item['name']} · ID {item['id']}": item for item in data}
        chosen = st.selectbox("Seller da modificare", list(seller_map))
        selected = seller_map[chosen]

        active = st.checkbox("Seller attivo", value=bool(selected["active"]))
        st.markdown("#### Ripartizione del margine utile generato")
        st.caption(
            "La ripartizione si applica al ricavo netto di ogni ordine. "
            "Vendite, commissioni, acquisti e margine originale non vengono modificati."
        )
        current_our, current_partner = normalized_percentages(
            selected.get("our_profit_pct"), selected.get("partner_profit_pct")
        )
        pct_col1, pct_col2 = st.columns(2)
        our_profit_pct = pct_col1.number_input(
            "% nostro guadagno sul margine utile",
            min_value=0.0,
            max_value=100.0,
            value=float(current_our),
            step=1.0,
            format="%.2f",
            key=f"seller_our_profit_{selected['id']}",
        )
        partner_profit_pct = pct_col2.number_input(
            f"% guadagno di {selected['name']}",
            min_value=0.0,
            max_value=100.0,
            value=float(current_partner),
            step=1.0,
            format="%.2f",
            key=f"seller_partner_profit_{selected['id']}",
        )
        total_pct = our_profit_pct + partner_profit_pct
        if abs(total_pct - 100.0) <= 0.01:
            st.success(f"Ripartizione valida: {our_profit_pct:.2f}% + {partner_profit_pct:.2f}% = 100%")
        else:
            st.error(f"Ripartizione non valida: il totale è {total_pct:.2f}%. Deve essere 100%.")

        if st.button("Aggiorna Seller e ripartizione", type="primary"):
            if abs(total_pct - 100.0) > 0.01:
                st.error("Correggi le percentuali: devono totalizzare 100%.")
            else:
                execute(
                    """UPDATE sellers SET
                    active=?,our_profit_pct=?,partner_profit_pct=? WHERE id=?""",
                    (
                        int(active),
                        float(our_profit_pct),
                        float(partner_profit_pct),
                        int(selected["id"]),
                    ),
                )
                st.success("Seller e ripartizione aggiornati.")
                st.rerun()

        st.divider()
        st.subheader("Elimina Seller")
        st.warning(
            "Verranno eliminati anche account marketplace, fornitori, listini, viste, "
            "regole e storico appartenenti al Seller."
        )
        expected = chosen.split(" · ID")[0]
        confirm_seller = st.text_input(
            f'Digita il nome esatto "{expected}" per confermare',
            key="confirm_delete_seller",
        )
        if st.button("Elimina definitivamente il Seller", type="secondary"):
            if confirm_seller != expected:
                st.error("Il nome inserito non corrisponde.")
            elif delete_seller(int(selected["id"])):
                st.session_state.pop("active_seller_id", None)
                st.success("Seller e relativi dati eliminati.")
                st.rerun()
            else:
                st.error("Seller non trovato.")


with tab4:
    st.subheader("Servizi esterni del Seller")
    st.caption(
        "Le integrazioni logistiche sono configurate per singolo Seller. "
        "Le chiavi API vengono cifrate con la stessa chiave master usata per gli account marketplace."
    )
    service_sellers = sellers(False)
    if not service_sellers:
        st.info("Registra prima un Seller.")
    else:
        service_map = {f"{item['name']} · ID {item['id']}": item for item in service_sellers}
        service_label = st.selectbox(
            "Seller", list(service_map), key="external_service_seller"
        )
        service_seller = service_map[service_label]
        service_seller_id = int(service_seller["id"])
        integration = packlink_integration_for_seller(service_seller_id, include_inactive=True)

        with st.container(border=True):
            st.markdown("### Packlink PRO")
            st.caption(
                "Inserisci la chiave API generata da Packlink PRO. Prima dell'attivazione "
                "Marketplace Hub esegue una chiamata di controllo all'account Packlink."
            )
            if integration:
                try:
                    existing_credentials = packlink_credentials(integration)
                except Exception:
                    existing_credentials = {}
                existing_key = str(existing_credentials.get("api_key") or "")
                status = str(integration.get("connection_status") or "").lower()
                status_text = "Connesso" if status == "connected" else ("Errore" if status == "error" else "Da verificare")
                s1, s2, s3 = st.columns(3)
                s1.metric("Stato", status_text)
                s2.metric("Servizio", "Attivo" if bool(integration.get("active")) else "Disattivato")
                s3.metric("API key", masked(existing_key))
                if integration.get("last_checked_at"):
                    st.caption(f"Ultimo controllo: {integration['last_checked_at']}")
                if integration.get("last_error"):
                    st.error(str(integration.get("last_error")))
            else:
                existing_key = ""
                st.info("Packlink PRO non è ancora configurato per questo Seller.")

            api_key_packlink = st.text_input(
                "API key Packlink PRO",
                type="password",
                placeholder=(
                    "Lascia vuoto per mantenere la chiave già salvata"
                    if existing_key else "Incolla la chiave API Packlink PRO"
                ),
                key=f"packlink_api_key_{service_seller_id}",
            )
            effective_key = api_key_packlink.strip() or existing_key

            verify_col, toggle_col, delete_col = st.columns([2, 1, 1])
            if verify_col.button(
                "Verifica e attiva Packlink PRO",
                type="primary",
                use_container_width=True,
                disabled=not bool(effective_key),
                key=f"packlink_verify_{service_seller_id}",
            ):
                with st.spinner("Verifica della chiave API Packlink PRO…"):
                    check = PacklinkClient(effective_key).validate()
                if check["ok"]:
                    activate_packlink(
                        service_seller_id, effective_key, client_data=check.get("client") or {}
                    )
                    st.success(
                        "Packlink PRO verificato e attivato. Ora puoi sincronizzare le "
                        "spedizioni dalla pagina Packlink PRO."
                    )
                    st.rerun()
                else:
                    if integration:
                        update_packlink_connection_status(
                            service_seller_id, ok=False, error=check.get("message") or "Verifica fallita"
                        )
                    st.error(
                        f"Connessione Packlink PRO non verificata: {check.get('message')} "
                        f"(HTTP {check.get('status') or '—'}). La nuova chiave non è stata attivata."
                    )

            if integration:
                if toggle_col.button(
                    "Disattiva" if bool(integration.get("active")) else "Riattiva",
                    use_container_width=True,
                    key=f"packlink_toggle_{service_seller_id}",
                ):
                    if bool(integration.get("active")):
                        set_packlink_active(service_seller_id, False)
                        st.success("Packlink PRO disattivato per questo Seller.")
                        st.rerun()
                    else:
                        check = PacklinkClient(existing_key).validate() if existing_key else {"ok": False, "message": "API key mancante"}
                        if check.get("ok"):
                            activate_packlink(
                                service_seller_id, existing_key, client_data=check.get("client") or {}
                            )
                            st.success("Packlink PRO riattivato dopo il controllo della connessione.")
                            st.rerun()
                        else:
                            st.error(f"Riattivazione bloccata: {check.get('message')}")

                confirm_delete = st.checkbox(
                    "Conferma rimozione", key=f"packlink_delete_confirm_{service_seller_id}"
                )
                if delete_col.button(
                    "Rimuovi",
                    use_container_width=True,
                    disabled=not confirm_delete,
                    key=f"packlink_delete_{service_seller_id}",
                ):
                    delete_packlink(service_seller_id)
                    st.success("Configurazione Packlink PRO rimossa. Lo storico spedizioni resta nel database.")
                    st.rerun()

            st.info(
                "Il software è ancora locale: in questa fase la sincronizzazione Packlink viene "
                "avviata dal programma (polling). Quando Marketplace Hub sarà online potremo "
                "aggiungere anche gli aggiornamenti automatici via webhook senza cambiare gli abbinamenti."
            )
