from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from services.cecotec_orders import (
    cached_orders,
    clean_text,
    default_marketplace_statuses,
    delete_cached_range,
    ensure_schema,
    fetch_kaufland_orders,
    fetch_worten_orders,
    marketplace_status_label,
    marketplace_status_options,
    upsert_order_cache,
)
from services.db import rows
from services.innpro_orders import (
    aggregate_innpro_order_lines,
    analyze_innpro_order_line,
    apply_duplicate_order_choice,
    default_innpro_file_name,
    ensure_innpro_schema,
    export_innpro_csv_bytes,
    innpro_export_history,
    previous_exports_for_orders,
    read_innpro_export_bytes,
    save_innpro_export,
)
from services.security import decrypt_dict
from services.session import bootstrap, seller_selector


bootstrap()
ensure_schema()
ensure_innpro_schema()
st.title("Creazione Ordini INNPRO")
st.caption(
    "Seleziona le righe ordine da inviare a INNPRO. Marketplace Hub riconosce "
    "gli SKU composti INNPRO, estrae l’EAN dal secondo valore dello SKU e genera "
    "il file CSV nel formato richiesto da INNPRO: EAN;quantità, senza intestazione."
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

marketplace_labels = {"kaufland": "Kaufland", "worten": "Worten"}
available_marketplaces = sorted(
    {clean_text(item.get("marketplace")).lower() for item in accounts}
)
marketplace = st.selectbox(
    "1. Marketplace degli ordini",
    available_marketplaces,
    format_func=lambda value: marketplace_labels.get(value, value.title()),
    key=f"innpro_marketplace_{seller_id}",
)
marketplace_accounts = [
    item
    for item in accounts
    if clean_text(item.get("marketplace")).lower() == marketplace
]
account_map = {
    f"{item.get('account_name') or 'Account'} · ID {item['id']}": item
    for item in marketplace_accounts
}
account_label = st.selectbox(
    "2. Account marketplace",
    list(account_map),
    key=f"innpro_account_{seller_id}_{marketplace}",
)
account = account_map[account_label]
account_id = int(account["id"])
credentials = decrypt_dict(account["credentials_encrypted"])
scope = f"{seller_id}_{marketplace}_{account_id}"
last_export_key = f"innpro_last_export_{scope}"


def _history_date(value) -> str:
    timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(timestamp):
        return clean_text(value) or "—"
    return timestamp.tz_convert("Europe/Rome").strftime("%d/%m/%Y %H:%M")


def _history_download(item, *, key_prefix: str, primary: bool = False) -> None:
    file_name = clean_text(item.get("file_name")) or "ordini_INNPRO.csv"
    try:
        archived = read_innpro_export_bytes(item.get("file_path"))
    except Exception as exc:
        st.warning(f"File non disponibile nell’archivio: {file_name} · {exc}")
        return
    st.download_button(
        f"Riscarica {file_name}",
        data=archived,
        file_name=file_name,
        mime="text/csv",
        type="primary" if primary else "secondary",
        use_container_width=True,
        key=f"{key_prefix}_{item.get('export_id')}",
    )


with st.expander("Archivio file INNPRO", expanded=False):
    history = innpro_export_history(seller_id, account_id, marketplace, limit=50)
    if not history:
        st.caption("Non è stato ancora generato alcun file INNPRO per questo account.")
    else:
        history_frame = pd.DataFrame([
            {
                "Generato il": _history_date(item.get("created_at")),
                "File": item.get("file_name"),
                "Ordini": int(item.get("order_count") or 0),
                "Righe esportate": int(item.get("exported_rows") or 0),
                "EAN unici": int(item.get("unique_eans") or 0),
                "Quantità": int(item.get("total_quantity") or 0),
            }
            for item in history
        ])
        st.dataframe(history_frame, use_container_width=True, hide_index=True)
        st.caption(
            "I file restano archiviati nei dati di Marketplace Hub e possono essere "
            "riscaricati anche dopo la chiusura del programma."
        )
        for item in history[:20]:
            _history_download(item, key_prefix="innpro_history_download")

st.divider()
st.markdown("### Sincronizzazione ordini")
period_col1, period_col2, sync_col = st.columns([1, 1, 1.2])
date_from = period_col1.date_input(
    "Ordini dal",
    value=date.today() - timedelta(days=30),
    max_value=date.today(),
    key=f"innpro_date_from_{scope}",
)
date_to = period_col2.date_input(
    "Ordini al",
    value=date.today(),
    min_value=date_from,
    max_value=date.today(),
    key=f"innpro_date_to_{scope}",
)

if sync_col.button(
    "Scarica/Aggiorna ordini",
    type="primary",
    use_container_width=True,
    key=f"innpro_sync_{scope}",
):
    try:
        with st.spinner(
            f"Lettura ordini {marketplace_labels.get(marketplace, marketplace)}…"
        ):
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
            delete_cached_range(
                seller_id,
                account_id,
                marketplace,
                date_from,
                date_to,
            )
            saved_count = upsert_order_cache(
                seller_id,
                account_id,
                marketplace,
                fresh_orders,
            )
        st.success(
            f"Sincronizzazione completata: {saved_count:,} righe ordine memorizzate."
        )
        st.rerun()
    except Exception as exc:
        st.error(f"Impossibile scaricare gli ordini: {exc}")

order_records = cached_orders(
    seller_id,
    account_id,
    marketplace,
    date_from=date_from,
    date_to=date_to,
)
if not order_records:
    st.info(
        "Non ci sono ordini memorizzati per il periodo selezionato. "
        "Premi “Scarica/Aggiorna ordini”."
    )
    st.stop()

orders = pd.DataFrame(order_records)
orders["raw_status"] = orders.get("raw_status", "").fillna("").astype(str)
orders["Stato"] = orders["raw_status"].map(
    lambda value: marketplace_status_label(marketplace, value)
)
analysis_rows = [
    analyze_innpro_order_line(item) for item in orders.to_dict("records")
]
analysis = pd.DataFrame(analysis_rows, index=orders.index)
orders["EAN INNPRO"] = analysis["ean"]
orders["INNPRO"] = analysis["is_innpro"]
orders["EAN valido"] = analysis["valid_ean"]
orders["Esportabile"] = analysis["exportable"]
orders["Motivo"] = analysis["error"]

st.divider()
st.markdown("### Selezione ordini")
available_statuses = [
    value for value in orders["raw_status"].astype(str).tolist() if value
]
status_options = marketplace_status_options(marketplace, available_statuses)
default_statuses = [
    value
    for value in default_marketplace_statuses(marketplace, available_statuses)
    if value in status_options
]
if not default_statuses:
    default_statuses = list(status_options)

filter_col1, filter_col2 = st.columns([1.5, 1])
selected_statuses = filter_col1.multiselect(
    "Stati ordine",
    status_options,
    default=default_statuses,
    format_func=lambda value: marketplace_status_label(marketplace, value),
    key=f"innpro_statuses_{scope}",
)
only_innpro = filter_col2.checkbox(
    "Mostra solo SKU INNPRO",
    value=True,
    key=f"innpro_only_supplier_{scope}",
)
search = st.text_input(
    "Cerca",
    placeholder="Numero ordine, SKU, EAN o prodotto…",
    key=f"innpro_search_{scope}",
).strip().lower()

filtered = orders.copy()
if selected_statuses:
    normalized_status_selection = {
        str(value).lower() if marketplace == "kaufland" else str(value).upper()
        for value in selected_statuses
    }
    normalized_raw = filtered["raw_status"].astype(str).map(
        lambda value: value.lower() if marketplace == "kaufland" else value.upper()
    )
    filtered = filtered[normalized_raw.isin(normalized_status_selection)].copy()
else:
    filtered = filtered.iloc[0:0].copy()
if only_innpro:
    filtered = filtered[filtered["INNPRO"].fillna(False).astype(bool)].copy()
if search and not filtered.empty:
    search_columns = [
        column
        for column in (
            "order_id",
            "order_line_id",
            "composite_sku",
            "EAN INNPRO",
            "product_title",
        )
        if column in filtered.columns
    ]
    searchable = (
        filtered[search_columns]
        .fillna("")
        .astype(str)
        .agg(" ".join, axis=1)
        .str.lower()
    )
    filtered = filtered[searchable.str.contains(search, regex=False)].copy()

if filtered.empty:
    st.warning("Nessuna riga ordine corrisponde ai filtri selezionati.")
    st.stop()

filter_signature = hashlib.sha1(
    repr(
        (
            date_from.isoformat(),
            date_to.isoformat(),
            tuple(selected_statuses),
            bool(only_innpro),
            search,
        )
    ).encode("utf-8")
).hexdigest()[:12]
selection_key = f"innpro_selected_{scope}_{filter_signature}"
editor_key = f"innpro_editor_{scope}_{filter_signature}"
st.session_state.setdefault(selection_key, [])

visible_row_keys = [clean_text(value) for value in filtered["row_key"].tolist()]
visible_row_key_set = set(visible_row_keys)
selected_row_keys = {
    clean_text(value)
    for value in st.session_state.get(selection_key, [])
    if clean_text(value) in visible_row_key_set
}

button_col1, button_col2 = st.columns(2)
if button_col1.button(
    f"☑ Seleziona tutte le righe visibili ({len(visible_row_keys):,})",
    use_container_width=True,
    key=f"innpro_select_all_{scope}_{filter_signature}",
):
    st.session_state[selection_key] = list(visible_row_keys)
    st.session_state.pop(editor_key, None)
    st.rerun()
if button_col2.button(
    "☐ Deseleziona tutte",
    use_container_width=True,
    key=f"innpro_deselect_all_{scope}_{filter_signature}",
):
    st.session_state[selection_key] = []
    st.session_state.pop(editor_key, None)
    st.rerun()

created = pd.to_datetime(filtered["order_created"], errors="coerce", utc=True)
created_display = created.dt.tz_convert("Europe/Rome").dt.strftime("%d/%m/%Y %H:%M")
display = pd.DataFrame(
    {
        "Seleziona": filtered["row_key"].astype(str).isin(selected_row_keys),
        "Data": created_display.fillna("—"),
        "Marketplace": marketplace_labels.get(marketplace, marketplace.title()),
        "Ordine": filtered["order_id"],
        "Riga": filtered["order_line_id"],
        "Stato": filtered["Stato"],
        "SKU composito": filtered["composite_sku"],
        "EAN INNPRO": filtered["EAN INNPRO"],
        "Quantità": pd.to_numeric(filtered["quantity"], errors="coerce").fillna(0).astype(int),
        "Prodotto": filtered["product_title"],
        "Esportabile": filtered["Esportabile"],
        "Nota": filtered["Motivo"].replace("", "OK"),
        "_row_key": filtered["row_key"].astype(str),
    }
)

edited = st.data_editor(
    display,
    use_container_width=True,
    hide_index=True,
    disabled=[column for column in display.columns if column != "Seleziona"],
    column_config={
        "Seleziona": st.column_config.CheckboxColumn("Seleziona"),
        "Esportabile": st.column_config.CheckboxColumn("Esportabile"),
        "_row_key": None,
    },
    key=editor_key,
)
selected_visible = set(
    edited.loc[edited["Seleziona"].fillna(False).astype(bool), "_row_key"]
    .astype(str)
    .tolist()
)
st.session_state[selection_key] = sorted(selected_visible)

selected = filtered[
    filtered["row_key"].astype(str).isin(selected_visible)
].to_dict("records")
st.caption(
    f"Righe visibili: {len(filtered):,} · selezionate: {len(selected):,}. "
    "Il file INNPRO considera esclusivamente le righe selezionate."
)
if not selected:
    st.info("Seleziona almeno una riga ordine per preparare il file INNPRO.")
    st.stop()

selected_order_ids = list(dict.fromkeys(
    clean_text(item.get("order_id"))
    for item in selected
    if clean_text(item.get("order_id"))
))
previous = previous_exports_for_orders(
    seller_id, account_id, marketplace, selected_order_ids
)
previous_order_ids = {clean_text(item.get("order_id")) for item in previous}

generation_choice = "all"
if previous:
    st.warning(
        f"Attenzione: {len(previous_order_ids)} ordini selezionati risultano già "
        "generati per INNPRO. Marketplace Hub li riconosce tramite il numero "
        "ordine del marketplace."
    )
    duplicate_frame = pd.DataFrame([
        {
            "Ordine": item.get("order_id"),
            "Già generato il": _history_date(item.get("created_at")),
            "File": item.get("file_name"),
        }
        for item in previous
    ])
    st.dataframe(duplicate_frame, use_container_width=True, hide_index=True)

    st.caption(
        "Per ogni ordine già generato trovi qui la data e il nome esatto del file "
        "nel quale era stato inserito. Puoi riscaricare quel file direttamente."
    )
    seen_export_ids: set[str] = set()
    duplicate_files = []
    for item in previous:
        export_id = clean_text(item.get("export_id"))
        if export_id and export_id not in seen_export_ids:
            seen_export_ids.add(export_id)
            duplicate_files.append(item)
    for item in duplicate_files:
        _history_download(item, key_prefix="innpro_duplicate_download")

    choice_labels = {
        "new_only": "Genera solo gli ordini non ancora generati",
        "all": "Genera comunque tutti gli ordini selezionati",
    }
    generation_choice = st.radio(
        "Come vuoi procedere?",
        ["new_only", "all"],
        index=0,
        format_func=lambda value: choice_labels[value],
        key=f"innpro_duplicate_choice_{scope}_{filter_signature}",
    )

selected_to_export = apply_duplicate_order_choice(
    selected, previous_order_ids, generation_choice
)
if previous and generation_choice == "new_only":
    excluded_duplicate_lines = len(selected) - len(selected_to_export)
    st.info(
        f"Saranno esclusi {len(previous_order_ids)} ordini già generati "
        f"({excluded_duplicate_lines} righe selezionate)."
    )

if not selected_to_export:
    st.info(
        "Tutti gli ordini selezionati risultano già generati. Puoi riscaricare i "
        "file indicati sopra oppure scegliere di generarli nuovamente."
    )
    st.stop()

aggregated, excluded = aggregate_innpro_order_lines(selected_to_export)
exportable_lines = len(selected_to_export) - len(excluded)
total_quantity = sum(int(item["Quantita"]) for item in aggregated)
metric1, metric2, metric3, metric4 = st.columns(4)
metric1.metric("Righe da generare", len(selected_to_export))
metric2.metric("Righe esportabili", exportable_lines)
metric3.metric("EAN unici", len(aggregated))
metric4.metric("Quantità totale", total_quantity)

if excluded:
    st.warning(
        f"{len(excluded)} righe selezionate non verranno inserite nel file perché "
        "non sono INNPRO oppure non contengono un EAN-13 valido nello SKU composto."
    )
    excluded_display = pd.DataFrame(
        [
            {
                "Ordine": item["order_id"],
                "Riga": item["order_line_id"],
                "SKU": item["composite_sku"],
                "EAN letto": item["ean"],
                "Motivo esclusione": item["error"],
            }
            for item in excluded
        ]
    )
    st.dataframe(excluded_display, use_container_width=True, hide_index=True)

if not aggregated:
    st.error("Nessuna delle righe selezionate è esportabile per INNPRO.")
    st.stop()

st.markdown("### Anteprima file INNPRO")
preview = pd.DataFrame(aggregated).rename(columns={"Quantita": "Quantità"})
st.dataframe(preview, use_container_width=True, hide_index=True)
st.caption(
    "Gli EAN ripetuti vengono accorpati automaticamente: nel file compare una "
    "sola riga per EAN con la somma delle quantità selezionate."
)

generate_clicked = st.button(
    "Genera, memorizza e prepara il download",
    type="primary",
    use_container_width=True,
    key=f"innpro_generate_{scope}_{filter_signature}",
)
if generate_clicked:
    try:
        csv_bytes = export_innpro_csv_bytes(selected_to_export)
        file_name = default_innpro_file_name()
        export_result = save_innpro_export(
            seller_id=seller_id,
            account_id=account_id,
            marketplace=marketplace,
            file_name=file_name,
            file_bytes=csv_bytes,
            selected_lines=selected_to_export,
        )
        st.session_state[last_export_key] = export_result
        st.success(
            f"File memorizzato: {export_result['file_name']} · "
            f"{export_result['order_count']} ordini registrati nello storico. "
            "Da questo momento, se uno di questi numeri ordine viene selezionato "
            "di nuovo, Marketplace Hub mostrerà data e file della generazione precedente."
        )
    except Exception as exc:
        st.error(f"Impossibile generare e memorizzare il file INNPRO: {exc}")

last_export = st.session_state.get(last_export_key)
if isinstance(last_export, dict):
    try:
        last_bytes = read_innpro_export_bytes(last_export.get("file_path"))
    except Exception as exc:
        st.warning(f"Ultimo file INNPRO non disponibile: {exc}")
    else:
        st.download_button(
            f"Scarica {last_export.get('file_name')}",
            data=last_bytes,
            file_name=clean_text(last_export.get("file_name")) or "ordini_INNPRO.csv",
            mime="text/csv",
            type="primary",
            use_container_width=True,
            key=f"innpro_download_last_{scope}_{last_export.get('export_id')}",
        )
        st.code(last_bytes.decode("utf-8").rstrip(), language=None)
        st.caption(
            "Formato identico all’esempio INNPRO: nessuna intestazione, separatore "
            "punto e virgola, una riga per EAN nel formato EAN;quantità."
        )
