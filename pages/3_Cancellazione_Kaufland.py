from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from services.db import execute, json_text, now_iso, rows
from services.kaufland import KauflandClient
from services.kaufland_history import active_sent_offers
from services.kaufland_live_inventory import (
    cached_summary,
    cached_units,
    ensure_schema,
    latest_syncs,
    mark_units_removed,
    sync_storefront,
)
from services.security import decrypt_dict
from services.session import bootstrap, seller_selector

try:
    from st_aggrid import AgGrid, DataReturnMode, GridOptionsBuilder, GridUpdateMode
except ImportError:  # pragma: no cover - fallback runtime
    AgGrid = None


COUNTRY_NAMES = {
    "de": "Germania",
    "at": "Austria",
    "pl": "Polonia",
    "cz": "Rep. Ceca",
    "sk": "Slovacchia",
    "fr": "Francia",
    "it": "Italia",
    "es": "Spagna",
    "nl": "Paesi Bassi",
}
CURRENCIES = {
    "de": "EUR",
    "at": "EUR",
    "fr": "EUR",
    "it": "EUR",
    "sk": "EUR",
    "es": "EUR",
    "nl": "EUR",
    "pl": "PLN",
    "cz": "CZK",
}
FALLBACK_STOREFRONTS = ["de", "at", "pl", "cz", "sk", "fr", "it"]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _label(code: str) -> str:
    value = _clean(code).lower()
    return f"{COUNTRY_NAMES.get(value, value.upper())} ({value.upper()})"


def _environment(playground: bool) -> str:
    return "test" if playground else "live"


def _supplier_prefix(value: str) -> str:
    return _clean(value).split("_", 1)[0].strip() if _clean(value) else ""


def _publication_origins(seller_id: int, account_id: int, environment: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Map live id_offer values back to list/supplier when Marketplace Hub published them.

    Offers created outside Marketplace Hub deliberately remain classified as unknown.
    """
    list_rows = rows(
        """
        SELECT pl.id,pl.name,s.name supplier_name
        FROM price_lists pl JOIN suppliers s ON s.id=pl.supplier_id
        WHERE pl.owner_seller_id=?
        """,
        (seller_id,),
    )
    list_info = {int(item["id"]): item for item in list_rows}
    operation_rows = rows(
        """
        SELECT id,price_list_id,operation_type,storefront,details_json,created_at
        FROM operations
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace='kaufland'
        ORDER BY created_at,id
        """,
        (seller_id, account_id),
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in operation_rows:
        list_id = int(item.get("price_list_id") or 0)
        if list_id:
            grouped.setdefault(list_id, []).append(item)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for list_id, operations in grouped.items():
        info = list_info.get(list_id, {})
        for offer in active_sent_offers(operations, environment):
            key = (_clean(offer.get("paese")).lower(), _clean(offer.get("sku_inviato")))
            if not all(key):
                continue
            result[key] = {
                "price_list_id": list_id,
                "price_list_name": _clean(info.get("name")),
                "supplier_name": _clean(info.get("supplier_name")),
            }
    return result


def _available_storefronts(client: KauflandClient, seller_id: int, account_id: int, environment: str) -> list[str]:
    state_key = f"kaufland_real_storefronts_v191_{account_id}_{environment}"
    current = st.session_state.get(state_key)
    if isinstance(current, list) and current:
        return current
    values: list[str] = []
    try:
        values.extend(client.storefronts())
    except Exception:
        pass
    values.extend(
        _clean(item.get("storefront")).lower()
        for item in cached_summary(seller_id, account_id, environment)
        if _clean(item.get("storefront"))
    )
    values.extend(
        _clean(item.get("storefront")).lower()
        for item in rows(
            """
            SELECT DISTINCT storefront FROM operations
            WHERE marketplace_account_id=? AND marketplace='kaufland' AND storefront<>''
            """,
            (account_id,),
        )
    )
    values.extend(FALLBACK_STOREFRONTS)
    cleaned = [value for value in dict.fromkeys(values) if value]
    st.session_state[state_key] = cleaned
    return cleaned


def _checkbox_form(
    *,
    title: str,
    codes: list[str],
    state_key: str,
    form_key: str,
    default_all: bool = True,
) -> list[str]:
    valid = list(dict.fromkeys(_clean(code).lower() for code in codes if _clean(code)))
    stored = st.session_state.get(state_key)
    if not isinstance(stored, list):
        stored = valid[:] if default_all else []
        st.session_state[state_key] = stored
    selected = set(stored) & set(valid)
    h1, h2 = st.columns(2)
    if h1.button("Seleziona tutti", key=f"{form_key}_all", use_container_width=True):
        st.session_state[state_key] = valid[:]
        for code in valid:
            st.session_state[f"{form_key}_{code}"] = True
        st.rerun()
    if h2.button("Deseleziona tutti", key=f"{form_key}_none", use_container_width=True):
        st.session_state[state_key] = []
        for code in valid:
            st.session_state[f"{form_key}_{code}"] = False
        st.rerun()
    with st.form(form_key):
        st.markdown(f"**{title}**")
        columns = st.columns(4)
        form_values: dict[str, bool] = {}
        for index, code in enumerate(valid):
            widget_key = f"{form_key}_{code}"
            if widget_key not in st.session_state:
                st.session_state[widget_key] = code in selected
            form_values[code] = columns[index % 4].checkbox(_label(code), key=widget_key)
        applied = st.form_submit_button("Applica selezione", type="primary", use_container_width=True)
        if applied:
            st.session_state[state_key] = [code for code, enabled in form_values.items() if enabled]
            st.rerun()
    return [code for code in valid if code in set(st.session_state.get(state_key, []))]


def _sync_selected(
    client: KauflandClient,
    *,
    seller_id: int,
    account_id: int,
    environment: str,
    storefronts: list[str],
    force_full: bool = False,
) -> list[dict[str, Any]]:
    if not storefronts:
        return []
    overall = st.progress(0.0)
    status_box = st.empty()
    results: list[dict[str, Any]] = []
    for position, storefront in enumerate(storefronts, 1):
        country = _label(storefront)
        status_box.info(f"Scaricamento offerte reali {country}…")
        country_progress = st.progress(0.0)

        def page_progress(done: int, total: int | None) -> None:
            if total and total > 0:
                country_progress.progress(min(1.0, done / total))
            status_box.caption(
                f"{country}: lette {done:,}" + (f" di {total:,} offerte" if total else " offerte")
            )

        try:
            result = sync_storefront(
                client,
                seller_id=seller_id,
                account_id=account_id,
                environment=environment,
                storefront=storefront,
                force_full=force_full,
                progress=page_progress,
            )
            results.append({**result, "ok": True})
            country_progress.progress(1.0)
        except Exception as error:
            results.append({"storefront": storefront, "ok": False, "error": str(error)})
            country_progress.empty()
        overall.progress(position / len(storefronts))
    status_box.empty()
    overall.empty()
    return results


def _display_sync_result(results: list[dict[str, Any]]) -> None:
    if not results:
        return
    frame = pd.DataFrame(
        [
            {
                "Paese": _label(item.get("storefront", "")),
                "Esito": "OK" if item.get("ok") else "Errore",
                "Modalità": item.get("mode", ""),
                "Offerte presenti": item.get("seen", ""),
                "Nuove": item.get("inserted", ""),
                "Aggiornate": item.get("updated", ""),
                "Invariate": item.get("unchanged", ""),
                "Non più presenti": item.get("missing", ""),
                "Ripresa da posizione": item.get("resumed_from", 0) or "",
                "Ultima offerta scaricata": item.get("last_offer_id", ""),
                "Data ultima offerta": item.get("last_offer_change_iso", ""),
                "Messaggio": item.get("error", ""),
            }
            for item in results
        ]
    )
    st.dataframe(frame, hide_index=True, use_container_width=True)
    failures = [item for item in results if not item.get("ok")]
    if failures:
        st.warning(f"{len(failures)} Paesi non sono stati aggiornati. I dati già memorizzati non sono stati cancellati.")
    else:
        st.success("Offerte reali presenti su Kaufland scaricate e memorizzate correttamente.")


embedded = bool(st.session_state.get("_embedded_marketplace_deletion"))
if not embedded:
    bootstrap()
    st.title("Cancellazione offerte Kaufland")
seller_id = st.session_state.get("active_seller_id") if embedded else seller_selector()
if seller_id is None:
    st.stop()

ensure_schema()
accounts = rows(
    """
    SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND marketplace='kaufland' AND active=1
    ORDER BY account_name
    """,
    (seller_id,),
)
if not accounts:
    st.error("Configura un account Kaufland per questo Seller.")
    st.stop()
account_map = {f"{item['account_name']} · ID {item['id']}": item for item in accounts}
account = account_map[
    st.selectbox("Account Kaufland", list(account_map), key="delete_kaufland_account_v191")
]
playground = st.checkbox(
    "Playground (test)",
    value=True,
    key=f"delete_kaufland_playground_v191_{account['id']}",
)
environment = _environment(playground)
credentials = decrypt_dict(account["credentials_encrypted"])
client = KauflandClient(
    credentials.get("client_key", ""),
    credentials.get("secret_key", ""),
    playground,
    requests_per_second=25.0,
)

st.info(
    f"Ambiente API attivo: {'PLAYGROUND (test)' if playground else 'PRODUZIONE'}. "
    "La pagina mostra esclusivamente le offerte restituite realmente dall'API Kaufland."
)
if not playground:
    st.warning("Stai lavorando in produzione: le cancellazioni saranno eseguite realmente su Kaufland.")
flash = st.session_state.pop("kaufland_live_delete_flash_v191", None)
if flash:
    st.success(flash)

storefronts = _available_storefronts(client, int(seller_id), int(account["id"]), environment)
download_countries = _checkbox_form(
    title="Paesi da interrogare sul marketplace",
    codes=storefronts,
    state_key=f"kaufland_download_countries_v191_{account['id']}_{environment}",
    form_key=f"kaufland_download_form_v191_{account['id']}_{environment}",
)

st.subheader("Offerte realmente presenti sul marketplace")
pre_summary_rows = cached_summary(int(seller_id), int(account["id"]), environment)
has_cached_catalog = any(int(item.get("active_count") or 0) > 0 for item in pre_summary_rows)
st.caption(
    "Le offerte restano memorizzate nel database. Al primo utilizzo viene scaricato il catalogo "
    "completo. Negli aggiornamenti successivi Marketplace Hub conserva l'ultima offerta e la "
    "relativa data di modifica, aggiorna soltanto le righe nuove o cambiate e mantiene quelle "
    "invariate. Se un download viene interrotto, riparte automaticamente dall'ultima pagina "
    "completata. Kaufland non espone un filtro updated_since per GET /units: per individuare "
    "anche offerte eliminate o modificate fuori dal programma viene comunque eseguito un "
    "controllo leggero del manifest, senza riscaricare ogni volta tutti i dettagli prodotto."
)
main_sync_label = (
    "Aggiorna offerte nuove e modificate"
    if has_cached_catalog
    else "Scarica offerte presenti sul marketplace"
)
if st.button(
    main_sync_label,
    type="primary",
    use_container_width=True,
    disabled=not download_countries,
    key=f"download_real_kaufland_offers_v191_{account['id']}_{environment}",
):
    sync_result = _sync_selected(
        client,
        seller_id=int(seller_id),
        account_id=int(account["id"]),
        environment=environment,
        storefronts=download_countries,
    )
    st.session_state[f"kaufland_sync_result_v191_{account['id']}_{environment}"] = sync_result
    st.rerun()

with st.expander("Risincronizzazione completa", expanded=False):
    st.caption(
        "Rilegge integralmente le offerte e i dati prodotto dei Paesi selezionati. "
        "Usala soltanto per ricostruire la memoria locale o correggere dati incompleti."
    )
    full_confirm = st.checkbox(
        "Confermo la risincronizzazione completa",
        key=f"kaufland_full_sync_confirm_v191_{account['id']}_{environment}",
    )
    if st.button(
        "Avvia risincronizzazione completa",
        use_container_width=True,
        disabled=not download_countries or not full_confirm,
        key=f"kaufland_full_sync_v191_{account['id']}_{environment}",
    ):
        sync_result = _sync_selected(
            client,
            seller_id=int(seller_id),
            account_id=int(account["id"]),
            environment=environment,
            storefronts=download_countries,
            force_full=True,
        )
        st.session_state[f"kaufland_sync_result_v191_{account['id']}_{environment}"] = sync_result
        st.session_state[f"kaufland_full_sync_confirm_v191_{account['id']}_{environment}"] = False
        st.rerun()

sync_result = st.session_state.pop(
    f"kaufland_sync_result_v191_{account['id']}_{environment}", None
)
if sync_result:
    _display_sync_result(sync_result)

summary_rows = cached_summary(int(seller_id), int(account["id"]), environment)
latest_by_country = latest_syncs(int(account["id"]), environment)
summary_map = {_clean(item["storefront"]).lower(): dict(item) for item in summary_rows}
for code, sync in latest_by_country.items():
    summary_map.setdefault(code, {"storefront": code, "active_count": 0, "last_seen_at": ""})
    if sync.get("status") == "completed" and not summary_map[code].get("last_sync_at"):
        summary_map[code]["last_sync_at"] = sync.get("completed_at") or sync.get("started_at") or ""
if not summary_map:
    st.info("Non sono ancora state memorizzate offerte reali. Premi il pulsante di download.")
    st.stop()
summary = [summary_map[code] for code in sorted(summary_map)]
summary_frame = pd.DataFrame(
    [
        {
            "Paese": _label(item["storefront"]),
            "Codice": _clean(item["storefront"]).upper(),
            "Offerte reali presenti": int(item.get("active_count") or 0),
            "Ultima offerta scaricata": item.get("last_offer_id") or "",
            "ID ultima unità": item.get("last_id_unit") or "",
            "Data ultima modifica offerta": item.get("last_offer_change_iso") or "",
            "Ultimo aggiornamento riuscito": item.get("last_sync_at") or item.get("last_seen_at") or "",
            "Stato ultimo controllo": item.get("last_status") or "",
            "Ripresa salvata da posizione": int(item.get("resume_offset") or 0) or "",
        }
        for item in summary
    ]
)
st.dataframe(summary_frame, hide_index=True, use_container_width=True)

interrupted = [
    item for item in summary
    if int(item.get("resume_offset") or 0) > 0
    and _clean(item.get("last_status")).lower() in {"error", "running", "interrupted"}
]
if interrupted:
    details = "; ".join(
        f"{_label(item['storefront'])}: ripresa dalla posizione {int(item.get('resume_offset') or 0):,}, "
        f"ultima offerta {_clean(item.get('last_offer_id')) or '-'}"
        for item in interrupted
    )
    st.warning(
        "Un aggiornamento precedente non è terminato. Il prossimo comando ripartirà dal punto "
        f"salvato senza ricominciare da zero. {details}"
    )

active_storefronts = [
    _clean(item["storefront"]).lower()
    for item in summary
    if int(item.get("active_count") or 0) > 0
]
if not active_storefronts:
    st.success("Nei Paesi sincronizzati non risultano offerte attualmente presenti su Kaufland.")
    st.stop()
action_countries = _checkbox_form(
    title="Paesi da visualizzare e, se richiesto, cancellare",
    codes=active_storefronts,
    state_key=f"kaufland_action_countries_v191_{account['id']}_{environment}",
    form_key=f"kaufland_action_form_v191_{account['id']}_{environment}",
)
if not action_countries:
    st.warning("Seleziona almeno un Paese da visualizzare.")
    st.stop()

units = cached_units(
    int(seller_id), int(account["id"]), environment, action_countries, present_only=True
)
# id_unit is the authoritative unique key returned by Kaufland.
unique_units: dict[tuple[str, int], dict[str, Any]] = {}
for item in units:
    unique_units[(_clean(item["storefront"]).lower(), int(item["id_unit"]))] = item
units = list(unique_units.values())
origins = _publication_origins(int(seller_id), int(account["id"]), environment)
supplier_names = {
    _clean(item["name"]).lower(): _clean(item["name"])
    for item in rows("SELECT name FROM suppliers WHERE owner_seller_id=?", (seller_id,))
}

records: list[dict[str, Any]] = []
for item in units:
    code = _clean(item["storefront"]).lower()
    offer_id = _clean(item.get("id_offer"))
    origin = origins.get((code, offer_id), {})
    supplier = _clean(origin.get("supplier_name"))
    if not supplier:
        prefix = _supplier_prefix(offer_id)
        supplier = supplier_names.get(prefix.lower(), prefix or "Sconosciuto")
    list_name = _clean(origin.get("price_list_name")) or "Non associato"
    cents = item.get("listing_price_cents")
    price = float(cents) / 100 if cents not in (None, "") else None
    row_key = f"{code}:{int(item['id_unit'])}"
    records.append(
        {
            "_row_key": row_key,
            "_storefront": code,
            "_id_unit": int(item["id_unit"]),
            "Paese": _label(code),
            "ID unità": int(item["id_unit"]),
            "SKU / ID offerta": offer_id,
            "EAN": _clean(item.get("ean")),
            "Prodotto": _clean(item.get("title")),
            "Produttore": _clean(item.get("manufacturer")),
            "Fornitore": supplier,
            "Listino": list_name,
            "Prezzo": price,
            "Valuta": CURRENCIES.get(code, ""),
            "Quantità": item.get("amount"),
            "Stato": _clean(item.get("status")),
            "Condizione": _clean(item.get("condition_code")),
            "Ultima modifica API": _clean(item.get("date_lastchange_iso")),
            "Ultima verifica": _clean(item.get("last_seen_at")),
        }
    )

catalog = pd.DataFrame(records)
if catalog.empty:
    st.info("Nei Paesi selezionati non risultano offerte attualmente presenti.")
    st.stop()

st.subheader("Filtri sulle offerte reali")
filter_scope = f"{account['id']}_{environment}_{'-'.join(action_countries)}"
with st.form(f"kaufland_live_filters_v191_{filter_scope}"):
    f1, f2, f3 = st.columns(3)
    supplier_filter = f1.multiselect(
        "Fornitori",
        sorted(value for value in catalog["Fornitore"].dropna().unique() if _clean(value)),
    )
    list_filter = f2.multiselect(
        "Listini",
        sorted(value for value in catalog["Listino"].dropna().unique() if _clean(value)),
    )
    country_filter = f3.multiselect(
        "Paesi",
        sorted(catalog["Paese"].dropna().unique()),
        default=sorted(catalog["Paese"].dropna().unique()),
    )
    p1, p2, p3, p4 = st.columns(4)
    price_min = p1.number_input("Prezzo minimo", min_value=0.0, value=0.0, step=1.0)
    price_max = p2.number_input("Prezzo massimo (0 = nessun limite)", min_value=0.0, value=0.0, step=1.0)
    qty_min = p3.number_input("Quantità minima", min_value=0, value=0, step=1)
    qty_max = p4.number_input("Quantità massima (0 = nessun limite)", min_value=0, value=0, step=1)
    s1, s2, s3 = st.columns(3)
    status_filter = s1.multiselect(
        "Stato offerta",
        sorted(value for value in catalog["Stato"].dropna().unique() if _clean(value)),
    )
    condition_filter = s2.multiselect(
        "Condizione",
        sorted(value for value in catalog["Condizione"].dropna().unique() if _clean(value)),
    )
    search_text = s3.text_input("Cerca SKU, EAN, prodotto, produttore o ID unità")
    apply_filters = st.form_submit_button("Applica filtri", type="primary", use_container_width=True)

filter_state_key = f"kaufland_live_filter_state_v191_{filter_scope}"
if apply_filters:
    st.session_state[filter_state_key] = {
        "suppliers": supplier_filter,
        "lists": list_filter,
        "countries": country_filter,
        "price_min": float(price_min),
        "price_max": float(price_max),
        "qty_min": int(qty_min),
        "qty_max": int(qty_max),
        "statuses": status_filter,
        "conditions": condition_filter,
        "search": search_text,
    }
filters = st.session_state.get(filter_state_key, {})
visible = catalog.copy()
if filters.get("suppliers"):
    visible = visible[visible["Fornitore"].isin(filters["suppliers"])]
if filters.get("lists"):
    visible = visible[visible["Listino"].isin(filters["lists"])]
if filters.get("countries"):
    visible = visible[visible["Paese"].isin(filters["countries"])]
if float(filters.get("price_min") or 0) > 0:
    visible = visible[visible["Prezzo"].fillna(-1) >= float(filters["price_min"])]
if float(filters.get("price_max") or 0) > 0:
    visible = visible[visible["Prezzo"].fillna(float("inf")) <= float(filters["price_max"])]
if int(filters.get("qty_min") or 0) > 0:
    visible = visible[pd.to_numeric(visible["Quantità"], errors="coerce").fillna(0) >= int(filters["qty_min"])]
if int(filters.get("qty_max") or 0) > 0:
    visible = visible[pd.to_numeric(visible["Quantità"], errors="coerce").fillna(0) <= int(filters["qty_max"])]
if filters.get("statuses"):
    visible = visible[visible["Stato"].isin(filters["statuses"])]
if filters.get("conditions"):
    visible = visible[visible["Condizione"].isin(filters["conditions"])]
if _clean(filters.get("search")):
    term = _clean(filters["search"]).lower()
    searchable = visible[["SKU / ID offerta", "EAN", "Prodotto", "Produttore", "ID unità"]].astype(str)
    visible = visible[searchable.apply(lambda column: column.str.lower().str.contains(term, regex=False)).any(axis=1)]

st.caption(
    f"Offerte reali memorizzate nei Paesi selezionati: {len(catalog):,} · visibili dopo i filtri: {len(visible):,}."
)

selection_key = f"kaufland_live_selected_v191_{filter_scope}"
revision_key = f"kaufland_live_grid_revision_v191_{filter_scope}"
selected_keys = set(st.session_state.get(selection_key, []))
visible_keys = set(visible["_row_key"].astype(str))
b1, b2, b3 = st.columns(3)
if b1.button("Seleziona tutti i filtrati", use_container_width=True, key=f"live_select_all_v191_{filter_scope}"):
    st.session_state[selection_key] = sorted(selected_keys | visible_keys)
    st.session_state[revision_key] = int(st.session_state.get(revision_key, 0)) + 1
    st.rerun()
if b2.button("Deseleziona tutti i filtrati", use_container_width=True, key=f"live_select_none_visible_v191_{filter_scope}"):
    st.session_state[selection_key] = sorted(selected_keys - visible_keys)
    st.session_state[revision_key] = int(st.session_state.get(revision_key, 0)) + 1
    st.rerun()
if b3.button("Azzera selezione", use_container_width=True, key=f"live_select_none_v191_{filter_scope}"):
    st.session_state[selection_key] = []
    st.session_state[revision_key] = int(st.session_state.get(revision_key, 0)) + 1
    st.rerun()
selected_keys = set(st.session_state.get(selection_key, []))

if AgGrid is not None:
    table = visible.copy()
    builder = GridOptionsBuilder.from_dataframe(table)
    builder.configure_default_column(sortable=True, filter=True, resizable=True, minWidth=90)
    builder.configure_column("_row_key", hide=True)
    builder.configure_column("_storefront", hide=True)
    builder.configure_column("_id_unit", hide=True)
    builder.configure_column(
        "Paese",
        checkboxSelection=True,
        headerCheckboxSelection=True,
        headerCheckboxSelectionFilteredOnly=True,
        pinned="left",
        minWidth=155,
    )
    builder.configure_column("SKU / ID offerta", minWidth=260)
    builder.configure_column("Prodotto", minWidth=280)
    builder.configure_column("Fornitore", minWidth=150)
    builder.configure_column("Listino", minWidth=180)
    builder.configure_selection(selection_mode="multiple", use_checkbox=True)
    builder.configure_grid_options(rowMultiSelectWithClick=True, enableRangeSelection=True, animateRows=False)
    preselected = [index for index, key in enumerate(table["_row_key"].astype(str)) if key in selected_keys]
    manual_mode = getattr(GridUpdateMode, "MANUAL", GridUpdateMode.SELECTION_CHANGED)
    response = AgGrid(
        table,
        gridOptions=builder.build(),
        data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
        update_mode=manual_mode,
        pre_selected_rows=preselected,
        height=560,
        fit_columns_on_grid_load=False,
        theme="streamlit",
        key=f"kaufland_live_grid_v191_{filter_scope}_{st.session_state.get(revision_key, 0)}",
    )
    returned = response.get("selected_rows")
    submitted = False
    returned_keys: set[str] = set()
    if isinstance(returned, pd.DataFrame):
        submitted = not returned.empty
        if "_row_key" in returned.columns:
            returned_keys = set(returned["_row_key"].astype(str))
        elif {"Paese", "ID unità"}.issubset(returned.columns):
            lookup = {(str(row["Paese"]), str(row["ID unità"])): str(row["_row_key"]) for _, row in table.iterrows()}
            returned_keys = {
                lookup.get((str(row.get("Paese")), str(row.get("ID unità"))), "")
                for _, row in returned.iterrows()
            }
            returned_keys.discard("")
    elif isinstance(returned, list) and returned:
        submitted = True
        for item in returned:
            if isinstance(item, dict) and _clean(item.get("_row_key")):
                returned_keys.add(_clean(item["_row_key"]))
    if submitted:
        selected_keys = (selected_keys - visible_keys) | returned_keys
        st.session_state[selection_key] = sorted(selected_keys)
    st.caption(
        "Seleziona più offerte senza refresh e premi **Applica** nel riquadro AgGrid. "
        "Il quadratino dell'intestazione seleziona tutte le righe filtrate nella griglia."
    )
else:
    fallback = visible.copy()
    fallback.insert(0, "Seleziona", fallback["_row_key"].isin(selected_keys))
    edited = st.data_editor(
        fallback,
        hide_index=True,
        use_container_width=True,
        height=560,
        disabled=[column for column in fallback.columns if column != "Seleziona"],
        column_config={"Seleziona": st.column_config.CheckboxColumn(default=False)},
        key=f"kaufland_live_editor_v191_{filter_scope}_{st.session_state.get(revision_key, 0)}",
    )
    changed = set(edited.loc[edited["Seleziona"] == True, "_row_key"].astype(str))
    st.session_state[selection_key] = sorted((selected_keys - visible_keys) | changed)
    selected_keys = set(st.session_state[selection_key])

selected_rows = catalog[catalog["_row_key"].isin(selected_keys)].copy()
st.metric("Offerte selezionate nella tabella", len(selected_rows))

st.subheader("Cancellazione")
mode = st.radio(
    "Operazione da eseguire",
    [
        "Cancella tutte le offerte dei Paesi selezionati",
        "Cancella soltanto le offerte selezionate nella tabella",
    ],
    horizontal=True,
    key=f"kaufland_live_delete_mode_v191_{filter_scope}",
)
all_country_units = cached_units(
    int(seller_id), int(account["id"]), environment, action_countries, present_only=True
)
all_country_count = len({(_clean(item["storefront"]).lower(), int(item["id_unit"])) for item in all_country_units})
if mode.startswith("Cancella tutte"):
    st.warning(
        f"Saranno cancellate direttamente le offerte dell'ultima verifica già memorizzata per i Paesi scelti: "
        f"{', '.join(_label(code) for code in action_countries)}. Set pronto: {all_country_count:,} offerte. "
        "Non verrà eseguito un secondo download delle offerte prima della cancellazione."
    )
    required_confirmation = "CANCELLA TUTTO"
    button_label = "Cancella tutte le offerte dei Paesi selezionati"
    disabled_by_selection = all_country_count == 0
else:
    st.info(
        f"Saranno cancellate soltanto le {len(selected_rows):,} offerte selezionate nella tabella, "
        "usando gli ID già memorizzati senza riscaricarle."
    )
    required_confirmation = "CANCELLA"
    button_label = "Cancella offerte selezionate"
    disabled_by_selection = selected_rows.empty

confirmation = st.text_input(
    f"Scrivi {required_confirmation} per confermare",
    key=f"kaufland_live_confirmation_v191_{filter_scope}",
)
if st.button(
    button_label,
    type="primary",
    use_container_width=True,
    disabled=disabled_by_selection or confirmation.strip().upper() != required_confirmation,
    key=f"kaufland_live_execute_v191_{filter_scope}",
):
    # v243: usa ESATTAMENTE il set di offerte già scaricato/verificato e memorizzato.
    # Non eseguire un secondo GET /units prima della cancellazione: sarebbe un doppio
    # download inutile e rallenterebbe molto i cataloghi grandi. Gli eventuali 404
    # durante DELETE sono trattati come "già assente" e quindi come esito valido.
    current_by_key = {
        f"{_clean(item['storefront']).lower()}:{int(item['id_unit'])}": item
        for item in all_country_units
    }
    if mode.startswith("Cancella tutte"):
        tasks = list(current_by_key.values())
    else:
        tasks = [current_by_key[key] for key in selected_keys if key in current_by_key]
    deduped = {
        (_clean(item["storefront"]).lower(), int(item["id_unit"])): item for item in tasks
    }
    tasks = list(deduped.values())
    if not tasks:
        st.info("Nessuna delle offerte selezionate risulta ancora presente su Kaufland.")
        st.stop()

    progress = st.progress(0.0)
    label = st.empty()

    def delete_one(item: dict[str, Any]) -> dict[str, Any]:
        code = _clean(item["storefront"]).lower()
        unit_id = int(item["id_unit"])
        try:
            client.delete_unit(unit_id, code)
            return {"storefront": code, "id_unit": unit_id, "ok": True, "already_absent": False}
        except Exception as error:
            message = str(error)
            if "HTTP 404" in message or "not found" in message.lower():
                return {"storefront": code, "id_unit": unit_id, "ok": True, "already_absent": True}
            return {"storefront": code, "id_unit": unit_id, "ok": False, "error": message}

    results: list[dict[str, Any]] = []
    # Il client applica già il limite condiviso per Seller; il pool serve solo
    # a non bloccare la UI durante le attese di rete.
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(delete_one, item) for item in tasks]
        for index, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            progress.progress(index / len(tasks))
            label.caption(f"Elaborate {index:,} di {len(tasks):,} offerte reali.")

    successful_keys = [
        (item["storefront"], int(item["id_unit"])) for item in results if item.get("ok")
    ]
    mark_units_removed(int(account["id"]), environment, successful_keys)
    failures = [item for item in results if not item.get("ok")]
    already_absent = sum(1 for item in results if item.get("already_absent"))
    execute(
        """
        INSERT INTO operations(
            seller_id,marketplace_account_id,marketplace,storefront,operation_type,status,
            total_rows,success_rows,failed_rows,details_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            seller_id,
            account["id"],
            "kaufland",
            ",".join(action_countries),
            "ELIMINA_LIVE_API",
            "success" if not failures else "partial",
            len(results),
            len(results) - len(failures),
            len(failures),
            json_text(
                {
                    "environment": environment,
                    "mode": "all_countries" if mode.startswith("Cancella tutte") else "selected",
                    "countries": action_countries,
                    "already_absent": already_absent,
                    "errors": failures[:200],
                    "errors_omitted": max(0, len(failures) - 200),
                }
            ),
            now_iso(),
        ),
    )

    # Nessuna terza scansione automatica dopo la cancellazione. Il database locale
    # viene aggiornato direttamente con gli ID eliminati/già assenti; una nuova
    # sincronizzazione resta disponibile solo quando l'utente la richiede.
    remaining = cached_units(
        int(seller_id), int(account["id"]), environment, action_countries, present_only=True
    )
    remaining_count = len({(_clean(item["storefront"]), int(item["id_unit"])) for item in remaining})
    st.session_state[selection_key] = []
    st.session_state[revision_key] = int(st.session_state.get(revision_key, 0)) + 1
    if failures:
        st.warning(
            f"Cancellate o già assenti {len(results)-len(failures):,} offerte; errori {len(failures):,}; "
            f"offerte reali residue nei Paesi selezionati {remaining_count:,}."
        )
        st.dataframe(pd.DataFrame(failures), hide_index=True, use_container_width=True)
    else:
        st.session_state["kaufland_live_delete_flash_v191"] = (
            f"Cancellazione completata: {len(results):,} offerte elaborate; "
            f"già assenti {already_absent:,}; offerte reali residue {remaining_count:,}."
        )
        st.rerun()
