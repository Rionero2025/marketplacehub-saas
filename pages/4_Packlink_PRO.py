from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Mapping
from pathlib import Path
import shutil
import importlib
import sys

import pandas as pd
import streamlit as st

from services.accounting import accounting_rows, ensure_schema as ensure_accounting_schema
from services.db import rows

# v217: quando un hotfix sostituisce un file di servizio mentre Streamlit è
# ancora aperto, Python può avere in sys.modules la vecchia versione del modulo.
# Prima di importare i simboli richiesti proviamo quindi un reload esplicito.
# Questo evita ImportError dopo un aggiornamento corretto dei file e rende la
# pagina auto-riparante al primo rerun.
def _repair_release_file(relative_path: str) -> bool:
    """Restore a critical application file from the bundled release payload.

    This is intentionally executed before importing Packlink services so an
    installation with a new page but an older service self-heals without a
    separate installer/update step.
    """
    root = Path(__file__).resolve().parents[1]
    live = root / relative_path
    payload = root / "_release_payload" / relative_path
    if not payload.is_file():
        return False
    try:
        source_hash = hashlib.sha256(payload.read_bytes()).hexdigest()
        current_hash = hashlib.sha256(live.read_bytes()).hexdigest() if live.is_file() else ""
        if source_hash == current_hash:
            return False
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(payload, live)
        importlib.invalidate_caches()
        return True
    except Exception:
        return False


_repaired_order_service = _repair_release_file("services/cecotec_orders.py")
_repaired_packlink_service = _repair_release_file("services/packlink.py")
_repaired_packlink_csv_service = _repair_release_file("services/packlink_csv.py")

import services.cecotec_orders as _order_service
if _repaired_order_service:
    try:
        _order_service = importlib.reload(_order_service)
    except Exception:
        pass

_REQUIRED_ORDER_SERVICE_SYMBOLS = (
    "cached_orders",
    "cached_order_cache_info",
    "clean_text",
    "delete_cached_range",
    "ensure_schema",
    "fetch_kaufland_orders",
    "fetch_kaufland_orders_by_ids",
    "fetch_worten_orders",
    "fetch_worten_orders_by_ids",
    "marketplace_status_label",
    "upsert_order_cache",
)
_missing_order_symbols = [
    name for name in _REQUIRED_ORDER_SERVICE_SYMBOLS
    if not hasattr(_order_service, name)
]
if _missing_order_symbols:
    try:
        _order_service = importlib.reload(_order_service)
    except Exception:
        pass
    _missing_order_symbols = [
        name for name in _REQUIRED_ORDER_SERVICE_SYMBOLS
        if not hasattr(_order_service, name)
    ]
if _missing_order_symbols:
    st.error(
        "Installazione ordini/Packlink incompleta: services\\cecotec_orders.py "
        "non è allineato alla pagina Packlink v255."
    )
    st.code(
        "File servizio caricato: " + str(getattr(_order_service, "__file__", ""))
        + "\nFunzioni mancanti: " + ", ".join(_missing_order_symbols)
        + "\nChiudi Marketplace Hub e avvia il nuovo pacchetto con AVVIA_WINDOWS.bat: la sincronizzazione è automatica.",
        language="text",
    )
    st.stop()

cached_orders = _order_service.cached_orders
cached_order_cache_info = _order_service.cached_order_cache_info
clean_text = _order_service.clean_text
delete_cached_range = _order_service.delete_cached_range
ensure_order_cache_schema = _order_service.ensure_schema
fetch_kaufland_orders = _order_service.fetch_kaufland_orders
fetch_kaufland_orders_by_ids = _order_service.fetch_kaufland_orders_by_ids
fetch_worten_orders = _order_service.fetch_worten_orders
fetch_worten_orders_by_ids = _order_service.fetch_worten_orders_by_ids
marketplace_status_label = _order_service.marketplace_status_label
upsert_order_cache = _order_service.upsert_order_cache

# Stessa protezione per services/packlink.py: dopo una sostituzione del file
# ricarichiamo il modulo se la vecchia istanza in memoria non espone i simboli
# richiesti dalla pagina corrente.
import services.packlink as _packlink_service
if _repaired_packlink_service:
    try:
        _packlink_service = importlib.reload(_packlink_service)
    except Exception:
        pass

_REQUIRED_PACKLINK_SERVICE_SYMBOLS = (
    "load_packlink_weight_catalog",
    "packlink_weight_catalog_signature",
    "resolve_packlink_order_weight",
    "saved_package_profiles",
    "save_package_profile",
    "remembered_package_for_order",
    "remembered_packages_for_orders",
    "remember_package_for_order",
    "last_order_draft_configuration",
    "PACKLINK_SERVICE_VERSION",
    "ensure_packlink_integration_registered",
)
_missing_packlink_symbols = [
    name for name in _REQUIRED_PACKLINK_SERVICE_SYMBOLS
    if not hasattr(_packlink_service, name)
]
if _missing_packlink_symbols:
    try:
        _packlink_service = importlib.reload(_packlink_service)
    except Exception:
        pass
    _missing_packlink_symbols = [
        name for name in _REQUIRED_PACKLINK_SERVICE_SYMBOLS
        if not hasattr(_packlink_service, name)
    ]
if _missing_packlink_symbols:
    st.error(
        "Installazione Packlink incompleta: services\\packlink.py non è "
        "allineato alla pagina Packlink v255."
    )
    st.code(
        "File servizio caricato: " + str(getattr(_packlink_service, "__file__", ""))
        + "\nFunzioni mancanti: " + ", ".join(_missing_packlink_symbols)
        + "\nChiudi Marketplace Hub e avvia il nuovo pacchetto con AVVIA_WINDOWS.bat: la sincronizzazione è automatica.",
        language="text",
    )
    st.stop()

_packlink_service_version = int(getattr(_packlink_service, "PACKLINK_SERVICE_VERSION", 0) or 0)
PACKLINK_REQUIRED_SERVICE_VERSION = 255
if _packlink_service_version < PACKLINK_REQUIRED_SERVICE_VERSION:
    if _repair_release_file("services/packlink.py"):
        try:
            _packlink_service = importlib.reload(_packlink_service)
            _packlink_service_version = int(getattr(_packlink_service, "PACKLINK_SERVICE_VERSION", 0) or 0)
        except Exception:
            pass
if _packlink_service_version < PACKLINK_REQUIRED_SERVICE_VERSION:
    st.error(
        f"Installazione Packlink non allineata: la pagina richiede services\\packlink.py v{PACKLINK_REQUIRED_SERVICE_VERSION} "
        f"ma è caricata la v{_packlink_service_version or 'sconosciuta'}."
    )
    st.code(
        "File servizio caricato: " + str(getattr(_packlink_service, "__file__", ""))
        + "\nChiudi Marketplace Hub e avvia il nuovo pacchetto con AVVIA_WINDOWS.bat: la sincronizzazione è automatica.",
        language="text",
    )
    st.stop()

from services.packlink import (
    PacklinkAPIError,
    PacklinkClient,
    build_packlink_draft_payload,
    packlink_draft_diagnostic,
    packlink_ready_for_payment_validation,
    packlink_destination_address,
    validate_packlink_destination_against_order,
    match_packlink_warehouse_id,
    cached_shipments,
    ensure_schema,
    group_marketplace_orders,
    integration_credentials,
    integration_for_seller,
    integration_settings,
    ensure_packlink_integration_registered,
    load_packlink_weight_catalog,
    order_declared_value,
    order_drafts,
    last_order_draft_configuration,
    package_payload,
    packlink_package_signature,
    packlink_weight_catalog_signature,
    remembered_package_for_order,
    remembered_packages_for_orders,
    remember_package_for_order,
    saved_package_profiles,
    save_package_profile,
    save_order_draft,
    sender_address_for_packlink,
    sender_addresses,
    register_sender_address,
    resolve_packlink_order_weight,
    update_sender_address,
    set_default_sender_address,
    delete_sender_address,
    sync_shipments,
    update_connection_status,
    update_integration_settings,
)
# Generatore CSV ufficiale Packlink PRO. Viene auto-riparato come gli altri
# file critici, così l'utente non deve eseguire installazioni/aggiornamenti separati.
try:
    import services.packlink_csv as _packlink_csv_service
    if _repaired_packlink_csv_service:
        _packlink_csv_service = importlib.reload(_packlink_csv_service)
except Exception as exc:
    st.error(f"Generatore CSV Packlink non disponibile: {exc}")
    st.stop()

_REQUIRED_PACKLINK_CSV_SYMBOLS = (
    "PACKLINK_CSV_HEADERS", "PACKLINK_CSV_VERSION", "build_packlink_csv",
    "normalize_packlink_postal_code", "packlink_postal_format_hint",
)
_missing_packlink_csv_symbols = [
    name for name in _REQUIRED_PACKLINK_CSV_SYMBOLS
    if not hasattr(_packlink_csv_service, name)
]
if _missing_packlink_csv_symbols:
    st.error(
        "Installazione CSV Packlink incompleta: services\\packlink_csv.py non contiene le funzioni base richieste."
    )
    st.code(
        "File servizio caricato: " + str(getattr(_packlink_csv_service, "__file__", ""))
        + "\nFunzioni mancanti: " + ", ".join(_missing_packlink_csv_symbols),
        language="text",
    )
    st.stop()

PACKLINK_CSV_HEADERS = _packlink_csv_service.PACKLINK_CSV_HEADERS
PACKLINK_CSV_VERSION = int(getattr(_packlink_csv_service, "PACKLINK_CSV_VERSION", 0) or 0)
# v271: il generatore CSV v253+ ha già lo stesso tracciato ufficiale e la stessa
# validazione usati dalla generazione automatica. La v270 aggiungeva soltanto
# choose_best_packlink_service; non blocchiamo quindi l'intera pagina se su un PC
# è rimasto temporaneamente il servizio v253/v269. Il selettore tariffa ha un
# fallback locale qui sotto e il launcher continuerà comunque a riallineare il file.
PACKLINK_REQUIRED_CSV_VERSION = 253
if PACKLINK_CSV_VERSION < PACKLINK_REQUIRED_CSV_VERSION:
    st.error(
        f"Installazione CSV Packlink troppo vecchia: la pagina richiede almeno services\\packlink_csv.py "
        f"v{PACKLINK_REQUIRED_CSV_VERSION} ma è caricata la v{PACKLINK_CSV_VERSION or 'sconosciuta'}."
    )
    st.stop()
build_packlink_csv = _packlink_csv_service.build_packlink_csv
normalize_packlink_postal_code = _packlink_csv_service.normalize_packlink_postal_code
packlink_postal_format_hint = _packlink_csv_service.packlink_postal_format_hint

def _choose_best_packlink_service_compat(services):
    """Compatibilità v271 per installazioni miste v269/v270.

    La vecchia services/packlink_csv.py è pienamente compatibile con il CSV
    ufficiale ma non esponeva ancora il piccolo helper di ranking introdotto
    dalla v270. Questo fallback evita di bloccare Packlink e sceglie la tariffa
    positiva più economica con ordinamento deterministico.
    """
    ranked = []
    for raw in services or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            price = float(str(raw.get("price")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        item = dict(raw)
        ranked.append((
            price,
            clean_text(item.get("carrier")).casefold(),
            clean_text(item.get("service")).casefold(),
            clean_text(item.get("id")).casefold(),
            item,
        ))
    if not ranked:
        return None
    ranked.sort(key=lambda value: value[:4])
    return dict(ranked[0][4])

choose_best_packlink_service = getattr(
    _packlink_csv_service,
    "choose_best_packlink_service",
    _choose_best_packlink_service_compat,
)

from services.packlink_order_import import (
    SUPPORTED_ORDER_DOCUMENT_EXTENSIONS,
    match_order_documents,
)
from services.security import decrypt_dict
from services.session import bootstrap, seller_selector

try:
    from st_aggrid import AgGrid, DataReturnMode, GridOptionsBuilder, GridUpdateMode
except Exception:  # pragma: no cover
    AgGrid = None
    DataReturnMode = GridOptionsBuilder = GridUpdateMode = None


@st.cache_data(show_spinner=False, ttl=3600)
def _packlink_cached_grouped_orders(
    seller_id: int,
    account_id: int,
    marketplace: str,
    account_name: str,
    cache_token: str,
) -> list[dict[str, Any]]:
    """Return persistent cached orders without re-reading/regrouping every rerun.

    ``cache_token`` contains row count + latest sync timestamp. It invalidates the
    Streamlit cache automatically after an order synchronization, while tariff
    choices, filters and widgets can rerun the page at memory speed.
    """
    _ = cache_token
    records = cached_orders(seller_id, account_id, marketplace)
    return group_marketplace_orders(
        records,
        account_id=account_id,
        marketplace=marketplace,
        account_name=account_name,
    )


bootstrap()
ensure_order_cache_schema()
ensure_accounting_schema()
ensure_schema()

st.title("Packlink PRO")
st.caption(
    "Scarica gli ordini dei marketplace del Seller, seleziona quelli da spedire senza "
    "refresh a ogni click, confronta le tariffe Packlink PRO e crea la spedizione pronta per pagamento della "
    "spedizione scelta. Il pagamento e la generazione definitiva dell'etichetta restano "
    "nel pannello Packlink PRO."
)

seller_id = seller_selector()
if seller_id is None:
    st.stop()

integration = integration_for_seller(seller_id, include_inactive=True)
if not integration:
    st.warning(
        "Packlink PRO non è configurato per questo Seller. Apri Gestione Seller → "
        "Servizi e integrazioni, inserisci la API key e completa la verifica."
    )
    st.stop()
if not bool(integration.get("active")):
    st.warning("Packlink PRO è configurato ma disattivato per questo Seller.")
    st.stop()

try:
    packlink_creds = integration_credentials(integration)
    packlink_api_key = clean_text(packlink_creds.get("api_key"))
except Exception as exc:
    st.error(f"Impossibile leggere le credenziali Packlink PRO: {exc}")
    st.stop()
if not packlink_api_key:
    st.error("La configurazione Packlink PRO non contiene una API key valida.")
    st.stop()

client = PacklinkClient(packlink_api_key, seller_id=seller_id)
# v234: Marketplace Hub usa direttamente la API key Packlink PRO, come nelle
# release che già creavano le bozze. Nessuna registrazione negozio obbligatoria
# viene eseguita all'apertura della pagina o prima di POST /v1/shipments.
integration = integration_for_seller(seller_id, include_inactive=True) or integration
settings = integration_settings(integration)
marketplace_labels = {"kaufland": "Kaufland", "worten": "Worten"}

# -----------------------------------------------------------------------------
# Connessione e configurazione Packlink (magazzino/pacco)
# -----------------------------------------------------------------------------
st.markdown("### 1. Configurazione Packlink PRO")
conn_col, profile_col = st.columns([1, 2])
connection_ok = clean_text(integration.get("connection_status")).lower() == "connected"
conn_col.metric("Connessione API", "Verificata" if connection_ok else "Da ricontrollare")
if conn_col.button("Verifica connessione", use_container_width=True, key=f"packlink_verify_{seller_id}"):
    with st.spinner("Verifica della API key Packlink PRO…"):
        check = client.validate()
    update_connection_status(seller_id, ok=bool(check.get("ok")), error=check.get("message") or "")
    if check.get("ok"):
        st.success("Connessione Packlink PRO verificata.")
        st.rerun()
    st.error(f"Connessione Packlink non valida: {check.get('message')}")

profile_cache_key = f"packlink_profile_v209_{seller_id}"
if profile_cache_key not in st.session_state:
    try:
        with st.spinner("Lettura magazzini e pacchi configurati in Packlink PRO…"):
            st.session_state[profile_cache_key] = {
                "warehouses": client.warehouses(),
                "parcels": client.parcels(),
                "error": "",
            }
    except Exception as exc:
        st.session_state[profile_cache_key] = {"warehouses": [], "parcels": [], "error": str(exc)}

profile = st.session_state.get(profile_cache_key, {})
warehouses = list(profile.get("warehouses") or [])
parcels = list(profile.get("parcels") or [])
if profile.get("error"):
    profile_col.warning(
        "Non sono riuscito a leggere automaticamente magazzini/pacchi Packlink. "
        f"Puoi usare i dati manuali qui sotto. Dettaglio: {profile['error']}"
    )
if profile_col.button("Ricarica magazzini e pacchi Packlink", use_container_width=True):
    st.session_state.pop(profile_cache_key, None)
    st.rerun()

saved_defaults = settings.get("shipping_defaults") if isinstance(settings.get("shipping_defaults"), Mapping) else {}

# -----------------------------------------------------------------------------
# Rubrica indirizzi mittente persistente per Seller
# -----------------------------------------------------------------------------
st.markdown("#### Indirizzo mittente")
st.caption(
    "Registra una o più sedi di partenza. Gli indirizzi restano salvati nel database del Seller; "
    "puoi scegliere quello da usare con il radio button e impostarne uno come predefinito."
)

registered_senders = sender_addresses(seller_id)
sender_by_id = {int(item["id"]): item for item in registered_senders if item.get("id") not in (None, "")}
default_sender_id = next(
    (int(item["id"]) for item in registered_senders if bool(item.get("is_default"))),
    int(registered_senders[0]["id"]) if registered_senders else 0,
)

with st.expander("Registra indirizzo mittente", expanded=not bool(registered_senders)):
    st.caption(
        "L'indirizzo viene memorizzato per questo Seller e sarà riutilizzabile per tutte le "
        "spedizioni Packlink PRO. Puoi registrarne quanti ne servono."
    )
    with st.form(key=f"packlink_sender_register_v209_{seller_id}", border=False):
        r1, r2, r3 = st.columns(3)
        sender_label = r1.text_input("Nome indirizzo *", placeholder="Esempio: Magazzino Napoli")
        sender_contact = r1.text_input("Nome e cognome contatto *")
        sender_company = r1.text_input("Società", placeholder="Facoltativo")
        sender_street1 = r2.text_input("Indirizzo *", placeholder="Via / Piazza e numero civico")
        sender_street2 = r2.text_input("Indirizzo 2", placeholder="Facoltativo")
        sender_zip = r2.text_input("CAP *")
        sender_city = r3.text_input("Città *")
        sender_country = r3.text_input("Paese ISO 2 *", value="IT", max_chars=2).upper()
        sender_phone = r3.text_input("Telefono *")
        sender_email = st.text_input("Email mittente *")
        sender_make_default = st.checkbox(
            "Imposta questo indirizzo come predefinito",
            value=not bool(registered_senders),
        )
        register_clicked = st.form_submit_button(
            "Salva nuovo indirizzo mittente",
            type="primary",
            use_container_width=True,
        )
    if register_clicked:
        try:
            new_sender_id = register_sender_address(
                seller_id,
                {
                    "label": sender_label,
                    "contact_name": sender_contact,
                    "company": sender_company,
                    "street1": sender_street1,
                    "street2": sender_street2,
                    "zip_code": sender_zip,
                    "city": sender_city,
                    "country": sender_country,
                    "phone": sender_phone,
                    "email": sender_email,
                },
                make_default=sender_make_default,
            )
            st.session_state[f"packlink_sender_radio_v209_{seller_id}"] = int(new_sender_id)
            st.success("Indirizzo mittente registrato e memorizzato.")
            st.rerun()
        except Exception as exc:
            st.error(f"Registrazione indirizzo mittente non riuscita: {exc}")

selected_sender_record = None
selected_sender = None
if registered_senders:
    sender_ids = list(sender_by_id)
    try:
        saved_sender_address_id = int(saved_defaults.get("sender_address_id") or 0)
    except (TypeError, ValueError):
        saved_sender_address_id = 0
    current_sender_state = st.session_state.get(f"packlink_sender_radio_v209_{seller_id}")
    if current_sender_state not in sender_by_id:
        current_sender_state = (
            saved_sender_address_id
            if saved_sender_address_id in sender_by_id
            else default_sender_id
        )
    selected_sender_id = st.radio(
        "Scegli l'indirizzo mittente da usare",
        sender_ids,
        index=sender_ids.index(int(current_sender_state)) if int(current_sender_state) in sender_ids else 0,
        format_func=lambda sender_id: (
            f"{sender_by_id[sender_id].get('label') or 'Mittente'} · "
            f"{sender_by_id[sender_id].get('street1') or '—'} · "
            f"{sender_by_id[sender_id].get('zip_code') or '—'} "
            f"{sender_by_id[sender_id].get('city') or ''} · "
            f"{sender_by_id[sender_id].get('country') or '—'}"
            + (" · PREDEFINITO" if bool(sender_by_id[sender_id].get('is_default')) else "")
        ),
        key=f"packlink_sender_radio_v209_{seller_id}",
    )
    selected_sender_record = dict(sender_by_id[int(selected_sender_id)])
    selected_sender = sender_address_for_packlink(selected_sender_record)
    # Memorizza anche l'ultima sede scelta: al prossimo avvio la pagina riparte
    # dallo stesso mittente, senza obbligare l'utente a renderlo predefinito.
    if int(selected_sender_id) != int(saved_sender_address_id or 0):
        persisted_defaults = dict(saved_defaults)
        persisted_defaults["sender_address_id"] = int(selected_sender_id)
        update_integration_settings(seller_id, {"shipping_defaults": persisted_defaults})
        saved_defaults = persisted_defaults

    sender_action1, sender_action2, sender_action3 = st.columns([1, 1, 2])
    if sender_action1.button(
        "Imposta come predefinito",
        use_container_width=True,
        disabled=bool(selected_sender_record.get("is_default")),
        key=f"packlink_sender_default_v209_{seller_id}_{selected_sender_id}",
    ):
        set_default_sender_address(seller_id, int(selected_sender_id))
        st.success("Indirizzo mittente predefinito aggiornato.")
        st.rerun()
    delete_sender_confirm = sender_action2.checkbox(
        "Conferma eliminazione",
        key=f"packlink_sender_delete_confirm_v209_{seller_id}_{selected_sender_id}",
    )
    if sender_action3.button(
        "Elimina indirizzo selezionato",
        use_container_width=True,
        disabled=not delete_sender_confirm,
        key=f"packlink_sender_delete_v209_{seller_id}_{selected_sender_id}",
    ):
        delete_sender_address(seller_id, int(selected_sender_id))
        st.session_state.pop(f"packlink_sender_radio_v209_{seller_id}", None)
        st.success("Indirizzo mittente rimosso dalla rubrica attiva.")
        st.rerun()

    with st.expander("Modifica indirizzo mittente selezionato", expanded=False):
        with st.form(key=f"packlink_sender_edit_v209_{seller_id}_{selected_sender_id}", border=False):
            e1, e2, e3 = st.columns(3)
            edit_label = e1.text_input("Nome indirizzo *", value=clean_text(selected_sender_record.get("label")))
            edit_contact = e1.text_input("Nome e cognome contatto *", value=clean_text(selected_sender_record.get("contact_name")))
            edit_company = e1.text_input("Società", value=clean_text(selected_sender_record.get("company")))
            edit_street1 = e2.text_input("Indirizzo *", value=clean_text(selected_sender_record.get("street1")))
            edit_street2 = e2.text_input("Indirizzo 2", value=clean_text(selected_sender_record.get("street2")))
            edit_zip = e2.text_input("CAP *", value=clean_text(selected_sender_record.get("zip_code")))
            edit_city = e3.text_input("Città *", value=clean_text(selected_sender_record.get("city")))
            edit_country = e3.text_input(
                "Paese ISO 2 *",
                value=clean_text(selected_sender_record.get("country")) or "IT",
                max_chars=2,
            ).upper()
            edit_phone = e3.text_input("Telefono *", value=clean_text(selected_sender_record.get("phone")))
            edit_email = st.text_input("Email mittente *", value=clean_text(selected_sender_record.get("email")))
            edit_clicked = st.form_submit_button("Salva modifiche indirizzo", use_container_width=True)
        if edit_clicked:
            try:
                update_sender_address(
                    seller_id,
                    int(selected_sender_id),
                    {
                        "label": edit_label,
                        "contact_name": edit_contact,
                        "company": edit_company,
                        "street1": edit_street1,
                        "street2": edit_street2,
                        "zip_code": edit_zip,
                        "city": edit_city,
                        "country": edit_country,
                        "phone": edit_phone,
                        "email": edit_email,
                    },
                )
                st.success("Indirizzo mittente aggiornato.")
                st.rerun()
            except Exception as exc:
                st.error(f"Aggiornamento indirizzo non riuscito: {exc}")
else:
    st.warning(
        "Non hai ancora registrato un indirizzo mittente. Puoi comunque usare temporaneamente "
        "un magazzino restituito da Packlink, ma per creare le bozze in modo affidabile registra "
        "qui sopra l'indirizzo completo."
    )

# Compatibilità: finché non esiste un indirizzo locale registrato, consenti l'uso
# temporaneo di un magazzino restituito dall'account Packlink o dei vecchi campi manuali.
if selected_sender is None and warehouses:
    warehouse_map = {
        f"{item.get('name') or 'Magazzino'} · {item.get('zip_code') or 'CAP n/d'} · {item.get('country') or '—'}": item
        for item in warehouses
    }
    warehouse_labels = list(warehouse_map)
    saved_warehouse_id = clean_text(saved_defaults.get("warehouse_id"))
    warehouse_index = next(
        (idx for idx, label in enumerate(warehouse_labels) if clean_text(warehouse_map[label].get("id")) == saved_warehouse_id),
        0,
    )
    selected_warehouse_label = st.selectbox(
        "Magazzino Packlink temporaneo",
        warehouse_labels,
        index=warehouse_index,
        key=f"packlink_warehouse_v209_{seller_id}",
    )
    selected_sender = dict(warehouse_map[selected_warehouse_label])
elif selected_sender is None:
    st.info("Inserisci temporaneamente un mittente oppure registralo nella rubrica sopra.")
    wm1, wm2, wm3 = st.columns(3)
    selected_sender = {
        "id": "",
        "name": wm1.text_input("Mittente / magazzino", value=clean_text(saved_defaults.get("sender_name")) or "Magazzino"),
        "contact_name": wm1.text_input("Nome contatto mittente", value=clean_text(saved_defaults.get("sender_contact"))),
        "company": wm1.text_input("Società mittente", value=clean_text(saved_defaults.get("sender_company"))),
        "street1": wm2.text_input("Indirizzo mittente", value=clean_text(saved_defaults.get("sender_street1"))),
        "street2": wm2.text_input("Indirizzo 2", value=clean_text(saved_defaults.get("sender_street2"))),
        "zip_code": wm2.text_input("CAP mittente", value=clean_text(saved_defaults.get("sender_zip"))),
        "city": wm3.text_input("Città mittente", value=clean_text(saved_defaults.get("sender_city"))),
        "country": wm3.text_input("Paese mittente (ISO 2)", value=clean_text(saved_defaults.get("sender_country")) or "IT").upper(),
        "phone": wm3.text_input("Telefono mittente", value=clean_text(saved_defaults.get("sender_phone"))),
        "email": wm3.text_input("Email mittente", value=clean_text(saved_defaults.get("sender_email"))),
    }

selected_sender_origin_id = ""
selected_sender_for_draft = dict(selected_sender or {})
packlink_warehouse_by_id = {
    clean_text(item.get("id")): dict(item)
    for item in warehouses
    if clean_text(item.get("id"))
}

if selected_sender is not None:
    # Packlink's official OrderService always uses a warehouse returned by
    # GET /v1/clients/warehouses and sends that real id as
    # additional_data.selectedWarehouseId. The dashboard may display the
    # country as a dropdown, but the API Address DTO still carries ISO-2.
    direct_remote_id = clean_text(selected_sender.get("id"))
    if direct_remote_id in packlink_warehouse_by_id:
        selected_sender_origin_id = direct_remote_id
    else:
        # A locally saved Marketplace Hub sender is automatically linked to the
        # corresponding Packlink warehouse when country/postcode/address match.
        saved_links = saved_defaults.get("sender_warehouse_links")
        saved_links = dict(saved_links) if isinstance(saved_links, Mapping) else {}
        local_sender_key = str(int(selected_sender_record.get("id") or 0)) if selected_sender_record else ""
        linked_remote_id = clean_text(saved_links.get(local_sender_key)) if local_sender_key else ""
        linked_candidate = packlink_warehouse_by_id.get(linked_remote_id) if linked_remote_id else None
        if linked_candidate and match_packlink_warehouse_id(selected_sender, [linked_candidate]) == linked_remote_id:
            selected_sender_origin_id = linked_remote_id
        else:
            selected_sender_origin_id = match_packlink_warehouse_id(selected_sender, warehouses)

    if selected_sender_origin_id:
        # Use Packlink's own canonical warehouse fields in ``from`` as well as
        # its real id. This mirrors Packlink's current addDepartureAddress() +
        # addAdditionalData() flow and keeps the PRO country selector valid.
        selected_sender_for_draft = dict(packlink_warehouse_by_id[selected_sender_origin_id])
        # Packlink may omit contact fields from the canonical warehouse object.
        # Keep the official warehouse address/id, but retain the locally saved
        # sender phone/email so they can be used for the v253 recipient fallback.
        if isinstance(selected_sender, Mapping):
            for contact_field in ("phone", "email"):
                if not clean_text(selected_sender_for_draft.get(contact_field)):
                    selected_sender_for_draft[contact_field] = clean_text(
                        selected_sender.get(contact_field)
                    )
        if selected_sender_record is not None:
            st.caption(
                "Mittente collegato all'indirizzo Packlink PRO · "
                f"ID {selected_sender_origin_id} · "
                f"{selected_sender_for_draft.get('zip_code') or '—'} "
                f"{selected_sender_for_draft.get('city') or ''} · "
                f"{selected_sender_for_draft.get('country') or '—'}"
            )
            # Remember a successful automatic association so punctuation/name
            # differences do not force a new match on every session.
            local_sender_key = str(int(selected_sender_record.get("id") or 0))
            current_links = saved_defaults.get("sender_warehouse_links")
            current_links = dict(current_links) if isinstance(current_links, Mapping) else {}
            if clean_text(current_links.get(local_sender_key)) != selected_sender_origin_id:
                current_links[local_sender_key] = selected_sender_origin_id
                persisted_defaults = dict(saved_defaults)
                persisted_defaults["sender_warehouse_links"] = current_links
                persisted_defaults["warehouse_id"] = selected_sender_origin_id
                update_integration_settings(seller_id, {"shipping_defaults": persisted_defaults})
                saved_defaults = persisted_defaults
    elif selected_sender_record is not None and warehouses:
        st.warning(
            "Il mittente salvato in Marketplace Hub non corrisponde ancora a un indirizzo "
            "restituito da Packlink PRO. Per una bozza completa Packlink richiede il vero "
            "warehouse ID del mittente. Seleziona qui una sola volta l'indirizzo Packlink "
            "corrispondente; l'associazione verrà ricordata."
        )
        labels = {
            f"{item.get('name') or 'Indirizzo Packlink'} · {item.get('street1') or '—'} · "
            f"{item.get('zip_code') or '—'} {item.get('city') or ''} · {item.get('country') or '—'}": item
            for item in warehouses if clean_text(item.get("id"))
        }
        if labels:
            chosen_label = st.selectbox(
                "Associa indirizzo Packlink al mittente selezionato",
                list(labels),
                key=f"packlink_sender_remote_link_v229_{seller_id}_{selected_sender_record.get('id')}",
            )
            if st.button(
                "Collega indirizzo Packlink",
                key=f"packlink_sender_remote_link_btn_v229_{seller_id}_{selected_sender_record.get('id')}",
            ):
                chosen_id = clean_text(labels[chosen_label].get("id"))
                current_links = saved_defaults.get("sender_warehouse_links")
                current_links = dict(current_links) if isinstance(current_links, Mapping) else {}
                current_links[str(int(selected_sender_record.get("id") or 0))] = chosen_id
                persisted_defaults = dict(saved_defaults)
                persisted_defaults["sender_warehouse_links"] = current_links
                persisted_defaults["warehouse_id"] = chosen_id
                update_integration_settings(seller_id, {"shipping_defaults": persisted_defaults})
                st.success("Mittente collegato all'indirizzo Packlink PRO. L'associazione è stata salvata.")
                st.rerun()
    elif selected_sender_record is not None:
        st.warning(
            "Packlink PRO non ha restituito alcun indirizzo da clients/warehouses. "
            "La creazione della bozza viene bloccata per evitare spedizioni INCOMPLETE."
        )

# Le dimensioni del pacco non sono più globali. Manteniamo solo un template
# interno per inizializzare i singoli ordini quando non esiste ancora una memoria
# pacco per quei prodotti. Il peso viene sempre risolto ordine per ordine.
saved_parcel_id = clean_text(saved_defaults.get("parcel_id"))
base_parcel_template: dict[str, Any] = {}
if parcels:
    base_parcel_template = next(
        (dict(item) for item in parcels if clean_text(item.get("id")) == saved_parcel_id),
        dict(parcels[0]),
    )
if not base_parcel_template:
    base_parcel_template = {
        "id": "",
        "name": "Pacco iniziale",
        "weight": float(saved_defaults.get("weight") or 1.0),
        "length": float(saved_defaults.get("length") or 10.0),
        "width": float(saved_defaults.get("width") or 10.0),
        "height": float(saved_defaults.get("height") or 10.0),
    }

with st.expander("Parametri API Packlink avanzati", expanded=False):
    st.caption(
        "Normalmente non serve modificarli. Sono disponibili per adeguare l'integrazione "
        "se l'account Packlink richiede identificativi specifici per la creazione bozze."
    )
    draft_api = settings.get("draft_api") if isinstance(settings.get("draft_api"), Mapping) else {}
    a1, a2, a3, a4 = st.columns(4)
    api_user_id = a1.text_input("user_id", value=clean_text(draft_api.get("user_id")))
    api_client_id = a2.text_input("client_id", value=clean_text(draft_api.get("client_id")))
    api_platform = a3.text_input("platform", value=clean_text(draft_api.get("platform")) or "PRO")
    _saved_source = clean_text(draft_api.get("source"))
    if not _saved_source or _saved_source.upper() == "PRO":
        _saved_source = "module_marketplace_hub"
    api_source = a4.text_input("source", value=_saved_source)
    if st.button("Salva parametri API avanzati", key=f"packlink_save_api_profile_{seller_id}"):
        update_integration_settings(
            seller_id,
            {"draft_api": {
                "user_id": api_user_id,
                "client_id": api_client_id,
                "platform": api_platform,
                "source": api_source,
            }},
        )
        st.success("Parametri API Packlink salvati.")
        st.rerun()

# -----------------------------------------------------------------------------
# Marketplace e download ordini
# -----------------------------------------------------------------------------
st.divider()
st.markdown("### 2. Marketplace e ordini")
accounts = rows(
    """SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND active=1 AND marketplace IN ('kaufland','worten')
    ORDER BY marketplace,account_name,id""",
    (seller_id,),
)
if not accounts:
    st.warning("Il Seller non possiede account Kaufland o Worten attivi.")
    st.stop()

account_selection_key = f"packlink_accounts_v207_{seller_id}"
valid_account_ids = [int(item["id"]) for item in accounts]
if account_selection_key not in st.session_state:
    st.session_state[account_selection_key] = list(valid_account_ids)
else:
    st.session_state[account_selection_key] = [
        int(value) for value in st.session_state[account_selection_key] if int(value) in valid_account_ids
    ]
    if len(valid_account_ids) == 1 and not st.session_state[account_selection_key]:
        st.session_state[account_selection_key] = list(valid_account_ids)

selected_account_ids: list[int] = []
account_columns = st.columns(min(4, max(1, len(accounts))))
for index, account in enumerate(accounts):
    account_id = int(account["id"])
    label = f"{marketplace_labels.get(clean_text(account['marketplace']).lower(), clean_text(account['marketplace']).title())} · {account['account_name']}"
    state_key = f"packlink_account_checkbox_v207_{seller_id}_{account_id}"
    if state_key not in st.session_state:
        st.session_state[state_key] = account_id in st.session_state[account_selection_key]
    checked = account_columns[index % len(account_columns)].checkbox(
        label,
        key=state_key,
        disabled=len(accounts) == 1,
    )
    if checked:
        selected_account_ids.append(account_id)
st.session_state[account_selection_key] = selected_account_ids

if not selected_account_ids:
    st.warning("Seleziona almeno un marketplace/account da cui scaricare gli ordini.")
    st.stop()

period1, period2, sync_col = st.columns([1, 1, 1.4])
today = date.today()
orders_from = period1.date_input(
    "Ordini dal",
    value=today - timedelta(days=30),
    max_value=today,
    key=f"packlink_orders_from_v207_{seller_id}",
)
orders_to = period2.date_input(
    "Ordini al",
    value=today,
    min_value=orders_from,
    max_value=today,
    key=f"packlink_orders_to_v207_{seller_id}",
)

account_lookup = {int(item["id"]): item for item in accounts}

# v255: la cache ordini è persistente nel database. Aprire/ricalcolare la pagina
# non deve più interrogare i marketplace. Le API vengono chiamate esclusivamente
# quando l'utente preme il pulsante di aggiornamento.
cache_info_by_account: dict[int, dict[str, Any]] = {}
for account_id in selected_account_ids:
    account = account_lookup[account_id]
    marketplace = clean_text(account.get("marketplace")).lower()
    try:
        cache_info_by_account[account_id] = cached_order_cache_info(
            seller_id, account_id, marketplace
        )
    except Exception:
        cache_info_by_account[account_id] = {
            "row_count": 0, "first_order_date": "",
            "last_order_date": "", "last_synced_at": "",
        }

_cache_rows_total = sum(
    int(info.get("row_count") or 0) for info in cache_info_by_account.values()
)
_last_sync_values = [
    clean_text(info.get("last_synced_at"))
    for info in cache_info_by_account.values()
    if clean_text(info.get("last_synced_at"))
]
_last_sync_text = max(_last_sync_values) if _last_sync_values else "mai"
st.caption(
    f"Memoria ordini persistente: {_cache_rows_total:,} righe salvate · "
    f"ultimo aggiornamento: {_last_sync_text}. "
    "Cambiare filtri, tariffe o altre opzioni della pagina usa questi dati già salvati e non riscarica gli ordini."
)

full_refresh = st.checkbox(
    "Forza riscaricamento completo dell'intervallo (più lento)",
    value=False,
    key=f"packlink_full_order_refresh_v255_{seller_id}",
    help=(
        "Normalmente Marketplace Hub aggiorna solo la parte nuova/recente e, per Kaufland, "
        "gli stati utili alla spedizione. Attiva questa opzione soltanto se vuoi ricostruire "
        "da zero tutto l'intervallo selezionato."
    ),
)

sync_label = (
    "Riscarica intervallo completo"
    if full_refresh else
    "Aggiorna nuovi / da spedire"
)
if sync_col.button(
    sync_label,
    type="primary",
    use_container_width=True,
    key=f"packlink_sync_orders_v255_{seller_id}",
):
    selected_accounts = [account_lookup[account_id] for account_id in selected_account_ids]
    progress = st.progress(0.0)
    status_box = st.empty()
    messages: list[str] = []
    for position, account in enumerate(selected_accounts, 1):
        marketplace = clean_text(account.get("marketplace")).lower()
        account_id = int(account["id"])
        info = cache_info_by_account.get(account_id) or {}

        # Aggiornamento incrementale: ripartiamo dall'ultimo ordine memorizzato
        # con 2 giorni di sovrapposizione, così eventuali modifiche recenti vengono
        # assorbite senza rileggere un mese/anno di storico a ogni click.
        effective_from = orders_from
        if not full_refresh:
            last_order_date = clean_text(info.get("last_order_date"))
            try:
                cached_last = date.fromisoformat(last_order_date[:10])
                effective_from = max(orders_from, cached_last - timedelta(days=2))
            except Exception:
                effective_from = orders_from

        try:
            credentials = decrypt_dict(account["credentials_encrypted"])
            with st.spinner(
                f"Aggiornamento {marketplace_labels.get(marketplace, marketplace.title())} "
                f"dal {effective_from:%d/%m/%Y}…"
            ):
                if marketplace == "kaufland":
                    if full_refresh:
                        fresh = fetch_kaufland_orders(
                            credentials,
                            account_id=account_id,
                            date_from=orders_from,
                            date_to=orders_to,
                        )
                    else:
                        # Packlink ha bisogno soprattutto degli ordini ancora da
                        # spedire. Limitare l'API a open/need_to_be_sent evita di
                        # scorrere migliaia di ordini storici già spediti/resi.
                        fresh = fetch_kaufland_orders(
                            credentials,
                            account_id=account_id,
                            date_from=effective_from,
                            date_to=orders_to,
                            statuses=("open", "need_to_be_sent"),
                            include_manifest=False,
                        )
                else:
                    fresh = fetch_worten_orders(
                        credentials,
                        account_id=account_id,
                        date_from=effective_from if not full_refresh else orders_from,
                        date_to=orders_to,
                    )

                # La modalità normale NON cancella mai lo storico: aggiorna/inserisce
                # soltanto le righe ricevute. La cancellazione dell'intervallo è
                # riservata al refresh completo esplicitamente richiesto.
                if full_refresh:
                    delete_cached_range(
                        seller_id, account_id, marketplace, orders_from, orders_to
                    )
                saved = upsert_order_cache(
                    seller_id, account_id, marketplace, fresh
                )
                # Aggiorna subito il token della cache per mostrare le nuove righe
                # nello stesso rerun, senza dover premere nuovamente il pulsante.
                cache_info_by_account[account_id] = cached_order_cache_info(
                    seller_id, account_id, marketplace
                )
            mode = "completo" if full_refresh else "incrementale"
            messages.append(
                f"{marketplace_labels.get(marketplace)} · {account['account_name']}: "
                f"{saved:,} righe aggiornate ({mode}, dal {effective_from:%d/%m/%Y})"
            )
        except Exception as exc:
            messages.append(
                f"{marketplace_labels.get(marketplace, marketplace.title())} · "
                f"{account['account_name']}: ERRORE {exc}"
            )
        progress.progress(position / len(selected_accounts))
        status_box.caption(f"Account elaborati: {position} di {len(selected_accounts)}")
    st.success("Aggiornamento ordini completato. Gli ordini restano memorizzati nel database.")
    st.code("\n".join(messages), language="text")


all_cached_order_groups: list[dict[str, Any]] = []
all_order_groups: list[dict[str, Any]] = []
accounting_by_account: dict[int, list[dict[str, Any]]] = {}
for account_id in selected_account_ids:
    account = account_lookup[account_id]
    marketplace = clean_text(account.get("marketplace")).lower()
    cache_info = cache_info_by_account.get(account_id) or cached_order_cache_info(
        seller_id, account_id, marketplace
    )
    cache_token = (
        f"{int(cache_info.get('row_count') or 0)}:"
        f"{clean_text(cache_info.get('last_synced_at'))}"
    )
    grouped = _packlink_cached_grouped_orders(
        seller_id, account_id, marketplace,
        clean_text(account.get("account_name")), cache_token,
    )
    for item in grouped:
        item["status_label"] = marketplace_status_label(
            marketplace, item.get("raw_status")
        )
        all_cached_order_groups.append(item)
        raw_date = clean_text(item.get("order_created"))
        try:
            order_date = datetime.fromisoformat(
                raw_date.replace("Z", "+00:00")
            ).date()
        except Exception:
            try:
                order_date = date.fromisoformat(raw_date[:10])
            except Exception:
                order_date = None
        if order_date is None or orders_from <= order_date <= orders_to:
            all_order_groups.append(item)
    try:
        accounting_by_account[account_id] = accounting_rows(
            seller_id, account_id, marketplace,
            date_from=orders_from, date_to=orders_to,
        )
    except Exception:
        accounting_by_account[account_id] = []

if not all_order_groups:
    st.info(
        "Nessun ordine della cache rientra nel periodo selezionato. "
        "Puoi comunque caricare un file con numeri ordine: i match già presenti "
        "nella cache o recuperati direttamente dal marketplace restano selezionabili "
        "e spedibili anche fuori dal periodo visualizzato."
    )

st.info(
    "Regola Packlink: un ordine non viene mai escluso perché è in perdita, ha "
    "margine negativo o costo non calcolabile. Se il numero ordine appartiene al "
    "Seller e viene trovato, può essere abbinato e usato per creare la spedizione."
)

# -----------------------------------------------------------------------------
# Peso reale dai listini del Seller
# -----------------------------------------------------------------------------
# Il peso generico del pacco rimane esclusivamente un fallback. Quando il prodotto
# è riconosciuto in uno dei listini accessibili al Seller, Packlink riceve il peso
# reale del listino moltiplicato per la quantità dell'ordine.
weight_catalog_signature = packlink_weight_catalog_signature(seller_id)
weight_catalog_state_key = f"packlink_weight_catalog_v211_{seller_id}"
weight_catalog_signature_key = f"{weight_catalog_state_key}_signature"
if (
    st.session_state.get(weight_catalog_signature_key) != weight_catalog_signature
    or weight_catalog_state_key not in st.session_state
):
    try:
        with st.spinner("Lettura pesi dai listini prodotti del Seller…"):
            st.session_state[weight_catalog_state_key] = load_packlink_weight_catalog(seller_id)
        st.session_state[weight_catalog_signature_key] = weight_catalog_signature
    except Exception as exc:
        st.session_state[weight_catalog_state_key] = {
            "ean_index": {}, "sku_index": {}, "source_count": 0,
            "weighted_products": 0, "unavailable": [],
        }
        st.session_state[weight_catalog_signature_key] = weight_catalog_signature
        st.warning(f"Lettura pesi listini non riuscita: {exc}. Verrà usato il peso generico dove necessario.")
weight_catalog = st.session_state.get(weight_catalog_state_key) or {}
package_profiles_all = saved_package_profiles(seller_id)
package_profiles_by_id = {int(item["id"]): dict(item) for item in package_profiles_all if item.get("id") not in (None, "")}
remembered_package_by_order = remembered_packages_for_orders(
    seller_id, all_cached_order_groups
)

for item in all_cached_order_groups:
    remembered_package = remembered_package_by_order.get(
        clean_text(item.get("order_key"))
    )
    item["_packlink_remembered_package"] = remembered_package
    if remembered_package and remembered_package.get("id") not in (None, ""):
        package_profiles_by_id.setdefault(int(remembered_package["id"]), dict(remembered_package))
    fallback_weight = float(
        (remembered_package or {}).get("weight")
        or base_parcel_template.get("weight")
        or 1.0
    )
    weight_info = resolve_packlink_order_weight(
        weight_catalog,
        item,
        fallback_weight_kg=fallback_weight,
    )
    item["_packlink_weight_info"] = weight_info
    item["packlink_weight_kg"] = weight_info.get("weight_kg")
    item["packlink_weight_source"] = weight_info.get("source")

st.caption(
    f"Pesi Packlink: {int(weight_catalog.get('weighted_products') or 0):,} prodotti con peso "
    f"indicizzati da {int(weight_catalog.get('source_count') or 0):,} sorgenti listino. "
    "Il peso generico viene usato solo quando il peso del prodotto non è disponibile."
)

# -----------------------------------------------------------------------------
# Selezione automatica da documenti / screenshot
# -----------------------------------------------------------------------------
selection_key = f"packlink_selected_orders_v212_{seller_id}"
st.session_state.setdefault(selection_key, [])

st.markdown("#### Selezione automatica da file")
st.caption(
    "Carica uno o più file contenenti numeri ordine. Marketplace Hub legge testo, Word, PDF, "
    "Excel/CSV e screenshot, confronta i numeri con gli ordini presenti e aggiunge automaticamente "
    "i match alla selezione già memorizzata."
)
uploaded_order_files = st.file_uploader(
    "File con numeri ordine",
    type=[extension.lstrip(".") for extension in sorted(SUPPORTED_ORDER_DOCUMENT_EXTENSIONS)],
    accept_multiple_files=True,
    key=f"packlink_order_documents_v212_{seller_id}",
)
file_match_report_key = f"packlink_order_file_report_v212_{seller_id}"
if st.button(
    "Leggi file e seleziona gli ordini trovati",
    type="primary",
    use_container_width=True,
    disabled=not bool(uploaded_order_files),
    key=f"packlink_order_documents_apply_v216_{seller_id}",
):
    documents = [(file.name, file.getvalue()) for file in uploaded_order_files]
    with st.spinner(
        "Lettura documenti, confronto con la cache e recupero diretto "
        "degli eventuali ordini mancanti…"
    ):
        # Il match usa l'intera cache del Seller, non soltanto il periodo visibile.
        report = match_order_documents(documents, all_cached_order_groups)
        unresolved = [
            clean_text(value)
            for value in report.get("unmatched_candidates", [])
            if clean_text(value)
        ]
        recovered_events: list[dict[str, Any]] = []

        # Se un numero ordine presente nel file non è nella cache, lo cerchiamo
        # direttamente nel marketplace. Nessun controllo di margine/perdita/costo
        # partecipa a questa decisione.
        if unresolved:
            cached_keys = {
                clean_text(item.get("order_key"))
                for item in all_cached_order_groups
                if clean_text(item.get("order_key"))
            }
            for account_id in selected_account_ids:
                account = account_lookup[account_id]
                marketplace = clean_text(account.get("marketplace")).lower()
                credentials = decrypt_dict(account["credentials_encrypted"])
                try:
                    if marketplace == "kaufland":
                        # Gli ID ordine Kaufland sono riferimenti alfanumerici
                        # corti (es. MWBDZL5). Evitiamo di interrogare l'endpoint
                        # per EAN numerici e altri token chiaramente estranei.
                        lookup_ids = [
                            value for value in unresolved
                            if 5 <= len(re.sub(r"[^A-Za-z0-9]", "", value)) <= 16
                            and re.search(r"[A-Za-z]", value)
                        ]
                        fresh = fetch_kaufland_orders_by_ids(
                            credentials,
                            account_id=int(account_id),
                            order_ids=lookup_ids,
                        )
                    elif marketplace == "worten":
                        fresh = fetch_worten_orders_by_ids(
                            credentials,
                            account_id=int(account_id),
                            order_ids=unresolved,
                        )
                    else:
                        fresh = []

                    if fresh:
                        upsert_order_cache(
                            seller_id, int(account_id), marketplace, fresh
                        )
                        recovered_groups = group_marketplace_orders(
                            fresh,
                            account_id=int(account_id),
                            marketplace=marketplace,
                            account_name=clean_text(account.get("account_name")),
                        )
                        for recovered in recovered_groups:
                            recovered["status_label"] = marketplace_status_label(
                                marketplace, recovered.get("raw_status")
                            )
                            remembered_package = remembered_package_for_order(
                                seller_id, recovered
                            )
                            recovered["_packlink_remembered_package"] = remembered_package
                            fallback_weight = float(
                                (remembered_package or {}).get("weight")
                                or base_parcel_template.get("weight")
                                or 1.0
                            )
                            weight_info = resolve_packlink_order_weight(
                                weight_catalog,
                                recovered,
                                fallback_weight_kg=fallback_weight,
                            )
                            recovered["_packlink_weight_info"] = weight_info
                            recovered["packlink_weight_kg"] = weight_info.get("weight_kg")
                            recovered["packlink_weight_source"] = weight_info.get("source")
                            key = clean_text(recovered.get("order_key"))
                            if key and key not in cached_keys:
                                cached_keys.add(key)
                                all_cached_order_groups.append(recovered)
                            recovered_events.append({
                                "Marketplace": marketplace_labels.get(
                                    marketplace, marketplace.title()
                                ),
                                "Account": clean_text(account.get("account_name")),
                                "Ordine": clean_text(recovered.get("order_id")),
                                "Stato": recovered.get("status_label"),
                            })
                except Exception as exc:
                    recovered_events.append({
                        "Marketplace": marketplace_labels.get(
                            marketplace, marketplace.title()
                        ),
                        "Account": clean_text(account.get("account_name")),
                        "Ordine": "—",
                        "Stato": f"Errore recupero API: {exc}",
                    })

        # Ripetiamo il match includendo gli ordini appena recuperati.
        report = match_order_documents(documents, all_cached_order_groups)
        report["recovered_from_marketplace"] = recovered_events

    selected_from_files = {
        clean_text(value)
        for value in report.get("matched_order_keys", [])
        if clean_text(value)
    }
    existing_selection = {
        clean_text(value)
        for value in st.session_state.get(selection_key, [])
        if clean_text(value)
    }
    existing_selection.update(selected_from_files)
    st.session_state[selection_key] = sorted(existing_selection)
    st.session_state[file_match_report_key] = report
    st.rerun()

file_match_report = st.session_state.get(file_match_report_key)
if isinstance(file_match_report, Mapping):
    fm1, fm2, fm3, fm4, fm5 = st.columns(5)
    fm1.metric("File letti", len(file_match_report.get("files") or []))
    fm2.metric("Ordini trovati", len(file_match_report.get("matches") or []))
    fm3.metric(
        "Recuperati via API",
        sum(
            1
            for item in (file_match_report.get("recovered_from_marketplace") or [])
            if clean_text(item.get("Ordine")) not in ("", "—")
        ),
    )
    fm4.metric("Candidati non abbinati", len(file_match_report.get("unmatched_candidates") or []))
    fm5.metric("Errori lettura", len(file_match_report.get("errors") or []))
    if file_match_report.get("matches"):
        with st.expander("Mostra ordini selezionati dal file", expanded=False):
            st.dataframe(
                pd.DataFrame(file_match_report["matches"]),
                use_container_width=True,
                hide_index=True,
            )
    if file_match_report.get("recovered_from_marketplace"):
        with st.expander("Mostra recupero diretto dai marketplace", expanded=False):
            st.dataframe(
                pd.DataFrame(file_match_report["recovered_from_marketplace"]),
                use_container_width=True,
                hide_index=True,
            )
    if file_match_report.get("unmatched_candidates"):
        with st.expander("Numeri/codici presenti nei file ma non abbinati", expanded=False):
            st.write(", ".join(file_match_report["unmatched_candidates"][:200]))
    if file_match_report.get("errors"):
        st.warning("Alcuni file non sono stati letti: " + " | ".join(file_match_report["errors"][:5]))

# -----------------------------------------------------------------------------
# Filtri e selezione persistente, senza rerun a ogni click
# -----------------------------------------------------------------------------
st.markdown("#### Filtri ordini")
order_frame = pd.DataFrame(all_order_groups)
filter1, filter2, filter3, filter4 = st.columns(4)
market_filter_values = sorted(order_frame.get("marketplace", pd.Series(dtype=str)).dropna().astype(str).unique().tolist())
selected_markets = filter1.multiselect(
    "Marketplace",
    market_filter_values,
    default=market_filter_values,
    format_func=lambda value: marketplace_labels.get(value, value.title()),
    key=f"packlink_filter_market_v207_{seller_id}",
)
supplier_values = sorted({
    supplier.strip()
    for value in order_frame.get("supplier", pd.Series(dtype=str)).fillna("").astype(str)
    for supplier in value.split(",")
    if supplier.strip()
})
selected_suppliers = filter2.multiselect(
    "Fornitore",
    supplier_values,
    default=[],
    placeholder="Tutti i fornitori",
    key=f"packlink_filter_supplier_v207_{seller_id}",
)
status_values = sorted(order_frame.get("status_label", pd.Series(dtype=str)).fillna("").astype(str).unique().tolist())
selected_statuses = filter3.multiselect(
    "Stato ordine",
    status_values,
    default=[],
    placeholder="Tutti gli stati",
    key=f"packlink_filter_status_v207_{seller_id}",
)
search_text = filter4.text_input(
    "Cerca",
    placeholder="ordine, cliente, SKU, prodotto, città…",
    key=f"packlink_filter_search_v207_{seller_id}",
).strip().casefold()

visible_orders = list(all_order_groups)
if selected_markets:
    visible_orders = [item for item in visible_orders if item["marketplace"] in selected_markets]
if selected_suppliers:
    supplier_set = {item.casefold() for item in selected_suppliers}
    visible_orders = [
        item for item in visible_orders
        if any(part.strip().casefold() in supplier_set for part in clean_text(item.get("supplier")).split(","))
    ]
if selected_statuses:
    visible_orders = [item for item in visible_orders if item.get("status_label") in selected_statuses]
if search_text:
    visible_orders = [
        item for item in visible_orders
        if search_text in " ".join(
            clean_text(item.get(key))
            for key in (
                "order_id", "customer_name", "supplier", "product_title", "composite_sku",
                "city", "postal_code", "country_code", "account_name",
            )
        ).casefold()
    ]

selected_keys = {clean_text(value) for value in st.session_state.get(selection_key, []) if clean_text(value)}
visible_keys = {clean_text(item.get("order_key")) for item in visible_orders}

sel1, sel2, sel3, sel4 = st.columns([1, 1, 1, 1.5])
if sel1.button("Seleziona tutti filtrati", use_container_width=True):
    selected_keys.update(visible_keys)
    st.session_state[selection_key] = sorted(selected_keys)
    st.rerun()
if sel2.button("Deseleziona filtrati", use_container_width=True):
    selected_keys.difference_update(visible_keys)
    st.session_state[selection_key] = sorted(selected_keys)
    st.rerun()
if sel3.button("Azzera selezione", use_container_width=True):
    st.session_state[selection_key] = []
    st.rerun()
sel4.metric("Ordini selezionati", len(selected_keys))

if visible_orders:
    table = pd.DataFrame([
        {
            "order_key": item["order_key"],
            "Data": item.get("order_created"),
            "Marketplace": f"{marketplace_labels.get(item['marketplace'], item['marketplace'].title())} · {item.get('account_name')}",
            "Ordine": item.get("order_id"),
            "Stato": item.get("status_label"),
            "Fornitore": item.get("supplier"),
            "Prodotti": item.get("product_title"),
            "Q.tà": item.get("quantity"),
            "Peso Packlink kg": item.get("packlink_weight_kg"),
            "Origine peso": item.get("packlink_weight_source"),
            "Cliente": item.get("customer_name"),
            "Indirizzo": item.get("address"),
            "CAP": item.get("postal_code"),
            "Città": item.get("city"),
            "Paese": item.get("country_code"),
        }
        for item in visible_orders
    ])

    if AgGrid is not None:
        builder = GridOptionsBuilder.from_dataframe(table)
        builder.configure_default_column(sortable=True, filter=True, resizable=True, minWidth=90)
        builder.configure_column("order_key", hide=True)
        builder.configure_column(
            "Ordine",
            checkboxSelection=True,
            headerCheckboxSelection=True,
            headerCheckboxSelectionFilteredOnly=True,
            pinned="left",
            minWidth=175,
        )
        builder.configure_column("Marketplace", minWidth=190)
        builder.configure_column("Fornitore", minWidth=150)
        builder.configure_column("Prodotti", minWidth=300)
        builder.configure_column("Peso Packlink kg", minWidth=145)
        builder.configure_column("Origine peso", minWidth=330)
        builder.configure_column("Cliente", minWidth=200)
        builder.configure_column("Indirizzo", minWidth=260)
        builder.configure_selection(selection_mode="multiple", use_checkbox=True)
        builder.configure_grid_options(
            rowMultiSelectWithClick=True,
            suppressRowClickSelection=False,
            enableRangeSelection=True,
            animateRows=False,
            pagination=True,
            paginationPageSize=100,
        )
        preselected = [idx for idx, value in enumerate(table["order_key"].tolist()) if value in selected_keys]
        manual_mode = getattr(GridUpdateMode, "MANUAL", GridUpdateMode.SELECTION_CHANGED)
        grid_response = AgGrid(
            table,
            gridOptions=builder.build(),
            data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
            update_mode=manual_mode,
            pre_selected_rows=preselected,
            height=520,
            fit_columns_on_grid_load=False,
            theme="streamlit",
            key=f"packlink_orders_grid_manual_v207_{seller_id}",
        )
        returned = grid_response.get("selected_rows")
        submitted = False
        returned_keys: set[str] = set()
        if isinstance(returned, pd.DataFrame):
            submitted = not returned.empty
            if "order_key" in returned.columns:
                returned_keys = {clean_text(value) for value in returned["order_key"].tolist() if clean_text(value)}
            elif "Ordine" in returned.columns:
                lookup = {clean_text(row["Ordine"]): clean_text(row["order_key"]) for _, row in table.iterrows()}
                returned_keys = {lookup.get(clean_text(value), "") for value in returned["Ordine"].tolist()}
                returned_keys.discard("")
        elif isinstance(returned, list):
            submitted = bool(returned)
            lookup = {clean_text(row["Ordine"]): clean_text(row["order_key"]) for _, row in table.iterrows()}
            for item in returned:
                if not isinstance(item, Mapping):
                    continue
                key = clean_text(item.get("order_key")) or lookup.get(clean_text(item.get("Ordine")), "")
                if key:
                    returned_keys.add(key)
        if submitted:
            selected_keys = (selected_keys - visible_keys) | returned_keys
            st.session_state[selection_key] = sorted(selected_keys)
        st.caption(
            "Spunta uno o più ordini senza refresh e poi premi **Applica** dentro la tabella. "
            "La selezione resta memorizzata anche se cambi fornitore, ricerca o pagina della griglia."
        )
    else:
        st.warning("st-aggrid non è disponibile: uso la selezione di emergenza Streamlit.")
        fallback = table.copy()
        fallback.insert(0, "Seleziona", fallback["order_key"].isin(selected_keys))
        edited = st.data_editor(
            fallback,
            hide_index=True,
            use_container_width=True,
            height=520,
            disabled=[column for column in fallback.columns if column != "Seleziona"],
            column_config={"Seleziona": st.column_config.CheckboxColumn(default=False), "order_key": None},
            key=f"packlink_orders_fallback_v207_{seller_id}",
        )
        checked = set(edited.loc[edited["Seleziona"], "order_key"].astype(str))
        selected_keys = (selected_keys - visible_keys) | checked
        st.session_state[selection_key] = sorted(selected_keys)
else:
    st.info("Nessun ordine corrisponde ai filtri selezionati.")

# Reload persisted selection after AgGrid's Apply.
selected_keys = {clean_text(value) for value in st.session_state.get(selection_key, []) if clean_text(value)}
all_orders_by_key = {
    clean_text(item.get("order_key")): item
    for item in all_cached_order_groups
    if clean_text(item.get("order_key"))
}
selected_orders = [
    all_orders_by_key[key]
    for key in selected_keys
    if key in all_orders_by_key
]
# v230: single source of truth for the address used at POST time.
# Batch candidates may survive fragment reruns, but their embedded order snapshot
# must never override the current marketplace order displayed on screen.
current_order_by_key = {
    clean_text(item.get("order_key")): dict(item)
    for item in selected_orders
    if clean_text(item.get("order_key"))
}
period_keys = {
    clean_text(item.get("order_key"))
    for item in all_order_groups
    if clean_text(item.get("order_key"))
}
selected_outside_period = [
    item
    for item in selected_orders
    if clean_text(item.get("order_key")) not in period_keys
]
if selected_outside_period:
    st.info(
        f"{len(selected_outside_period)} ordini selezionati (ad esempio tramite file) "
        "sono fuori dal periodo visualizzato, ma restano comunque abbinati e "
        "spedibili con Packlink."
    )
    with st.expander("Mostra ordini selezionati fuori dal periodo", expanded=False):
        st.dataframe(
            pd.DataFrame([
                {
                    "Marketplace": marketplace_labels.get(
                        clean_text(item.get("marketplace")),
                        clean_text(item.get("marketplace")).title(),
                    ),
                    "Account": item.get("account_name"),
                    "Ordine": item.get("order_id"),
                    "Stato": item.get("status_label"),
                    "Cliente": item.get("customer_name"),
                    "Paese": item.get("country_code"),
                }
                for item in selected_outside_period
            ]),
            use_container_width=True,
            hide_index=True,
        )

# Lo storico bozze deve esistere anche quando non è selezionato alcun ordine.
# In v213 veniva inizializzato soltanto nel ramo selected_orders, causando un
# NameError nella sezione 4 quando la selezione era vuota.
created_drafts = order_drafts(seller_id)

# v220: conserva per ogni ordine l'ultima configurazione ESATTA realmente
# inviata a Packlink (pacco + servizio + costo storico).  In caso di
# rigenerazione forzata questa configurazione viene proposta per prima, senza
# ricostruire dimensioni generiche o perdere il costo/servizio precedente.
def _draft_scope_key(value: Mapping[str, Any]) -> tuple[int, str, str]:
    return (
        int(value.get("marketplace_account_id") or 0),
        clean_text(value.get("marketplace")).lower(),
        clean_text(value.get("order_id")).upper(),
    )


def _draft_package_from_row(value: Mapping[str, Any]) -> dict[str, float] | None:
    try:
        raw = value.get("request_json")
        payload = json.loads(raw) if isinstance(raw, str) and raw else {}
        packages = payload.get("packages") if isinstance(payload, Mapping) else None
        if isinstance(packages, list) and packages and isinstance(packages[0], Mapping):
            return package_payload(packages[0])
    except Exception:
        pass
    return None


previous_draft_by_scope: dict[tuple[int, str, str], dict[str, Any]] = {}
for _draft_row in created_drafts:
    _row = dict(_draft_row)
    _row["_previous_package"] = _draft_package_from_row(_row)
    previous_draft_by_scope[_draft_scope_key(_row)] = _row

# -----------------------------------------------------------------------------
# Quotes Packlink, pacco per singolo ordine e scelta manuale servizio
# -----------------------------------------------------------------------------
st.divider()
st.markdown("### 3. Pacco e tariffe Packlink per gli ordini selezionati")
if not selected_orders:
    st.info("Seleziona almeno un ordine nella tabella e premi Applica.")
else:
    st.caption(
        "Peso e dimensioni sono gestiti per ogni singolo ordine. Il peso da listino viene proposto "
        "automaticamente; le dimensioni possono essere inserite/modificate per quel pacco. I pacchi "
        "usati in una bozza riuscita vengono memorizzati e possono essere riutilizzati in futuro."
    )

    sender_quote_signature = hashlib.sha1(
        "|".join((
            clean_text(selected_sender.get("country")),
            clean_text(selected_sender.get("zip_code")),
            clean_text(selected_sender.get("street1")),
            clean_text(selected_sender.get("local_sender_id")),
        )).encode("utf-8")
    ).hexdigest()[:10]
    quotes_key = f"packlink_quotes_v212_{seller_id}_{sender_quote_signature}"
    st.session_state.setdefault(quotes_key, {})
    quotes_state: dict[str, dict[str, Any]] = st.session_state[quotes_key]

    current_integration = integration_for_seller(seller_id, include_inactive=True) or integration
    current_settings = integration_settings(current_integration)
    draft_api = current_settings.get("draft_api") if isinstance(current_settings.get("draft_api"), Mapping) else {}
    service_source = clean_text(draft_api.get("source")) or "PRO"

    def _order_token(order_key: str) -> str:
        return hashlib.sha1(clean_text(order_key).encode("utf-8")).hexdigest()[:12]

    def _previous_order_config(order: Mapping[str, Any]) -> dict[str, Any] | None:
        return previous_draft_by_scope.get(_draft_scope_key(order))

    def _previous_declared_value(order: Mapping[str, Any]) -> float | None:
        previous = _previous_order_config(order)
        if not isinstance(previous, Mapping):
            return None
        try:
            raw = previous.get("request_json")
            payload = json.loads(raw) if isinstance(raw, str) and raw else {}
            value = payload.get("contentvalue") if isinstance(payload, Mapping) else None
            if value not in (None, ""):
                numeric = float(value)
                return numeric if numeric > 0 else None
        except Exception:
            pass
        return None

    def _historical_service(order: Mapping[str, Any]) -> dict[str, Any] | None:
        """Exact tariff/service used by the latest successful draft of this order."""
        previous = _previous_order_config(order)
        if not isinstance(previous, Mapping):
            return None
        service_id = clean_text(previous.get("service_id"))
        if not service_id:
            return None
        return {
            "id": service_id,
            "carrier": clean_text(previous.get("carrier")),
            "service": clean_text(previous.get("service")),
            "price": previous.get("quoted_price"),
            "base_price": None,
            "tax_price": None,
            "currency": clean_text(previous.get("currency")) or "EUR",
            "transit_time": "tariffa precedente",
            "estimated_delivery": "",
            "delivery_to_parcelshop": False,
            "dropoff": False,
            "labels_required": True,
            "service_info": ["Tariffa selezionata nell'ultima organizzazione dell'ordine"],
            "tags": [],
            "_historical": True,
        }

    def _same_service(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> bool:
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        left_id, right_id = clean_text(left.get("id")), clean_text(right.get("id"))
        if left_id and right_id and left_id == right_id:
            return True
        return bool(
            clean_text(left.get("carrier")).lower()
            and clean_text(left.get("carrier")).lower() == clean_text(right.get("carrier")).lower()
            and clean_text(left.get("service")).lower() == clean_text(right.get("service")).lower()
        )

    def _automatic_package(order: Mapping[str, Any]) -> dict[str, float]:
        remembered = order.get("_packlink_remembered_package") if isinstance(order.get("_packlink_remembered_package"), Mapping) else {}
        weight_info = order.get("_packlink_weight_info") if isinstance(order.get("_packlink_weight_info"), Mapping) else {}
        dimension_source = remembered or base_parcel_template
        return {
            "weight": max(0.01, float(weight_info.get("weight_kg") or remembered.get("weight") or base_parcel_template.get("weight") or 1.0)),
            "length": max(1.0, float(dimension_source.get("length") or base_parcel_template.get("length") or 10.0)),
            "width": max(1.0, float(dimension_source.get("width") or base_parcel_template.get("width") or 10.0)),
            "height": max(1.0, float(dimension_source.get("height") or base_parcel_template.get("height") or 10.0)),
        }

    def _package_widget_keys(order: Mapping[str, Any]) -> dict[str, str]:
        token = _order_token(clean_text(order.get("order_key")))
        return {
            "choice": f"packlink_pkg_choice_v212_{seller_id}_{token}",
            "applied": f"packlink_pkg_applied_v212_{seller_id}_{token}",
            "weight": f"packlink_pkg_weight_v212_{seller_id}_{token}",
            "length": f"packlink_pkg_length_v212_{seller_id}_{token}",
            "width": f"packlink_pkg_width_v212_{seller_id}_{token}",
            "height": f"packlink_pkg_height_v212_{seller_id}_{token}",
        }

    def _ensure_package_state(order: Mapping[str, Any]) -> dict[str, str]:
        keys = _package_widget_keys(order)
        automatic = _automatic_package(order)
        previous = _previous_order_config(order)
        previous_package = (
            previous.get("_previous_package")
            if isinstance(previous, Mapping) and isinstance(previous.get("_previous_package"), Mapping)
            else None
        )
        initial_package = previous_package or automatic
        initial_choice = "previous" if previous_package else "auto"
        if keys["choice"] not in st.session_state:
            st.session_state[keys["choice"]] = initial_choice
        if keys["weight"] not in st.session_state:
            st.session_state[keys["weight"]] = float(initial_package["weight"])
        if keys["length"] not in st.session_state:
            st.session_state[keys["length"]] = float(initial_package["length"])
        if keys["width"] not in st.session_state:
            st.session_state[keys["width"]] = float(initial_package["width"])
        if keys["height"] not in st.session_state:
            st.session_state[keys["height"]] = float(initial_package["height"])
        if keys["applied"] not in st.session_state:
            st.session_state[keys["applied"]] = st.session_state[keys["choice"]]
        return keys

    def _current_package(order: Mapping[str, Any]) -> dict[str, float]:
        keys = _ensure_package_state(order)
        return {
            "weight": float(st.session_state[keys["weight"]]),
            "length": float(st.session_state[keys["length"]]),
            "width": float(st.session_state[keys["width"]]),
            "height": float(st.session_state[keys["height"]]),
        }

    def _apply_package_choice(order: Mapping[str, Any], choice: str) -> None:
        keys = _ensure_package_state(order)
        if choice == "previous":
            previous = _previous_order_config(order) or {}
            package = previous.get("_previous_package") if isinstance(previous, Mapping) else None
            if not isinstance(package, Mapping):
                package = _automatic_package(order)
        elif choice == "auto":
            package = _automatic_package(order)
        elif choice.startswith("saved:"):
            try:
                profile_id = int(choice.split(":", 1)[1])
                dynamic_profiles = {
                    int(item["id"]): item
                    for item in saved_package_profiles(seller_id, limit=5000)
                    if item.get("id") not in (None, "")
                }
                package = dynamic_profiles.get(profile_id) or package_profiles_by_id.get(profile_id)
                if not package:
                    package = _automatic_package(order)
            except Exception:
                package = _automatic_package(order)
        elif choice.startswith("packlink:"):
            raw_id = choice.split(":", 1)[1]
            package = next((item for item in parcels if clean_text(item.get("id")) == raw_id), _automatic_package(order))
        else:
            st.session_state[keys["applied"]] = choice
            return
        st.session_state[keys["weight"]] = max(0.01, float(package.get("weight") or 0.01))
        st.session_state[keys["length"]] = max(1.0, float(package.get("length") or 1.0))
        st.session_state[keys["width"]] = max(1.0, float(package.get("width") or 1.0))
        st.session_state[keys["height"]] = max(1.0, float(package.get("height") or 1.0))
        st.session_state[keys["applied"]] = choice

    # -------------------------------------------------------------------------
    # Contatti destinatario: modifica diretta + valori predefiniti per Seller
    # -------------------------------------------------------------------------
    recipient_contact_defaults = (
        settings.get("recipient_contact_defaults")
        if isinstance(settings.get("recipient_contact_defaults"), Mapping)
        else {}
    )
    default_email_key = f"packlink_recipient_default_email_v269_{seller_id}"
    default_phone_key = f"packlink_recipient_default_phone_v269_{seller_id}"
    use_default_email_key = f"packlink_recipient_use_default_email_v269_{seller_id}"
    use_default_phone_key = f"packlink_recipient_use_default_phone_v269_{seller_id}"
    if default_email_key not in st.session_state:
        st.session_state[default_email_key] = clean_text(recipient_contact_defaults.get("email"))
    if default_phone_key not in st.session_state:
        st.session_state[default_phone_key] = clean_text(recipient_contact_defaults.get("phone"))
    if use_default_email_key not in st.session_state:
        st.session_state[use_default_email_key] = bool(recipient_contact_defaults.get("use_email_for_missing"))
    if use_default_phone_key not in st.session_state:
        st.session_state[use_default_phone_key] = bool(recipient_contact_defaults.get("use_phone_for_missing"))

    original_recipient_contacts: dict[str, dict[str, str]] = {}
    missing_contact_orders: list[dict[str, Any]] = []
    for _order in selected_orders:
        _order_key = clean_text(_order.get("order_key"))
        _recipient = packlink_destination_address(_order)
        _contact = {
            "email": clean_text(_recipient.get("email")),
            "phone": clean_text(_recipient.get("phone")),
        }
        original_recipient_contacts[_order_key] = _contact
        if not _contact["email"] or not _contact["phone"]:
            missing_contact_orders.append(_order)

    st.markdown("#### Email e telefono destinatari")
    st.caption(
        "Puoi correggere email e telefono direttamente qui prima di generare il CSV o creare la bozza Packlink. "
        "I valori predefiniti sono specifici del Seller e possono essere applicati automaticamente solo agli ordini "
        "in cui il dato manca; non sostituiscono i contatti già presenti del cliente."
    )
    contact_default_col1, contact_default_col2 = st.columns(2)
    default_recipient_email = contact_default_col1.text_input(
        "Email predefinita per destinatari senza email",
        key=default_email_key,
        placeholder="es. spedizioni@azienda.it",
    )
    default_recipient_phone = contact_default_col2.text_input(
        "Telefono predefinito per destinatari senza telefono",
        key=default_phone_key,
        placeholder="es. +39 081 1234567",
    )
    use_default_col1, use_default_col2 = st.columns(2)
    use_default_recipient_email = use_default_col1.checkbox(
        "Usa l'email predefinita per tutti quelli senza email",
        key=use_default_email_key,
    )
    use_default_recipient_phone = use_default_col2.checkbox(
        "Usa il telefono predefinito per tutti quelli senza telefono",
        key=use_default_phone_key,
        help=(
            "Se non attivi questa opzione, il comportamento precedente resta invariato: "
            "quando il telefono cliente manca, il CSV può usare il telefono del mittente."
        ),
    )

    save_contact_defaults_col, _ = st.columns([1, 2])
    if save_contact_defaults_col.button(
        "Salva email/telefono predefiniti",
        key=f"packlink_save_recipient_defaults_v269_{seller_id}",
        use_container_width=True,
    ):
        update_integration_settings(
            seller_id,
            {
                "recipient_contact_defaults": {
                    "email": clean_text(default_recipient_email),
                    "phone": clean_text(default_recipient_phone),
                    "use_email_for_missing": bool(use_default_recipient_email),
                    "use_phone_for_missing": bool(use_default_recipient_phone),
                }
            },
        )
        st.success("Email e telefono predefiniti salvati per questo Seller.")

    if use_default_recipient_email and not clean_text(default_recipient_email):
        st.warning("Hai attivato l'email predefinita, ma il campo email è vuoto.")
    if use_default_recipient_phone and not clean_text(default_recipient_phone):
        st.warning("Hai attivato il telefono predefinito, ma il campo telefono è vuoto.")

    if missing_contact_orders:
        missing_email_count = sum(
            not bool(original_recipient_contacts.get(clean_text(item.get("order_key")), {}).get("email"))
            for item in missing_contact_orders
        )
        missing_phone_count = sum(
            not bool(original_recipient_contacts.get(clean_text(item.get("order_key")), {}).get("phone"))
            for item in missing_contact_orders
        )
        with st.expander(
            f"Modifica direttamente i contatti mancanti ({len(missing_contact_orders)} ordini)",
            expanded=bool(missing_email_count),
        ):
            st.caption(
                f"Email mancanti: {missing_email_count} · Telefoni mancanti: {missing_phone_count}. "
                "Puoi inserire un valore specifico per il singolo ordine; il valore specifico ha priorità sul default."
            )
            header_order, header_customer, header_email, header_phone = st.columns([1.05, 1.4, 2.4, 1.8])
            header_order.markdown("**Ordine**")
            header_customer.markdown("**Cliente**")
            header_email.markdown("**Email destinatario**")
            header_phone.markdown("**Telefono destinatario**")
            for _order in missing_contact_orders:
                _order_key = clean_text(_order.get("order_key"))
                _order_id = clean_text(_order.get("order_id"))
                _token = _order_token(_order_key)
                _original = original_recipient_contacts.get(_order_key, {})
                row_order, row_customer, row_email, row_phone = st.columns([1.05, 1.4, 2.4, 1.8])
                row_order.markdown(f"**{_order_id or '-'}**")
                row_customer.caption(clean_text(_order.get("customer_name")) or "-")
                row_email.text_input(
                    f"Email destinatario {_order_id}",
                    value=clean_text(_original.get("email")),
                    key=f"packlink_recipient_email_override_v269_{seller_id}_{_token}",
                    placeholder=(
                        clean_text(default_recipient_email)
                        if use_default_recipient_email and clean_text(default_recipient_email)
                        else "Inserisci email"
                    ),
                    label_visibility="collapsed",
                )
                row_phone.text_input(
                    f"Telefono destinatario {_order_id}",
                    value=clean_text(_original.get("phone")),
                    key=f"packlink_recipient_phone_override_v269_{seller_id}_{_token}",
                    placeholder=(
                        clean_text(default_recipient_phone)
                        if use_default_recipient_phone and clean_text(default_recipient_phone)
                        else "Inserisci telefono"
                    ),
                    label_visibility="collapsed",
                )
    else:
        st.success("Tutti gli ordini selezionati hanno già email e telefono destinatario.")

    # Applica i contatti effettivi all'ordine corrente. Le modifiche sono usate sia
    # dal CSV ufficiale sia dal payload della bozza Packlink creato più sotto.
    applied_default_email = 0
    applied_default_phone = 0
    applied_manual_email = 0
    applied_manual_phone = 0
    for _order in selected_orders:
        _order_key = clean_text(_order.get("order_key"))
        _token = _order_token(_order_key)
        _original = original_recipient_contacts.get(_order_key, {})
        _original_email = clean_text(_original.get("email"))
        _original_phone = clean_text(_original.get("phone"))
        _manual_email = clean_text(
            st.session_state.get(f"packlink_recipient_email_override_v269_{seller_id}_{_token}")
        )
        _manual_phone = clean_text(
            st.session_state.get(f"packlink_recipient_phone_override_v269_{seller_id}_{_token}")
        )

        _effective_email = _manual_email or _original_email
        if _manual_email and _manual_email != _original_email:
            applied_manual_email += 1
        if not _effective_email and use_default_recipient_email and clean_text(default_recipient_email):
            _effective_email = clean_text(default_recipient_email)
            applied_default_email += 1

        _effective_phone = _manual_phone or _original_phone
        if _manual_phone and _manual_phone != _original_phone:
            applied_manual_phone += 1
        if not _effective_phone and use_default_recipient_phone and clean_text(default_recipient_phone):
            _effective_phone = clean_text(default_recipient_phone)
            applied_default_phone += 1

        if _effective_email:
            _order["customer_email"] = _effective_email
            _order["email"] = _effective_email
        if _effective_phone:
            _order["phone"] = _effective_phone

        # v230/v269: anche la copia usata immediatamente prima del POST deve
        # vedere le modifiche fatte nella UI, evitando che il rerun riprenda
        # una snapshot dei contatti antecedente alla correzione.
        if _order_key:
            current_order_by_key[_order_key] = dict(_order)

    applied_parts = []
    if applied_manual_email or applied_manual_phone:
        applied_parts.append(
            f"modifiche manuali: {applied_manual_email} email, {applied_manual_phone} telefoni"
        )
    if applied_default_email or applied_default_phone:
        applied_parts.append(
            f"default applicati: {applied_default_email} email, {applied_default_phone} telefoni"
        )
    if applied_parts:
        st.info("Contatti applicati agli ordini selezionati — " + " · ".join(applied_parts) + ".")

    for _order in selected_orders:
        _ensure_package_state(_order)

    # -------------------------------------------------------------------------
    # CSV ufficiale Packlink PRO
    # -------------------------------------------------------------------------
    st.markdown("#### Esporta CSV ufficiale Packlink PRO")
    st.caption(
        "Genera il tracciato CSV ufficiale Packlink PRO allegato al progetto: 30 colonne, "
        "separatore `;`, CAP normalizzati secondo il formato postale del Paese (spazi/trattini inclusi), "
        "zeri iniziali preservati, assicurazione `yes/no` e decimali con punto. Email e telefono possono essere "
        "corretti nella pagina o completati con i valori predefiniti del Seller; se il telefono resta assente, "
        "viene usato automaticamente il numero del mittente. Non usa la creazione bozza via API."
    )

    csv_insurance = st.checkbox(
        "Assicura le spedizioni nel CSV (yes)",
        value=False,
        key=f"packlink_csv_insurance_v240_{seller_id}",
        help="Il template Packlink accetta soltanto yes oppure no nella colonna assicurazione.",
    )

    def _csv_declared_value(order: Mapping[str, Any]) -> float:
        token = _order_token(clean_text(order.get("order_key")))
        state_key = f"packlink_declared_v218_{seller_id}_{token}"
        if state_key in st.session_state:
            try:
                value = float(st.session_state[state_key])
                if value > 0:
                    return value
            except Exception:
                pass
        previous = _previous_declared_value(order)
        if previous is not None and previous > 0:
            return float(previous)
        account_id = int(order.get("marketplace_account_id") or 0)
        return float(order_declared_value(order, accounting_by_account.get(account_id, [])))

    # v270: un solo comando prepara in massa pacco + tariffa migliore per tutti
    # gli ordini selezionati e congela il CSV risultante. Per sicurezza non prova
    # dimensioni generiche appartenenti ad altri prodotti: confronta soltanto la
    # configurazione automatica dell'ordine, i valori correnti e l'eventuale pacco
    # usato in precedenza per QUELLO stesso ordine.
    auto_csv_state_key = f"packlink_auto_csv_v271_{seller_id}"

    def _auto_csv_context_signature() -> str:
        context_rows = []
        for context_order in selected_orders:
            context_rows.append({
                "order_key": clean_text(context_order.get("order_key")),
                "email": clean_text(context_order.get("customer_email") or context_order.get("email")),
                "phone": clean_text(context_order.get("phone")),
                "package": packlink_package_signature(_current_package(context_order)),
                "declared_value": _csv_declared_value(context_order),
            })
        raw = json.dumps({
            "orders": context_rows,
            "sender": {
                "id": clean_text(selected_sender.get("local_sender_id")),
                "country": clean_text(selected_sender.get("country")),
                "zip": clean_text(selected_sender.get("zip_code")),
                "phone": clean_text(selected_sender.get("phone")),
                "email": clean_text(selected_sender.get("email")),
            },
            "insurance": bool(csv_insurance),
        }, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _candidate_packages_for_auto_csv(order: Mapping[str, Any]) -> list[tuple[str, dict[str, float]]]:
        candidates: list[tuple[str, dict[str, float]]] = []

        def _append(label: str, package: Mapping[str, Any] | None) -> None:
            if not isinstance(package, Mapping):
                return
            try:
                normalized = package_payload(package)
                signature = packlink_package_signature(normalized)
            except Exception:
                return
            if any(packlink_package_signature(value) == signature for _, value in candidates):
                return
            candidates.append((label, dict(normalized)))

        _append("Automatico ordine", _automatic_package(order))
        _append("Valori correnti", _current_package(order))
        previous = _previous_order_config(order) or {}
        _append(
            "Ultimo pacco usato per questo ordine",
            previous.get("_previous_package") if isinstance(previous, Mapping) else None,
        )
        return candidates

    auto_generate_clicked = st.button(
        f"Genera automaticamente migliore combinazione + CSV completo ({len(selected_orders)} ordini)",
        type="primary",
        use_container_width=True,
        key=f"packlink_auto_best_csv_v271_{seller_id}",
        help=(
            "Per ogni ordine confronta le configurazioni pacco sicure disponibili, interroga le tariffe "
            "Packlink e sceglie quella con il prezzo totale più basso. Poi prepara il CSV ufficiale completo."
        ),
    )

    if auto_generate_clicked:
        progress = st.progress(0.0)
        status = st.empty()
        combo_results: list[dict[str, Any]] = []
        quote_failures = 0
        for position, order in enumerate(selected_orders, 1):
            order_key = clean_text(order.get("order_key"))
            token = _order_token(order_key)
            best_choice: dict[str, Any] | None = None
            last_error = ""
            for package_label, candidate_package in _candidate_packages_for_auto_csv(order):
                try:
                    services = client.shipping_services(
                        from_country=clean_text(selected_sender.get("country")),
                        from_zip=clean_text(selected_sender.get("zip_code")),
                        to_country=clean_text(order.get("country_code")),
                        to_zip=clean_text(order.get("postal_code")),
                        packages=[candidate_package],
                        source=service_source,
                    )
                    best_service = choose_best_packlink_service(services)
                    if not isinstance(best_service, Mapping):
                        continue
                    candidate = {
                        "package_label": package_label,
                        "package": dict(candidate_package),
                        "service": dict(best_service),
                        "price": float(best_service.get("price") or 0),
                    }
                    if best_choice is None or candidate["price"] < float(best_choice.get("price") or 10**12):
                        best_choice = candidate
                except Exception as exc:
                    last_error = clean_text(exc)

            if isinstance(best_choice, Mapping):
                chosen_package = dict(best_choice["package"])
                chosen_service = dict(best_choice["service"])
                package_keys = _ensure_package_state(order)
                # Mantiene i valori scelti dall'automazione senza farli rimpiazzare
                # dal radio al rerun successivo: diventano "manual"/valori correnti.
                st.session_state[package_keys["weight"]] = float(chosen_package["weight"])
                st.session_state[package_keys["length"]] = float(chosen_package["length"])
                st.session_state[package_keys["width"]] = float(chosen_package["width"])
                st.session_state[package_keys["height"]] = float(chosen_package["height"])
                st.session_state[package_keys["choice"]] = "manual"
                st.session_state[package_keys["applied"]] = "manual"

                # Conserva la tariffa scelta, così le schede ordine e l'eventuale
                # invio massivo API risultano già precompilati con la stessa scelta.
                quotes_state[order_key] = {
                    "package_signature": packlink_package_signature(chosen_package),
                    "package": dict(chosen_package),
                    "services": [dict(chosen_service)],
                    "loaded_at": datetime.now().isoformat(timespec="seconds"),
                    "automatic_best": True,
                }
                service_id = clean_text(chosen_service.get("id"))
                if service_id:
                    st.session_state[f"packlink_service_radio_v218_{seller_id}_{token}"] = service_id
                declared_value = _csv_declared_value(order)
                st.session_state[f"packlink_batch_candidate_v230_{seller_id}_{token}"] = {
                    "order_key": order_key,
                    "order": dict(order),
                    "package": dict(chosen_package),
                    "service": dict(chosen_service),
                    "declared_value": declared_value,
                    "forced": bool(st.session_state.get(f"packlink_force_recreate_v218_{seller_id}_{token}")),
                }
                combo_results.append({
                    "Ordine": clean_text(order.get("order_id")),
                    "Pacco scelto": clean_text(best_choice.get("package_label")),
                    "Peso kg": float(chosen_package.get("weight") or 0),
                    "Dimensioni cm": (
                        f"{float(chosen_package.get('length') or 0):g}×"
                        f"{float(chosen_package.get('width') or 0):g}×"
                        f"{float(chosen_package.get('height') or 0):g}"
                    ),
                    "Corriere più conveniente": clean_text(chosen_service.get("carrier")),
                    "Servizio": clean_text(chosen_service.get("service")),
                    "Costo": float(chosen_service.get("price") or 0),
                    "Valuta": clean_text(chosen_service.get("currency")) or "EUR",
                    "Esito": "OK",
                })
            else:
                quote_failures += 1
                combo_results.append({
                    "Ordine": clean_text(order.get("order_id")),
                    "Pacco scelto": "Valori correnti",
                    "Peso kg": float(_current_package(order).get("weight") or 0),
                    "Dimensioni cm": "",
                    "Corriere più conveniente": "",
                    "Servizio": "",
                    "Costo": None,
                    "Valuta": "",
                    "Esito": f"Tariffa non disponibile{': ' + last_error if last_error else ''}",
                })
            progress.progress(position / max(1, len(selected_orders)))
            status.caption(
                f"Calcolo automatico: {position} di {len(selected_orders)} · "
                f"combinazioni trovate {position - quote_failures}"
            )

        st.session_state[quotes_key] = quotes_state
        generated_shipments = [
            {
                "order": dict(order),
                "package": _current_package(order),
                "declared_value": _csv_declared_value(order),
            }
            for order in selected_orders
        ]
        try:
            generated_payload, generated_report = build_packlink_csv(
                generated_shipments,
                sender=dict(selected_sender or {}),
                insurance=bool(csv_insurance),
            )
        except Exception as exc:
            generated_payload, generated_report = b"", []
            st.error(f"Generazione automatica CSV Packlink non riuscita: {exc}")

        generated_invalid = [item for item in generated_report if not bool(item.get("valid"))]
        st.session_state[auto_csv_state_key] = {
            "payload": generated_payload,
            "report": generated_report,
            "combos": combo_results,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "complete": bool(generated_report) and not bool(generated_invalid)
                and len(generated_report) == len(selected_orders),
            "quote_failures": quote_failures,
            "context_signature": _auto_csv_context_signature(),
        }
        if generated_invalid:
            st.warning(
                f"Combinazioni calcolate, ma il CSV non è ancora completo: "
                f"{len(generated_invalid)} ordine/i hanno dati obbligatori mancanti. "
                "Correggili nella sezione email/telefono o nei dati ordine e premi di nuovo il tasto."
            )
        else:
            st.success(
                f"CSV completo generato automaticamente per {len(generated_report)} ordini. "
                f"Tariffa più conveniente rilevata per {len(selected_orders) - quote_failures}/{len(selected_orders)} ordini."
            )

    auto_csv_state = st.session_state.get(auto_csv_state_key)
    auto_csv_is_current = bool(
        isinstance(auto_csv_state, Mapping)
        and clean_text(auto_csv_state.get("context_signature")) == _auto_csv_context_signature()
    )
    if isinstance(auto_csv_state, Mapping) and auto_csv_state.get("combos") and not auto_csv_is_current:
        st.info(
            "La selezione o i dati degli ordini sono cambiati dopo l'ultima generazione automatica. "
            "Premi di nuovo il tasto per rigenerare combinazioni e CSV aggiornati."
        )
    if isinstance(auto_csv_state, Mapping) and auto_csv_state.get("combos") and auto_csv_is_current:
        with st.expander("Risultato combinazione automatica per ordine", expanded=bool(auto_generate_clicked)):
            st.dataframe(
                pd.DataFrame(auto_csv_state.get("combos") or []),
                hide_index=True,
                use_container_width=True,
            )
        if bool(auto_csv_state.get("complete")) and auto_csv_state.get("payload"):
            st.download_button(
                f"Scarica CSV completo generato automaticamente ({len(auto_csv_state.get('report') or [])} spedizioni)",
                data=auto_csv_state.get("payload"),
                file_name="packlink_pro_import_completo.csv",
                mime="text/csv",
                type="primary",
                use_container_width=True,
                key=f"packlink_auto_csv_download_v271_{seller_id}",
            )
            st.caption(
                "Il CSV ufficiale Packlink contiene peso e dimensioni della combinazione selezionata. "
                "Il formato ufficiale non prevede colonne corriere/servizio: il servizio più conveniente è mostrato "
                "nel riepilogo come controllo e resta preselezionato anche nelle schede Marketplace Hub."
            )

    csv_shipments = [
        {
            "order": dict(order),
            "package": _current_package(order),
            "declared_value": _csv_declared_value(order),
        }
        for order in selected_orders
    ]
    try:
        csv_payload, csv_report = build_packlink_csv(
            csv_shipments,
            sender=dict(selected_sender or {}),
            insurance=bool(csv_insurance),
        )
    except Exception as exc:
        csv_payload, csv_report = b"", []
        st.error(f"Preparazione CSV Packlink non riuscita: {exc}")

    csv_valid = [item for item in csv_report if bool(item.get("valid"))]
    csv_invalid = [item for item in csv_report if not bool(item.get("valid"))]
    c_csv1, c_csv2, c_csv3 = st.columns(3)
    c_csv1.metric("Ordini selezionati", len(selected_orders))
    c_csv2.metric("Pronti nel CSV", len(csv_valid))
    c_csv3.metric("Da completare", len(csv_invalid))
    csv_phone_fallbacks = sum(bool(item.get("recipient_phone_fallback")) for item in csv_report)
    if csv_phone_fallbacks:
        st.info(
            f"Telefono destinatario assente in {csv_phone_fallbacks} ordine/i: nel CSV viene usato "
            "automaticamente il numero del mittente, come richiesto da Packlink."
        )

    if csv_invalid:
        st.warning(
            f"{len(csv_invalid)} ordini non vengono inseriti nel CSV perché manca almeno un dato obbligatorio. "
            "Gli altri ordini restano esportabili."
        )
        with st.expander("Mostra ordini da completare", expanded=False):
            st.dataframe(
                pd.DataFrame([
                    {
                        "Ordine": item.get("order_id"),
                        "Riferimento CSV": item.get("reference"),
                        "Dati mancanti": "; ".join(item.get("errors") or []),
                    }
                    for item in csv_invalid
                ]),
                hide_index=True,
                use_container_width=True,
            )

    if csv_valid:
        with st.expander("Controllo CAP destinatari scritti nel CSV", expanded=True):
            preview_rows = []
            for item in csv_report:
                if not bool(item.get("valid")):
                    continue
                preview_rows.append({
                    "Ordine": item.get("order_id"),
                    "Paese": item.get("recipient_country"),
                    "CAP origine": item.get("recipient_postal_original"),
                    "CAP CSV Packlink": item.get("recipient_postal_csv"),
                    "Formato nazionale": item.get("recipient_postal_format"),
                    "Città": item.get("recipient_city"),
                })
            st.dataframe(pd.DataFrame(preview_rows), hide_index=True, use_container_width=True)
            changed = [
                item for item in csv_report
                if bool(item.get("valid"))
                and clean_text(item.get("recipient_postal_original")) != clean_text(item.get("recipient_postal_csv"))
            ]
            if changed:
                st.info(
                    f"{len(changed)} CAP sono stati normalizzati automaticamente secondo il formato del Paese. "
                    "Esempio: Slovacchia 04011 → 040 11."
                )

    st.download_button(
        f"Scarica CSV ufficiale Packlink ({len(csv_valid)} spedizioni)",
        data=csv_payload,
        file_name="packlink_pro_import.csv",
        mime="text/csv",
        type="primary",
        use_container_width=True,
        disabled=not bool(csv_valid),
        key=f"packlink_csv_download_v240_{seller_id}",
    )
    st.caption(
        "In Packlink PRO: freccia accanto a **Nuova spedizione** → **Importa un file CSV** → "
        "carica `packlink_pro_import.csv`. Il file mantiene tutte le 30 colonne del modello ufficiale."
    )
    st.divider()

    def _valid_services(order: Mapping[str, Any]) -> list[dict[str, Any]]:
        entry = quotes_state.get(clean_text(order.get("order_key")))
        if not isinstance(entry, Mapping):
            return []
        try:
            signature = packlink_package_signature(_current_package(order))
        except Exception:
            return []
        if clean_text(entry.get("package_signature")) != signature:
            return []
        services_value = entry.get("services")
        return list(services_value) if isinstance(services_value, list) else []

    def _restorable_historical_service(order: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return the persisted tariff when bulk regeneration may reuse it without a new quote."""
        token = _order_token(clean_text(order.get("order_key")))
        if not bool(st.session_state.get(f"packlink_force_recreate_v218_{seller_id}_{token}")):
            return None
        if bool(st.session_state.get(f"packlink_manual_tariff_v223_{seller_id}_{token}")):
            return None
        previous = _previous_order_config(order)
        historical = _historical_service(order)
        previous_package = (
            previous.get("_previous_package")
            if isinstance(previous, Mapping) and isinstance(previous.get("_previous_package"), Mapping)
            else None
        )
        if not isinstance(historical, Mapping) or not isinstance(previous_package, Mapping):
            return None
        try:
            if packlink_package_signature(previous_package) != packlink_package_signature(_current_package(order)):
                return None
        except Exception:
            return None
        return dict(historical)

    def _effective_services(order: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Services usable now, including the exact historical tariff restored for regeneration."""
        services = [dict(item) for item in _valid_services(order) if isinstance(item, Mapping)]
        historical = _restorable_historical_service(order)
        if isinstance(historical, Mapping):
            services = [dict(historical)] + [item for item in services if not _same_service(item, historical)]
        return services

    def _load_quotes(order: Mapping[str, Any]) -> tuple[bool, str]:
        try:
            package = package_payload(_current_package(order))
            services = client.shipping_services(
                from_country=clean_text(selected_sender.get("country")),
                from_zip=clean_text(selected_sender.get("zip_code")),
                to_country=clean_text(order.get("country_code")),
                to_zip=clean_text(order.get("postal_code")),
                packages=[package],
                source=service_source,
            )
            services.sort(key=lambda item: (float(item.get("price") or 10**9), clean_text(item.get("carrier"))))
            # Ogni configurazione pacco realmente usata per una richiesta tariffe
            # entra nella memoria del Seller, anche prima della creazione bozza.
            try:
                save_package_profile(seller_id, package, increment_use=False)
            except Exception:
                pass
            quotes_state[clean_text(order.get("order_key"))] = {
                "package_signature": packlink_package_signature(package),
                "package": package,
                "services": services,
                "loaded_at": datetime.now().isoformat(timespec="seconds"),
            }
            st.session_state[quotes_key] = quotes_state
            return True, ""
        except Exception as exc:
            quotes_state[clean_text(order.get("order_key"))] = {
                "package_signature": "",
                "package": {},
                "services": [],
                "error": str(exc),
            }
            st.session_state[quotes_key] = quotes_state
            return False, str(exc)

    st.caption(
        "Modalità ultra veloce attiva (v220): ogni ordine è un frammento Streamlit indipendente. "
        "Cambiare pacco, dimensioni o servizio ricalcola solo quella singola scheda, non gli altri ordini "
        "e non riscarica marketplace, documenti o listini."
    )

    selected_orders_by_key = {
        clean_text(item.get("order_key")): item for item in selected_orders
        if clean_text(item.get("order_key"))
    }

    draft_by_scope = {
        (
            int(item.get("marketplace_account_id") or 0),
            clean_text(item.get("marketplace")).lower(),
            clean_text(item.get("order_id")).upper(),
        ): dict(item)
        for item in created_drafts
    }

    # Anche una spedizione già presente/pagata su Packlink viene considerata
    # "organizzata" se il suo order reference/custom reference contiene il
    # numero ordine. Questo evita duplicati anche quando la spedizione non era stata
    # originariamente creata da Marketplace Hub, purché sia stata sincronizzata.
    shipment_by_order_id: dict[str, dict[str, Any]] = {}
    for shipment_row in cached_shipments(seller_id):
        references: list[str] = [clean_text(shipment_row.get("order_reference"))]
        try:
            raw_value = shipment_row.get("raw_json")
            raw = json.loads(raw_value) if isinstance(raw_value, str) and raw_value else {}
            if isinstance(raw, Mapping):
                references.extend([
                    clean_text(raw.get("shipment_custom_reference")),
                    clean_text(raw.get("shipmentCustomReference")),
                    clean_text(raw.get("order_reference")),
                    clean_text(raw.get("orderReference")),
                ])
        except Exception:
            pass
        for reference_text in references:
            upper = clean_text(reference_text).upper()
            if not upper:
                continue
            candidates = {upper}
            if "-" in upper:
                candidates.add(upper.rsplit("-", 1)[-1])
            for candidate in candidates:
                if candidate:
                    shipment_by_order_id.setdefault(candidate, dict(shipment_row))

    def _existing_shipping(order: Mapping[str, Any]) -> dict[str, Any] | None:
        # v231: this helper is called while rendering the bulk-regeneration controls,
        # before _create_draft_for_order() defines its local ``current_order``.
        # Always build the lookup scope from the order explicitly passed to us.
        scope = (
            int(order.get("marketplace_account_id") or 0),
            clean_text(order.get("marketplace")).lower(),
            clean_text(order.get("order_id")).upper(),
        )
        local = draft_by_scope.get(scope)
        if local:
            value = dict(local)
            value["_existing_source"] = "Marketplace Hub"
            return value
        shipment = shipment_by_order_id.get(clean_text(order.get("order_id")).upper())
        if shipment:
            return {
                "shipment_reference": clean_text(shipment.get("reference")),
                "carrier": clean_text(shipment.get("carrier")),
                "service": clean_text(shipment.get("service")),
                "quoted_price": shipment.get("price"),
                "currency": clean_text(shipment.get("currency")),
                "status": clean_text(shipment.get("status")),
                "_existing_source": "Packlink sincronizzato",
            }
        return None

    @st.fragment
    def _render_force_recreate_bulk_controls() -> None:
        organized_orders = [item for item in selected_orders if _existing_shipping(item)]
        if not organized_orders:
            return
        selected_force = 0
        for item in organized_orders:
            token = _order_token(clean_text(item.get("order_key")))
            if bool(st.session_state.get(f"packlink_force_recreate_v218_{seller_id}_{token}")):
                selected_force += 1
        st.markdown("#### Rigenerazione ordini già organizzati")
        c1, c2, c3 = st.columns([1, 1, 1.4])
        if c1.button(
            f"Seleziona tutti da rigenerare ({len(organized_orders)})",
            use_container_width=True,
            key=f"packlink_force_all_v223_{seller_id}",
        ):
            restored_tariffs = 0
            for item in organized_orders:
                token = _order_token(clean_text(item.get("order_key")))
                st.session_state[f"packlink_force_recreate_v218_{seller_id}_{token}"] = True
                st.session_state[f"packlink_restore_previous_tariff_v223_{seller_id}_{token}"] = True
                # Clicking the bulk restore is authoritative: discard any temporary
                # single-order tariff editing state from the current Streamlit session.
                st.session_state.pop(f"packlink_manual_tariff_v223_{seller_id}_{token}", None)

                # Bulk regeneration explicitly restores the exact package used before.
                previous = _previous_order_config(item) or {}
                previous_package = previous.get("_previous_package") if isinstance(previous, Mapping) else None
                if isinstance(previous_package, Mapping):
                    _apply_package_choice(item, "previous")
                    package_keys = _package_widget_keys(item)
                    st.session_state[package_keys["choice"]] = "previous"

                # The previously selected Packlink tariff is restored per order.
                old_service = _historical_service(item)
                if isinstance(old_service, Mapping):
                    st.session_state[f"packlink_service_radio_v218_{seller_id}_{token}"] = clean_text(old_service.get("id"))
                    restored_tariffs += 1

                old_declared = _previous_declared_value(item)
                if old_declared is not None:
                    st.session_state[f"packlink_declared_v218_{seller_id}_{token}"] = float(old_declared)
            st.session_state[f"packlink_force_all_restored_v223_{seller_id}"] = restored_tariffs
            st.rerun()
        if c2.button(
            "Deseleziona tutti",
            use_container_width=True,
            key=f"packlink_force_none_v220_{seller_id}",
        ):
            for item in organized_orders:
                token = _order_token(clean_text(item.get("order_key")))
                st.session_state[f"packlink_force_recreate_v218_{seller_id}_{token}"] = False
            st.rerun()
        c3.metric("Selezionati per rigenerazione", f"{selected_force} / {len(organized_orders)}")
        restored_tariffs = st.session_state.get(f"packlink_force_all_restored_v223_{seller_id}")
        st.caption(
            "La rigenerazione massiva ripristina automaticamente, per ogni ordine, l'ultimo pacco, "
            "peso/dimensioni, tariffa Packlink selezionata, corriere/servizio, costo storico e valore dichiarato. "
            "Non devi riselezionare le tariffe una per una; cambiano solo gli ordini che modifichi manualmente."
            + (f" Tariffe ripristinate nell'ultima selezione: {int(restored_tariffs)}." if restored_tariffs is not None else "")
        )

    _render_force_recreate_bulk_controls()

    def _service_info_text(item: Mapping[str, Any]) -> str:
        values: list[str] = []
        for value in item.get("service_info") or []:
            if isinstance(value, Mapping):
                text_value = clean_text(
                    value.get("label") or value.get("name") or value.get("description")
                    or value.get("value") or value.get("text")
                )
            else:
                text_value = clean_text(value)
            if text_value and text_value not in values:
                values.append(text_value)
        for value in item.get("tags") or []:
            text_value = clean_text(value.get("name") if isinstance(value, Mapping) else value)
            if text_value and text_value not in values:
                values.append(text_value)
        return " · ".join(values)[:500]

    # Compatibilità funzionale: nel singolo ordine il payload equivale a
    # build_packlink_draft_payload(..., package=order_package, ...) e dopo il
    # successo viene eseguito remember_package_for_order(seller_id, order, order_package).
    def _create_draft_for_order(
        order: Mapping[str, Any],
        package: Mapping[str, Any],
        service: Mapping[str, Any],
        declared_value: float,
        *,
        forced: bool,
    ) -> str:
        # v230: resolve the current grouped marketplace order immediately before POST.
        order_key_now = clean_text(order.get("order_key"))
        current_order = dict(current_order_by_key.get(order_key_now) or order)
        current = integration_for_seller(seller_id, include_inactive=True) or integration
        draft_payload = build_packlink_draft_payload(
            integration=current,
            order=current_order,
            sender=selected_sender_for_draft,
            package=package,
            service=service,
            declared_value=declared_value,
            warehouse_id=selected_sender_origin_id,
        )
        # Final overwrite from the exact row currently shown in the UI. This makes
        # stale Streamlit candidates incapable of sending IT for an order shown as SK.
        sender_phone = clean_text(
            draft_payload.get("from", {}).get("phone")
            if isinstance(draft_payload.get("from"), Mapping) else ""
        )
        draft_payload["to"] = packlink_destination_address(
            current_order, fallback_phone=sender_phone
        )
        validate_packlink_destination_against_order(draft_payload, current_order)
        missing_ready_fields = packlink_ready_for_payment_validation(draft_payload)
        if missing_ready_fields:
            raise ValueError(
                "Per creare la spedizione in Bozza > Pronti per il pagamento mancano: "
                + ", ".join(missing_ready_fields)
                + ". Completa questi dati e riprova."
            )
        result = client.create_draft(draft_payload)
        submitted_payload = (
            result.get("submitted_payload")
            if isinstance(result, Mapping) and isinstance(result.get("submitted_payload"), Mapping)
            else draft_payload
        )
        save_order_draft(
            seller_id, current_order, service, submitted_payload, result, forced=forced,
        )
        remember_package_for_order(seller_id, current_order, package)
        scope = (
            int(order.get("marketplace_account_id") or 0),
            clean_text(order.get("marketplace")).lower(),
            clean_text(order.get("order_id")).upper(),
        )
        draft_by_scope[scope] = {
            "marketplace_account_id": scope[0],
            "marketplace": scope[1],
            "order_id": clean_text(current_order.get("order_id")),
            "shipment_reference": clean_text(result.get("reference")),
            "carrier": clean_text(service.get("carrier")),
            "service": clean_text(service.get("service")),
            "status": clean_text(result.get("remote_status")) or "ready_for_payment",
        }
        order_status_key = f"packlink_remote_status_v219_{seller_id}_{_order_token(clean_text(current_order.get('order_key')))}"
        st.session_state[order_status_key] = {
            "reference": clean_text(result.get("reference")),
            "remote_status": clean_text(result.get("remote_status")),
            "checked": bool(result.get("shipment")),
        }
        return clean_text(result.get("reference"))


    @st.fragment
    def _render_packlink_quote_all_controls() -> None:
        effective_by_key = {
            clean_text(item.get("order_key")): _effective_services(item)
            for item in selected_orders
        }
        pending_orders = [
            item for item in selected_orders
            if not effective_by_key.get(clean_text(item.get("order_key")))
        ]
        valid_count = len(selected_orders) - len(pending_orders)
        restored_count = sum(bool(_restorable_historical_service(item)) for item in selected_orders)

        q1, q2, q3 = st.columns([1.5, 1, 1])
        q1.metric("Ordini selezionati", len(selected_orders))
        q2.metric("Con tariffe valide", valid_count)
        q3.metric("Da quotare / ricalcolare", len(pending_orders))
        if restored_count:
            st.caption(
                f"{restored_count} tariff{'a' if restored_count == 1 else 'e'} precedent{'e' if restored_count == 1 else 'i'} "
                "ripristinate: non vengono richieste nuovamente a Packlink finché non modifichi manualmente il singolo ordine."
            )

        flash_key = f"packlink_quote_all_flash_v227_{seller_id}"
        flash = st.session_state.pop(flash_key, None)
        if isinstance(flash, Mapping):
            if flash.get("failures"):
                failures = list(flash.get("failures") or [])
                st.warning(
                    f"Ricalcolo completato con {len(failures)} errori. "
                    + (f"Primo errore: {failures[0]}" if failures else "")
                )
            elif flash.get("done"):
                st.success("Tariffe Packlink aggiornate soltanto per gli ordini che ne avevano bisogno.")

        quote_button_label = (
            f"Calcola tariffe solo per gli ordini senza tariffa valida ({len(pending_orders)})"
            if pending_orders else
            "Tutte le tariffe sono già valide · nessun ricalcolo necessario"
        )
        if st.button(
            quote_button_label,
            type="primary", use_container_width=True, disabled=not bool(pending_orders),
            key=f"packlink_load_missing_quotes_v227_{seller_id}",
        ):
            progress = st.progress(0.0)
            label = st.empty()
            failures: list[str] = []
            total_pending = len(pending_orders)
            for position, order in enumerate(pending_orders, 1):
                ok, error = _load_quotes(order)
                if not ok:
                    failures.append(f"{order.get('order_id')}: {error}")
                progress.progress(position / max(1, total_pending))
                label.caption(f"Tariffe ricalcolate: {position} di {total_pending} ordini necessari")
            st.session_state[flash_key] = {"done": True, "failures": failures}
            st.rerun()

    _render_packlink_quote_all_controls()

    st.caption(
        "Gli ordini in rigenerazione con tariffa precedente valida sono già pronti e non vengono riquotati. "
        "Calcola una nuova tariffa soltanto per gli ordini nuovi o che modifichi manualmente."
    )

    @st.fragment
    def _render_packlink_order_card(order_key_value: str) -> None:
        order = selected_orders_by_key.get(clean_text(order_key_value))
        if not isinstance(order, Mapping):
            return
        current_package_profiles = saved_package_profiles(seller_id, limit=5000)
        current_package_profiles_by_id = {
            int(item["id"]): item for item in current_package_profiles
            if item.get("id") not in (None, "")
        }
        order_key = clean_text(order.get("order_key"))
        account_id = int(order.get("marketplace_account_id") or 0)
        declared_default = order_declared_value(order, accounting_by_account.get(account_id, []))
        form_token = _order_token(order_key)
        candidate_key = f"packlink_batch_candidate_v230_{seller_id}_{form_token}"
        existing_shipping = _existing_shipping(order)
        previous_config = _previous_order_config(order)
        previous_package = (
            previous_config.get("_previous_package")
            if isinstance(previous_config, Mapping) and isinstance(previous_config.get("_previous_package"), Mapping)
            else None
        )

        with st.container(border=True):
            header1, header2, header3, header4 = st.columns([1.4, 1.1, 1.1, 1])
            header1.markdown(
                f"**{marketplace_labels.get(order['marketplace'])} · Ordine {order['order_id']}**"
            )
            header2.caption(f"Fornitore: {order.get('supplier') or '—'}")
            header3.caption(f"Cliente: {order.get('customer_name') or '—'}")
            header4.caption(
                f"{order.get('postal_code') or '—'} {order.get('city') or ''} · "
                f"{order.get('country_code') or '—'}"
            )
            st.caption(
                f"{order.get('product_count', 0)} righe prodotto · quantità {order.get('quantity', 0)} · "
                f"{order.get('product_title') or 'Prodotto non indicato'}"
            )

            force_recreate = False
            if existing_shipping:
                reference = clean_text(existing_shipping.get("shipment_reference")) or "riferimento disponibile in Packlink"
                carrier_service = " ".join(
                    part for part in (
                        clean_text(existing_shipping.get("carrier")),
                        clean_text(existing_shipping.get("service")),
                    ) if part
                )
                st.success(
                    "✅ **Ordine già organizzato per la spedizione su Packlink** · "
                    f"riferimento **{reference}**"
                    + (f" · {carrier_service}" if carrier_service else "")
                )
                if isinstance(previous_config, Mapping):
                    old_price = previous_config.get("quoted_price")
                    old_currency = clean_text(previous_config.get("currency")) or "EUR"
                    old_carrier = clean_text(previous_config.get("carrier"))
                    old_service = clean_text(previous_config.get("service"))
                    parts = []
                    if previous_package:
                        parts.append(
                            f"pacco {float(previous_package.get('weight') or 0):.2f} kg · "
                            f"{float(previous_package.get('length') or 0):g}×"
                            f"{float(previous_package.get('width') or 0):g}×"
                            f"{float(previous_package.get('height') or 0):g} cm"
                        )
                    if old_carrier or old_service:
                        parts.append("servizio " + " ".join(x for x in (old_carrier, old_service) if x))
                    if old_price not in (None, ""):
                        try:
                            parts.append(f"costo storico {float(old_price):.2f} {old_currency}")
                        except Exception:
                            parts.append(f"costo storico {old_price} {old_currency}")
                    if parts:
                        st.info("Ultima configurazione usata: " + " · ".join(parts))
                force_recreate = st.checkbox(
                    "Seleziona se vuoi ricreare e forzare l'ordine per Packlink",
                    key=f"packlink_force_recreate_v218_{seller_id}_{form_token}",
                    help=(
                        "Di default Marketplace Hub impedisce una seconda spedizione pronta per pagamento per lo stesso ordine. "
                        "Attiva questa casella soltanto se vuoi crearne volontariamente un'altra."
                    ),
                )
                if not force_recreate:
                    st.session_state.pop(candidate_key, None)
                    st.caption(
                        "Nessun nuovo invio verrà effettuato per questo ordine. "
                        "La spunta di forzatura riattiva pacco, tariffe e creazione spedizione."
                    )
                    return
                st.warning("Ricreazione forzata attiva: verrà generato un nuovo riferimento Packlink.")

            weight_info = order.get("_packlink_weight_info") or {}
            remembered = (
                order.get("_packlink_remembered_package")
                if isinstance(order.get("_packlink_remembered_package"), Mapping) else None
            )
            keys = _ensure_package_state(order)

            choice_options = []
            if previous_package:
                choice_options.append("previous")
            choice_options.append("auto")
            if remembered and remembered.get("id") not in (None, ""):
                remembered_option = f"saved:{int(remembered['id'])}"
                if remembered_option not in choice_options:
                    choice_options.append(remembered_option)
            # Tutti i pacchi creati/memorizzati dal Seller devono restare disponibili.
            for profile_row in current_package_profiles:
                option = f"saved:{int(profile_row['id'])}"
                if option not in choice_options:
                    choice_options.append(option)
            for parcel in parcels:
                if clean_text(parcel.get("id")):
                    option = f"packlink:{clean_text(parcel.get('id'))}"
                    if option not in choice_options:
                        choice_options.append(option)
            choice_options.append("manual")

            def _format_package_choice(choice: str) -> str:
                if choice == "previous":
                    if previous_package:
                        return (
                            "Ultimo pacco usato per QUESTO ordine · "
                            f"{float(previous_package.get('weight') or 0):.2f} kg · "
                            f"{float(previous_package.get('length') or 0):g}×"
                            f"{float(previous_package.get('width') or 0):g}×"
                            f"{float(previous_package.get('height') or 0):g} cm"
                        )
                    return "Ultimo pacco usato per questo ordine"
                if choice == "auto":
                    remembered_note = " + ultime dimensioni usate per questi prodotti" if remembered else ""
                    return f"Automatico ordine · peso da listino{remembered_note}"
                if choice == "manual":
                    return "Inserimento manuale / valori correnti"
                if choice.startswith("saved:"):
                    try:
                        profile_row = current_package_profiles_by_id[int(choice.split(":", 1)[1])]
                        return (
                            f"Memoria · {profile_row.get('label') or 'Pacco'} · "
                            f"{float(profile_row.get('weight') or 0):.2f} kg · "
                            f"{float(profile_row.get('length') or 0):g}×"
                            f"{float(profile_row.get('width') or 0):g}×"
                            f"{float(profile_row.get('height') or 0):g} cm · "
                            f"usato {int(profile_row.get('use_count') or 0)} volte"
                        )
                    except Exception:
                        return "Pacco memorizzato"
                raw_id = choice.split(":", 1)[1]
                parcel = next(
                    (item for item in parcels if clean_text(item.get("id")) == raw_id), {}
                )
                return (
                    f"Packlink · {parcel.get('name') or 'Pacco'} · "
                    f"{float(parcel.get('weight') or 0):.2f} kg · "
                    f"{float(parcel.get('length') or 0):g}×{float(parcel.get('width') or 0):g}×"
                    f"{float(parcel.get('height') or 0):g} cm"
                )

            default_choice = "previous" if previous_package else "auto"
            current_choice = clean_text(st.session_state.get(keys["choice"])) or default_choice
            if current_choice not in choice_options:
                st.session_state[keys["choice"]] = default_choice
                current_choice = default_choice
            choice = st.radio(
                "Configurazione pacco per questo ordine",
                choice_options,
                format_func=_format_package_choice,
                key=keys["choice"],
            )
            if clean_text(st.session_state.get(keys["applied"])) != choice:
                _apply_package_choice(order, choice)
                # Nessun secondo rerun: i widget del pacco non sono ancora stati
                # creati in questo ciclo e leggono subito i nuovi valori.

            p1, p2, p3, p4 = st.columns(4)
            p1.number_input(
                "Peso (kg)", min_value=0.01, step=0.05, format="%.2f", key=keys["weight"],
                help=(
                    "Viene proposto il peso ricavato dai listini quando disponibile; "
                    "puoi correggerlo per il pacco reale."
                ),
            )
            p2.number_input(
                "Lunghezza (cm)", min_value=1.0, step=1.0, format="%.1f", key=keys["length"]
            )
            p3.number_input(
                "Larghezza (cm)", min_value=1.0, step=1.0, format="%.1f", key=keys["width"]
            )
            p4.number_input(
                "Altezza (cm)", min_value=1.0, step=1.0, format="%.1f", key=keys["height"]
            )
            order_package = _current_package(order)
            try:
                normalized_order_package = package_payload(order_package)
                package_ok = True
            except Exception as package_error:
                normalized_order_package = {}
                package_ok = False
                st.error(str(package_error))

            if weight_info.get("uses_fallback"):
                st.warning(
                    f"Peso proposto: {float(order_package.get('weight') or 0):.2f} kg · "
                    f"{weight_info.get('source') or 'peso non disponibile nei listini'}"
                )
            else:
                st.success(
                    f"Peso proposto dal listino: {float(weight_info.get('weight_kg') or 0):.2f} kg · "
                    f"{weight_info.get('source') or ''}"
                )
            if remembered:
                st.caption(
                    f"Memoria prodotti: ultimo pacco usato {float(remembered.get('weight') or 0):.2f} kg · "
                    f"{float(remembered.get('length') or 0):g}×{float(remembered.get('width') or 0):g}×"
                    f"{float(remembered.get('height') or 0):g} cm."
                )

            memory_col1, memory_col2 = st.columns([2, 1])
            package_memory_label = memory_col1.text_input(
                "Nome pacco da memorizzare (facoltativo)",
                key=f"packlink_package_label_v218_{seller_id}_{form_token}",
                placeholder="Esempio: Ciclette grande 114×102×120",
            )
            if memory_col2.button(
                "Salva questo pacco in memoria",
                use_container_width=True,
                disabled=not package_ok,
                key=f"packlink_save_package_v218_{seller_id}_{form_token}",
            ):
                try:
                    profile = save_package_profile(
                        seller_id, order_package, label=package_memory_label, increment_use=False,
                    )
                    st.success(f"Pacco memorizzato: {profile.get('label') or 'Pacco'}")
                except Exception as exc:
                    st.error(f"Memorizzazione pacco non riuscita: {exc}")

            with st.expander("Dettaglio peso prodotti", expanded=False):
                weight_details = list(weight_info.get("details") or [])
                if weight_details:
                    st.dataframe(
                        pd.DataFrame(weight_details), use_container_width=True, hide_index=True,
                        column_config={
                            "unit_weight_kg": st.column_config.NumberColumn(
                                "Peso unitario kg", format="%.4f"
                            ),
                            "line_weight_kg": st.column_config.NumberColumn(
                                "Peso riga kg", format="%.4f"
                            ),
                        },
                    )
                else:
                    st.caption("Nessun dettaglio peso disponibile per questo ordine.")

            quote_entry = (
                quotes_state.get(order_key)
                if isinstance(quotes_state.get(order_key), Mapping) else {}
            )
            current_signature = (
                packlink_package_signature(order_package) if package_ok else ""
            )
            quote_signature = clean_text(quote_entry.get("package_signature"))
            services = (
                list(quote_entry.get("services") or [])
                if quote_signature == current_signature else []
            )

            historical_service = _historical_service(order)
            restorable_historical_service = _restorable_historical_service(order)
            restore_previous_tariff = isinstance(restorable_historical_service, Mapping)
            if restore_previous_tariff:
                historical_service = dict(restorable_historical_service)
                services = [dict(historical_service)] + [
                    item for item in services
                    if isinstance(item, Mapping) and not _same_service(item, historical_service)
                ]

            if quote_entry and quote_signature and quote_signature != current_signature:
                st.warning(
                    "Peso/dimensioni del pacco sono cambiati: le tariffe precedenti sono obsolete "
                    "e vanno ricalcolate."
                )

            manual_tariff_key = f"packlink_manual_tariff_v223_{seller_id}_{form_token}"
            if st.button(
                "Calcola / aggiorna tariffe per questo ordine",
                use_container_width=True,
                disabled=not package_ok,
                key=f"packlink_quote_one_v218_{seller_id}_{form_token}",
            ):
                # This is an explicit single-order edit: from now on this row may use new tariffs.
                st.session_state[manual_tariff_key] = True
                st.session_state[f"packlink_restore_previous_tariff_v223_{seller_id}_{form_token}"] = False
                ok, error = _load_quotes(order)
                if ok:
                    st.success(
                        "Tariffe Packlink aggiornate. Questa configurazione pacco è stata memorizzata."
                    )
                    quote_entry = quotes_state.get(order_key) if isinstance(quotes_state.get(order_key), Mapping) else {}
                    quote_signature = clean_text(quote_entry.get("package_signature"))
                    services = list(quote_entry.get("services") or []) if quote_signature == current_signature else []
                else:
                    st.error(f"Lettura tariffe Packlink non riuscita: {error}")

            # Mostra l'ultimo errore anche dopo il rerun, così è possibile correggere
            # CAP/peso/dimensioni senza perdere la diagnostica del singolo ordine.
            if not services and clean_text(quote_entry.get("error")):
                st.error(f"Ultimo errore tariffe: {clean_text(quote_entry.get('error'))}")

            if not services:
                st.session_state.pop(candidate_key, None)
                st.info(
                    "Nessuna tariffa valida per il pacco corrente. Verifica CAP, peso e dimensioni "
                    "oppure modifica il collo e ricalcola. Gli altri ordini restano elaborabili."
                )
                return

            quotes_frame = pd.DataFrame([
                {
                    "Corriere": item.get("carrier"),
                    "Servizio": item.get("service"),
                    "Categoria": item.get("category"),
                    "Prezzo totale": item.get("price"),
                    "Prezzo base": item.get("base_price"),
                    "Imposte": item.get("tax_price"),
                    "Valuta": item.get("currency"),
                    "Transito": item.get("transit_time") or (
                        f"{item.get('transit_hours')} h" if item.get("transit_hours") else ""
                    ),
                    "Consegna stimata": item.get("estimated_delivery"),
                    "Consegna al punto": "Sì" if item.get("delivery_to_parcelshop") else "No",
                    "Partenza drop-off": "Sì" if item.get("dropoff") else "No",
                    "Etichetta richiesta": "Sì" if item.get("labels_required") else "No",
                    "Origine": "TARIFFA PRECEDENTE" if bool(item.get("_historical")) else "Packlink attuale",
                    "Info servizio": _service_info_text(item),
                }
                for item in services
            ])
            st.dataframe(
                quotes_frame,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Prezzo totale": st.column_config.NumberColumn(format="%.2f"),
                    "Prezzo base": st.column_config.NumberColumn(format="%.2f"),
                    "Imposte": st.column_config.NumberColumn(format="%.2f"),
                },
            )

            service_map = {
                clean_text(item.get("id")): item
                for item in services if clean_text(item.get("id"))
            }
            service_ids = list(service_map)
            service_key = f"packlink_service_radio_v218_{seller_id}_{form_token}"
            current_service_state = clean_text(st.session_state.get(service_key))
            if current_service_state not in service_ids:
                preferred_service_id = ""
                if restore_previous_tariff and isinstance(historical_service, Mapping):
                    historical_id = clean_text(historical_service.get("id"))
                    if historical_id in service_ids:
                        preferred_service_id = historical_id
                    else:
                        for candidate_id, candidate_service in service_map.items():
                            if _same_service(candidate_service, historical_service):
                                preferred_service_id = candidate_id
                                break
                if preferred_service_id:
                    st.session_state[service_key] = preferred_service_id
                else:
                    st.session_state.pop(service_key, None)
            selected_service_id = st.radio(
                "Scegli il servizio di spedizione",
                service_ids,
                index=None,
                format_func=lambda service_id: (
                    f"{service_map[service_id].get('carrier') or 'Corriere'} · "
                    f"{service_map[service_id].get('service') or service_id} · "
                    f"{float(service_map[service_id].get('price') or 0):.2f} "
                    f"{service_map[service_id].get('currency') or 'EUR'} · "
                    f"{service_map[service_id].get('transit_time') or 'tempi n/d'}"
                    + (
                        " · RIPRISTINATO AUTOMATICAMENTE · USATO PRIMA"
                        if restore_previous_tariff and isinstance(previous_config, Mapping) and (
                            clean_text(previous_config.get('service_id')) == clean_text(service_id)
                            or (
                                clean_text(previous_config.get('carrier')).lower() == clean_text(service_map[service_id].get('carrier')).lower()
                                and clean_text(previous_config.get('service')).lower() == clean_text(service_map[service_id].get('service')).lower()
                            )
                        ) else ""
                    )
                ),
                key=service_key,
            )

            declared_key = f"packlink_declared_v218_{seller_id}_{form_token}"
            if declared_key not in st.session_state:
                previous_declared = _previous_declared_value(order) if force_recreate else None
                st.session_state[declared_key] = float(max(0.01, previous_declared or declared_default))
            declared_value = float(st.number_input(
                "Valore dichiarato contenuto (€)",
                min_value=0.01,
                step=0.01,
                format="%.2f",
                key=declared_key,
                help=(
                    "Se la contabilità dell'ordine è disponibile viene proposto il valore vendita; "
                    "altrimenti puoi inserirlo manualmente."
                ),
            ))

            selected_service = (
                service_map.get(clean_text(selected_service_id)) if selected_service_id else None
            )
            sender_phone_preview = clean_text(
                selected_sender_for_draft.get("phone")
                if isinstance(selected_sender_for_draft, Mapping) else ""
            ) or clean_text(
                selected_sender.get("phone") if isinstance(selected_sender, Mapping) else ""
            )
            destination_without_phone_fallback = packlink_destination_address(order)
            destination_preview = packlink_destination_address(
                order, fallback_phone=sender_phone_preview
            )
            st.caption(
                "Destinazione che verrà inviata a Packlink: "
                f"**{destination_preview.get('country') or '—'} · "
                f"{destination_preview.get('zip_code') or '—'} · "
                f"{destination_preview.get('city') or '—'}**"
            )
            if (
                not clean_text(destination_without_phone_fallback.get("phone"))
                and clean_text(destination_preview.get("phone"))
            ):
                st.caption("Telefono cliente assente: verrà usato automaticamente il numero del mittente.")
            st.session_state[candidate_key] = {
                "order_key": order_key,
                "order": dict(order),
                "package": dict(order_package),
                "service": dict(selected_service) if isinstance(selected_service, Mapping) else None,
                "declared_value": declared_value,
                "forced": bool(force_recreate),
                "previous_reference": clean_text(previous_config.get("shipment_reference")) if isinstance(previous_config, Mapping) else "",
                "previous_service_id": clean_text(previous_config.get("service_id")) if isinstance(previous_config, Mapping) else "",
                "previous_quoted_price": previous_config.get("quoted_price") if isinstance(previous_config, Mapping) else None,
                "previous_currency": clean_text(previous_config.get("currency")) if isinstance(previous_config, Mapping) else "",
            }

            if st.button(
                "Crea spedizione pronta per pagamento Packlink",
                type="primary",
                use_container_width=True,
                disabled=not bool(service_ids),
                key=f"packlink_create_one_v226_{seller_id}_{form_token}",
            ):
                if not selected_service:
                    st.error("Scegli un servizio Packlink prima della creazione.")
                else:
                    try:
                        reference = _create_draft_for_order(
                            order, order_package, selected_service, declared_value,
                            forced=bool(force_recreate),
                        )
                        st.session_state.pop(candidate_key, None)
                        update_connection_status(seller_id, ok=True)
                        remote_info = st.session_state.get(
                            f"packlink_remote_status_v219_{seller_id}_{form_token}", {}
                        )
                        remote_status = clean_text(remote_info.get("remote_status")) if isinstance(remote_info, Mapping) else ""
                        st.success(
                            f"Spedizione Packlink creata: {reference}. Destinazione operativa: "
                            "**Bozza > Pronti per il pagamento**. Il pacco utilizzato è stato memorizzato."
                            + (f" Stato API: {remote_status}." if remote_status else "")
                        )
                    except Exception as exc:
                        update_connection_status(seller_id, ok=False, error=str(exc))
                        st.error(f"Creazione spedizione Packlink non riuscita: {exc}")
                        if isinstance(exc, PacklinkAPIError):
                            st.caption(
                                "Diagnostica API: riepilogo tecnico senza nome, indirizzo, telefono o email completi."
                            )



    for _selected_order in selected_orders:
        _render_packlink_order_card(clean_text(_selected_order.get("order_key")))

    def _mass_candidate_key(order: Mapping[str, Any]) -> str:
        return f"packlink_batch_candidate_v230_{seller_id}_{_order_token(clean_text(order.get('order_key')))}"

    @st.fragment
    def _render_packlink_batch_creator() -> None:
        ready_candidates: list[dict[str, Any]] = []
        already_organized_count = 0
        missing_service_count = 0
        for order in selected_orders:
            form_token = _order_token(clean_text(order.get("order_key")))
            force_key = f"packlink_force_recreate_v218_{seller_id}_{form_token}"
            forced = bool(st.session_state.get(force_key))
            if _existing_shipping(order) and not forced:
                already_organized_count += 1
                st.session_state.pop(_mass_candidate_key(order), None)
                continue
            candidate = st.session_state.get(_mass_candidate_key(order))
            if isinstance(candidate, Mapping) and isinstance(candidate.get("service"), Mapping):
                refreshed = dict(candidate)
                refreshed["order_key"] = clean_text(order.get("order_key"))
                refreshed["order"] = dict(order)
                st.session_state[_mass_candidate_key(order)] = refreshed
                ready_candidates.append(refreshed)
            else:
                missing_service_count += 1

        st.markdown("#### Creazione massiva spedizioni pronte per pagamento Packlink")
        b1, b2, b3 = st.columns(3)
        b1.metric("Pronti per invio massivo", len(ready_candidates))
        b2.metric("Già organizzati / non forzati", already_organized_count)
        b3.metric("Senza tariffa disponibile", missing_service_count)
        st.caption(
            "Per gli ordini selezionati con «Seleziona tutti da rigenerare», pacco e tariffa precedenti vengono "
            "ripristinati automaticamente e sono già candidati all'invio massivo. Non devi riselezionare i servizi. "
            "Solo una modifica manuale del singolo ordine sostituisce la sua configurazione precedente."
        )
        st.caption(
            "v230: prima di ogni POST la destinazione viene ricostruita dall'ordine corrente mostrato a video; "
            "Paese e Città/codice postale non vengono mai riutilizzati da una vecchia candidatura di sessione."
        )

        batch_result_key = f"packlink_batch_result_v219_{seller_id}"
        previous_batch_result = st.session_state.get(batch_result_key)
        if isinstance(previous_batch_result, list) and previous_batch_result:
            st.dataframe(pd.DataFrame(previous_batch_result), use_container_width=True, hide_index=True)

        if st.button(
            f"Crea massivamente tutte le spedizioni Packlink pronte per pagamento ({len(ready_candidates)})",
            type="primary", use_container_width=True,
            # v253: le schede ordine sono frammenti indipendenti. Dopo la scelta manuale
            # di una tariffa, il candidato è già in session_state ma questo riepilogo può
            # essere ancora visivamente fermo al rerun precedente. Il pulsante resta quindi
            # cliccabile: il suo stesso rerun rilegge tutti i candidati aggiornati e procede.
            disabled=not bool(selected_orders),
            key=f"packlink_create_batch_v253_{seller_id}",
        ):
            if not ready_candidates:
                st.warning(
                    "Nessuna spedizione è ancora pronta per l'invio massivo. Scegli una tariffa "
                    "per almeno un ordine: il pulsante resta attivo e rilegge automaticamente "
                    "le scelte manuali al click."
                )
                return
            progress = st.progress(0.0)
            status_box = st.empty()
            results: list[dict[str, Any]] = []
            success_count = 0
            for position, candidate in enumerate(ready_candidates, 1):
                order = candidate["order"]
                try:
                    reference = _create_draft_for_order(
                        order, candidate["package"], candidate["service"],
                        float(candidate["declared_value"]), forced=bool(candidate.get("forced")),
                    )
                    success_count += 1
                    st.session_state.pop(_mass_candidate_key(order), None)
                    results.append({
                        "Ordine": clean_text(order.get("order_id")),
                        "Esito": "Creato" + (" · FORZATO" if candidate.get("forced") else ""),
                        "Riferimento Packlink": reference,
                        "Corriere": clean_text(candidate["service"].get("carrier")),
                        "Servizio": clean_text(candidate["service"].get("service")),
                        "Errore": "",
                    })
                except Exception as exc:
                    results.append({
                        "Ordine": clean_text(order.get("order_id")),
                        "Esito": "Errore", "Riferimento Packlink": "",
                        "Corriere": clean_text(candidate["service"].get("carrier")),
                        "Servizio": clean_text(candidate["service"].get("service")),
                        "Errore": clean_text(exc),
                    })
                progress.progress(position / max(1, len(ready_candidates)))
                status_box.caption(
                    f"Invio spedizioni: {position} di {len(ready_candidates)} · create {success_count}"
                )
            st.session_state[batch_result_key] = results
            if success_count:
                update_connection_status(seller_id, ok=True)
            if success_count == len(ready_candidates):
                st.success(
                    f"Creazione massiva completata: {success_count} spedizioni create. "
                    "Apri Packlink PRO → Bozza → Pronti per il pagamento per pagarle insieme."
                )
            else:
                st.warning(
                    f"Creazione massiva completata: {success_count} create, "
                    f"{len(ready_candidates) - success_count} con errore. "
                    "Gli ordini riusciti restano memorizzati e non verranno duplicati al prossimo tentativo."
                )

    _render_packlink_batch_creator()

# -----------------------------------------------------------------------------
# Storico pronti per pagamento e sincronizzazione spedizioni create/pagate
# -----------------------------------------------------------------------------
st.divider()
st.markdown("### 4. Pronti per pagamento e spedizioni Packlink")
if created_drafts:
    st.dataframe(
        pd.DataFrame([
            {
                "Creato": item.get("created_at"),
                "Marketplace": marketplace_labels.get(clean_text(item.get("marketplace")), clean_text(item.get("marketplace")).title()),
                "Ordine": item.get("order_id"),
                "Riferimento Packlink": item.get("shipment_reference"),
                "Corriere": item.get("carrier"),
                "Servizio": item.get("service"),
                "Prezzo": item.get("quoted_price"),
                "Valuta": item.get("currency"),
                "Stato": item.get("status"),
            }
            for item in created_drafts
        ]),
        use_container_width=True,
        hide_index=True,
        column_config={"Prezzo": st.column_config.NumberColumn(format="%.2f")},
    )
else:
    st.caption("Nessuna spedizione Packlink pronta per pagamento creata da Marketplace Hub.")

with st.expander("Sincronizza spedizioni Packlink già presenti / pagate", expanded=False):
    st.write(
        "Questa funzione mantiene l'archivio delle spedizioni Packlink già create, utile per "
        "tracking e corriere nella sezione Tracciabilità ordini."
    )
    if st.button("Scarica / aggiorna spedizioni Packlink", use_container_width=True):
        try:
            with st.spinner("Sincronizzazione spedizioni Packlink…"):
                result = sync_shipments(seller_id, client)
            update_connection_status(seller_id, ok=True)
            st.success(
                f"{result['fetched']:,} spedizioni lette · {result['inserted']:,} nuove · "
                f"{result['updated']:,} aggiornate."
            )
            st.rerun()
        except Exception as exc:
            update_connection_status(seller_id, ok=False, error=str(exc))
            st.error(f"Sincronizzazione spedizioni Packlink non riuscita: {exc}")
    shipments = cached_shipments(seller_id)
    st.metric("Spedizioni Packlink memorizzate", len(shipments))
