from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from services.db import rows
from services.fx import get_ecb_rates
from services.kaufland_order_costs import (
    load_published_supplier_cost_catalog,
    resolve_supplier_purchase_cost,
)
from services.kaufland_orders import (
    composed_sku_order_financials,
    country_label,
    detect_tracking_columns,
    import_order_tracking,
    last_sync,
    order_amounts_to_eur,
    payment_schedule,
    save_order_tracking,
    saved_orders,
    saved_orders_archive_info,
    selected_order_financial_summary,
    selected_payment_deadline,
    SHIPPED_ORDER_UNIT_STATUSES,
    status_label,
    ticket_holds,
)
from services.session import bootstrap, seller_selector
from services.order_selection import apply_editor_checkbox_changes
from marketplace_core.orders import OrderScope, OrdersCore
from marketplace_core.jobs import JobsCore


if not st.session_state.get("_embedded_marketplace_orders"):
    bootstrap()
    st.title("Ordini Kaufland")
    seller_id = seller_selector()
else:
    seller_id = st.session_state.get("active_seller_id")
    st.subheader("Ordini Kaufland")

if seller_id is None:
    st.stop()

st.caption(
    "Ogni riga rappresenta una singola unità acquistata. Gli importi provengono "
    "dall’API Kaufland; corriere e tracking vengono conservati nell’archivio "
    "locale quando disponibili o importati dal Seller Portal. Tutti i prezzi, "
    "le commissioni e i netti, inclusi quelli originariamente in PLN e CZK, "
    "sono sempre convertiti e mostrati in EUR con l’ultimo cambio BCE "
    "disponibile. La sincronizzazione legge "
    "separatamente anche gli stati spedito e cancellato: senza questo filtro "
    "Kaufland restituisce principalmente gli ordini ancora da evadere. Per lo "
    "pagamento viene stimato 14 giorni dopo la consegna con tracking oppure "
    "21 giorni dopo la spedizione senza tracking. La durata degli eventuali "
    "ticket viene aggiunta alla data. Quando Kaufland ha già reso il ricavato "
    "disponibile, viene mostrata invece la data effettiva."
)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_ecb_rates_v105() -> dict:
    return get_ecb_rates()


@st.cache_data(ttl=300, show_spinner=False)
def cached_published_cost_catalog_v116(
    seller_id: int,
    account_id: int,
    environment: str,
) -> dict:
    return load_published_supplier_cost_catalog(
        seller_id,
        account_id,
        environment,
    )


accounts = rows(
    """
    SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND marketplace='kaufland' AND active=1
    ORDER BY account_name
    """,
    (seller_id,),
)
if not accounts:
    st.error("Configura prima un account Kaufland per questo Seller.")
    st.stop()

account_map = {
    f"{item['account_name']} · ID {item['id']}": item for item in accounts
}
account = account_map[
    st.selectbox(
        "Account Kaufland",
        list(account_map),
        key=f"orders_kaufland_account_{seller_id}",
    )
]
playground = st.checkbox(
    "Playground (test)",
    value=False,
    key=f"orders_kaufland_playground_{account['id']}",
    help="Produzione e Playground conservano archivi separati.",
)
environment = "test" if playground else "live"
st.info(
    f"Ambiente API: {'PLAYGROUND (test)' if playground else 'PRODUZIONE'}. "
    "Questa sezione esegue soltanto letture e non modifica gli ordini."
)

orders_core = OrdersCore()
jobs_core = JobsCore()

sync_col, scope_col, detail_col = st.columns([1.2, 1, 1.3])
scope_label = scope_col.selectbox(
    "Ordini da sincronizzare",
    ["Ultimi 500", "Ultimi 1.000", "Ultimi 5.000", "Tutti disponibili"],
    index=1,
    key=f"orders_scope_{account['id']}_{environment}",
)
maximum = {
    "Ultimi 500": 500,
    "Ultimi 1.000": 1000,
    "Ultimi 5.000": 5000,
    "Tutti disponibili": None,
}[scope_label]
include_tracking = detail_col.checkbox(
    "Verifica dettagli API degli ordini spediti",
    value=True,
    key=f"orders_tracking_{account['id']}_{environment}",
    help=(
        "Effettua una lettura aggiuntiva per ogni ordine spedito per recuperare "
        "eventuali dati di spedizione. La Seller API pubblica non garantisce "
        "la restituzione dello storico tracking mostrato nel Seller Portal."
    ),
)
if sync_col.button(
    "Sincronizza ordini da Kaufland",
    type="primary",
    use_container_width=True,
    key=f"orders_sync_{account['id']}_{environment}",
):
    request = orders_core.build_sync_job(
        OrderScope(int(seller_id), int(account["id"]), "kaufland", environment),
        maximum=maximum,
        include_tracking_details=include_tracking,
    )
    receipt = jobs_core.submit(request)
    jobs_core.start_local(receipt.job_id)
    st.session_state[f"orders_job_{account['id']}_{environment}"] = receipt.job_id
    st.success("Sincronizzazione avviata in background. Puoi continuare a usare il programma.")

active_job_id = st.session_state.get(f"orders_job_{account['id']}_{environment}")
if active_job_id:
    job = jobs_core.snapshot(active_job_id)
    if job:
        st.progress(min(1.0, max(0.0, job.progress_pct / 100.0)), text=job.message or job.status)
        jc1, jc2 = st.columns([1, 4])
        if jc1.button("Aggiorna stato", key=f"orders_job_refresh_{active_job_id}"):
            st.rerun()
        if job.status == "done":
            st.success(f"Sincronizzazione completata: {dict(job.result)}")
        elif job.status == "error":
            st.error(f"Sincronizzazione non riuscita: {job.error}")
        else:
            jc2.caption(f"Job {job.job_id[:8]} · {job.status} · puoi cambiare pagina senza interromperlo.")

sync_info = last_sync(seller_id, account["id"], environment)
if sync_info:
    st.caption(
        f"Ultima sincronizzazione: "
        f"{sync_info['completed_at'] or sync_info['started_at']} · "
        f"unità salvate {sync_info['units_saved']} · "
        f"tracking controllati {sync_info['details_checked']} · "
        f"stato {sync_info['status']}."
    )

support_sync_rows = rows(
    """
    SELECT completed_at,started_at,status,tickets_saved
    FROM kaufland_support_syncs
    WHERE marketplace_account_id=? AND environment=?
    ORDER BY id DESC LIMIT 1
    """,
    (account["id"], environment),
)
if support_sync_rows:
    support_sync = support_sync_rows[0]
    st.caption(
        "Dati ticket usati per il posticipo: sincronizzazione "
        f"{support_sync['completed_at'] or support_sync['started_at']} · "
        f"ticket {support_sync['tickets_saved']} · stato {support_sync['status']}."
    )
else:
    st.warning(
        "Per includere i ritardi causati dai ticket, apri Assistenza Kaufland "
        "ed esegui almeno una volta «Sincronizza da Kaufland»."
    )

archive_info = saved_orders_archive_info(
    seller_id, account["id"], environment
)
archive_total = int(archive_info.get("row_count") or 0)
if archive_total <= 0:
    st.info("Premi «Sincronizza ordini da Kaufland» per creare l’archivio ordini.")
    st.stop()

# v303: daily use no longer transfers and enriches the entire historical archive
# on every Streamlit rerun. Full history remains one click away.
archive_mode = st.selectbox(
    "Caricamento archivio",
    ["Ultimi 90 giorni (veloce)", "Ultimi 365 giorni", "Tutto archivio"],
    index=0,
    key=f"orders_archive_window_{account['id']}_{environment}",
    help=(
        "Limita già la query PostgreSQL prima di costruire la tabella. "
        "Usa Tutto archivio solo quando devi lavorare sullo storico completo."
    ),
)
last_archive_dt = pd.to_datetime(
    archive_info.get("last_created_at"), errors="coerce", utc=True
)
archive_date_to = (
    last_archive_dt.date() if pd.notna(last_archive_dt) else date.today()
)
archive_date_from = None
if archive_mode.startswith("Ultimi 90"):
    archive_date_from = archive_date_to - timedelta(days=89)
elif archive_mode.startswith("Ultimi 365"):
    archive_date_from = archive_date_to - timedelta(days=364)

orders = saved_orders(
    seller_id,
    account["id"],
    environment,
    date_from=archive_date_from,
    date_to=archive_date_to if archive_date_from is not None else None,
)
if not orders:
    st.info("Nessun ordine presente nella finestra di archivio selezionata.")
    st.stop()
if len(orders) < archive_total:
    st.caption(
        f"Modalità veloce: caricate {len(orders):,} di {archive_total:,} unità "
        "direttamente da PostgreSQL. Se ti serve lo storico precedente scegli "
        "«Tutto archivio»."
    )
else:
    st.caption(f"Archivio completo caricato: {archive_total:,} unità.")

frame = pd.DataFrame(orders)
legacy_defaults = {
    "commission_pct": None,
    "commission_source": "",
    "received_at": "",
    "received_source": "",
    "payment_due_at": "",
    "payment_source": "",
    "shipped_at": "",
    "shipped_source": "",
    "payment_days_remaining": None,
    "payment_available": False,
    "payment_status": "Non ancora spedito",
    "payment_date_final": False,
    "payment_rule": "",
    "ticket_delay_days": 0.0,
    "ticket_open": False,
    "ticket_count": 0,
    "open_ticket_count": 0,
    "ticket_ids": "",
}
for column, default in legacy_defaults.items():
    if column not in frame.columns:
        frame[column] = default

# Compatibility with order rows archived before v101. The database migration
# normally adds these columns at startup; this fallback also covers partially
# overwritten Windows installations and immediately avoids a blank error page.
missing_commission_pct = frame["commission_pct"].isna()
valid_sales = pd.to_numeric(
    frame["sold_total_local"], errors="coerce"
).fillna(0).ne(0)
frame.loc[missing_commission_pct & valid_sales, "commission_pct"] = (
    pd.to_numeric(frame["commission_local"], errors="coerce")
    / pd.to_numeric(frame["sold_total_local"], errors="coerce")
    * 100.0
).round(4)

holds_by_unit = ticket_holds(account["id"], environment)
for index, item in frame.iterrows():
    unit_id = str(item.get("id_order_unit") or "").strip()
    hold = holds_by_unit.get(unit_id, {})
    previous_source = str(item.get("payment_source") or "").strip()
    actual_release = (
        str(item.get("payment_due_at") or "")
        if previous_source.startswith("API Kaufland:")
        else ""
    )
    payment = payment_schedule(
        str(item.get("status") or ""),
        received_at=str(item.get("received_at") or ""),
        released_at=actual_release,
        shipped_at=str(item.get("shipped_at") or ""),
        has_tracking=bool(
            str(item.get("tracking_numbers") or "").strip()
        ),
        ticket_delay_seconds=float(hold.get("delay_seconds") or 0.0),
        ticket_open=bool(hold.get("open_ticket_count")),
    )
    for field, value in payment.items():
        frame.at[index, field] = value
    frame.at[index, "payment_source"] = (
        previous_source if actual_release else payment["payment_rule"]
    )
    frame.at[index, "ticket_count"] = int(hold.get("ticket_count") or 0)
    frame.at[index, "open_ticket_count"] = int(
        hold.get("open_ticket_count") or 0
    )
    frame.at[index, "ticket_ids"] = ", ".join(hold.get("ticket_ids") or [])

rates_data = cached_ecb_rates_v105()
rates = rates_data.get("rates") or {}
converted_amounts = pd.DataFrame(
    [
        order_amounts_to_eur(item, rates)
        for item in frame.to_dict("records")
    ],
    index=frame.index,
)
for column in converted_amounts.columns:
    frame[column] = converted_amounts[column]

sku_financials = pd.DataFrame(
    [
        composed_sku_order_financials(
            item.get("sku"),
            item.get("ean"),
            item.get("payout_eur"),
        )
        for item in frame.to_dict("records")
    ],
    index=frame.index,
)
for column in sku_financials.columns:
    frame[column] = sku_financials[column]

supplier_catalog_error = ""
try:
    supplier_cost_catalog = cached_published_cost_catalog_v116(
        int(seller_id),
        int(account["id"]),
        environment,
    )
except Exception as error:
    supplier_cost_catalog = {}
    supplier_catalog_error = str(error)

for index, item in frame.iterrows():
    existing_cost = pd.to_numeric(
        item.get("purchase_cost_eur"),
        errors="coerce",
    )
    if pd.notna(existing_cost) and float(existing_cost) > 0:
        continue
    resolved_cost = resolve_supplier_purchase_cost(
        supplier_cost_catalog,
        order_sku=item.get("sku"),
        order_ean=item.get("ean"),
        storefront=item.get("storefront"),
        product_code=item.get("sku_product_code"),
    )
    if not resolved_cost:
        frame.at[index, "purchase_cost_method"] = "Costo non calcolabile"
        frame.at[index, "purchase_cost_source"] = "Costo non calcolabile"
        continue
    purchase = float(resolved_cost["purchase_cost_eur"])
    payout = pd.to_numeric(item.get("payout_eur"), errors="coerce")
    profit = round(float(payout) - purchase, 2) if pd.notna(payout) else None
    frame.at[index, "purchase_cost_eur"] = purchase
    frame.at[index, "order_profit_eur"] = profit
    frame.at[index, "order_profit_pct"] = (
        round(profit / purchase * 100.0, 2)
        if profit is not None and purchase > 0
        else None
    )
    frame.at[index, "purchase_cost_method"] = "Listino pubblicato"
    frame.at[index, "purchase_cost_source"] = resolved_cost[
        "purchase_cost_source"
    ]
    frame.at[index, "sku_ean_note"] = (
        f"Costo recuperato per {resolved_cost['matched_by']}: "
        f"{resolved_cost['matched_value']}"
    )

cancelled_profit_mask = (
    frame["status"].fillna("").astype(str).str.lower().isin(
        {"cancelled", "canceled"}
    )
)
frame.loc[cancelled_profit_mask, "order_profit_eur"] = 0.0
frame.loc[cancelled_profit_mask, "order_profit_pct"] = None

sent_statuses = SHIPPED_ORDER_UNIT_STATUSES
sent_mask = frame["status"].fillna("").astype(str).str.lower().isin(
    sent_statuses
)
tracking_mask = (
    frame["tracking_numbers"].fillna("").astype(str).str.strip().ne("")
)
st.caption(
    f"Archivio: {len(frame):,} unità · spedite: {int(sent_mask.sum()):,} · "
    f"spedite con tracking disponibile nell’archivio: "
    f"{int((sent_mask & tracking_mask).sum()):,}."
)
if supplier_catalog_error:
    st.warning(
        "La ricerca aggiuntiva nei listini pubblicati non è disponibile: "
        f"{supplier_catalog_error}. Gli SKU composti validi continuano a "
        "essere calcolati normalmente."
    )

with st.expander("Recupera o completa corriere e tracking"):
    st.info(
        "Il Seller Portal mostra lo storico di spedizione, ma i GET pubblici "
        "della Seller API non espongono sempre quei campi. Puoi importare "
        "l’export CSV/XLSX di Kaufland oppure completare un ordine manualmente. "
        "I valori restano salvati anche dopo le sincronizzazioni successive."
    )
    tracking_file = st.file_uploader(
        "Export ordini Kaufland (CSV o XLSX)",
        type=["csv", "xlsx", "xls"],
        key=f"orders_tracking_file_{account['id']}_{environment}",
    )
    if tracking_file is not None:
        try:
            suffix = tracking_file.name.lower().rsplit(".", 1)[-1]
            if suffix == "csv":
                import_frame = pd.read_csv(
                    tracking_file,
                    dtype=str,
                    sep=None,
                    engine="python",
                    keep_default_na=False,
                )
            else:
                import_frame = pd.read_excel(
                    tracking_file,
                    dtype=str,
                    keep_default_na=False,
                )
            import_frame.columns = [str(column).strip() for column in import_frame.columns]
            detected = detect_tracking_columns(import_frame.columns)
            st.caption(
                f"File letto: {len(import_frame):,} righe. "
                "Controlla l’associazione delle colonne prima di importare."
            )
            st.dataframe(
                import_frame.head(10),
                use_container_width=True,
                hide_index=True,
            )
            column_options = ["—"] + list(import_frame.columns)

            def mapping_select(label: str, field: str, key: str) -> str:
                detected_column = detected.get(field, "")
                default_index = (
                    column_options.index(detected_column)
                    if detected_column in column_options else 0
                )
                value = st.selectbox(
                    label,
                    column_options,
                    index=default_index,
                    key=(
                        f"orders_tracking_map_{key}_"
                        f"{account['id']}_{environment}"
                    ),
                )
                return "" if value == "—" else value

            map_unit_col, map_order_col = st.columns(2)
            with map_unit_col:
                mapped_unit = mapping_select(
                    "ID unità ordine (se presente)",
                    "id_order_unit",
                    "unit",
                )
            with map_order_col:
                mapped_order = mapping_select(
                    "Numero ordine",
                    "id_order",
                    "order",
                )
            map_carrier_col, map_tracking_col, map_combined_col = st.columns(3)
            with map_carrier_col:
                mapped_carrier = mapping_select(
                    "Corriere",
                    "carrier_code",
                    "carrier",
                )
            with map_tracking_col:
                mapped_tracking = mapping_select(
                    "Tracking",
                    "tracking_numbers",
                    "tracking",
                )
            with map_combined_col:
                mapped_combined = mapping_select(
                    "Campo combinato (es. DPD | numero)",
                    "combined_shipment",
                    "combined",
                )
            if st.button(
                "Importa tracking nell’archivio",
                type="primary",
                key=f"orders_tracking_import_{account['id']}_{environment}",
            ):
                if not mapped_unit and not mapped_order:
                    st.error(
                        "Associa almeno la colonna Numero ordine oppure "
                        "ID unità ordine."
                    )
                elif not mapped_tracking and not mapped_combined and not mapped_carrier:
                    st.error(
                        "Associa almeno Tracking, Corriere oppure il campo "
                        "combinato."
                    )
                else:
                    result = import_order_tracking(
                        seller_id,
                        account["id"],
                        environment,
                        import_frame.to_dict("records"),
                        {
                            "id_order_unit": mapped_unit,
                            "id_order": mapped_order,
                            "carrier_code": mapped_carrier,
                            "tracking_numbers": mapped_tracking,
                            "combined_shipment": mapped_combined,
                        },
                    )
                    if result["updated"]:
                        st.success(
                            f"Aggiornate {result['updated']:,} unità ordine."
                        )
                    if result["unmatched"]:
                        st.warning(
                            f"{len(result['unmatched']):,} righe non "
                            "corrispondono agli ordini già sincronizzati."
                        )
                    if result["invalid"]:
                        st.warning(
                            f"{len(result['invalid']):,} righe non contengono "
                            "dati sufficienti."
                        )
                    if result["updated"]:
                        st.rerun()
        except Exception as error:
            st.error(f"Impossibile leggere l’export tracking: {error}")

    st.markdown("#### Inserimento o correzione manuale")
    order_choices = {
        (
            f"{item['id_order']} · unità {item['id_order_unit']} · "
            f"{country_label(item['storefront'])} · {item['product_name']}"
        ): item
        for item in orders
    }
    manual_label = st.selectbox(
        "Ordine da completare",
        list(order_choices),
        key=f"orders_tracking_manual_order_{account['id']}_{environment}",
    )
    manual_item = order_choices[manual_label]
    manual_carrier = st.text_input(
        "Corriere",
        value=str(manual_item.get("carrier_code") or ""),
        placeholder="Esempio: DPD",
        key=(
            f"orders_tracking_manual_carrier_{manual_item['id']}_"
            f"{account['id']}_{environment}"
        ),
    )
    manual_tracking = st.text_input(
        "Tracking",
        value=str(manual_item.get("tracking_numbers") or ""),
        placeholder="Esempio: 08448875901263",
        key=(
            f"orders_tracking_manual_number_{manual_item['id']}_"
            f"{account['id']}_{environment}"
        ),
    )
    if st.button(
        "Salva corriere e tracking",
        key=(
            f"orders_tracking_manual_save_{manual_item['id']}_"
            f"{account['id']}_{environment}"
        ),
    ):
        try:
            updated = save_order_tracking(
                seller_id,
                account["id"],
                environment,
                id_order_unit=manual_item["id_order_unit"],
                id_order=manual_item["id_order"],
                carrier_code=manual_carrier,
                tracking_numbers=manual_tracking,
            )
            if updated:
                st.success("Corriere e tracking salvati nell’archivio.")
                st.rerun()
            else:
                st.error("L’unità ordine non è presente nell’archivio.")
        except Exception as error:
            st.error(f"Salvataggio tracking non riuscito: {error}")

created = pd.to_datetime(frame["ts_created_iso"], errors="coerce", utc=True)
frame["_date"] = created.dt.date
valid_dates = [value for value in frame["_date"].tolist() if pd.notna(value)]
today = date.today()
minimum_date = min(valid_dates) if valid_dates else today
maximum_date = max(valid_dates) if valid_dates else today

st.markdown("### Filtri")
date_from_col, date_to_col, status_col = st.columns([1, 1, 1.4])
date_from = date_from_col.date_input(
    "Data da",
    value=minimum_date,
    min_value=minimum_date,
    max_value=maximum_date,
    key=f"orders_from_{account['id']}_{environment}",
)
date_to = date_to_col.date_input(
    "Data a",
    value=maximum_date,
    min_value=minimum_date,
    max_value=maximum_date,
    key=f"orders_to_{account['id']}_{environment}",
)
if date_from > date_to:
    st.error("La data iniziale non può essere successiva alla data finale.")
    st.stop()

status_values = sorted({
    str(value).strip().lower() for value in frame["status"] if str(value).strip()
})
chosen_statuses = status_col.multiselect(
    "Stato ordine",
    status_values,
    default=status_values,
    format_func=status_label,
    key=f"orders_statuses_{account['id']}_{environment}",
)
if not chosen_statuses:
    st.warning("Seleziona almeno uno stato ordine.")
    st.stop()

storefronts = sorted({
    str(value).strip().lower()
    for value in frame["storefront"] if str(value).strip()
})
st.markdown("#### Nazioni")
all_country_col, no_country_col = st.columns([1, 1])
country_prefix = f"orders_country_{account['id']}_{environment}_"
if all_country_col.button(
    "☑ Seleziona tutte",
    use_container_width=True,
    key=f"orders_all_countries_{account['id']}_{environment}",
):
    for code in storefronts:
        st.session_state[country_prefix + code] = True
    st.rerun()
if no_country_col.button(
    "☐ Deseleziona tutte",
    use_container_width=True,
    key=f"orders_no_countries_{account['id']}_{environment}",
):
    for code in storefronts:
        st.session_state[country_prefix + code] = False
    st.rerun()

country_columns = st.columns(min(4, max(1, len(storefronts))))
chosen_storefronts: list[str] = []
for index, code in enumerate(storefronts):
    key = country_prefix + code
    if key not in st.session_state:
        st.session_state[key] = True
    if country_columns[index % len(country_columns)].checkbox(
        f"{country_label(code)} ({code.upper()})",
        key=key,
    ):
        chosen_storefronts.append(code)
if not chosen_storefronts:
    st.warning("Seleziona almeno una nazione tramite i quadratini.")
    st.stop()

search = st.text_input(
    "Cerca",
    placeholder="Ordine, SKU, EAN, prodotto o tracking…",
    key=f"orders_search_{account['id']}_{environment}",
).strip().lower()

with st.expander("Altri filtri", expanded=True):
    payment_filter_col, tracking_filter_col, commission_filter_col = st.columns(3)
    payment_filter = payment_filter_col.selectbox(
        "Stato pagamento",
        [
            "Tutti",
            "Netto disponibile",
            "In attesa di accredito",
            "Data non determinabile",
            "Ticket aperto",
        ],
        key=f"orders_payment_filter_{account['id']}_{environment}",
    )
    tracking_filter = tracking_filter_col.selectbox(
        "Tracking",
        ["Tutti", "Con tracking", "Senza tracking"],
        key=f"orders_tracking_filter_{account['id']}_{environment}",
    )
    commission_filter = commission_filter_col.selectbox(
        "Commissione",
        ["Tutte", "Commissione rilevata", "Commissione non rilevata"],
        key=f"orders_commission_filter_{account['id']}_{environment}",
    )

    currency_values = sorted({
        str(value).strip().upper()
        for value in frame["source_currency"]
        if pd.notna(value) and str(value).strip()
    })
    carrier_values = sorted({
        str(value).strip()
        for value in frame["carrier_code"]
        if pd.notna(value) and str(value).strip()
    })
    currency_col, carrier_col = st.columns(2)
    chosen_currencies = currency_col.multiselect(
        "Valuta originale",
        currency_values,
        default=currency_values,
        key=f"orders_currencies_{account['id']}_{environment}",
    )
    chosen_carriers = carrier_col.multiselect(
        "Corriere",
        carrier_values,
        default=[],
        placeholder="Tutti i corrieri",
        help="Lascia vuoto per includere tutti i corrieri.",
        key=f"orders_carriers_{account['id']}_{environment}",
    )

    sold_values = pd.to_numeric(
        frame["sold_total_eur"], errors="coerce"
    ).dropna()
    minimum_sold = float(sold_values.min()) if not sold_values.empty else 0.0
    maximum_sold = float(sold_values.max()) if not sold_values.empty else 0.0
    amount_from_col, amount_to_col = st.columns(2)
    amount_from = amount_from_col.number_input(
        "Totale venduto (€) da",
        min_value=0.0,
        value=max(0.0, minimum_sold),
        step=1.0,
        key=f"orders_amount_from_{account['id']}_{environment}",
    )
    amount_to = amount_to_col.number_input(
        "Totale venduto (€) a",
        min_value=0.0,
        value=max(0.0, maximum_sold),
        step=1.0,
        key=f"orders_amount_to_{account['id']}_{environment}",
    )
    if amount_from > amount_to:
        st.error(
            "Nel filtro importo, il valore iniziale non può superare quello finale."
        )
        st.stop()

filtered = frame[
    frame["storefront"].astype(str).str.lower().isin(chosen_storefronts)
    & frame["status"].astype(str).str.lower().isin(chosen_statuses)
    & frame["source_currency"].fillna("").astype(str).str.upper().isin(
        chosen_currencies
    )
    & frame["_date"].apply(
        lambda value: pd.notna(value) and date_from <= value <= date_to
    )
].copy()
filtered_amounts = pd.to_numeric(
    filtered["sold_total_eur"], errors="coerce"
)
filtered = filtered[
    filtered_amounts.between(float(amount_from), float(amount_to), inclusive="both")
].copy()
if chosen_carriers:
    filtered = filtered[
        filtered["carrier_code"].fillna("").astype(str).isin(chosen_carriers)
    ].copy()
if payment_filter == "Netto disponibile":
    filtered = filtered[
        filtered["payment_available"].fillna(False).astype(bool)
    ].copy()
elif payment_filter == "In attesa di accredito":
    filtered = filtered[
        ~filtered["payment_available"].fillna(False).astype(bool)
        & filtered["payment_due_at"].fillna("").astype(str).str.strip().ne("")
    ].copy()
elif payment_filter == "Data non determinabile":
    filtered = filtered[
        filtered["payment_due_at"].fillna("").astype(str).str.strip().eq("")
    ].copy()
elif payment_filter == "Ticket aperto":
    filtered = filtered[
        filtered["ticket_open"].fillna(False).astype(bool)
    ].copy()
tracking_present = (
    filtered["tracking_numbers"].fillna("").astype(str).str.strip().ne("")
)
if tracking_filter == "Con tracking":
    filtered = filtered[tracking_present].copy()
elif tracking_filter == "Senza tracking":
    filtered = filtered[~tracking_present].copy()
commission_present = pd.to_numeric(
    filtered["commission_local"], errors="coerce"
).notna()
if commission_filter == "Commissione rilevata":
    filtered = filtered[commission_present].copy()
elif commission_filter == "Commissione non rilevata":
    filtered = filtered[~commission_present].copy()
if search:
    searchable = (
        filtered[
            [
                "id_order", "id_order_unit", "sku", "ean", "product_name",
                "tracking_numbers", "carrier_code",
            ]
        ]
        .fillna("")
        .astype(str)
        .agg(" ".join, axis=1)
        .str.lower()
    )
    filtered = filtered[searchable.str.contains(search, regex=False)].copy()

if filtered.empty:
    st.warning("Nessun ordine corrisponde ai filtri selezionati.")
    st.stop()

st.markdown("### Ordini filtrati")
filter_signature = hashlib.sha1(repr((
    date_from.isoformat(),
    date_to.isoformat(),
    tuple(chosen_statuses),
    tuple(chosen_storefronts),
    search,
    payment_filter,
    tracking_filter,
    commission_filter,
    tuple(chosen_currencies),
    tuple(chosen_carriers),
    round(float(amount_from), 2),
    round(float(amount_to), 2),
)).encode("utf-8")).hexdigest()[:12]
selection_state_key = (
    f"orders_selected_ids_{seller_id}_{account['id']}_{environment}_"
    f"{filter_signature}"
)
editor_key = (
    f"orders_editor_{seller_id}_{account['id']}_{environment}_"
    f"{filter_signature}"
)
visible_ids = {
    int(value) for value in filtered["id"].astype(int).tolist()
}
if selection_state_key not in st.session_state:
    st.session_state[selection_state_key] = sorted(visible_ids)
persisted_selected_ids = {
    int(value) for value in st.session_state.get(selection_state_key, [])
    if int(value) in visible_ids
}

select_col, deselect_col = st.columns(2)
if select_col.button(
    f"☑ Seleziona tutti i filtrati ({len(visible_ids):,})",
    use_container_width=True,
    key=f"orders_select_filtered_{editor_key}",
):
    st.session_state[selection_state_key] = sorted(visible_ids)
    st.session_state.pop(editor_key, None)
    st.rerun()
if deselect_col.button(
    f"☐ Deseleziona tutti i filtrati ({len(visible_ids):,})",
    use_container_width=True,
    key=f"orders_deselect_filtered_{editor_key}",
):
    st.session_state[selection_state_key] = []
    st.session_state.pop(editor_key, None)
    st.rerun()

def shipping_text(item, field: str) -> str:
    raw_value = item.get(field)
    value = "" if pd.isna(raw_value) else str(raw_value or "").strip()
    if value:
        return value
    status = str(item.get("status") or "").strip().lower()
    if status in sent_statuses:
        return "Non disponibile nella Seller API"
    return "—"


def build_display(source: pd.DataFrame, selected_ids: set[int]) -> pd.DataFrame:
    def optional_column(name: str, default=None) -> pd.Series:
        if name in source.columns:
            return source[name]
        return pd.Series(default, index=source.index)

    if "sku_ean_note" in source.columns:
        sku_ean_note = source["sku_ean_note"]
    elif "sku_ean_matches_order" in source.columns:
        sku_ean_note = source["sku_ean_matches_order"].map(
            {
                True: "Codice prodotto ed EAN ordine coincidono",
                False: "Codice prodotto distinto dall’EAN ordine",
            }
        ).fillna("Riferimento non disponibile")
    else:
        sku_ean_note = pd.Series(
            "Riferimento non disponibile",
            index=source.index,
        )

    return pd.DataFrame({
        "Seleziona": source["id"].astype(int).isin(selected_ids),
        "Nazione": source["storefront"].map(country_label),
        "Data": pd.to_datetime(
            source["ts_created_iso"], errors="coerce", utc=True
        ).dt.tz_convert("Europe/Rome").dt.strftime("%d/%m/%Y %H:%M"),
        "Ordine": source["id_order"],
        "Unità ordine": source["id_order_unit"],
        "SKU": source["sku"],
        "EAN": source["ean"],
        "Nome prodotto": source["product_name"],
        "Prezzo prodotto": source["product_price_eur"],
        "Spedizione": source["shipping_eur"],
        "Totale venduto": source["sold_total_eur"],
        "Commissione": source["commission_eur"],
        "Commissione %": optional_column("commission_pct"),
        "Fonte commissione": optional_column(
            "commission_source", "Non disponibile"
        ),
        "Da ricevere": optional_column("payout_eur"),
        "Costo acquisto": optional_column("purchase_cost_eur"),
        "Guadagno ordine": optional_column("order_profit_eur"),
        "Guadagno %": optional_column("order_profit_pct"),
        "Metodo costo": optional_column(
            "purchase_cost_method", "Costo non calcolabile"
        ),
        "Fonte costo": optional_column(
            "purchase_cost_source", "Costo non calcolabile"
        ),
        "Riferimento costo": sku_ean_note,
        "Valuta": "EUR",
        "Valuta originale": optional_column("source_currency", "EUR"),
        "Stato": source["status"].map(status_label),
        "Spedito il": pd.to_datetime(
            optional_column("shipped_at"), errors="coerce", utc=True
        ).dt.tz_convert("Europe/Rome").dt.strftime(
            "%d/%m/%Y %H:%M"
        ).fillna("—"),
        "Ricevuto il": pd.to_datetime(
            optional_column("received_at"), errors="coerce", utc=True
        ).dt.tz_convert("Europe/Rome").dt.strftime(
            "%d/%m/%Y %H:%M"
        ).fillna("—"),
        "Pagamento / disponibilità": pd.to_datetime(
            optional_column("payment_due_at"), errors="coerce", utc=True
        ).dt.tz_convert("Europe/Rome").dt.strftime(
            "%d/%m/%Y %H:%M"
        ).fillna("—"),
        "Giorni al pagamento": optional_column("payment_days_remaining"),
        "Stato pagamento": optional_column(
            "payment_status", "Non ancora determinabile"
        ),
        "Regola pagamento": optional_column(
            "payment_rule", "Non ancora determinabile"
        ),
        "Ritardo ticket (giorni)": optional_column("ticket_delay_days", 0.0),
        "Ticket aperti": optional_column("open_ticket_count", 0),
        "ID ticket": optional_column("ticket_ids", ""),
        "Fonte data ricezione": optional_column("received_source", ""),
        "Fonte data pagamento": optional_column("payment_source", ""),
        "Corriere": [
            shipping_text(item, "carrier_code")
            for item in source.to_dict("records")
        ],
        "Tracking": [
            shipping_text(item, "tracking_numbers")
            for item in source.to_dict("records")
        ],
        "_id": source["id"].astype(int),
    })


display = build_display(filtered, persisted_selected_ids)
display_row_ids = tuple(display["_id"].astype(int).tolist())


def persist_order_editor_selection() -> None:
    st.session_state[selection_state_key] = apply_editor_checkbox_changes(
        existing=st.session_state.get(selection_state_key, []),
        row_ids=display_row_ids,
        editor_state=st.session_state.get(editor_key),
    )


st.data_editor(
    display,
    use_container_width=True,
    hide_index=True,
    disabled=[column for column in display.columns if column != "Seleziona"],
    column_config={
        "Seleziona": st.column_config.CheckboxColumn("Seleziona"),
        "Prezzo prodotto": st.column_config.NumberColumn(
            "Prezzo venduto (€)", format="%.2f"
        ),
        "Spedizione": st.column_config.NumberColumn(
            "Spedizione (€)", format="%.2f"
        ),
        "Totale venduto": st.column_config.NumberColumn(
            "Totale venduto (€)", format="%.2f"
        ),
        "Commissione": st.column_config.NumberColumn(
            "Commissione (€)", format="%.2f"
        ),
        "Commissione %": st.column_config.NumberColumn(
            "Commissione %", format="%.2f%%"
        ),
        "Da ricevere": st.column_config.NumberColumn(
            "Da ricevere (€)", format="%.2f"
        ),
        "Costo acquisto": st.column_config.NumberColumn(
            "Costo acquisto (€)", format="%.2f"
        ),
        "Guadagno ordine": st.column_config.NumberColumn(
            "Guadagno ordine (€)", format="%.2f"
        ),
        "Guadagno %": st.column_config.NumberColumn(
            "Guadagno %", format="%.2f%%"
        ),
        "Giorni al pagamento": st.column_config.NumberColumn(
            "Giorni al pagamento", format="%d"
        ),
        "Ritardo ticket (giorni)": st.column_config.NumberColumn(
            "Ritardo ticket (giorni)", format="%.2f"
        ),
        "Ticket aperti": st.column_config.NumberColumn(
            "Ticket aperti", format="%d"
        ),
        "_id": None,
    },
    key=editor_key,
    on_change=persist_order_editor_selection,
)
selected_ids = {
    int(value)
    for value in st.session_state.get(selection_state_key, [])
    if int(value) in visible_ids
}
st.session_state[selection_state_key] = sorted(selected_ids)
selected = filtered[
    filtered["id"].astype(int).isin(selected_ids)
].to_dict("records")

st.caption(
    f"Ordini nell’intervallo e nei filtri correnti: {len(filtered):,} · "
    f"selezionati: {len(selected):,}. Totali ed esportazioni considerano "
    "esclusivamente questo blocco visibile."
)
if not selected:
    st.warning("Seleziona almeno un ordine per calcolare i totali.")
    st.stop()

def effective_amount(item: dict, key: str) -> float | None:
    if str(item.get("status") or "").lower() in {"cancelled", "canceled"}:
        return 0.0
    value = item.get(key)
    if value in (None, "") or pd.isna(value):
        return None
    return float(value)

sold_eur = 0.0
commission_eur = 0.0
payout_eur = 0.0
available_eur = 0.0
waiting_eur = 0.0
purchase_cost_eur = 0.0
order_profit_eur = 0.0
known_cost_units = 0
loss_units = 0
missing_cost_ids: list[str] = []
sku_cost_units = 0
list_cost_units = 0
missing_rates: set[str] = set()
for item in selected:
    status = str(item.get("status") or "").lower()
    if status not in {"cancelled", "canceled"}:
        purchase = effective_amount(item, "purchase_cost_eur")
        profit = effective_amount(item, "order_profit_eur")
        if purchase is None or profit is None:
            missing_cost_ids.append(
                str(item.get("id_order_unit") or item.get("id_order") or "")
            )
        else:
            purchase_cost_eur += purchase
            order_profit_eur += profit
            known_cost_units += 1
            method = str(item.get("purchase_cost_method") or "")
            if method == "Listino pubblicato":
                list_cost_units += 1
            else:
                sku_cost_units += 1
            if profit < 0:
                loss_units += 1
    currency = str(item.get("source_currency") or "EUR").upper()
    values = [
        effective_amount(item, key)
        for key in ("sold_total_eur", "commission_eur", "payout_eur")
    ]
    if any(value is None for value in values):
        missing_rates.add(currency)
        continue
    sold_eur += values[0] or 0.0
    commission_eur += values[1] or 0.0
    payout_eur += values[2] or 0.0
    if status in sent_statuses:
        if bool(item.get("payment_available")):
            available_eur += values[2] or 0.0
        else:
            waiting_eur += values[2] or 0.0

st.markdown("### Totali degli ordini selezionati")
(
    metric_units,
    metric_sales,
    metric_fees,
    metric_payout,
    metric_available,
    metric_waiting,
) = st.columns(6)
metric_units.metric("Unità selezionate", f"{len(selected):,}")
metric_sales.metric("Totale venduto", f"{sold_eur:,.2f} €")
metric_fees.metric("Commissioni Kaufland", f"{commission_eur:,.2f} €")
metric_payout.metric("Totale da ricevere", f"{payout_eur:,.2f} €")
metric_available.metric("Netto disponibile", f"{available_eur:,.2f} €")
metric_waiting.metric("Netto in attesa", f"{waiting_eur:,.2f} €")

profit_pct = (
    order_profit_eur / purchase_cost_eur * 100.0
    if purchase_cost_eur > 0 else None
)
(
    profit_metric_1,
    profit_metric_2,
    profit_metric_3,
    profit_metric_4,
    profit_metric_5,
    profit_metric_6,
) = st.columns(6)
partial_suffix = " rilevato" if missing_cost_ids else ""
profit_metric_1.metric(
    f"Costo acquisto{partial_suffix}",
    f"{purchase_cost_eur:,.2f} €",
)
profit_metric_2.metric(
    f"Guadagno{partial_suffix}",
    f"{order_profit_eur:,.2f} €",
)
profit_metric_3.metric(
    "Guadagno sul costo",
    "—" if profit_pct is None else f"{profit_pct:,.2f}%",
)
profit_metric_4.metric(
    "Unità in perdita",
    f"{loss_units:,}",
)
profit_metric_5.metric(
    "Costo da SKU",
    f"{sku_cost_units:,}",
)
profit_metric_6.metric(
    "Da listino / non calcolabile",
    f"{list_cost_units:,} / {len(missing_cost_ids):,}",
)
st.caption(
    f"Conversione: {rates_data.get('source', 'BCE')} · "
    f"data cambio {rates_data.get('date', '—')}. "
    "Il costo viene letto prima dal terzo valore dello SKU composto e, come "
    "ripiego, dai listini già pubblicati su Kaufland tramite EAN o SKU/codice. "
    "Gli ordini cancellati restano visibili ma incidono zero sui totali."
)
if missing_rates:
    st.warning(
        "Totali EUR parziali: cambio non disponibile per "
        + ", ".join(sorted(missing_rates))
        + "."
    )
if missing_cost_ids:
    st.warning(
        f"Costo e guadagno sono parziali: {len(missing_cost_ids):,} unità "
        "non hanno uno SKU composto valido nel formato "
        "fornitore_codice_costoacquisto_prezzominimo e non sono state trovate "
        "nei listini precedentemente pubblicati tramite EAN o SKU/codice. "
        "Per queste righe viene indicato «Costo non calcolabile». Unità: "
        + ", ".join(value for value in missing_cost_ids[:20] if value)
        + ("…" if len(missing_cost_ids) > 20 else "")
    )

st.markdown("### Date previste di pagamento")


def italian_date(value, *, include_time: bool = False) -> str:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return "Non ancora determinabile"
    local = parsed.tz_convert("Europe/Rome")
    return local.strftime(
        "%d-%m-%Y %H:%M" if include_time else "%d-%m-%Y"
    )


payment_visible_ids = {
    int(item["id"])
    for item in selected
}
payment_selection_state_key = (
    f"orders_payment_selection_{seller_id}_{account['id']}_{environment}"
)
if payment_selection_state_key not in st.session_state:
    st.session_state[payment_selection_state_key] = []
payment_selected_ids = set()
for value in st.session_state.get(payment_selection_state_key, []):
    try:
        clean_value = int(value)
    except (TypeError, ValueError):
        continue
    if clean_value in payment_visible_ids:
        payment_selected_ids.add(clean_value)
st.session_state[payment_selection_state_key] = sorted(payment_selected_ids)

payment_signature = hashlib.sha256(
    "|".join(str(value) for value in sorted(payment_visible_ids)).encode("utf-8")
).hexdigest()[:12]
payment_editor_key = (
    f"orders_payment_editor_{account['id']}_{environment}_{payment_signature}"
)
payment_select_col, payment_deselect_col = st.columns(2)
if payment_select_col.button(
    f"☑ Seleziona tutte le righe di pagamento ({len(payment_visible_ids):,})",
    use_container_width=True,
    key=f"orders_payment_select_all_{payment_editor_key}",
):
    st.session_state[payment_selection_state_key] = sorted(payment_visible_ids)
    st.session_state.pop(payment_editor_key, None)
    st.rerun()
if payment_deselect_col.button(
    "☐ Deseleziona tutte le righe di pagamento",
    use_container_width=True,
    key=f"orders_payment_deselect_all_{payment_editor_key}",
):
    st.session_state[payment_selection_state_key] = []
    st.session_state.pop(payment_editor_key, None)
    st.rerun()

payment_rows = []
for item in selected:
    status = str(item.get("status") or "").strip().lower()
    cancelled = status in {"cancelled", "canceled"}
    due_at = str(item.get("payment_due_at") or "")
    item_id = int(item["id"])
    payment_rows.append(
        {
            "Seleziona": item_id in payment_selected_ids,
            "Ordine": item.get("id_order", ""),
            "Unità ordine": item.get("id_order_unit", ""),
            "Nazione": country_label(item.get("storefront", "")),
            "Stato ordine": status_label(status),
            "Tracking": (
                "Sì"
                if str(item.get("tracking_numbers") or "").strip()
                else "No"
            ),
            "Spedito il": (
                "—"
                if cancelled
                else italian_date(item.get("shipped_at"), include_time=True)
            ),
            "Ricevuto il": (
                "—"
                if cancelled
                else italian_date(item.get("received_at"), include_time=True)
            ),
            "Data pagamento / disponibilità": (
                "Ordine cancellato"
                if cancelled
                else italian_date(due_at)
            ),
            "Giorni al pagamento": (
                None if cancelled else item.get("payment_days_remaining")
            ),
            "Netto da pagare (€)": (
                0.0
                if cancelled
                else effective_amount(item, "payout_eur")
            ),
            "Costo acquisto (€)": (
                0.0
                if cancelled
                else effective_amount(item, "purchase_cost_eur")
            ),
            "Guadagno ordine (€)": (
                0.0
                if cancelled
                else effective_amount(item, "order_profit_eur")
            ),
            "Metodo costo": str(
                item.get("purchase_cost_method") or "Costo non calcolabile"
            ),
            "Fonte costo": str(
                item.get("purchase_cost_source") or "Costo non calcolabile"
            ),
            "Stato pagamento": (
                "Non dovuto · ordine cancellato"
                if cancelled
                else str(
                    item.get("payment_status")
                    or "Non ancora determinabile"
                )
            ),
            "Regola applicata": str(item.get("payment_rule") or "—"),
            "Ritardo ticket (giorni)": (
                0.0 if cancelled else float(item.get("ticket_delay_days") or 0)
            ),
            "Ticket aperti": (
                0 if cancelled else int(item.get("open_ticket_count") or 0)
            ),
            "ID ticket": str(item.get("ticket_ids") or "—"),
            "_id": item_id,
        }
    )

payment_editor_row_ids = tuple(item["_id"] for item in payment_rows)


def persist_payment_editor_selection() -> None:
    st.session_state[payment_selection_state_key] = (
        apply_editor_checkbox_changes(
            existing=st.session_state.get(payment_selection_state_key, []),
            row_ids=payment_editor_row_ids,
            editor_state=st.session_state.get(payment_editor_key),
        )
    )


payment_display = pd.DataFrame(payment_rows)
st.data_editor(
    payment_display,
    use_container_width=True,
    hide_index=True,
    disabled=[
        column for column in payment_display.columns if column != "Seleziona"
    ],
    column_config={
        "Seleziona": st.column_config.CheckboxColumn(
            "Seleziona",
            help=(
                "Scegli una o più righe. Il riepilogo sottostante considera "
                "soltanto le righe spuntate in questa tabella."
            ),
        ),
        "Giorni al pagamento": st.column_config.NumberColumn(format="%d"),
        "Netto da pagare (€)": st.column_config.NumberColumn(format="%.2f €"),
        "Costo acquisto (€)": st.column_config.NumberColumn(format="%.2f €"),
        "Guadagno ordine (€)": st.column_config.NumberColumn(format="%.2f €"),
        "Ritardo ticket (giorni)": st.column_config.NumberColumn(format="%.2f"),
        "Ticket aperti": st.column_config.NumberColumn(format="%d"),
        "_id": None,
    },
    key=payment_editor_key,
    on_change=persist_payment_editor_selection,
)

payment_selected_ids = set()
for value in st.session_state.get(payment_selection_state_key, []):
    try:
        clean_value = int(value)
    except (TypeError, ValueError):
        continue
    if clean_value in payment_visible_ids:
        payment_selected_ids.add(clean_value)
st.session_state[payment_selection_state_key] = sorted(payment_selected_ids)
payment_selected = [
    item for item in selected if int(item["id"]) in payment_selected_ids
]
st.caption(
    f"Righe disponibili nella tabella: {len(payment_rows):,} · "
    f"righe selezionate per il riepilogo: {len(payment_selected):,}."
)

if not payment_selected:
    st.info(
        "Spunta uno o più quadratini nella tabella per vedere netto, costo, "
        "guadagno e data finale esclusivamente delle righe scelte."
    )
else:
    payment_financials = selected_order_financial_summary(payment_selected)
    (
        payment_metric_units,
        payment_metric_payout,
        payment_metric_cost,
        payment_metric_profit,
        payment_metric_available,
        payment_metric_waiting,
    ) = st.columns(6)
    payment_metric_units.metric(
        "Righe scelte",
        f"{payment_financials['selected_units']:,}",
    )
    payment_metric_payout.metric(
        "Netto da ricevere",
        f"{payment_financials['payout_eur']:,.2f} €",
    )
    payment_metric_cost.metric(
        "Costo rilevato",
        f"{payment_financials['purchase_cost_eur']:,.2f} €",
    )
    payment_metric_profit.metric(
        "Guadagno rilevato",
        f"{payment_financials['profit_eur']:,.2f} €",
    )
    payment_metric_available.metric(
        "Netto disponibile",
        f"{payment_financials['available_eur']:,.2f} €",
    )
    payment_metric_waiting.metric(
        "Netto in attesa",
        f"{payment_financials['waiting_eur']:,.2f} €",
    )
    if payment_financials["unknown_cost_units"]:
        st.warning(
            f"{payment_financials['unknown_cost_units']:,} righe selezionate "
            "hanno il costo non calcolabile e non incidono sui totali di costo "
            "e guadagno."
        )
    if payment_financials["cancelled_units"]:
        st.caption(
            f"Righe cancellate selezionate, conteggiate a zero ed escluse dalla "
            f"data finale: {payment_financials['cancelled_units']:,}."
        )

    block_payment = selected_payment_deadline(payment_selected)
    latest_payment_date = italian_date(
        block_payment.get("latest_payment_due_at")
    )
    if not block_payment["payable_units"]:
        st.info(
            "Le righe selezionate sono tutte cancellate: non esiste un "
            "pagamento da prevedere."
        )
    elif block_payment["all_dates_known"]:
        if block_payment["all_available"]:
            st.success(
                "Il blocco delle righe selezionate è stato pagato del tutto "
                f"entro la data {latest_payment_date}."
            )
        else:
            st.success(
                "Il blocco delle righe selezionate sarà pagato del tutto entro "
                f"la data {latest_payment_date}."
            )
    elif block_payment["scheduled_units"]:
        st.warning(
            f"La data completa del blocco selezionato non è ancora "
            f"determinabile: {block_payment['unscheduled_units']:,} righe non "
            "hanno ancora una data definitiva. Tra quelle già determinate, "
            f"l’ultima è il {latest_payment_date}."
        )
    else:
        st.warning(
            "La data di pagamento del blocco selezionato non è ancora "
            "determinabile perché nessuna delle righe scelte dispone di una "
            "data prevista."
        )
st.caption(
    "La data effettiva comunicata da Kaufland ha priorità. Solo quando manca, "
    "la stima è: consegna + 14 giorni con tracking, oppure spedizione + 21 "
    "giorni senza tracking. I ticket posticipano la data per tutta la loro "
    "durata; il booking report Kaufland resta la conferma definitiva."
)

converted_summary = {}
for item in selected:
    source_currency = str(item.get("source_currency") or "EUR").upper()
    target = converted_summary.setdefault(
        source_currency,
        {
            "Valuta originale": source_currency,
            "Venduto EUR": 0.0,
            "Commissioni EUR": 0.0,
            "Da ricevere EUR": 0.0,
            "Costo acquisto EUR": 0.0,
            "Guadagno EUR": 0.0,
            "Unità": 0,
        },
    )
    target["Venduto EUR"] += effective_amount(item, "sold_total_eur") or 0.0
    target["Commissioni EUR"] += effective_amount(item, "commission_eur") or 0.0
    target["Da ricevere EUR"] += effective_amount(item, "payout_eur") or 0.0
    target["Costo acquisto EUR"] += (
        effective_amount(item, "purchase_cost_eur") or 0.0
    )
    target["Guadagno EUR"] += (
        effective_amount(item, "order_profit_eur") or 0.0
    )
    target["Unità"] += 1
converted_summary_rows = [
    {
        **item,
        "Venduto EUR": round(item["Venduto EUR"], 2),
        "Commissioni EUR": round(item["Commissioni EUR"], 2),
        "Da ricevere EUR": round(item["Da ricevere EUR"], 2),
        "Costo acquisto EUR": round(item["Costo acquisto EUR"], 2),
        "Guadagno EUR": round(item["Guadagno EUR"], 2),
    }
    for item in sorted(
        converted_summary.values(),
        key=lambda value: value["Valuta originale"],
    )
]
with st.expander("Riepilogo in euro per valuta originale"):
    st.dataframe(
        pd.DataFrame(converted_summary_rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Venduto EUR": st.column_config.NumberColumn(format="%.2f €"),
            "Commissioni EUR": st.column_config.NumberColumn(format="%.2f €"),
            "Da ricevere EUR": st.column_config.NumberColumn(format="%.2f €"),
            "Costo acquisto EUR": st.column_config.NumberColumn(format="%.2f €"),
            "Guadagno EUR": st.column_config.NumberColumn(format="%.2f €"),
        },
    )

export_columns = [
    column for column in display.columns if column not in {"Seleziona", "_id"}
]
selected_frame = filtered[
    filtered["id"].astype(int).isin(selected_ids)
].copy()
selected_export = build_display(
    selected_frame, selected_ids
)[export_columns]
filtered_export = build_display(
    filtered, selected_ids
)[export_columns]
selected_download_col, filtered_download_col = st.columns(2)
selected_download_col.download_button(
    "Scarica gli ordini selezionati nel filtro CSV",
    data=selected_export.to_csv(index=False).encode("utf-8-sig"),
    file_name=(
        f"ordini_kaufland_selezionati_"
        f"{date_from.isoformat()}_{date_to.isoformat()}.csv"
    ),
    mime="text/csv",
    key=f"orders_download_selected_{account['id']}_{environment}",
    use_container_width=True,
)
filtered_download_col.download_button(
    "Scarica il blocco filtrato CSV",
    data=filtered_export.to_csv(index=False).encode("utf-8-sig"),
    file_name=(
        f"ordini_kaufland_filtrati_"
        f"{date_from.isoformat()}_{date_to.isoformat()}.csv"
    ),
    mime="text/csv",
    key=f"orders_download_filtered_{account['id']}_{environment}",
    use_container_width=True,
)

with st.expander("Come vengono calcolati commissione e pagamento"):
    st.markdown(
        "- **Totale venduto** = prezzo del prodotto pagato dal cliente + spedizione.\n"
        "- **Commissione** = importo esatto della commissione restituito "
        "dall’ordine; quando il tenant non espone un campo dedicato viene "
        "ricavata come prezzo prodotto − `revenue_gross`.\n"
        "- **Commissione %** = commissione ÷ totale venduto × 100.\n"
        "- **Da ricevere** = totale venduto − commissione.\n"
        "- Per uno SKU nel formato "
        "`fornitore_codice_costoacquisto_prezzominimo`, il **costo di acquisto** "
        "è il terzo valore. Il codice può essere un EAN, uno SKU fornitore o "
        "un altro identificativo di qualsiasi lunghezza. Esempio: "
        "`Innpro_6974662350503_335.07_452.34` → costo **335,07 €**.\n"
        "- **Guadagno ordine** = netto pagato da Kaufland − costo di acquisto "
        "letto dallo SKU. Per gli SKU vecchi o non conformi, il programma cerca "
        "il prodotto nelle viste/listini realmente pubblicati per questo account, "
        "prima tramite EAN e poi tramite SKU/codice. Se anche questa ricerca non "
        "trova un costo, mostra **Costo non calcolabile** senza inventare valori.\n"
        "- **Con tracking**, **Pagamento previsto** = data di consegna + 14 "
        "giorni. Finché la consegna non viene rilevata, la data resta "
        "correttamente non determinabile.\n"
        "- **Senza tracking**, **Pagamento previsto** = data in cui l’unità è "
        "stata contrassegnata come spedita + 21 giorni.\n"
        "- Un **ticket** posticipa la data per tutta la propria durata. Con un "
        "ticket ancora aperto viene mostrata una data provvisoria, ma il "
        "riepilogo non la considera definitiva fino alla chiusura.\n"
        "- Per lo stato **Spedito e pagato automaticamente**, la data mostrata "
        "è quella effettiva in cui Kaufland ha reso disponibile il ricavato. "
        "In questo stato `ts_updated_iso` identifica il passaggio allo stato "
        "pagato e ha priorità sulla stima.\n"
        "- Tutti gli importi PLN e CZK vengono convertiti in EUR prima di essere "
        "mostrati, filtrati, sommati o esportati; la valuta originaria resta "
        "indicata in una colonna separata.\n"
        "- Tabella, totali e CSV considerano esclusivamente l’intervallo e i "
        "filtri correnti.\n"
        "- La data è una stima operativa: il booking report Kaufland resta la "
        "fonte definitiva per l’effettivo rilascio."
    )
