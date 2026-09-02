from __future__ import annotations

import hashlib

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import streamlit as st

from services.cecotec_orders import (
    apply_duplicate_generation_choice,
    build_cecotec_export,
    cached_orders,
    clean_text,
    default_file_name,
    default_marketplace_statuses,
    delete_cached_range,
    ensure_schema,
    export_excel_bytes,
    export_history,
    fetch_kaufland_orders,
    filter_order_frame,
    fetch_worten_orders,
    find_cecotec_snapshots,
    load_cecotec_catalog,
    load_cecotec_catalogs,
    marketplace_status_label,
    marketplace_status_options,
    normalized_status,
    previous_exports_for_rows,
    save_export,
    status_code_key,
    upsert_order_cache,
    update_cached_order_statuses,
)
from services.db import rows
from services.marketplace_order_states import verify_order_rows
from services.security import decrypt_dict
from services.session import bootstrap, seller_selector

try:
    from st_aggrid import AgGrid, DataReturnMode, GridOptionsBuilder, GridUpdateMode
except Exception:  # pragma: no cover
    AgGrid = None
    DataReturnMode = GridOptionsBuilder = GridUpdateMode = None


bootstrap()
ensure_schema()
st.title("Creazione Ordini Cecotec")
st.caption(
    "Seleziona il marketplace, filtra gli ordini, abbina l’EAN dello SKU composito "
    "al listino Cecotec e genera il file pronto per il pannello Cecotec."
)

seller_id = seller_selector()
if seller_id is None:
    st.stop()

accounts = rows(
    """SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND active=1 AND marketplace IN ('kaufland','worten')
    ORDER BY marketplace,account_name,id""",
    (seller_id,),
)
if not accounts:
    st.error("Configura prima almeno un account Kaufland o Worten per questo Seller.")
    st.stop()

# La scelta del marketplace è il primo controllo operativo della pagina.
available_marketplaces = sorted({clean_text(item.get("marketplace")).lower() for item in accounts})
marketplace_labels = {"kaufland": "Kaufland", "worten": "Worten"}
marketplace = st.selectbox(
    "1. Marketplace su cui lavorare",
    available_marketplaces,
    format_func=lambda value: marketplace_labels.get(value, value.title()),
    key=f"cecotec_marketplace_{seller_id}",
)
marketplace_accounts = [
    item for item in accounts if clean_text(item.get("marketplace")).lower() == marketplace
]
account_map = {
    f"{item.get('account_name') or 'Account'} · ID {item['id']}": item
    for item in marketplace_accounts
}
account_label = st.selectbox(
    "2. Account marketplace",
    list(account_map),
    key=f"cecotec_account_{seller_id}_{marketplace}",
)
account = account_map[account_label]
account_id = int(account["id"])
credentials = decrypt_dict(account["credentials_encrypted"])

scope = f"{seller_id}_{marketplace}_{account_id}"
selection_key = f"cecotec_selected_rows_{scope}"
last_export_key = f"cecotec_last_export_{scope}"
st.session_state.setdefault(selection_key, [])

st.divider()
st.markdown("### Sincronizzazione ordini")
period_col1, period_col2, sync_col = st.columns([1, 1, 1.2])
default_from = date.today() - timedelta(days=30)
date_from = period_col1.date_input(
    "Ordini dal",
    value=default_from,
    max_value=date.today(),
    key=f"cecotec_date_from_{scope}",
)
date_to = period_col2.date_input(
    "Ordini al",
    value=date.today(),
    min_value=date_from,
    max_value=date.today(),
    key=f"cecotec_date_to_{scope}",
)

if sync_col.button(
    "Scarica/Aggiorna ordini",
    type="primary",
    use_container_width=True,
    key=f"cecotec_sync_{scope}",
):
    try:
        with st.spinner(f"Lettura ordini {marketplace_labels.get(marketplace, marketplace)}…"):
            if marketplace == "kaufland":
                fresh_orders = fetch_kaufland_orders(
                    credentials,
                    account_id=account_id,
                    date_from=date_from,
                    date_to=date_to,
                )
            else:
                fresh_orders = fetch_worten_orders(
                    credentials,
                    account_id=account_id,
                    date_from=date_from,
                    date_to=date_to,
                )
            # La cancellazione avviene solo dopo una risposta API valida, così un errore
            # di rete non elimina gli ordini già presenti in cache.
            delete_cached_range(seller_id, account_id, marketplace, date_from, date_to)
            saved_count = upsert_order_cache(
                seller_id, account_id, marketplace, fresh_orders
            )
        st.success(f"Sincronizzazione completata: {saved_count:,} righe ordine memorizzate.")
    except Exception as exc:
        st.error(f"Impossibile scaricare gli ordini: {exc}")

order_records = cached_orders(seller_id, account_id, marketplace)
if not order_records:
    st.info("Non ci sono ancora ordini in memoria per questo account. Premi “Scarica/Aggiorna ordini”.")
    st.stop()

orders = pd.DataFrame(order_records)
orders["order_created_dt"] = pd.to_datetime(orders["order_created"], errors="coerce", utc=True)
period_mask = orders["order_created_dt"].isna() | (
    orders["order_created_dt"].dt.date.between(date_from, date_to)
)
orders = orders.loc[period_mask].copy()

# Ricalcola sempre stato raggruppato e label dal codice API grezzo. In questo
# modo anche la cache creata da versioni precedenti mostra subito SHIPPING di
# Worten come "In attesa di spedizione", senza obbligare a riscaricare gli ordini.
orders["raw_status"] = orders.get("raw_status", "").fillna("").astype(str)
orders["normalized_status"] = orders["raw_status"].map(
    lambda value: normalized_status(value, marketplace)
)
orders["status_label"] = orders["raw_status"].map(
    lambda value: marketplace_status_label(marketplace, value)
)

st.divider()
st.markdown("### Listino Cecotec per il match EAN → SKU Cecotec")
snapshots = find_cecotec_snapshots(seller_id)
if not snapshots:
    st.error(
        "Non è stato trovato un listino/snapshot Cecotec salvato. "
        "Carica e salva prima il listino Cecotec usato per creare le offerte."
    )
    st.stop()

snapshot_map: dict[str, Mapping[str, Any]] = {}
for item in snapshots:
    label = (
        f"{item.get('price_list_name') or 'Listino Cecotec'} · "
        f"{item.get('updated_at') or ''} · vista {item.get('id')}"
    )
    # Evita collisioni quando due viste hanno lo stesso nome e la stessa data.
    unique_label = label
    suffix = 2
    while unique_label in snapshot_map:
        unique_label = f"{label} ({suffix})"
        suffix += 1
    snapshot_map[unique_label] = item

list_selection_key = f"cecotec_snapshots_multi_v168_{scope}"
all_snapshot_labels = list(snapshot_map)
if list_selection_key not in st.session_state:
    # Come concordato, al primo accesso sono selezionati tutti i listini Cecotec.
    st.session_state[list_selection_key] = list(all_snapshot_labels)
else:
    st.session_state[list_selection_key] = [
        value for value in st.session_state[list_selection_key] if value in snapshot_map
    ]

list_buttons = st.columns([1, 1, 2])
if list_buttons[0].button(
    "Seleziona tutti i listini",
    key=f"cecotec_select_all_lists_{scope}",
    use_container_width=True,
):
    st.session_state[list_selection_key] = list(all_snapshot_labels)
    st.rerun()
if list_buttons[1].button(
    "Deseleziona tutti i listini",
    key=f"cecotec_deselect_all_lists_{scope}",
    use_container_width=True,
):
    st.session_state[list_selection_key] = []
    st.rerun()
list_buttons[2].caption(
    "I listini sono uniti in ordine di priorità: in caso di EAN duplicato prevale "
    "la prima versione selezionata nell’elenco."
)

selected_snapshot_labels = st.multiselect(
    "Listini/versioni Cecotec da utilizzare",
    all_snapshot_labels,
    key=list_selection_key,
    placeholder="Seleziona uno o più listini Cecotec",
    help=(
        "Puoi usare contemporaneamente tutti i listini Cecotec accessibili al Seller. "
        "L’ordine mostrato segue la versione più recente; in caso di EAN presente in più "
        "listini, prevale il primo selezionato."
    ),
)
if not selected_snapshot_labels:
    st.warning("Seleziona almeno un listino Cecotec per effettuare il match EAN → SKU.")
    st.stop()
selected_snapshots = [snapshot_map[label] for label in selected_snapshot_labels]
try:
    catalog, loaded_catalogs, catalog_conflicts = load_cecotec_catalogs(selected_snapshots)
except Exception as exc:
    st.error(f"Uno dei listini Cecotec selezionati non può essere letto: {exc}")
    st.stop()

st.caption(
    f"Listini selezionati: {len(loaded_catalogs):,} · "
    f"prodotti EAN unici indicizzati: {len(catalog):,}."
)
with st.expander("Dettaglio listini Cecotec utilizzati", expanded=False):
    st.dataframe(
        pd.DataFrame([
            {
                "Priorità": item["priority"],
                "Listino": item["price_list_name"],
                "Versione": item["updated_at"],
                "Vista": item["snapshot_id"],
                "Righe": item["rows"],
                "EAN indicizzati": item["indexed"],
                "Colonna EAN": item["columns"].get("ean_col", ""),
                "Colonna SKU": item["columns"].get("sku_col", ""),
            }
            for item in loaded_catalogs
        ]),
        use_container_width=True,
        hide_index=True,
    )
if catalog_conflicts:
    st.warning(
        f"Rilevati {len(catalog_conflicts):,} EAN presenti in più listini con SKU diversi. "
        "È stato utilizzato il valore del listino con priorità maggiore."
    )
    with st.expander("Controlla conflitti EAN tra listini", expanded=False):
        st.dataframe(pd.DataFrame(catalog_conflicts), use_container_width=True, hide_index=True)

st.divider()
st.markdown("### Filtri e selezione ordini")

# Lo storico viene letto prima dei filtri per consentire il filtro
# "già generati / non ancora generati" sull'intero periodo.
all_previous_exports = previous_exports_for_rows(
    seller_id, account_id, marketplace, orders["row_key"].astype(str).tolist()
)
generated_keys_all = {clean_text(item.get("row_key")) for item in all_previous_exports}

filter_row1 = st.columns([1.15, 1.55, 1.15, 1.15])
supplier_values = sorted(
    value for value in orders["supplier"].fillna("").astype(str).unique().tolist() if value
)
default_suppliers = [value for value in supplier_values if "cecotec" in value.lower()]
selected_suppliers = filter_row1[0].multiselect(
    "Fornitore",
    supplier_values,
    default=default_suppliers,
    placeholder="Tutti i fornitori",
    key=f"cecotec_supplier_filter_{scope}",
)
status_options = marketplace_status_options(
    marketplace,
    orders["raw_status"].fillna("").astype(str).tolist(),
)
status_counts = orders["raw_status"].map(status_code_key).value_counts().to_dict()
status_defaults = default_marketplace_statuses(
    marketplace,
    orders["raw_status"].fillna("").astype(str).tolist(),
)
selected_statuses = filter_row1[1].multiselect(
    "Stato API dettagliato",
    status_options,
    default=status_defaults,
    format_func=lambda code: (
        f"{marketplace_status_label(marketplace, code)} "
        f"({int(status_counts.get(status_code_key(code), 0))}) · {code}"
    ),
    placeholder="Tutti gli stati API",
    key=f"cecotec_api_status_filter_v3_{scope}",
)
macro_status_values = [
    "In attesa", "Da spedire", "Spedito", "Ricevuto",
    "Restituito/Rimborsato", "Cancellato",
]
selected_macro_statuses = filter_row1[2].multiselect(
    "Macro-stato",
    macro_status_values,
    default=[],
    placeholder="Tutti",
    key=f"cecotec_macro_status_filter_{scope}",
)
country_values = sorted(
    value for value in orders["country_code"].fillna("").astype(str).unique().tolist() if value
)
selected_countries = filter_row1[3].multiselect(
    "Paese di consegna",
    country_values,
    default=[],
    placeholder="Tutti i Paesi",
    key=f"cecotec_country_filter_{scope}",
)
storefront_values = sorted(
    value for value in orders.get("storefront", pd.Series(dtype=str)).fillna("").astype(str).unique().tolist()
    if value
)
selected_storefronts = st.multiselect(
    "Marketplace / nazione Kaufland",
    storefront_values,
    default=[],
    format_func=lambda value: f"Kaufland.{value} ({value.upper()})",
    placeholder="Tutti i marketplace Kaufland",
    key=f"cecotec_storefront_filter_v180_{scope}",
    disabled=marketplace != "kaufland",
)

filter_row2 = st.columns([1.55, 1.05, 1.05, 1.25])
search_text = filter_row2[0].text_input(
    "Ricerca libera",
    placeholder="Ordine, riga, SKU, prodotto, cliente, CAP, città…",
    key=f"cecotec_search_{scope}",
).strip().lower()
quantity_min = int(filter_row2[1].number_input(
    "Quantità minima", min_value=1, value=1, step=1,
    key=f"cecotec_quantity_min_{scope}",
))
quantity_max_enabled = filter_row2[2].checkbox(
    "Limita quantità massima", value=False,
    key=f"cecotec_quantity_max_enabled_{scope}",
)
quantity_max = int(filter_row2[2].number_input(
    "Quantità massima", min_value=1, value=max(1, int(pd.to_numeric(orders["quantity"], errors="coerce").fillna(1).max())), step=1,
    disabled=not quantity_max_enabled,
    key=f"cecotec_quantity_max_{scope}",
)) if quantity_max_enabled else None
generated_mode_label = filter_row2[3].selectbox(
    "Elaborazione Cecotec",
    ["Tutti", "Non ancora generati", "Già generati"],
    key=f"cecotec_generated_mode_{scope}",
)
generated_mode = {
    "Tutti": "all",
    "Non ancora generati": "not_generated",
    "Già generati": "generated",
}[generated_mode_label]

filter_row3 = st.columns([1.35, 1.35, 1.35, 1.2])
data_quality = filter_row3[0].multiselect(
    "Qualità dati",
    ["Dati cliente completi", "Dati incompleti", "Telefono presente", "Email presente", "SKU valido"],
    default=[],
    placeholder="Qualsiasi qualità",
    key=f"cecotec_data_quality_{scope}",
)
actionable_only = filter_row3[1].checkbox(
    "Solo ordini operativi da elaborare",
    value=False,
    key=f"cecotec_actionable_only_{scope}",
    help="Worten: SHIPPING. Kaufland: need_to_be_sent.",
)
only_cecotec_sku = filter_row3[2].checkbox(
    "Solo SKU riconosciuti come Cecotec",
    value=False,
    key=f"cecotec_only_cecotec_sku_{scope}",
)
show_duplicates = filter_row3[3].checkbox(
    "Mostra righe duplicate ordine/SKU",
    value=True,
    key=f"cecotec_show_duplicates_{scope}",
)

actionable_codes = list(default_marketplace_statuses(marketplace))
visible = filter_order_frame(
    orders,
    suppliers=selected_suppliers,
    statuses=selected_statuses,
    normalized_statuses=selected_macro_statuses,
    countries=selected_countries,
    search_text=search_text,
    quantity_min=quantity_min,
    quantity_max=quantity_max,
    actionable_only=actionable_only,
    actionable_statuses=actionable_codes,
    generated_mode=generated_mode,
    generated_keys=generated_keys_all,
    data_quality=data_quality,
)
if selected_storefronts and "storefront" in visible.columns:
    visible = visible[
        visible["storefront"].fillna("").astype(str).isin(selected_storefronts)
    ]
if only_cecotec_sku and not visible.empty:
    visible = visible[
        visible["composite_sku"].map(
            lambda value: "cecotec" in clean_text(value).lower().split("_", 1)[0]
        )
    ]
if not show_duplicates and not visible.empty:
    visible = visible.drop_duplicates(
        subset=["order_id", "composite_sku"], keep="first"
    )
visible = visible.sort_values(["order_created", "order_id"], ascending=[False, True]).reset_index(drop=True)

actionable_code = "SHIPPING" if marketplace == "worten" else "need_to_be_sent"
st.caption(
    "I filtri vuoti includono tutti i valori. Lo stato API dettagliato conserva i codici "
    "originali del marketplace; il macro-stato serve per raggrupparli. "
    f"Lo stato operativo principale è {marketplace_status_label(marketplace, actionable_code)} "
    f"({actionable_code})."
)

with st.expander("Legenda completa degli stati API"):
    if marketplace == "worten":
        st.markdown(
            "**Worten / Mirakl**  \n"
            "`STAGING` preparazione · `WAITING_ACCEPTANCE` da accettare · "
            "`WAITING_DEBIT` e `WAITING_DEBIT_PAYMENT` attese pagamento · "
            "`SHIPPING` accettato e da spedire · `SHIPPED` spedito · "
            "`TO_COLLECT` disponibile per il ritiro · `RECEIVED` ricevuto · "
            "`CLOSED` concluso · `REFUSED` rifiutato · `CANCELED` cancellato. "
            "Gli eventuali stati di rimborso storici vengono mantenuti e mostrati separatamente."
        )
    else:
        st.markdown(
            "**Kaufland**  \n"
            "`open` ordine aperto: il programma verifica live se l'indirizzo è già "
            "disponibile; può essere preparato per Cecotec ma non marcato spedito · "
            "`need_to_be_sent` pronto da spedire · `sent` spedito · "
            "`sent_and_autopaid` spedito e pagato automaticamente · "
            "`received` ricevuto · `returned` restituito · "
            "`returned_paid` reso rimborsato · `cancelled` cancellato."
        )

summary_col1, summary_col2, summary_col3, summary_col4, summary_col5 = st.columns(5)
summary_col1.metric("Righe nel periodo", len(orders))
summary_col2.metric(
    "Righe Cecotec",
    int(orders["supplier"].fillna("").astype(str).str.contains("cecotec", case=False, regex=False).sum()),
)
summary_col3.metric(
    marketplace_status_label(marketplace, actionable_code),
    int((orders["raw_status"].map(status_code_key) == status_code_key(actionable_code)).sum()),
)
summary_col4.metric("Non ancora generate", int((~orders["row_key"].astype(str).isin(generated_keys_all)).sum()))
summary_col5.metric("Righe visibili", len(visible))

selected_ids = set(st.session_state.get(selection_key, []))
visible_ids = set(visible["row_key"].astype(str).tolist())
select_col, deselect_col, clear_col, count_col = st.columns([1, 1, 1, 2])
if select_col.button(
    "Seleziona tutti filtrati",
    use_container_width=True,
    key=f"cecotec_select_all_{scope}",
):
    selected_ids.update(visible_ids)
    st.session_state[selection_key] = sorted(selected_ids)
    st.rerun()
if deselect_col.button(
    "Deseleziona tutti filtrati",
    use_container_width=True,
    key=f"cecotec_deselect_filtered_{scope}",
):
    selected_ids.difference_update(visible_ids)
    st.session_state[selection_key] = sorted(selected_ids)
    st.rerun()
if clear_col.button(
    "Azzera selezione",
    use_container_width=True,
    key=f"cecotec_clear_selection_{scope}",
):
    st.session_state[selection_key] = []
    st.rerun()
count_col.metric("Righe selezionate", len(selected_ids))

if visible.empty:
    if orders.empty:
        st.warning(
            "Nessun ordine è presente nel periodo scelto. Amplia l’intervallo date "
            "e premi ‘Scarica/Aggiorna ordini’."
        )
    else:
        st.warning(
            "Nessun ordine corrisponde ai filtri attivi. Azzera Stato API, Macro-stato, "
            "Nazione e Qualità dati per visualizzare nuovamente tutte le righe."
        )
else:
    generated_keys = generated_keys_all
    table = pd.DataFrame({
        "row_key": visible["row_key"].astype(str),
        "Data": visible["order_created"].fillna(""),
        "Ordine": visible["order_id"].fillna(""),
        "Marketplace": visible.get("storefront", pd.Series("", index=visible.index)).fillna("").map(
            lambda value: f"Kaufland.{value}" if marketplace == "kaufland" and clean_text(value) else marketplace_labels.get(marketplace, marketplace.title())
        ),
        "Riga": visible["order_line_id"].fillna(""),
        "Stato": visible["status_label"].fillna(""),
        "Macro-stato": visible["normalized_status"].fillna(""),
        "Codice stato API": visible["raw_status"].fillna(""),
        "Fornitore": visible["supplier"].fillna(""),
        "SKU composito": visible["composite_sku"].fillna(""),
        "Prodotto": visible["product_title"].fillna(""),
        "Q.tà": visible["quantity"].fillna(1).astype(int),
        "Cliente": visible["customer_name"].fillna(""),
        "Telefono": visible["phone"].fillna(""),
        "Email": visible["customer_email"].fillna(""),
        "CAP": visible["postal_code"].fillna(""),
        "Città": visible["city"].fillna(""),
        "Nazione": visible["country_code"].fillna(""),
        "Già generato": visible["row_key"].astype(str).isin(generated_keys),
    })

    if AgGrid is not None:
        grid_builder = GridOptionsBuilder.from_dataframe(table)
        grid_builder.configure_default_column(
            sortable=True,
            filter=True,
            resizable=True,
            minWidth=90,
        )
        grid_builder.configure_column("row_key", hide=True)
        grid_builder.configure_column("Codice stato API", minWidth=155)
        grid_builder.configure_column("Marketplace", minWidth=145)
        grid_builder.configure_column(
            "Ordine",
            checkboxSelection=True,
            headerCheckboxSelection=True,
            headerCheckboxSelectionFilteredOnly=True,
            pinned="left",
            minWidth=150,
        )
        grid_builder.configure_column("SKU composito", minWidth=300)
        grid_builder.configure_column("Prodotto", minWidth=260)
        grid_builder.configure_column("Cliente", minWidth=210)
        grid_builder.configure_column("Già generato", minWidth=125)
        grid_builder.configure_selection(
            selection_mode="multiple",
            use_checkbox=True,
        )
        grid_builder.configure_grid_options(
            rowMultiSelectWithClick=True,
            suppressRowClickSelection=False,
            enableRangeSelection=True,
            animateRows=False,
        )
        preselected = [
            index for index, row_id in enumerate(table["row_key"].tolist())
            if row_id in selected_ids
        ]
        # MANUAL evita il rerun a ogni singolo click: l'utente può spuntare tutte
        # le righe desiderate e poi premere il pulsante Applica del componente.
        manual_mode = getattr(GridUpdateMode, "MANUAL", GridUpdateMode.SELECTION_CHANGED)
        grid_response = AgGrid(
            table,
            gridOptions=grid_builder.build(),
            data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
            update_mode=manual_mode,
            pre_selected_rows=preselected,
            height=590,
            fit_columns_on_grid_load=False,
            theme="streamlit",
            key=f"cecotec_orders_grid_manual_v164_{scope}",
        )
        returned_selected = grid_response.get("selected_rows")
        grid_selected_ids: set[str] = set()
        selection_was_submitted = False
        if isinstance(returned_selected, pd.DataFrame):
            # In alcune versioni di streamlit-aggrid la modalità MANUAL restituisce
            # un DataFrame vuoto al primo rendering, anche quando le righe sono state
            # preselezionate tramite i pulsanti superiori. Un DataFrame non vuoto,
            # invece, indica che l'utente ha effettivamente premuto Applica.
            selection_was_submitted = not returned_selected.empty
            if "row_key" in returned_selected.columns:
                grid_selected_ids = {
                    clean_text(value)
                    for value in returned_selected["row_key"].tolist()
                    if clean_text(value)
                }
            elif {"Ordine", "Riga"}.issubset(returned_selected.columns):
                # Fallback per versioni AgGrid che omettono le colonne nascoste.
                lookup = {
                    (clean_text(row["Ordine"]), clean_text(row["Riga"])): clean_text(row["row_key"])
                    for _, row in table.iterrows()
                }
                grid_selected_ids = {
                    lookup.get((clean_text(row.get("Ordine")), clean_text(row.get("Riga"))), "")
                    for _, row in returned_selected.iterrows()
                }
                grid_selected_ids.discard("")
        elif isinstance(returned_selected, list):
            selection_was_submitted = bool(returned_selected)
            lookup = {
                (clean_text(row["Ordine"]), clean_text(row["Riga"])): clean_text(row["row_key"])
                for _, row in table.iterrows()
            }
            for item in returned_selected:
                if not isinstance(item, Mapping):
                    continue
                row_id = clean_text(item.get("row_key"))
                if not row_id:
                    row_id = lookup.get(
                        (clean_text(item.get("Ordine")), clean_text(item.get("Riga"))),
                        "",
                    )
                if row_id:
                    grid_selected_ids.add(row_id)

        # Non azzerare mai la selezione persistente a causa della risposta vuota
        # prodotta dal primo rendering della griglia MANUAL. Questo era il motivo
        # per cui le righe apparivano spuntate ma la sezione Cecotec riportava
        # "Seleziona almeno una riga". La deselezione totale resta disponibile con
        # i pulsanti espliciti sopra la tabella.
        if selection_was_submitted:
            selected_ids = (selected_ids - visible_ids) | grid_selected_ids
            st.session_state[selection_key] = sorted(selected_ids)

        st.caption(
            "Puoi selezionare più righe senza refresh. Dopo aver terminato la selezione, "
            "premi **Applica** nel riquadro AgGrid per trasferirla al programma. "
            "Le selezioni effettuate con i pulsanti superiori sono invece applicate subito."
        )
    else:
        st.warning(
            "st-aggrid non è installato: uso la tabella di emergenza. "
            "Le selezioni restano comunque memorizzate nei rerun."
        )
        fallback = table.copy()
        fallback.insert(0, "Seleziona", fallback["row_key"].isin(selected_ids))
        edited = st.data_editor(
            fallback,
            hide_index=True,
            use_container_width=True,
            height=590,
            disabled=[column for column in fallback.columns if column != "Seleziona"],
            column_config={
                "Seleziona": st.column_config.CheckboxColumn("Seleziona", default=False),
                "row_key": None,
            },
            key=f"cecotec_orders_fallback_v164_{scope}",
        )
        checked = set(edited.loc[edited["Seleziona"], "row_key"].astype(str))
        selected_ids = (selected_ids - visible_ids) | checked
        st.session_state[selection_key] = sorted(selected_ids)

st.caption(
    "Il quadratino nell’intestazione della colonna Ordine seleziona tutte le righe filtrate. "
    "Con Shift puoi selezionare intervalli consecutivi; i pulsanti superiori operano su tutte "
    "le righe visibili e la selezione viene conservata anche cambiando filtro."
)

# Ricarica la selezione dopo l'eventuale aggiornamento del componente AgGrid.
selected_ids = set(st.session_state.get(selection_key, []))
all_rows_by_key = {clean_text(item.get("row_key")): item for item in order_records}
selected_lines = [all_rows_by_key[key] for key in selected_ids if key in all_rows_by_key]

st.divider()
st.markdown("### Controllo e generazione file Cecotec")
if not selected_lines:
    st.info("Seleziona almeno una riga ordine dalla tabella.")
else:
    selected_state_token = hashlib.sha256(
        "|".join(sorted(
            f"{clean_text(item.get('row_key'))}:{clean_text(item.get('order_id'))}:"
            f"{clean_text(item.get('order_line_id'))}"
            for item in selected_lines
        )).encode("utf-8")
    ).hexdigest()[:16]
    live_state_cache_key = f"cecotec_live_states_v176_{scope}_{selected_state_token}"
    refresh_state_col, live_note_col = st.columns([1, 3])
    force_live_refresh = refresh_state_col.button(
        "Aggiorna stati selezionati via API",
        use_container_width=True,
        key=f"cecotec_refresh_live_states_{scope}_{selected_state_token}",
    )
    live_note_col.caption(
        "Prima di creare il file, Marketplace Hub legge stato, storefront, nazione e "
        "indirizzo direttamente da Kaufland/Worten. Su Kaufland un'unità `open` viene "
        "ricontrollata: se l'API restituisce l'indirizzo completo può essere inserita "
        "nel file Cecotec, ma non può ancora essere marcata come spedita fino a "
        "`need_to_be_sent`."
    )
    live_state_data = st.session_state.get(live_state_cache_key)
    if force_live_refresh or not isinstance(live_state_data, Mapping):
        try:
            with st.spinner(
                f"Verifica dello stato attuale su {marketplace_labels.get(marketplace, marketplace.title())}…"
            ):
                verified_lines, live_audit, live_errors = verify_order_rows(
                    marketplace=marketplace,
                    credentials=credentials,
                    rows=selected_lines,
                    force_refresh=force_live_refresh,
                )
                update_cached_order_statuses(
                    seller_id,
                    account_id,
                    marketplace,
                    verified_lines,
                )
            live_state_data = {
                "rows": verified_lines,
                "audit": live_audit,
                "errors": live_errors,
            }
            st.session_state[live_state_cache_key] = live_state_data
        except Exception as exc:
            live_state_data = {"rows": [], "audit": [], "errors": [str(exc)]}
            st.session_state[live_state_cache_key] = live_state_data

    verified_selected_lines = [
        dict(item) for item in (live_state_data.get("rows") or [])
        if isinstance(item, Mapping)
    ]
    live_audit = [
        dict(item) for item in (live_state_data.get("audit") or [])
        if isinstance(item, Mapping)
    ]
    live_errors = [clean_text(item) for item in (live_state_data.get("errors") or []) if clean_text(item)]
    if live_audit:
        st.dataframe(pd.DataFrame(live_audit), use_container_width=True, hide_index=True)
    if live_errors:
        st.error(
            "Verifica API incompleta: " + " · ".join(live_errors[:10])
            + (f" · altri {len(live_errors) - 10} errori" if len(live_errors) > 10 else "")
        )

    live_ready_lines = [
        item for item in verified_selected_lines
        if bool(item.get("live_verified"))
        and bool(item.get("live_can_generate_supplier_order"))
    ]
    partial_live_lines = [
        item for item in live_ready_lines if bool(item.get("live_partial"))
    ]
    if partial_live_lines:
        st.warning(
            f"{len(partial_live_lines):,} righe contengono unità Kaufland in stati differenti. "
            "Nel file Cecotec saranno incluse soltanto le unità ancora da spedire."
        )
        st.dataframe(
            pd.DataFrame([
                {
                    "Ordine": clean_text(item.get("order_id")),
                    "Unità incluse nel file Cecotec": ", ".join(
                        item.get("live_supplier_order_unit_ids")
                        or item.get("live_shippable_unit_ids") or []
                    ),
                    "Unità escluse": ", ".join(item.get("live_excluded_unit_ids") or []),
                    "Quantità Cecotec": int(item.get("quantity") or 0),
                    "Motivo": clean_text(item.get("live_reason")),
                }
                for item in partial_live_lines
            ]),
            use_container_width=True,
            hide_index=True,
        )
    live_blocked_lines = [
        item for item in verified_selected_lines
        if item not in live_ready_lines
    ]
    verified_keys = {clean_text(item.get("row_key")) for item in verified_selected_lines}
    missing_live_rows = [
        item for item in selected_lines
        if clean_text(item.get("row_key")) not in verified_keys
    ]
    if live_blocked_lines or missing_live_rows:
        blocked_frame = pd.DataFrame([
            {
                "Ordine": clean_text(item.get("order_id")),
                "Riga/unità": clean_text(item.get("order_line_id")),
                "Stato API": clean_text(item.get("live_raw_status") or item.get("raw_status")),
                "Descrizione": clean_text(item.get("live_status_label") or item.get("status_label")),
                "Motivo esclusione": clean_text(item.get("live_reason")) or "stato live non verificato",
            }
            for item in [*live_blocked_lines, *missing_live_rows]
        ])
        st.warning(
            f"{len(blocked_frame):,} righe non possono essere inserite nel file Cecotec "
            "in base allo stato attuale del marketplace."
        )
        st.dataframe(blocked_frame, use_container_width=True, hide_index=True)

    try:
        restcountries_api_key = ""
        try:
            restcountries_api_key = clean_text(st.secrets.get("RESTCOUNTRIES_API_KEY", ""))
        except Exception:
            pass
        valid_rows, validation = build_cecotec_export(
            live_ready_lines,
            catalog,
            marketplace_label=marketplace_labels.get(marketplace, marketplace.title()),
            restcountries_api_key=restcountries_api_key,
        )
    except Exception as exc:
        st.error(f"Errore durante la verifica degli ordini: {exc}")
        st.stop()

    invalid = [item for item in validation if not item["exportable"]]
    warning_count = sum(bool(item.get("warnings")) for item in validation)
    metric1, metric2, metric3, metric4 = st.columns(4)
    metric1.metric("Selezionate", len(selected_lines))
    metric2.metric("Pronte per Cecotec", len(valid_rows))
    metric3.metric("Escluse dal file", len(invalid))
    metric4.metric("Con avvisi", warning_count)

    validation_frame = pd.DataFrame([
        {
            "Ordine": item["order_id"],
            "Riga": item["order_line_id"],
            "Fornitore": item["supplier"],
            "EAN estratto": item["ean"],
            "SKU Cecotec": item["article"],
            "Esportabile": item["exportable"],
            "Errori": " · ".join(item["errors"]),
            "Avvisi": " · ".join(item["warnings"]),
            "Telefono": item["phone_note"],
        }
        for item in validation
    ])
    st.dataframe(validation_frame, use_container_width=True, hide_index=True, height=300)
    blank_article_count = sum(
        bool(item.get("exportable")) and not clean_text(item.get("article"))
        for item in validation
    )
    if blank_article_count:
        st.warning(
            f"{blank_article_count} righe non hanno uno SKU Cecotec riconosciuto. "
            "Saranno comunque incluse nel file con la colonna article vuota, "
            "da compilare manualmente dopo il download."
        )
    if invalid:
        st.warning(
            "Il file sarà generato comunque. Saranno escluse soltanto le righe "
            "riconosciute come appartenenti a un fornitore diverso da Cecotec."
        )

    previous = previous_exports_for_rows(
        seller_id, account_id, marketplace,
        [clean_text(item.get("row_key")) for item in live_ready_lines],
    )
    previously_generated_keys = {clean_text(item.get("row_key")) for item in previous}
    duplicate_count = len(previously_generated_keys)
    generation_choice = "all"
    generation_choice_ready = True
    if duplicate_count:
        st.warning(
            f"Attenzione: {duplicate_count} delle righe selezionate risultano già inserite "
            "in un precedente file Cecotec."
        )
        only_new_key = f"cecotec_generate_only_new_{scope}"
        generate_all_key = f"cecotec_generate_all_selected_{scope}"

        def _choose_only_new() -> None:
            if st.session_state.get(only_new_key):
                st.session_state[generate_all_key] = False

        def _choose_generate_all() -> None:
            if st.session_state.get(generate_all_key):
                st.session_state[only_new_key] = False

        choice_col1, choice_col2 = st.columns(2)
        only_new = choice_col1.checkbox(
            "Seleziona solo le offerte che non sono state ancora elaborate",
            value=False,
            key=only_new_key,
            on_change=_choose_only_new,
        )
        generate_all = choice_col2.checkbox(
            "Ignora l’avviso e genera tutte le offerte selezionate",
            value=False,
            key=generate_all_key,
            on_change=_choose_generate_all,
        )
        if only_new:
            generation_choice = "new_only"
        elif generate_all:
            generation_choice = "all"
        else:
            generation_choice = ""
            generation_choice_ready = False
            st.caption("Scegli una delle due modalità prima di generare il file.")

    selected_lines_to_export = live_ready_lines
    valid_rows_to_export = valid_rows
    validation_to_export = validation
    if generation_choice_ready:
        selected_lines_to_export, valid_rows_to_export, validation_to_export = (
            apply_duplicate_generation_choice(
                selected_lines,
                valid_rows,
                validation,
                previously_generated_keys,
                generation_choice,
            )
        )
        if duplicate_count and generation_choice == "new_only":
            st.info(
                f"Saranno elaborate {len(selected_lines_to_export)} righe non ancora generate; "
                f"{duplicate_count} righe già elaborate saranno escluse."
            )
        elif duplicate_count and generation_choice == "all":
            st.info(
                f"Saranno elaborate tutte le {len(selected_lines_to_export)} righe selezionate, "
                "comprese quelle già presenti nello storico."
            )

    invalid_to_export = [item for item in validation_to_export if not item.get("exportable")]
    no_rows_for_choice = generation_choice_ready and not selected_lines_to_export
    if no_rows_for_choice:
        st.info("Tutte le righe selezionate risultano già elaborate: non ci sono nuove offerte da generare.")

    format_col, generate_col = st.columns([1, 2])
    file_format = format_col.radio(
        "Formato file",
        ["xlsx", "xls"],
        horizontal=True,
        key=f"cecotec_file_format_{scope}",
    )
    generate_clicked = generate_col.button(
        "Genera, salva e prepara il download",
        type="primary",
        use_container_width=True,
        disabled=(not generation_choice_ready or no_rows_for_choice or not live_ready_lines),
        key=f"cecotec_generate_{scope}",
    )
    if generate_clicked:
        try:
            # Ultimo controllo immediatamente prima della generazione: evita di
            # creare ordini fornitore se nel frattempo il marketplace ha cancellato
            # l'ordine oppure lo ha già portato a SHIPPED/sent.
            fresh_lines, fresh_audit, fresh_errors = verify_order_rows(
                marketplace=marketplace,
                credentials=credentials,
                rows=selected_lines_to_export,
            )
            update_cached_order_statuses(
                seller_id,
                account_id,
                marketplace,
                fresh_lines,
            )
            if fresh_errors:
                st.warning(
                    "Alcune righe non sono state verificate e verranno escluse: "
                    + " · ".join(fresh_errors[:10])
                )
            fresh_ready = [
                item for item in fresh_lines
                if bool(item.get("live_verified"))
                and bool(item.get("live_can_generate_supplier_order"))
            ]
            if len(fresh_ready) != len(selected_lines_to_export):
                blocked = len(selected_lines_to_export) - len(fresh_ready)
                st.warning(
                    f"{blocked} righe sono cambiate di stato e sono state escluse "
                    "dalla generazione."
                )
            if not fresh_ready:
                raise RuntimeError(
                    "nessun ordine selezionato risulta ancora pronto per l'invio a Cecotec"
                )
            fresh_valid_rows, fresh_validation = build_cecotec_export(
                fresh_ready,
                catalog,
                marketplace_label=marketplace_labels.get(marketplace, marketplace.title()),
                restcountries_api_key=restcountries_api_key,
            )
            file_name = default_file_name(marketplace, file_format)
            file_bytes = export_excel_bytes(fresh_valid_rows, file_format)
            export_result = save_export(
                seller_id=seller_id,
                account_id=account_id,
                marketplace=marketplace,
                file_name=file_name,
                file_format=file_format,
                file_bytes=file_bytes,
                selected_lines=fresh_ready,
                valid_rows=fresh_valid_rows,
                issues=fresh_validation,
            )
            st.session_state[last_export_key] = export_result
            fresh_invalid = [item for item in fresh_validation if not item.get("exportable")]
            st.success(
                f"File salvato: {len(fresh_valid_rows):,} righe esportate · "
                f"{len(fresh_invalid):,} righe escluse."
            )
            st.session_state.pop(live_state_cache_key, None)
        except Exception as exc:
            st.error(f"Impossibile generare il file Cecotec: {exc}")

last_export = st.session_state.get(last_export_key)
if isinstance(last_export, Mapping):
    last_path = Path(clean_text(last_export.get("file_path")))
    if last_path.exists():
        st.download_button(
            "Scarica l’ultimo file Cecotec generato",
            data=last_path.read_bytes(),
            file_name=clean_text(last_export.get("file_name")) or last_path.name,
            mime=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if clean_text(last_export.get("file_format")) == "xlsx"
                else "application/vnd.ms-excel"
            ),
            type="primary",
            use_container_width=True,
            key=f"cecotec_download_last_{scope}_{last_export.get('export_id')}",
        )

st.divider()
st.markdown("### Storico file Cecotec")
history = export_history(seller_id, account_id, marketplace, limit=50)
if not history:
    st.caption("Non è stato ancora generato alcun file per questo account.")
else:
    history_frame = pd.DataFrame([
        {
            "Data": item.get("created_at"),
            "File": item.get("file_name"),
            "Selezionate": item.get("selected_rows"),
            "Esportate": item.get("exported_rows"),
            "Escluse": item.get("excluded_rows"),
            "Formato": str(item.get("file_format") or "").upper(),
        }
        for item in history
    ])
    st.dataframe(history_frame, use_container_width=True, hide_index=True)
    with st.expander("Scarica nuovamente un file archiviato"):
        for item in history[:15]:
            archived_path = Path(clean_text(item.get("file_path")))
            label = f"{item.get('created_at')} · {item.get('file_name')}"
            if archived_path.exists():
                st.download_button(
                    label,
                    data=archived_path.read_bytes(),
                    file_name=clean_text(item.get("file_name")) or archived_path.name,
                    mime=(
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        if clean_text(item.get("file_format")) == "xlsx"
                        else "application/vnd.ms-excel"
                    ),
                    use_container_width=True,
                    key=f"cecotec_history_download_{item.get('export_id')}",
                )
            else:
                st.warning(f"File non trovato nell’archivio: {label}")
