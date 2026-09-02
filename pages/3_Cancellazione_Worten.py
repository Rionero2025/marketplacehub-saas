from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st

from services.batch_memory import (
    attach_product_keys,
    frame_records,
    load_state,
    progress_summary,
    record_result,
    reset_state,
    select_next,
    select_range,
)
from services.db import execute, json_text, now_iso, rows
from services.deletion_scope import detect_supplier_from_sku
from services.security import decrypt_dict
from services.session import bootstrap, seller_selector
from services.worten import (
    DEFAULT_API_URL,
    build_delete_offer_csv,
    list_offers,
    offer_import_status,
    upload_offer_csv,
    validate_credentials,
)


embedded = bool(st.session_state.get("_embedded_marketplace_deletion"))
if not embedded:
    bootstrap()
    st.title("Cancellazione offerte Worten")
seller_id = st.session_state.get("active_seller_id") if embedded else seller_selector()
if seller_id is None:
    st.stop()

accounts = rows(
    """SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND marketplace='worten' AND active=1
    ORDER BY account_name""",
    (seller_id,),
)
if not accounts:
    st.error("Configura un account Worten per questo Seller.")
    st.stop()
account_map = {f"{item['account_name']} · ID {item['id']}": item for item in accounts}
account = account_map[
    st.selectbox("Account Worten", list(account_map), key="delete_worten_account")
]
credentials = decrypt_dict(account["credentials_encrypted"])
api_key = credentials.get("api_key", "")
shop_id = credentials.get("shop_id", "")
api_url = credentials.get("api_url", DEFAULT_API_URL)

st.info(
    "Le offerte vengono lette direttamente dall'account Worten. La cancellazione "
    "usa gli SKU esatti presenti sul marketplace."
)
st.warning(
    "Stai lavorando in produzione: gli SKU selezionati saranno inviati realmente "
    "a Worten per la cancellazione."
)

check_col, refresh_col = st.columns(2)
if check_col.button(
    "Verifica connessione Worten",
    key=f"delete_worten_verify_{account['id']}",
    use_container_width=True,
):
    check = validate_credentials(api_key, shop_id, api_url)
    if check["ok"]:
        st.success(check["message"])
    else:
        st.error(f"{check['message']} (HTTP {check['status'] or '—'})")


@st.cache_data(ttl=120, show_spinner=False)
def cached_live_offers(key: str, shop: str, url: str) -> list[dict]:
    return list_offers(key, shop, api_url=url)


if refresh_col.button(
    "Aggiorna offerte da Worten",
    key=f"delete_worten_refresh_{account['id']}",
    use_container_width=True,
):
    cached_live_offers.clear()
    st.rerun()

try:
    with st.spinner("Lettura delle offerte presenti su Worten…"):
        live_offers = cached_live_offers(api_key, shop_id, api_url)
except Exception as error:
    st.error(f"Impossibile leggere le offerte Worten: {error}")
    st.stop()
if not live_offers:
    st.success("Non risultano offerte presenti su questo account Worten.")
    st.stop()

accessible_suppliers = rows(
    """SELECT DISTINCT s.id,s.name
    FROM suppliers s
    JOIN price_lists pl ON pl.supplier_id=s.id
    LEFT JOIN price_list_access a
      ON a.price_list_id=pl.id AND a.seller_id=?
    WHERE pl.active=1
      AND (pl.owner_seller_id=? OR pl.visibility='global' OR a.seller_id=?)
    ORDER BY s.name""",
    (seller_id, seller_id, seller_id),
)

offers = pd.DataFrame(live_offers)
for column in ("sku", "ean", "name", "state"):
    if column not in offers:
        offers[column] = ""
offers["sku"] = offers["sku"].fillna("").astype(str)
offers["ean"] = offers["ean"].fillna("").astype(str)
offers["name"] = offers["name"].fillna("").astype(str)
offers["quantity"] = (
    pd.to_numeric(offers.get("quantity", 0), errors="coerce").fillna(0).astype(int)
)
offers["price"] = (
    pd.to_numeric(offers.get("price", 0), errors="coerce").fillna(0).round(2)
)
detected_suppliers = [
    detect_supplier_from_sku(sku, accessible_suppliers) for sku in offers["sku"]
]
offers["supplier_id"] = [int(item.get("id") or 0) for item in detected_suppliers]
offers["supplier"] = [str(item.get("name") or "Sconosciuto") for item in detected_suppliers]


def supplier_checkbox_form(options: list[tuple[int, str]]) -> list[int]:
    state_key = f"delete_worten_suppliers_{account['id']}"
    revision_key = f"delete_worten_suppliers_revision_{account['id']}"
    revision = int(st.session_state.get(revision_key, 0) or 0)
    selected = {
        int(value) for value in st.session_state.get(state_key, []) if str(value).isdigit()
    }
    values: dict[int, bool] = {}
    with st.form(f"delete_worten_suppliers_form_{account['id']}_{revision}"):
        st.markdown("**Fornitori da cancellare**")
        st.caption(
            "Spunta uno o più fornitori. La pagina non esegue rerun mentre scegli "
            "i quadratini; premi «Applica selezione» al termine."
        )
        columns = st.columns(3)
        for position, (supplier_id, supplier_name) in enumerate(options):
            with columns[position % 3]:
                values[supplier_id] = st.checkbox(
                    supplier_name,
                    value=supplier_id in selected,
                    key=(
                        f"delete_worten_supplier_checkbox_{account['id']}_"
                        f"{revision}_{supplier_id}"
                    ),
                )
        apply_column, all_column, none_column = st.columns(3)
        with apply_column:
            apply_clicked = st.form_submit_button(
                "Applica selezione", type="primary", use_container_width=True
            )
        with all_column:
            all_clicked = st.form_submit_button(
                "Seleziona tutti", use_container_width=True
            )
        with none_column:
            none_clicked = st.form_submit_button(
                "Deseleziona tutti", use_container_width=True
            )
    if apply_clicked or all_clicked or none_clicked:
        if all_clicked:
            chosen = [supplier_id for supplier_id, _ in options]
        elif none_clicked:
            chosen = []
        else:
            chosen = [supplier_id for supplier_id, checked in values.items() if checked]
        st.session_state[state_key] = chosen
        st.session_state[revision_key] = revision + 1
        st.rerun()
    return sorted(selected)


st.markdown("### Ambito della cancellazione")
mode_labels = {
    "Cancella tutto il catalogo": "all",
    "Cancella uno o più fornitori": "suppliers",
    "Selezione manuale delle offerte": "manual",
}
mode_label = st.radio(
    "Cosa vuoi cancellare?",
    list(mode_labels),
    horizontal=True,
    key=f"delete_worten_mode_{account['id']}",
)
mode = mode_labels[mode_label]
selected_supplier_ids: list[int] = []
if mode == "suppliers":
    supplier_options = [
        (int(item["id"]), str(item["name"])) for item in accessible_suppliers
    ]
    # Le offerte con prefisso non riconosciuto restano separabili e non vengono
    # associate arbitrariamente a un fornitore.
    if (offers["supplier_id"] == 0).any():
        supplier_options.append((0, "Sconosciuto / SKU non riconosciuto"))
    selected_supplier_ids = supplier_checkbox_form(supplier_options)
    if not selected_supplier_ids:
        st.warning("Seleziona almeno un fornitore e premi «Applica selezione».")
        st.stop()
    offers = offers[offers["supplier_id"].isin(selected_supplier_ids)].copy()
    if offers.empty:
        st.success("Non risultano offerte dei fornitori selezionati.")
        st.stop()
    selected_names = [
        name for supplier_id, name in supplier_options if supplier_id in selected_supplier_ids
    ]
    st.info(
        f"Fornitori selezionati: {', '.join(selected_names)} · "
        f"offerte comprese: {len(offers):,}."
    )
elif mode == "all":
    st.warning(
        f"Modalità **Cancella tutto il catalogo**: sono comprese "
        f"{len(offers):,} offerte presenti sull'account Worten."
    )

search = st.text_input(
    "Cerca nelle offerte",
    placeholder="SKU, EAN, nome prodotto o fornitore…",
    key=f"delete_worten_search_{account['id']}",
)
if search.strip():
    needle = search.strip().lower()
    mask = (
        offers["sku"].str.lower().str.contains(needle, regex=False)
        | offers["ean"].str.lower().str.contains(needle, regex=False)
        | offers["name"].str.lower().str.contains(needle, regex=False)
        | offers["supplier"].str.lower().str.contains(needle, regex=False)
    )
    offers = offers[mask].copy()
if offers.empty:
    st.warning("Nessuna offerta corrisponde ai filtri applicati.")
    st.stop()

offers = offers.reset_index(drop=True)
offers = attach_product_keys(offers)
records = frame_records(offers)
scope_digest = hashlib.sha256(
    f"{mode}|{','.join(map(str, selected_supplier_ids))}".encode()
).hexdigest()[:12]
scope = {
    "marketplace": "worten",
    "action": "delete_live_offers",
    "seller_id": int(seller_id),
    "account_id": int(account["id"]),
    "shop_id": str(shop_id),
    "scope_digest": scope_digest,
}
state = load_state(scope)
summary = progress_summary(state, records)
selection_id = f"{seller_id}_{account['id']}_{scope_digest}"
selected_key = f"worten_delete_selected_{selection_id}"
grid_key = f"worten_delete_grid_{selection_id}_{search.strip().lower()}"

st.caption(
    f"Offerte lette da Worten: {len(live_offers):,} · mostrate: {len(offers):,}."
)
select_all_label = {
    "all": "☑ Seleziona tutto il catalogo",
    "suppliers": "☑ Seleziona tutte dei fornitori",
    "manual": "☑ Seleziona tutte le offerte filtrate",
}[mode]
c1, c2, c3, c4 = st.columns(4)
if c1.button(
    select_all_label,
    key=f"worten_delete_all_{selection_id}",
    use_container_width=True,
):
    st.session_state[selected_key] = [item["key"] for item in records]
    st.session_state.pop(grid_key, None)
    st.rerun()
if c2.button(
    "☐ Deseleziona tutte",
    key=f"worten_delete_none_{selection_id}",
    use_container_width=True,
):
    st.session_state[selected_key] = []
    st.session_state.pop(grid_key, None)
    st.rerun()
count = c3.number_input(
    "Offerte per intervallo",
    min_value=1,
    value=min(100, max(1, len(offers))),
    step=1,
    key=f"worten_delete_count_{selection_id}",
)
if c4.button(
    "Seleziona prossimo intervallo",
    key=f"worten_delete_next_{selection_id}",
    use_container_width=True,
):
    keys, _, _ = select_next(scope, records, int(count))
    st.session_state[selected_key] = keys
    st.session_state.pop(grid_key, None)
    st.rerun()

with st.expander("Seleziona un intervallo preciso da X a X"):
    range_a, range_b, range_button = st.columns([1, 1, 1])
    start = range_a.number_input(
        "Da posizione",
        min_value=1,
        max_value=max(1, len(offers)),
        value=1,
        step=1,
        key=f"worten_delete_range_start_{selection_id}",
    )
    end = range_b.number_input(
        "A posizione",
        min_value=1,
        max_value=max(1, len(offers)),
        value=min(100, max(1, len(offers))),
        step=1,
        key=f"worten_delete_range_end_{selection_id}",
    )
    if range_button.button(
        "Seleziona intervallo X–X",
        key=f"worten_delete_range_{selection_id}",
        use_container_width=True,
    ):
        try:
            keys, _, _ = select_range(scope, records, int(start), int(end))
            st.session_state[selected_key] = keys
            st.session_state.pop(grid_key, None)
            st.rerun()
        except ValueError as error:
            st.error(str(error))

st.caption(
    f"Memoria cancellazioni: inviate {summary['completed']:,} · "
    f"rimanenti {summary['remaining']:,} · totale corrente {summary['total']:,}."
)
if summary.get("active") and summary["active"].get("selected_count"):
    active = summary["active"]
    st.info(
        f"Intervallo {active['number']}: {active['selected_count']:,} offerte · "
        f"da SKU {active['first_sku']} a SKU {active['last_sku']}."
    )
with st.expander("Storico intervalli di cancellazione"):
    if summary.get("history"):
        st.dataframe(
            pd.DataFrame(summary["history"][-20:]).drop(
                columns=["metadata"], errors="ignore"
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Nessuna cancellazione inviata.")
    reset_confirm = st.checkbox(
        "Confermo di voler azzerare la memoria",
        key=f"worten_delete_reset_confirm_{selection_id}",
    )
    if st.button(
        "Azzera memoria cancellazioni",
        disabled=not reset_confirm,
        key=f"worten_delete_reset_{selection_id}",
    ):
        reset_state(scope)
        st.session_state[selected_key] = []
        st.session_state.pop(grid_key, None)
        st.rerun()

defaults = set(st.session_state.get(selected_key, []))
offers.insert(0, "Seleziona", offers["_batch_key"].isin(defaults))
displayed = st.data_editor(
    offers[
        [
            "Seleziona",
            "supplier",
            "sku",
            "ean",
            "name",
            "quantity",
            "price",
            "state",
        ]
    ],
    use_container_width=True,
    height=480,
    hide_index=True,
    key=grid_key,
    column_config={
        "Seleziona": st.column_config.CheckboxColumn(required=True),
        "supplier": "Fornitore",
        "sku": "SKU offerta Worten",
        "ean": "EAN",
        "name": "Prodotto",
        "quantity": st.column_config.NumberColumn("Quantità", format="%d"),
        "price": st.column_config.NumberColumn("Prezzo", format="%.2f"),
        "state": "Stato",
    },
    disabled=["supplier", "sku", "ean", "name", "quantity", "price", "state"],
)
selected_indexes = displayed.index[displayed["Seleziona"] == True]
selected = offers.loc[selected_indexes].copy()
st.session_state[selected_key] = selected["_batch_key"].astype(str).tolist()
st.metric("Offerte selezionate per la cancellazione", len(selected))

last_import_key = f"worten_delete_last_import_{account['id']}"
last_import = st.session_state.get(last_import_key)
if last_import:
    status_col, info_col = st.columns([1, 2])
    if status_col.button(
        "Verifica stato ultimo import",
        key=f"worten_delete_status_{account['id']}",
        use_container_width=True,
    ):
        try:
            last_import["status_response"] = offer_import_status(
                api_key, last_import["import_id"], api_url=api_url, shop_id=shop_id
            )
            st.session_state[last_import_key] = last_import
        except Exception as error:
            st.error(f"Verifica import non riuscita: {error}")
    info_col.caption(f"Ultimo import di cancellazione: {last_import.get('import_id', '—')}")
    if last_import.get("status_response"):
        st.json(last_import["status_response"])

confirmation_phrase = "CANCELLA TUTTO" if mode == "all" else "CANCELLA"
confirmation = st.text_input(
    f"Scrivi {confirmation_phrase} per confermare",
    key=f"worten_delete_confirmation_{selection_id}",
)
button_label = {
    "all": "Cancella il catalogo selezionato da Worten",
    "suppliers": "Cancella le offerte dei fornitori selezionati",
    "manual": "Cancella le offerte selezionate da Worten",
}[mode]
if st.button(
    button_label,
    type="primary",
    disabled=selected.empty or confirmation.strip().upper() != confirmation_phrase,
    key=f"worten_delete_execute_{selection_id}",
):
    selected_records = frame_records(selected)
    progress = st.progress(0.0)
    progress_text = st.empty()
    try:
        progress.progress(0.15)
        progress_text.caption("Preparazione del file di cancellazione…")
        csv_bytes = build_delete_offer_csv(selected["sku"].astype(str).tolist())
        progress.progress(0.55)
        progress_text.caption("Invio del file a Worten…")
        response = upload_offer_csv(
            api_key,
            csv_bytes,
            api_url=api_url,
            shop_id=shop_id,
            import_mode="NORMAL",
        )
        import_id = response.get("import_id") or response.get("importId") or response.get("id")
        if not import_id:
            raise RuntimeError(f"Worten non ha restituito l'ID import: {response}")
        progress.progress(0.80)
        progress_text.caption("Registrazione degli SKU inviati…")
        try:
            status_response = offer_import_status(
                api_key, import_id, api_url=api_url, shop_id=shop_id
            )
        except Exception as status_error:
            status_response = {
                "status": "SUBMITTED",
                "status_check_error": str(status_error),
            }
        successful = set(selected["_batch_key"].astype(str))
        interval = record_result(
            scope,
            selected_records,
            successful,
            set(),
            "submitted",
            {
                "import_id": import_id,
                "marketplace": "worten",
                "shop_id": shop_id,
                "mode": mode,
                "supplier_ids": selected_supplier_ids,
            },
        )
        operation_rows = [
            {
                "ok": True,
                "sku_inviato": str(item["sku"]),
                "ean": str(item["ean"]),
                "name": str(item["name"]),
                "supplier_name": str(item["supplier"]),
                "status": "submitted",
            }
            for _, item in selected.iterrows()
        ]
        execute(
            """INSERT INTO operations(
            seller_id,marketplace_account_id,price_list_id,marketplace,storefront,
            operation_type,status,total_rows,success_rows,failed_rows,details_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                seller_id,
                account["id"],
                None,
                "worten",
                "pt",
                "ELIMINA_STORICO",
                "submitted",
                len(operation_rows),
                0,
                0,
                json_text(
                    {
                        "shop_id": shop_id,
                        "import_id": import_id,
                        "response": response,
                        "status_response": status_response,
                        "interval": interval,
                        "deletion_mode": mode,
                        "supplier_ids": selected_supplier_ids,
                        "rows": operation_rows,
                    }
                ),
                now_iso(),
            ),
        )
        st.session_state[last_import_key] = {
            "import_id": import_id,
            "status_response": status_response,
        }
        progress.progress(1.0)
        progress_text.caption(
            f"Inviate {len(operation_rows):,} di {len(operation_rows):,} offerte."
        )
        result_frame = selected[["supplier", "sku", "ean", "name"]].copy()
        result_frame.insert(0, "Esito", "Inviata per cancellazione")
        st.dataframe(result_frame, use_container_width=True, hide_index=True)
        st.success(
            f"Intervallo {interval['number']} inviato a Worten: "
            f"{len(operation_rows):,} offerte, da SKU {interval['first_sku']} "
            f"a SKU {interval['last_sku']}. ID import: {import_id}. "
            "Usa «Verifica stato ultimo import» per controllare l'elaborazione."
        )
    except Exception as error:
        failed = {item["key"] for item in selected_records}
        record_result(
            scope,
            selected_records,
            set(),
            failed,
            "failed",
            {"error": str(error)[:500]},
        )
        progress.empty()
        progress_text.empty()
        st.error(f"Cancellazione Worten non riuscita: {error}")
