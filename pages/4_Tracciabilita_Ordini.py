from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import pandas as pd
import streamlit as st

from marketplace_core.accounting import AccountingPeriod
from marketplace_core.jobs import JobsCore
from marketplace_core.tracking import TrackingCore, TrackingScope

from services.accounting import (
    accounting_cache_summary,
    accounting_rows,
    accounting_sync_environment,
    accounting_sync_state,
    ensure_schema as ensure_accounting_schema,
    synchronize_accounting_orders,
)
from services.cecotec_orders import clean_text
from services.db import accessible_lists, rows
from services.kaufland import KauflandClient
from services.marketplace_order_states import (
    aggregate_kaufland_states,
    classify_kaufland_unit,
    classify_worten_order,
    fetch_kaufland_unit_states,
    fetch_worten_order_states,
)
from services.packlink import (
    integration_for_seller as packlink_integration_for_seller,
    shipments_as_tracking_rows as packlink_tracking_rows,
)
from services.order_tracking import (
    archive_tracking_file,
    archived_tracking_file,
    archived_tracking_files,
    delete_archived_tracking_files,
    detect_supplier_from_orders,
    download_tracking_file_from_url,
    ensure_schema,
    mark_accounting_order_shipped,
    match_tracking_rows,
    order_tracking_rows,
    parse_tracking_document,
    partition_shipping_rows,
    persist_import,
    recent_imports,
    record_api_result,
    supplier_names_from_orders,
    successful_api_orders,
    mark_rows_already_sent,
    update_archived_tracking_file_supplier,
)
from services.security import decrypt_dict
from services.shipping_deadlines import (
    is_deadline_in_date_range,
    shipping_deadline_for_order,
)
from services.tracking_shipping_rules import (
    CANCELLED_FILE_STATUSES,
    SHIPPED_FILE_STATUSES,
    WAITING_FILE_STATUSES,
    WAREHOUSE_READY_STATUSES,
    apply_worten_eligibility,
    canonical_file_status,
    split_tracking_numbers,
)
from services.session import bootstrap, seller_selector
from services.worten_tracking_api import WortenTrackingClient, ship_selected_worten_orders

try:
    from st_aggrid import AgGrid, DataReturnMode, GridOptionsBuilder, GridUpdateMode, JsCode
except Exception:  # pragma: no cover
    AgGrid = None
    DataReturnMode = GridOptionsBuilder = GridUpdateMode = JsCode = None


def _tracking_marketplace_macro(value: object, marketplace: str) -> str:
    status = clean_text(value).upper().replace("-", "_").replace(" ", "_")
    if marketplace == "worten":
        mapping = {
            "STAGING": "In attesa",
            "WAITING_ACCEPTANCE": "In attesa",
            "WAITING_DEBIT": "In attesa",
            "WAITING_DEBIT_PAYMENT": "In attesa",
            "SHIPPING": "Da spedire",
            "SHIPPED": "Spedito",
            "TO_COLLECT": "Spedito",
            "RECEIVED": "Ricevuto",
            "CLOSED": "Ricevuto",
            "REFUSED": "Cancellato",
            "CANCELED": "Cancellato",
            "CANCELLED": "Cancellato",
            "WAITING_REFUND_TAX_CONFIRMATION": "Restituito/Rimborsato",
            "WAITING_REFUND": "Restituito/Rimborsato",
            "WAITING_REFUND_PAYMENT": "Restituito/Rimborsato",
            "REFUNDED": "Restituito/Rimborsato",
        }
    else:
        mapping = {
            "OPEN": "In attesa",
            "NEED_TO_BE_SENT": "Da spedire",
            "SENT": "Spedito",
            "SENT_AND_AUTOPAID": "Spedito",
            "RECEIVED": "Ricevuto",
            "RETURNED": "Restituito/Rimborsato",
            "RETURNED_PAID": "Restituito/Rimborsato",
            "CANCELLED": "Cancellato",
            "CANCELED": "Cancellato",
        }
    return mapping.get(status, "Altro / sconosciuto")


def _tracking_file_macro(value: object) -> str:
    status = clean_text(value).upper().replace("-", "_").replace(" ", "_")
    if status in {"WAITING_LABEL", "CREATED"}:
        return "Etichetta / creata"
    if status in {"SENT_TO_WAREHOUSE", "WAREHOUSE", "READY_FOR_WAREHOUSE"}:
        return "In magazzino"
    if status in {"IN_TRANSIT", "IN_TRANSIT/INCIDENCE", "OUT_FOR_DELIVERY"}:
        return "In transito"
    if status in {"DELIVERED", "DELIVERED_TO_AGENCY", "DELIVERED_TO_PICKUP_POINT"}:
        return "Consegnata"
    if status in {"CANCELLED", "CANCELED", "REFUSED"}:
        return "Annullata"
    return "Altro / non disponibile"


def _tracking_sync_time_label(value: object) -> str:
    raw = clean_text(value)
    if not raw:
        return "mai"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo

            parsed = parsed.astimezone(ZoneInfo("Europe/Rome"))
        except Exception:
            parsed = parsed.astimezone()
        return parsed.strftime("%d/%m/%Y alle %H:%M")
    except Exception:
        return raw.replace("T", " ")[:16]


def _tracking_file_size_label(value: object) -> str:
    try:
        size = max(0, int(value or 0))
    except Exception:
        size = 0
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _tracking_archive_label(item: dict[str, Any]) -> str:
    source = "URL" if clean_text(item.get("source_type")).lower() == "url" else "Upload"
    created = _tracking_sync_time_label(item.get("created_at"))
    supplier = clean_text(item.get("supplier")) or "fornitore da rilevare"
    return (
        f"#{int(item['id'])} · {clean_text(item.get('file_name'))} · "
        f"{source} · {supplier} · {created}"
    )


def _live_worten_order_status(order: dict[str, Any]) -> str:
    return classify_worten_order(order).raw_status


def _apply_live_kaufland_state(
    row: Mapping[str, Any],
    aggregate: Mapping[str, Any],
) -> dict[str, Any]:
    item = dict(row)
    item["Stato marketplace"] = clean_text(aggregate.get("raw_status"))
    item["marketplace_status"] = clean_text(aggregate.get("raw_status"))
    item["Macro-stato marketplace"] = clean_text(aggregate.get("macro_status"))
    item["order_line_ids_to_send"] = list(aggregate.get("shippable_unit_ids") or [])
    item["already_shipped"] = bool(aggregate.get("already_shipped"))
    item["live_cancelled"] = bool(aggregate.get("cancelled"))

    if item["already_shipped"]:
        item["Invio consentito"] = "No"
        item["api_allowed"] = False
        item["Stato operativo"] = "Già spedito sul marketplace"
        item["Problemi"] = clean_text(aggregate.get("reason"))
        return item
    if item["live_cancelled"]:
        item["Invio consentito"] = "No"
        item["api_allowed"] = False
        item["Stato operativo"] = "Cancellato sul marketplace"
        item["Problemi"] = clean_text(aggregate.get("reason"))
        return item
    if not aggregate.get("can_mark_shipped"):
        item["Invio consentito"] = "No"
        item["api_allowed"] = False
        item["Stato operativo"] = "Non ancora spedibile"
        item["Problemi"] = clean_text(aggregate.get("reason"))
        return item

    tracking = clean_text(item.get("Tracking") or item.get("tracking"))
    carrier = clean_text(item.get("Corriere") or item.get("carrier"))
    tracking_values = list(dict.fromkeys(split_tracking_numbers(tracking)))
    source = canonical_file_status(
        item.get("Stato file originale") or item.get("file_status") or item.get("Stato file")
    )
    supplier = clean_text(item.get("Fornitore") or item.get("supplier")).casefold()
    is_cecotec = supplier == "cecotec" or supplier.startswith("cecotec ")
    ready_source = source in (WAREHOUSE_READY_STATUSES | SHIPPED_FILE_STATUSES)
    cecotec_waiting_ready = (
        source in WAITING_FILE_STATUSES
        and is_cecotec
        and bool(tracking)
        and bool(carrier)
        and len(tracking_values) == 1
    )
    problems: list[str] = []
    if source in CANCELLED_FILE_STATUSES:
        problems.append(f"stato file {source}: spedizione annullata")
    elif not ready_source and not cecotec_waiting_ready:
        problems.append(f"stato file {source or 'non disponibile'} non pronto")
    if len(tracking_values) != 1:
        problems.append(
            "tracking mancante" if not tracking_values
            else "più tracking differenti sullo stesso ordine"
        )
    if not carrier:
        problems.append("corriere non disponibile")

    allowed = not problems and bool(item.get("order_line_ids_to_send"))
    item["Invio consentito"] = "Sì" if allowed else "No"
    item["api_allowed"] = allowed
    item["Stato operativo"] = "Pronta per l'invio" if allowed else "Non ancora spedibile"
    item["Problemi"] = "; ".join(problems) if problems else clean_text(aggregate.get("reason"))
    return item


bootstrap()
ensure_accounting_schema()
ensure_schema()

st.title("Tracciabilità ordini")
st.caption(
    "Carica i documenti di spedizione del fornitore, confrontali con gli ordini "
    "Kaufland o Worten e invia tracking, corriere e conferma di spedizione via API."
)

seller_id = seller_selector()
if seller_id is None:
    st.stop()

accounts = rows(
    """SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND active=1 AND marketplace IN ('kaufland','worten')
    ORDER BY marketplace,account_name""",
    (seller_id,),
)
if not accounts:
    st.warning("Il Seller non possiede account Kaufland o Worten attivi.")
    st.stop()

account_map = {
    f"{item['marketplace'].title()} · {item['account_name']} · ID {item['id']}": item
    for item in accounts
}
account_label = st.selectbox("Account marketplace", list(account_map))
account = account_map[account_label]
account_id = int(account["id"])
marketplace = clean_text(account["marketplace"]).lower()
tracking_core = TrackingCore()
tracking_scope = TrackingScope(int(seller_id), int(account_id), marketplace)
jobs_core = JobsCore()
try:
    credentials = decrypt_dict(account["credentials_encrypted"])
except Exception as exc:
    st.error(f"Impossibile decifrare le credenziali dell'account: {exc}")
    st.stop()

period_col1, period_col2 = st.columns(2)
date_to = period_col2.date_input("Ordini al", value=date.today())
date_from = period_col1.date_input("Ordini dal", value=date_to - timedelta(days=60))
if date_from > date_to:
    st.error("La data iniziale non può essere successiva alla data finale.")
    st.stop()

environment = accounting_sync_environment(marketplace, credentials)
sync_state = accounting_sync_state(
    seller_id, account_id, marketplace, environment=environment
)
cache_summary = accounting_cache_summary(seller_id, account_id, marketplace)
st.info(
    f"**Ultimo tentativo:** "
    f"{_tracking_sync_time_label(sync_state.get('last_attempted_at') or sync_state.get('last_started_at'))}  ·  "
    f"**Ultimo aggiornamento riuscito:** "
    f"{_tracking_sync_time_label(sync_state.get('last_completed_at'))}  ·  "
    f"**Ultimo ordine scaricato:** "
    f"{_tracking_sync_time_label(cache_summary.get('last_order_created'))}"
    + (
        f" ({cache_summary['last_order_id']})" if cache_summary.get("last_order_id") else ""
    )
    + f"  ·  **In memoria:** {cache_summary['total_orders']:,} ordini."
)

sync_col, full_col, info_col = st.columns([1.15, 1.15, 2])
incremental_clicked = sync_col.button(
    "Aggiorna ordini mancanti e modificati",
    type="primary",
    use_container_width=True,
)
full_clicked = full_col.button(
    "Risincronizzazione completa",
    use_container_width=True,
)
full_request_key = f"tracking_full_sync_requested_{seller_id}_{account_id}_{environment}"
if full_clicked:
    st.session_state[full_request_key] = True
full_confirmed = False
if st.session_state.get(full_request_key):
    st.warning(
        "Conferma la lettura completa dell'intervallo. Gli ordini e i tracking già "
        "memorizzati non saranno eliminati."
    )
    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button(
        "Conferma risincronizzazione completa",
        type="primary",
        use_container_width=True,
        key=f"tracking_confirm_full_{seller_id}_{account_id}_{environment}",
    ):
        full_confirmed = True
        st.session_state[full_request_key] = False
    if cancel_col.button(
        "Annulla",
        use_container_width=True,
        key=f"tracking_cancel_full_{seller_id}_{account_id}_{environment}",
    ):
        st.session_state[full_request_key] = False
        st.rerun()
tracking_sync_job_key = f"tracking_orders_job_{seller_id}_{account_id}_{marketplace}_{environment}"
if incremental_clicked or full_confirmed:
    existing = jobs_core.snapshot(st.session_state.get(tracking_sync_job_key, "")) if st.session_state.get(tracking_sync_job_key) else None
    if existing and not existing.terminal:
        st.warning("È già in corso un aggiornamento ordini per Tracciabilità.")
    else:
        request = tracking_core.build_orders_sync_job(
            tracking_scope, AccountingPeriod(date_from, date_to), full=bool(full_confirmed)
        )
        receipt = jobs_core.submit(request)
        jobs_core.start_local(receipt.job_id)
        st.session_state[tracking_sync_job_key] = receipt.job_id
        st.success(
            f"Aggiornamento ordini {marketplace.title()} avviato in background. "
            "Puoi continuare a usare il programma."
        )

tracking_sync_job_id = st.session_state.get(tracking_sync_job_key)
if tracking_sync_job_id:
    tracking_sync_job = jobs_core.snapshot(tracking_sync_job_id)
    if tracking_sync_job:
        st.progress(
            min(1.0, max(0.0, tracking_sync_job.progress_pct / 100.0)),
            text=tracking_sync_job.message or tracking_sync_job.status,
        )
        tj1, tj2 = st.columns([1, 4])
        if tj1.button("Aggiorna stato", key=f"tracking_orders_refresh_{tracking_sync_job_id}"):
            st.rerun()
        if tracking_sync_job.status == "done":
            result = dict(tracking_sync_job.result)
            st.success(
                f"Aggiornamento completato · nuovi {int(result.get('new_orders') or 0):,} · "
                f"modificati {int(result.get('updated_orders') or 0):,} · "
                f"invariati {int(result.get('unchanged_orders') or 0):,}."
            )
        elif tracking_sync_job.status == "error":
            st.error(
                "Sincronizzazione non riuscita. Gli ordini già presenti sono rimasti "
                f"memorizzati: {tracking_sync_job.error}"
            )
        else:
            tj2.caption(
                f"Job {tracking_sync_job.job_id[:8]} · {tracking_sync_job.status} · "
                "continua in background."
            )

info_col.info(
    "Il Customer Name è il riferimento principale. EAN, SKU, prodotto, email, "
    "telefono, indirizzo e data vengono usati per confermare o distinguere gli abbinamenti."
)

orders = tracking_core.orders(
    tracking_scope, AccountingPeriod(date_from, date_to)
)
if not orders:
    st.warning("Non risultano ordini nel periodo. Aggiorna prima gli ordini mancanti via API.")

st.divider()
st.markdown("### 1. Fornitore e documento spedizioni")

supplier_names = set(supplier_names_from_orders(orders))
for item in accessible_lists(seller_id):
    name = clean_text(item.get("supplier_name"))
    if name:
        supplier_names.add(name)

supplier_options = ["Riconoscimento automatico dal file"] + sorted(supplier_names, key=str.casefold)
supplier_choice = st.selectbox(
    "Fornitore delle spedizioni da confrontare",
    supplier_options,
    help=(
        "In modalità automatica il programma usa il formato del file e confronta "
        "EAN, SKU e prodotti con gli ordini del Seller."
    ),
)

source_col1, source_col2 = st.columns(2)
with source_col1:
    uploaded_files = st.file_uploader(
        "Carica uno o più file spedizioni",
        type=["xlsx", "xls", "csv", "tsv", "txt", "pdf"],
        accept_multiple_files=True,
        help=(
            "I file utilizzati vengono salvati automaticamente nell'archivio "
            "permanente dell'account e restano disponibili dopo il riavvio."
        ),
    )
with source_col2:
    shipment_urls_text = st.text_area(
        "URL dei file spedizioni",
        placeholder="https://fornitore.example/spedizioni.xlsx",
        help=(
            "Inserisci uno o più URL pubblici, uno per riga. Sono accettati XLSX, "
            "XLS, CSV, TSV, TXT e PDF fino a 25 MB. Ogni versione scaricata viene "
            "conservata nell'archivio."
        ),
    )
shipment_urls = [
    clean_text(line)
    for line in shipment_urls_text.splitlines()
    if clean_text(line)
]

archive_rows = archived_tracking_files(seller_id, account_id, marketplace)
archive_label_map = {_tracking_archive_label(item): int(item["id"]) for item in archive_rows}
selected_archive_labels = st.multiselect(
    "Usa file già presenti nell'archivio",
    list(archive_label_map),
    help=(
        "Puoi riutilizzare i documenti già caricati senza selezionarli nuovamente "
        "dal computer o riscaricarli dal fornitore."
    ),
    key=f"tracking_archive_selection_{seller_id}_{account_id}_{marketplace}",
)
selected_archive_ids = [archive_label_map[label] for label in selected_archive_labels]

with st.expander(f"Archivio file spedizioni ({len(archive_rows)})"):
    if archive_rows:
        archive_frame = pd.DataFrame([
            {
                "ID": int(item["id"]),
                "File": clean_text(item.get("file_name")),
                "Origine": "URL" if clean_text(item.get("source_type")).lower() == "url" else "Upload",
                "Fornitore": clean_text(item.get("supplier")) or "Da rilevare",
                "Dimensione": _tracking_file_size_label(item.get("size_bytes")),
                "Salvato il": _tracking_sync_time_label(item.get("created_at")),
                "Ultimo utilizzo": _tracking_sync_time_label(item.get("last_used_at")),
                "Utilizzi": int(item.get("use_count") or 0),
                "URL": clean_text(item.get("source_url")),
            }
            for item in archive_rows
        ])
        st.dataframe(archive_frame, use_container_width=True, hide_index=True)

        archive_download_map = {_tracking_archive_label(item): int(item["id"]) for item in archive_rows}
        download_label = st.selectbox(
            "File da scaricare dall'archivio",
            ["—"] + list(archive_download_map),
            key=f"tracking_archive_download_select_{seller_id}_{account_id}",
        )
        if download_label != "—":
            download_record = archived_tracking_file(
                archive_download_map[download_label],
                seller_id=seller_id,
                account_id=account_id,
            )
            if download_record:
                st.download_button(
                    "Scarica il file archiviato",
                    data=download_record["content"],
                    file_name=clean_text(download_record.get("file_name")) or "spedizioni.xlsx",
                    mime=clean_text(download_record.get("mime_type")) or "application/octet-stream",
                    use_container_width=True,
                    key=f"tracking_archive_download_{download_record['id']}",
                )

        delete_labels = st.multiselect(
            "File da eliminare dall'archivio",
            list(archive_download_map),
            key=f"tracking_archive_delete_select_{seller_id}_{account_id}",
        )
        delete_confirmed = st.checkbox(
            "Confermo l'eliminazione definitiva dei file selezionati",
            key=f"tracking_archive_delete_confirm_{seller_id}_{account_id}",
        )
        if st.button(
            "Elimina file selezionati",
            disabled=not delete_labels or not delete_confirmed,
            use_container_width=True,
            key=f"tracking_archive_delete_button_{seller_id}_{account_id}",
        ):
            deleted = delete_archived_tracking_files(
                [archive_download_map[label] for label in delete_labels],
                seller_id=seller_id,
                account_id=account_id,
            )
            st.success(f"File eliminati dall'archivio: {deleted}.")
            st.rerun()
    else:
        st.info(
            "L'archivio è vuoto. I file caricati dal computer o scaricati tramite URL "
            "verranno salvati automaticamente alla prima analisi."
        )

analysis_key = f"tracking_analysis_{seller_id}_{account_id}"

packlink_integration = packlink_integration_for_seller(seller_id, include_inactive=False)
if packlink_integration:
    packlink_rows = packlink_tracking_rows(
        seller_id, account_id, marketplace, only_matched=True
    )
    with st.container(border=True):
        st.markdown("#### Oppure usa Packlink PRO")
        st.caption(
            "Le spedizioni già abbinate nella pagina Packlink PRO possono essere usate "
            "direttamente qui: non è necessario caricare un file del corriere o del fornitore."
        )
        pack_col1, pack_col2 = st.columns([2, 1])
        pack_col1.metric("Spedizioni Packlink abbinate a questo account", len(packlink_rows))
        if pack_col2.button(
            "Usa abbinamenti Packlink",
            type="primary",
            use_container_width=True,
            disabled=not bool(packlink_rows),
            key=f"tracking_use_packlink_{seller_id}_{account_id}_{marketplace}",
        ):
            token_source = "|".join(
                f"{item.get('source_reference')}:{item.get('order_id')}:{item.get('tracking')}"
                for item in packlink_rows
            )
            st.session_state[analysis_key] = {
                "matches": [dict(item) for item in packlink_rows],
                "supplier": "",
                "confidence": 1.0,
                "ranking": [],
                "import_id": 0,
                "file_ids": [],
                "file_names": ["Packlink PRO API"],
                "source": "packlink",
                "token": hashlib.sha256(token_source.encode("utf-8")).hexdigest(),
            }
            st.success(
                f"Caricate {len(packlink_rows):,} spedizioni Packlink già abbinate. "
                "Tracking e corriere sono pronti per il controllo operativo."
            )
            st.rerun()

has_tracking_sources = bool(uploaded_files or shipment_urls or selected_archive_ids)
tracking_analysis_job_key = f"tracking_analysis_job_{seller_id}_{account_id}_{marketplace}"
if st.button(
    "Salva, analizza e abbina agli ordini",
    type="primary",
    use_container_width=True,
    disabled=not has_tracking_sources or not orders,
):
    existing = jobs_core.snapshot(st.session_state.get(tracking_analysis_job_key, "")) if st.session_state.get(tracking_analysis_job_key) else None
    if existing and not existing.terminal:
        st.warning("È già in corso un'analisi dei documenti spedizioni.")
    else:
        try:
            job_file_ids = list(selected_archive_ids)
            # Gli upload vengono archiviati subito; nel job passiamo soltanto gli ID,
            # mai megabyte di file dentro la coda PostgreSQL.
            for upload in uploaded_files or []:
                content = upload.getvalue()
                archived = archive_tracking_file(
                    seller_id=seller_id,
                    account_id=account_id,
                    marketplace=marketplace,
                    file_name=upload.name,
                    content=content,
                    source_type="upload",
                    mime_type=clean_text(getattr(upload, "type", "")),
                )
                file_id = int(archived["id"])
                if file_id not in job_file_ids:
                    job_file_ids.append(file_id)
            request = tracking_core.build_analysis_job(
                tracking_scope,
                AccountingPeriod(date_from, date_to),
                file_ids=job_file_ids,
                urls=shipment_urls,
                supplier_choice=supplier_choice,
            )
            receipt = jobs_core.submit(request)
            jobs_core.start_local(receipt.job_id)
            st.session_state[tracking_analysis_job_key] = receipt.job_id
            st.success(
                "Documenti salvati. Analisi e matching avviati in background: "
                "puoi cambiare pagina senza interrompere il lavoro."
            )
        except Exception as exc:
            st.error(f"Avvio analisi documenti non riuscito: {exc}")

tracking_analysis_job_id = st.session_state.get(tracking_analysis_job_key)
if tracking_analysis_job_id:
    tracking_analysis_job = jobs_core.snapshot(tracking_analysis_job_id)
    if tracking_analysis_job:
        st.progress(
            min(1.0, max(0.0, tracking_analysis_job.progress_pct / 100.0)),
            text=tracking_analysis_job.message or tracking_analysis_job.status,
        )
        aj1, aj2 = st.columns([1, 4])
        if aj1.button("Aggiorna stato analisi", key=f"tracking_analysis_refresh_{tracking_analysis_job_id}"):
            st.rerun()
        if tracking_analysis_job.status == "done":
            result = dict(tracking_analysis_job.result)
            import_id = int(result.get("import_id") or 0)
            current_analysis = st.session_state.get(analysis_key) or {}
            if import_id and int(current_analysis.get("import_id") or 0) != import_id:
                matches = tracking_core.import_matches(import_id)
                st.session_state[analysis_key] = {
                    "matches": matches,
                    "supplier": clean_text(result.get("supplier")),
                    "confidence": float(result.get("confidence") or 0.0),
                    "ranking": list(result.get("ranking") or []),
                    "import_id": import_id,
                    "file_ids": list(result.get("file_ids") or []),
                    "file_names": list(result.get("file_names") or []),
                    "token": clean_text(result.get("token")),
                    "source": "worker",
                }
            st.success(
                f"Analisi completata · {int(result.get('total') or 0):,} righe · "
                f"abbinate {int(result.get('matched') or 0):,} · "
                f"ambigue {int(result.get('ambiguous') or 0):,} · "
                f"non abbinate {int(result.get('unmatched') or 0):,}."
            )
        elif tracking_analysis_job.status == "error":
            st.error(f"Analisi dei documenti non riuscita: {tracking_analysis_job.error}")
        else:
            aj2.caption(
                f"Job {tracking_analysis_job.job_id[:8]} · {tracking_analysis_job.status} · "
                "parsing e matching continuano in background."
            )

analysis = st.session_state.get(analysis_key)
if analysis:
    matches = [dict(item) for item in analysis["matches"]]
    supplier = clean_text(analysis.get("supplier"))
    matched_count = sum(item.get("match_status") == "Abbinato automaticamente" for item in matches)
    ambiguous_count = sum(str(item.get("match_status", "")).startswith("Ambiguo") for item in matches)
    unmatched_count = len(matches) - matched_count - ambiguous_count
    ready_count = sum(item.get("operational_status") == "Spedita · tracking disponibile" for item in matches)
    waiting_count = sum(item.get("operational_status") == "In attesa di spedizione" for item in matches)
    cancelled_count = sum(item.get("operational_status") == "Annullata nel file" for item in matches)

    metric_cols = st.columns(6)
    metric_cols[0].metric("Righe file", len(matches))
    metric_cols[1].metric("Abbinate", matched_count)
    metric_cols[2].metric("Ambigue", ambiguous_count)
    metric_cols[3].metric("Non abbinate", unmatched_count)
    metric_cols[4].metric("Pronte", ready_count)
    metric_cols[5].metric("In attesa / annullate", waiting_count + cancelled_count)

    with st.expander("Riconoscimento del fornitore"):
        source_is_packlink = clean_text(analysis.get("source")).lower() == "packlink"
        supplier_label = (
            "Tutti i fornitori · origine Packlink PRO"
            if source_is_packlink and not supplier else supplier
        )
        st.write(
            f"Fornitore utilizzato: **{supplier_label or 'Non specificato'}** · confidenza: "
            f"**{float(analysis.get('confidence') or 0):.0%}**"
        )
        ranking = analysis.get("ranking") or []
        if ranking:
            st.dataframe(ranking, use_container_width=True, hide_index=True)

    st.markdown("### 2. Verifica e correggi gli abbinamenti")
    st.caption(
        "Le righe in WAITING LABEL o CREATED restano in attesa. SENT TO WAREHOUSE "
        "con tracking e corriere è invece pronto per l'invio quando Worten è in SHIPPING."
    )

    detail_rows = []
    for position, item in enumerate(matches):
        detail_rows.append({
            "Rif. interno": position,
            "Esito": item.get("match_status", ""),
            "Punteggio": item.get("match_score", 0),
            "Ordine associato": item.get("order_id", ""),
            "Customer Name file": item.get("customer_name", ""),
            "Customer Name ordine": item.get("customer_name_order", ""),
            "Prodotto / codice": item.get("product", ""),
            "Stato file": item.get("file_status", ""),
            "Stato operativo": item.get("operational_status", ""),
            "Tracking": item.get("tracking", ""),
            "Corriere": item.get("carrier", ""),
            "Motivo": item.get("match_reason", ""),
        })
    detail_frame = pd.DataFrame(detail_rows)
    edited_detail = st.data_editor(
        detail_frame,
        use_container_width=True,
        hide_index=True,
        disabled=[
            "Rif. interno", "Esito", "Punteggio", "Customer Name file",
            "Customer Name ordine", "Prodotto / codice", "Stato file",
            "Stato operativo", "Motivo",
        ],
        column_config={
            "Rif. interno": None,
            "Punteggio": st.column_config.NumberColumn(format="%.1f"),
            "Ordine associato": st.column_config.TextColumn(width="medium"),
            "Tracking": st.column_config.TextColumn(width="medium"),
            "Corriere": st.column_config.TextColumn(width="medium"),
        },
        key=f"tracking_detail_editor_v150_{seller_id}_{account_id}_{analysis.get('token','')[:12]}",
    )

    order_lookup: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        order_lookup.setdefault(clean_text(order.get("order_id")), []).append(order)
    corrected_matches = [dict(item) for item in matches]
    for _, row in edited_detail.iterrows():
        position = int(row["Rif. interno"])
        if position < 0 or position >= len(corrected_matches):
            continue
        item = corrected_matches[position]
        order_id = clean_text(row["Ordine associato"])
        item["order_id"] = order_id
        item["tracking"] = clean_text(row["Tracking"])
        item["carrier"] = clean_text(row["Corriere"])
        if order_id in order_lookup:
            lines = order_lookup[order_id]
            item["customer_name_order"] = clean_text(lines[0].get("customer_name"))
            item["marketplace_status"] = clean_text(lines[0].get("raw_status"))
            item["market_label"] = clean_text(lines[0].get("market_label"))
            item["order_line_ids"] = [clean_text(line.get("order_line_id")) for line in lines]
            item["row_keys"] = [clean_text(line.get("row_key")) for line in lines]
        elif order_id:
            item["order_id"] = ""

    st.markdown("### 3. Ordini e tracciabilità")
    st.caption(
        "La tabella mostra una sola riga per ordine. Con Visualizza tutti gli ordini "
        "sono inclusi anche gli ordini non ancora spediti e in attesa di tracciabilità. "
        "Le selezioni vengono mantenute dopo il rerun di Streamlit; gli ordini già "
        "spediti restano nella tabella storica separata."
    )
    shipping_rows = order_tracking_rows(
        corrected_matches,
        marketplace=marketplace,
        orders=orders,
        supplier=supplier,
        include_without_tracking=True,
    )
    if not shipping_rows:
        st.info("Non risultano ordini da gestire nel periodo selezionato.")
    else:
        # Refresh live marketplace states before displaying or sending anything.
        # Cached order states are useful for speed but never authoritative for an
        # irreversible action such as creating a supplier order or confirming a shipment.
        live_metadata_error = ""
        if marketplace == "worten":
            order_ids_for_refresh = sorted({
                clean_text(item.get("Ordine")) for item in shipping_rows
                if clean_text(item.get("Ordine"))
            })
            order_ids_token = hashlib.sha256(
                "|".join(order_ids_for_refresh).encode("utf-8")
            ).hexdigest()[:12]
            live_metadata_key = (
                f"tracking_live_worten_orders_v176_{seller_id}_{account_id}_"
                f"{analysis.get('token','')[:12]}_{order_ids_token}"
            )
            cached_live_metadata = st.session_state.get(live_metadata_key)
            if not isinstance(cached_live_metadata, dict):
                with st.spinner("Lettura degli stati e delle scadenze esatte da Worten…"):
                    live_states, live_errors = fetch_worten_order_states(
                        credentials, order_ids_for_refresh
                    )
                    live_orders = {
                        order_id: dict(state.raw or {})
                        for order_id, state in live_states.items()
                    }
                    cached_live_metadata = {
                        "states": live_states,
                        "orders": live_orders,
                        "error": " · ".join(live_errors),
                    }
                st.session_state[live_metadata_key] = cached_live_metadata
            live_states = dict(cached_live_metadata.get("states") or {})
            live_orders = dict(cached_live_metadata.get("orders") or {})
            live_metadata_error = clean_text(cached_live_metadata.get("error"))

            refreshed_rows: list[dict[str, Any]] = []
            for source in shipping_rows:
                item = dict(source)
                order_id = clean_text(item.get("Ordine"))
                state = live_states.get(order_id)
                if state is not None:
                    item["Stato marketplace"] = state.raw_status
                    item["marketplace_status"] = state.raw_status
                    item["Macro-stato marketplace"] = state.macro_status
                    item["_live_order"] = live_orders.get(order_id, {})
                    item = apply_worten_eligibility(item)
                    if state.already_shipped:
                        item["already_shipped"] = True
                        item["Invio consentito"] = "No"
                        item["api_allowed"] = False
                        item["Stato operativo"] = "Già spedito sul marketplace"
                        item["Problemi"] = state.reason
                    elif state.cancelled:
                        item["live_cancelled"] = True
                        item["Invio consentito"] = "No"
                        item["api_allowed"] = False
                        item["Stato operativo"] = "Cancellato sul marketplace"
                        item["Problemi"] = state.reason
                    elif not state.can_mark_shipped:
                        item["Invio consentito"] = "No"
                        item["api_allowed"] = False
                        item["Stato operativo"] = state.label
                        item["Problemi"] = state.reason
                    elif item.get("waiting_for_tracking"):
                        item["Invio consentito"] = "No"
                        item["api_allowed"] = False
                        item["Stato operativo"] = "In attesa di tracciabilità"
                        item["Problemi"] = (
                            "tracking non ancora disponibile; attendere o caricare un "
                            "documento spedizioni aggiornato"
                        )
                else:
                    item["Invio consentito"] = "No"
                    item["api_allowed"] = False
                    item["Problemi"] = "stato live Worten non verificato"
                refreshed_rows.append(item)
            shipping_rows = refreshed_rows

        elif marketplace == "kaufland":
            unit_ids_for_refresh = sorted({
                clean_text(unit_id)
                for item in shipping_rows
                for unit_id in (item.get("order_line_ids") or [])
                if clean_text(unit_id)
            })
            units_token = hashlib.sha256(
                "|".join(unit_ids_for_refresh).encode("utf-8")
            ).hexdigest()[:12]
            live_metadata_key = (
                f"tracking_live_kaufland_units_v176_{seller_id}_{account_id}_"
                f"{analysis.get('token','')[:12]}_{units_token}"
            )
            cached_live_metadata = st.session_state.get(live_metadata_key)
            if not isinstance(cached_live_metadata, dict):
                with st.spinner("Lettura degli stati attuali delle unità Kaufland…"):
                    unit_states, unit_errors = fetch_kaufland_unit_states(
                        credentials, unit_ids_for_refresh
                    )
                    cached_live_metadata = {
                        "states": unit_states,
                        "error": " · ".join(unit_errors),
                    }
                st.session_state[live_metadata_key] = cached_live_metadata
            unit_states = dict(cached_live_metadata.get("states") or {})
            live_metadata_error = clean_text(cached_live_metadata.get("error"))

            refreshed_rows = []
            for source in shipping_rows:
                item = dict(source)
                order_id = clean_text(item.get("Ordine"))
                order_unit_ids = [
                    clean_text(value) for value in (item.get("order_line_ids") or [])
                    if clean_text(value)
                ]
                states = [unit_states[value] for value in order_unit_ids if value in unit_states]
                if states:
                    aggregate = aggregate_kaufland_states(states, order_id=order_id)
                    item = _apply_live_kaufland_state(item, aggregate)
                else:
                    item["Invio consentito"] = "No"
                    item["api_allowed"] = False
                    item["Problemi"] = "stato live delle unità Kaufland non verificato"
                refreshed_rows.append(item)
            shipping_rows = refreshed_rows

        # The successful-send history is stored in the database and is checked
        # before every display and again immediately before the API request.
        sent_history = successful_api_orders(
            seller_id=seller_id,
            account_id=account_id,
            marketplace=marketplace,
            order_ids=[clean_text(item.get("Ordine")) for item in shipping_rows],
        )
        shipping_rows = mark_rows_already_sent(shipping_rows, sent_history)
        shipping_rows, already_shipped_rows = partition_shipping_rows(shipping_rows)

        if live_metadata_error:
            st.warning(
                "La verifica live degli stati marketplace non è stata completata. "
                "Gli ordini non verificati restano bloccati per sicurezza: "
                f"{live_metadata_error}"
            )

        # Add the exact marketplace shipping deadline. Estimates based on
        # leadtime_to_ship are deliberately disabled in this operational table:
        # if the API does not return shipping_deadline, it is shown as unavailable.
        now_utc = datetime.now(timezone.utc)
        deadline_by_order: dict[str, Any] = {}
        for item in shipping_rows:
            order_id = clean_text(item.get("Ordine"))
            deadline_lines = list(order_lookup.get(order_id, []))
            live_order = item.get("_live_order")
            if isinstance(live_order, dict):
                deadline_lines.append({"raw_json": {"order": live_order}})
            deadline = shipping_deadline_for_order(
                deadline_lines,
                marketplace=marketplace,
                reference=now_utc,
                allow_estimate=False,
            )
            deadline_by_order[order_id] = deadline
            item["Scadenza invio"] = deadline.display
            item["Urgenza"] = deadline.status
            item["Fonte scadenza"] = deadline.source
            item["_deadline_utc"] = deadline.deadline_utc
            item["_deadline_local"] = deadline.deadline_local
            item["_overdue"] = deadline.overdue
            item["_urgent"] = deadline.urgent

        shipping_rows.sort(
            key=lambda item: (
                item.get("_deadline_utc") is None,
                item.get("_deadline_utc") or datetime.max.replace(tzinfo=timezone.utc),
                clean_text(item.get("Ordine")),
            )
        )

        # Filtri avanzati applicati prima dei filtri rapidi sulle scadenze.
        # I codici API grezzi restano visibili e selezionabili: eventuali nuovi
        # stati aggiunti dai marketplace vengono inclusi automaticamente.
        for item in shipping_rows:
            item["Macro-stato marketplace"] = (
                clean_text(item.get("Macro-stato marketplace"))
                or _tracking_marketplace_macro(
                    item.get("Stato marketplace"), marketplace
                )
            )
            item["Macro-stato file"] = _tracking_file_macro(
                item.get("Stato file originale") or item.get("file_status")
            )
            item["Tracking presente"] = "Sì" if clean_text(item.get("Tracking")) else "No"
            item["Corriere presente"] = "Sì" if clean_text(item.get("Corriere")) else "No"

        advanced_scope = (
            f"tracking_advanced_filters_v165_{seller_id}_{account_id}_"
            f"{analysis.get('token','')[:12]}"
        )
        st.markdown("#### Filtri avanzati")
        search_value = st.text_input(
            "Ricerca ordine, cliente, tracking, corriere, market, fornitore o problema",
            key=f"{advanced_scope}_search",
        ).strip().casefold()
        fcol1, fcol2, fcol3, fcol4 = st.columns(4)
        marketplace_status_options = sorted({
            clean_text(item.get("Stato marketplace")) for item in shipping_rows
            if clean_text(item.get("Stato marketplace"))
        })
        selected_marketplace_statuses = fcol1.multiselect(
            "Stati API marketplace", marketplace_status_options,
            key=f"{advanced_scope}_marketplace_status",
        )
        marketplace_macro_options = sorted({
            clean_text(item.get("Macro-stato marketplace")) for item in shipping_rows
        })
        selected_marketplace_macros = fcol2.multiselect(
            "Macro-stato marketplace", marketplace_macro_options,
            key=f"{advanced_scope}_marketplace_macro",
        )
        file_status_options = sorted({
            clean_text(item.get("Stato file originale") or item.get("file_status"))
            for item in shipping_rows
            if clean_text(item.get("Stato file originale") or item.get("file_status"))
        })
        selected_file_statuses = fcol3.multiselect(
            "Stati originali file spedizioni", file_status_options,
            key=f"{advanced_scope}_file_status",
        )
        operational_options = sorted({
            clean_text(item.get("Stato operativo")) for item in shipping_rows
            if clean_text(item.get("Stato operativo"))
        })
        selected_operational = fcol4.multiselect(
            "Stati operativi", operational_options,
            key=f"{advanced_scope}_operational",
        )

        fcol5, fcol6, fcol7, fcol8 = st.columns(4)
        selected_tracking_presence = fcol5.multiselect(
            "Tracking", ["Sì", "No"], key=f"{advanced_scope}_tracking"
        )
        selected_carrier_presence = fcol6.multiselect(
            "Corriere", ["Sì", "No"], key=f"{advanced_scope}_carrier"
        )
        selected_api_allowed = fcol7.multiselect(
            "Invio API consentito", ["Sì", "No"], key=f"{advanced_scope}_api_allowed"
        )
        selected_urgency = fcol8.multiselect(
            "Urgenza", sorted({clean_text(item.get("Urgenza")) for item in shipping_rows}),
            key=f"{advanced_scope}_urgency",
        )

        fcol9, fcol10, fcol11, fcol12 = st.columns(4)
        selected_markets = fcol9.multiselect(
            "Market / nazione", sorted({clean_text(item.get("Market")) for item in shipping_rows}),
            key=f"{advanced_scope}_market",
        )
        selected_suppliers = fcol10.multiselect(
            "Fornitore", sorted({clean_text(item.get("Fornitore")) for item in shipping_rows}),
            key=f"{advanced_scope}_supplier",
        )
        min_units = fcol11.number_input(
            "Unità/righe minime", min_value=0, value=0, step=1,
            key=f"{advanced_scope}_min_units",
        )
        only_with_problems = fcol12.checkbox(
            "Solo ordini con problemi", key=f"{advanced_scope}_problems"
        )

        advanced_filtered_rows: list[dict[str, Any]] = []
        for item in shipping_rows:
            haystack = " | ".join([
                clean_text(item.get("Ordine")), clean_text(item.get("Customer Name")),
                clean_text(item.get("Tracking")), clean_text(item.get("Corriere")),
                clean_text(item.get("Market")), clean_text(item.get("Fornitore")),
                clean_text(item.get("Problemi")), clean_text(item.get("Stato file")),
                clean_text(item.get("Stato marketplace")),
            ]).casefold()
            if search_value and search_value not in haystack:
                continue
            if selected_marketplace_statuses and clean_text(item.get("Stato marketplace")) not in selected_marketplace_statuses:
                continue
            if selected_marketplace_macros and clean_text(item.get("Macro-stato marketplace")) not in selected_marketplace_macros:
                continue
            original_file_status = clean_text(item.get("Stato file originale") or item.get("file_status"))
            if selected_file_statuses and original_file_status not in selected_file_statuses:
                continue
            if selected_operational and clean_text(item.get("Stato operativo")) not in selected_operational:
                continue
            if selected_tracking_presence and item.get("Tracking presente") not in selected_tracking_presence:
                continue
            if selected_carrier_presence and item.get("Corriere presente") not in selected_carrier_presence:
                continue
            if selected_api_allowed and clean_text(item.get("Invio consentito")) not in selected_api_allowed:
                continue
            if selected_urgency and clean_text(item.get("Urgenza")) not in selected_urgency:
                continue
            if selected_markets and clean_text(item.get("Market")) not in selected_markets:
                continue
            if selected_suppliers and clean_text(item.get("Fornitore")) not in selected_suppliers:
                continue
            if int(item.get("Unità/righe") or 0) < int(min_units):
                continue
            if only_with_problems and not clean_text(item.get("Problemi")):
                continue
            advanced_filtered_rows.append(item)

        reset_filters_col, filter_count_col = st.columns([1, 3])
        if reset_filters_col.button(
            "Azzera filtri avanzati", use_container_width=True,
            key=f"{advanced_scope}_reset",
        ):
            for suffix in (
                "search", "marketplace_status", "marketplace_macro", "file_status",
                "operational", "tracking", "carrier", "api_allowed", "urgency",
                "market", "supplier", "min_units", "problems",
            ):
                st.session_state.pop(f"{advanced_scope}_{suffix}", None)
            st.rerun()
        filter_count_col.info(
            f"Ordini dopo i filtri avanzati: {len(advanced_filtered_rows):,} su {len(shipping_rows):,}."
        )

        filter_scope = (
            f"tracking_deadline_filter_{seller_id}_{account_id}_"
            f"{analysis.get('token','')[:12]}"
        )
        filter_mode_key = f"{filter_scope}_mode"
        if filter_mode_key not in st.session_state:
            st.session_state[filter_mode_key] = "all"

        deadline_dates = [
            item["_deadline_local"].date()
            for item in shipping_rows
            if item.get("_deadline_local") is not None
        ]
        default_deadline_from = min(deadline_dates) if deadline_dates else date.today()
        default_deadline_to = max(deadline_dates) if deadline_dates else date.today()
        deadline_from_key = f"{filter_scope}_from"
        deadline_to_key = f"{filter_scope}_to"
        if deadline_from_key not in st.session_state:
            st.session_state[deadline_from_key] = default_deadline_from
        if deadline_to_key not in st.session_state:
            st.session_state[deadline_to_key] = default_deadline_to

        st.markdown("#### Scadenze di spedizione")
        st.caption(
            "Per Worten viene utilizzata esclusivamente la scadenza esatta "
            "`shipping_deadline` restituita dall'API e visualizzata nel fuso del "
            "marketplace (WEST/WET). Le stime da lead time non vengono usate."
        )
        date_col1, date_col2 = st.columns(2)
        deadline_from = date_col1.date_input(
            "Scadenza dal",
            key=deadline_from_key,
        )
        deadline_to = date_col2.date_input(
            "Scadenza al",
            key=deadline_to_key,
        )
        if deadline_from > deadline_to:
            st.error("La data iniziale della scadenza non può superare quella finale.")
            deadline_from, deadline_to = deadline_to, deadline_from

        filter_col1, filter_col2, filter_col3, filter_col4, filter_col5 = st.columns(5)
        if filter_col1.button(
            "Filtra ordini urgenti (<24 ore)",
            type="primary",
            use_container_width=True,
            key=f"{filter_scope}_urgent",
        ):
            st.session_state[filter_mode_key] = "urgent"
            st.rerun()
        if filter_col2.button(
            "Filtra ordini scaduti",
            use_container_width=True,
            key=f"{filter_scope}_expired",
        ):
            st.session_state[filter_mode_key] = "expired"
            st.rerun()
        if filter_col3.button(
            "In attesa di tracciabilità",
            use_container_width=True,
            key=f"{filter_scope}_waiting_tracking",
        ):
            st.session_state[filter_mode_key] = "waiting_tracking"
            st.rerun()
        if filter_col4.button(
            "Applica intervallo date",
            use_container_width=True,
            key=f"{filter_scope}_date",
        ):
            st.session_state[filter_mode_key] = "date"
            st.rerun()
        if filter_col5.button(
            "Visualizza tutti gli ordini",
            use_container_width=True,
            key=f"{filter_scope}_all",
        ):
            st.session_state[filter_mode_key] = "all"
            st.rerun()

        filter_mode = st.session_state.get(filter_mode_key, "all")
        if filter_mode == "urgent":
            visible_shipping_rows = [
                item for item in advanced_filtered_rows
                if (item.get("_urgent") or item.get("_overdue"))
                and not item.get("already_shipped")
            ]
            active_filter_label = "Urgenti: scaduti o con meno di 24 ore residue"
        elif filter_mode == "expired":
            visible_shipping_rows = [
                item for item in advanced_filtered_rows
                if item.get("_overdue") and not item.get("already_shipped")
            ]
            active_filter_label = "Solo ordini già scaduti"
        elif filter_mode == "waiting_tracking":
            visible_shipping_rows = [
                item for item in advanced_filtered_rows
                if bool(item.get("waiting_for_tracking"))
                and not item.get("already_shipped")
            ]
            active_filter_label = "Ordini in attesa di tracciabilità"
        elif filter_mode == "date":
            visible_shipping_rows = [
                item for item in advanced_filtered_rows
                if is_deadline_in_date_range(
                    deadline_by_order[clean_text(item.get("Ordine"))],
                    date_from=deadline_from,
                    date_to=deadline_to,
                )
            ]
            active_filter_label = (
                f"Scadenza dal {deadline_from:%d/%m/%Y} al {deadline_to:%d/%m/%Y}"
            )
        else:
            visible_shipping_rows = list(advanced_filtered_rows)
            active_filter_label = (
                "Tutti gli ordini da gestire, compresi quelli in attesa di tracciabilità"
            )

        expired_count = sum(bool(item.get("_overdue")) for item in shipping_rows)
        urgent_count = sum(
            bool(item.get("_urgent")) and not bool(item.get("_overdue"))
            for item in shipping_rows
        )
        waiting_tracking_count = sum(
            bool(item.get("waiting_for_tracking")) for item in shipping_rows
        )
        ready_tracking_count = sum(
            bool(clean_text(item.get("Tracking"))) for item in shipping_rows
        )
        st.caption(
            f"Filtro attivo: **{active_filter_label}** · "
            f"con tracking: **{ready_tracking_count}** · "
            f"in attesa di tracciabilità: **{waiting_tracking_count}** · "
            f"urgenti entro 24 ore: **{urgent_count}** · scaduti: **{expired_count}**. "
            "Le scadenze non restituite dall'API sono indicate come Non disponibile."
        )

        if not visible_shipping_rows:
            st.info("Nessun ordine corrisponde al filtro di scadenza selezionato.")
        else:
            display_columns = [
                "Ordine", "Customer Name", "Market", "Fornitore",
                "Tracking", "Corriere", "Scadenza invio", "Urgenza",
                "Stato operativo", "Stato file", "Stato marketplace",
                "Macro-stato marketplace", "Unità/righe", "Righe file",
                "Invio consentito", "Problemi",
            ]

            selection_scope = (
                f"tracking_selection_v165_{seller_id}_{account_id}_"
                f"{analysis.get('token','')[:12]}"
            )
            selected_key = f"{selection_scope}_orders"
            selection_revision_key = f"{selection_scope}_revision"
            st.session_state.setdefault(selected_key, [])
            st.session_state.setdefault(selection_revision_key, 0)
            selected_ids = {
                clean_text(item) for item in st.session_state.get(selected_key, [])
                if clean_text(item)
            }
            active_order_ids = {
                clean_text(item.get("Ordine")) for item in shipping_rows
                if clean_text(item.get("Ordine"))
            }
            selected_ids &= active_order_ids
            visible_ids = [clean_text(item.get("Ordine")) for item in visible_shipping_rows]
            visible_id_set = set(visible_ids)

            select_col1, select_col2, select_col3 = st.columns(3)
            if select_col1.button(
                "Seleziona tutti i filtrati", type="primary", use_container_width=True,
                key=f"{selection_scope}_select_all_{filter_mode}",
            ):
                selected_ids.update(visible_id_set)
                st.session_state[selected_key] = sorted(selected_ids)
                st.session_state[selection_revision_key] += 1
                st.rerun()
            if select_col2.button(
                "Seleziona solo inviabili filtrati", use_container_width=True,
                key=f"{selection_scope}_select_valid_{filter_mode}",
            ):
                selected_ids.update(
                    clean_text(item.get("Ordine")) for item in visible_shipping_rows
                    if item.get("Invio consentito") == "Sì"
                )
                st.session_state[selected_key] = sorted(selected_ids)
                st.session_state[selection_revision_key] += 1
                st.rerun()
            if select_col3.button(
                "Deseleziona tutti", use_container_width=True,
                key=f"{selection_scope}_clear",
            ):
                st.session_state[selected_key] = []
                st.session_state[selection_revision_key] += 1
                st.rerun()

            selection_revision = int(st.session_state[selection_revision_key])
            table = pd.DataFrame([
                {"_order_id": clean_text(item.get("Ordine")), **{
                    column: item.get(column, "") for column in display_columns
                }}
                for item in visible_shipping_rows
            ])

            if AgGrid is not None:
                grid_builder = GridOptionsBuilder.from_dataframe(table)
                grid_builder.configure_default_column(
                    sortable=True, filter=True, resizable=True, minWidth=95
                )
                grid_builder.configure_column("_order_id", hide=True)
                grid_builder.configure_column(
                    "Ordine", checkboxSelection=True, headerCheckboxSelection=True,
                    headerCheckboxSelectionFilteredOnly=True, pinned="left", minWidth=155,
                )
                grid_builder.configure_column("Customer Name", minWidth=210)
                grid_builder.configure_column("Tracking", minWidth=180)
                grid_builder.configure_column("Corriere", minWidth=130)
                grid_builder.configure_column("Scadenza invio", minWidth=185)
                grid_builder.configure_column("Problemi", minWidth=320)
                grid_builder.configure_selection(selection_mode="multiple", use_checkbox=True)
                grid_builder.configure_grid_options(
                    rowMultiSelectWithClick=True, suppressRowClickSelection=False,
                    enableRangeSelection=True, animateRows=False,
                )
                preselected = [
                    index for index, order_id in enumerate(table["_order_id"].tolist())
                    if order_id in selected_ids
                ]
                manual_mode = getattr(
                    GridUpdateMode, "MANUAL", GridUpdateMode.SELECTION_CHANGED
                )
                grid_response = AgGrid(
                    table, gridOptions=grid_builder.build(),
                    data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                    update_mode=manual_mode, pre_selected_rows=preselected,
                    height=590, fit_columns_on_grid_load=False, theme="streamlit",
                    key=(
                        f"tracking_orders_grid_manual_v167_{selection_scope}_"
                        f"{filter_mode}_{selection_revision}"
                    ),
                )
                returned_selected = grid_response.get("selected_rows")
                if isinstance(returned_selected, pd.DataFrame):
                    grid_selected_ids = set(
                        returned_selected.get("_order_id", pd.Series(dtype=str)).astype(str)
                    )
                elif isinstance(returned_selected, list):
                    grid_selected_ids = {
                        clean_text(item.get("_order_id")) for item in returned_selected
                        if isinstance(item, Mapping) and clean_text(item.get("_order_id"))
                    }
                else:
                    grid_selected_ids = set()
                selected_ids = (selected_ids - visible_id_set) | grid_selected_ids
                st.session_state[selected_key] = sorted(selected_ids)
                st.caption(
                    "Puoi selezionare più ordini senza refresh. Dopo aver terminato, "
                    "premi **Applica** nel riquadro AgGrid per trasferire la selezione "
                    "al comando di invio. Il quadratino nell'intestazione seleziona "
                    "tutti gli ordini attualmente filtrati nella griglia."
                )
            else:
                st.warning(
                    "st-aggrid non è disponibile: viene usata la tabella di emergenza, "
                    "che può eseguire un rerun a ogni modifica."
                )
                fallback = table.copy()
                fallback.insert(0, "Seleziona", fallback["_order_id"].isin(selected_ids))
                edited_shipping = st.data_editor(
                    fallback, use_container_width=True, hide_index=True, height=590,
                    disabled=[column for column in fallback.columns if column != "Seleziona"],
                    column_config={
                        "_order_id": None,
                        "Seleziona": st.column_config.CheckboxColumn(required=True),
                    },
                    key=(
                        f"tracking_orders_fallback_v167_{selection_scope}_"
                        f"{filter_mode}_{selection_revision}"
                    ),
                )
                selected_visible_ids = {
                    clean_text(row.get("_order_id")) for _, row in edited_shipping.iterrows()
                    if bool(row.get("Seleziona")) and clean_text(row.get("_order_id"))
                }
                selected_ids = (selected_ids - visible_id_set) | selected_visible_ids
                st.session_state[selected_key] = sorted(selected_ids)

            shipping_by_order = {
                clean_text(item.get("Ordine")): item for item in shipping_rows
            }
            selected = [
                dict(shipping_by_order[order_id])
                for order_id in sorted(selected_ids)
                if order_id in shipping_by_order
            ]
            for item in selected:
                item["Aggiorna"] = True
            valid_selected = [
                item for item in selected if item.get("Invio consentito") == "Sì"
            ]
            already_sent_selected = [
                item for item in selected if item.get("already_shipped")
            ]
            blocked_selected = [
                item for item in selected
                if item.get("Invio consentito") != "Sì" and not item.get("already_shipped")
            ]
            units_count = sum(
                int(item.get("Unità/righe") or 0) for item in valid_selected
            )
            st.caption(
                f"Ordini ancora da gestire: {len(shipping_rows)} · visibili: "
                f"{len(visible_shipping_rows)} · selezionati: {len(selected)} · "
                f"da inviare via API: {len(valid_selected)} · "
                f"righe/unità da trasmettere: {units_count}."
            )
            if already_sent_selected:
                st.info(
                    f"{len(already_sent_selected)} ordini selezionati risultano già "
                    "spediti e saranno saltati. Verranno inviati soltanto i restanti "
                    f"{len(valid_selected)} ordini mancanti."
                )
            if blocked_selected:
                st.warning(
                    f"{len(blocked_selected)} ordini selezionati non sono inviabili. "
                    "Saranno saltati senza bloccare l'invio degli altri; controlla "
                    "la colonna Problemi."
                )

            st.caption(
                "Il pulsante invia immediatamente al marketplace il tracking e il "
                "corriere degli ordini ancora mancanti e conferma la spedizione. "
                "Gli ordini già spediti vengono riconosciuti e ignorati."
            )
            if st.button(
                "Invia aggiornamento spedizione",
                type="primary",
                use_container_width=True,
                disabled=not valid_selected,
                key=(
                    f"tracking_send_update_v155_{seller_id}_{account_id}_"
                    f"{analysis.get('token','')[:12]}"
                ),
            ):
                results: list[dict[str, Any]] = []
                # Recheck the persistent history immediately before the API call
                # to prevent double sends from two reruns or browser tabs.
                fresh_history = successful_api_orders(
                    seller_id=seller_id,
                    account_id=account_id,
                    marketplace=marketplace,
                    order_ids=[clean_text(item.get("Ordine")) for item in valid_selected],
                )
                skipped_now = [
                    item for item in valid_selected
                    if clean_text(item.get("Ordine")) in fresh_history
                ]
                to_send = [
                    item for item in valid_selected
                    if clean_text(item.get("Ordine")) not in fresh_history
                ]
                for item in skipped_now:
                    results.append({
                        "Ordine": clean_text(item.get("Ordine")),
                        "Esito": "Già spedito · saltato",
                        "Tracking inviato": clean_text(item.get("Tracking")),
                        "Corriere inviato": clean_text(item.get("Corriere")),
                        "Messaggio": "Ordine già inviato con successo in precedenza",
                    })

                # Verifica live immediatamente prima dell'invio. Gli stati salvati
                # nella cache o visualizzati nella griglia non vengono mai usati da
                # soli per una modifica irreversibile sul marketplace.
                if marketplace == "worten" and to_send:
                    live_states, live_errors = fetch_worten_order_states(
                        credentials,
                        [clean_text(item.get("Ordine")) for item in to_send],
                    )
                    if live_errors:
                        for error in live_errors:
                            results.append({
                                "Ordine": "",
                                "Esito": "Errore",
                                "Tracking inviato": "",
                                "Corriere inviato": "",
                                "Messaggio": f"Verifica stato Worten: {error}",
                            })
                    live_ready: list[dict[str, Any]] = []
                    for item in to_send:
                        order_id = clean_text(item.get("Ordine"))
                        state = live_states.get(order_id)
                        if state is None:
                            results.append({
                                "Ordine": order_id,
                                "Esito": "Errore",
                                "Tracking inviato": clean_text(item.get("Tracking")),
                                "Corriere inviato": clean_text(item.get("Corriere")),
                                "Messaggio": "Stato live Worten non verificato: invio bloccato",
                            })
                            continue
                        if state.already_shipped:
                            results.append({
                                "Ordine": order_id,
                                "Esito": "Già spedito · saltato",
                                "Tracking inviato": clean_text(item.get("Tracking")),
                                "Corriere inviato": clean_text(item.get("Corriere")),
                                "Messaggio": f"{state.raw_status}: {state.reason}",
                            })
                            continue
                        if state.cancelled:
                            results.append({
                                "Ordine": order_id,
                                "Esito": "Cancellato · saltato",
                                "Tracking inviato": clean_text(item.get("Tracking")),
                                "Corriere inviato": clean_text(item.get("Corriere")),
                                "Messaggio": f"{state.raw_status}: {state.reason}",
                            })
                            continue
                        if not state.can_mark_shipped:
                            results.append({
                                "Ordine": order_id,
                                "Esito": "Stato non spedibile · saltato",
                                "Tracking inviato": clean_text(item.get("Tracking")),
                                "Corriere inviato": clean_text(item.get("Corriere")),
                                "Messaggio": f"{state.raw_status}: {state.reason}",
                            })
                            continue
                        ready_item = dict(item)
                        ready_item["marketplace_status"] = state.raw_status
                        ready_item["Stato marketplace"] = state.raw_status
                        ready_item["api_allowed"] = True
                        ready_item["Invio consentito"] = "Sì"
                        live_ready.append(ready_item)
                    to_send = live_ready

                elif marketplace == "kaufland" and to_send:
                    all_unit_ids = [
                        clean_text(unit_id)
                        for item in to_send
                        for unit_id in (
                            item.get("order_line_ids_to_send")
                            or item.get("order_line_ids")
                            or []
                        )
                        if clean_text(unit_id)
                    ]
                    live_units, live_errors = fetch_kaufland_unit_states(
                        credentials, all_unit_ids
                    )
                    if live_errors:
                        for error in live_errors:
                            results.append({
                                "Ordine": "",
                                "Esito": "Errore",
                                "Tracking inviato": "",
                                "Corriere inviato": "",
                                "Messaggio": f"Verifica stato Kaufland: {error}",
                            })
                    live_ready = []
                    for item in to_send:
                        order_id = clean_text(item.get("Ordine"))
                        source_ids = [
                            clean_text(value)
                            for value in (item.get("order_line_ids") or [])
                            if clean_text(value)
                        ]
                        states = [live_units[value] for value in source_ids if value in live_units]
                        aggregate = aggregate_kaufland_states(states, order_id=order_id)
                        send_ids = list(aggregate.get("shippable_unit_ids") or [])
                        if not send_ids:
                            if aggregate.get("already_shipped"):
                                outcome = "Già spedito · saltato"
                            elif aggregate.get("cancelled"):
                                outcome = "Cancellato · saltato"
                            else:
                                outcome = "Stato non spedibile · saltato"
                            results.append({
                                "Ordine": order_id,
                                "Esito": outcome,
                                "Tracking inviato": clean_text(item.get("Tracking")),
                                "Corriere inviato": clean_text(item.get("Corriere")),
                                "Messaggio": clean_text(aggregate.get("reason")),
                            })
                            continue
                        ready_item = dict(item)
                        ready_item["order_line_ids_to_send"] = send_ids
                        ready_item["api_allowed"] = True
                        ready_item["Invio consentito"] = "Sì"
                        live_ready.append(ready_item)
                    to_send = live_ready

                with st.spinner(
                    f"Invio di tracking, corriere e stato spedito a {marketplace.title()}…"
                ):
                    if marketplace == "worten" and to_send:
                        client = WortenTrackingClient(
                            api_url=credentials.get("api_url", ""),
                            api_key=credentials.get("api_key", ""),
                            shop_id=credentials.get("shop_id"),
                        )
                        api_results = ship_selected_worten_orders(client, to_send)
                        for result in api_results:
                            source = next(
                                (
                                    item for item in to_send
                                    if clean_text(item.get("Ordine")) == result.order_id
                                ),
                                shipping_by_order.get(result.order_id, {}),
                            )
                            tracking_sent = clean_text(
                                source.get("Tracking") or source.get("tracking")
                            )
                            carrier_sent = clean_text(
                                source.get("Corriere") or source.get("carrier")
                            )
                            if result.success:
                                mark_accounting_order_shipped(
                                    account_id=account_id,
                                    marketplace=marketplace,
                                    order_id=result.order_id,
                                    tracking=tracking_sent,
                                    carrier=carrier_sent,
                                )
                            record_api_result(
                                seller_id=seller_id,
                                account_id=account_id,
                                marketplace=marketplace,
                                order_id=result.order_id,
                                success=result.success,
                                message=result.message,
                            )
                            results.append({
                                "Ordine": result.order_id,
                                "Esito": "OK" if result.success else "Errore",
                                "Tracking inviato": tracking_sent,
                                "Corriere inviato": carrier_sent,
                                "Tracking aggiornato": result.tracking_updated,
                                "Spedizione confermata": result.shipment_validated,
                                "Messaggio": result.message,
                            })
                    elif marketplace == "kaufland" and to_send:
                        client = KauflandClient(
                            clean_text(credentials.get("client_key")),
                            clean_text(credentials.get("secret_key")),
                            playground=bool(
                                credentials.get("playground") or credentials.get("test")
                            ),
                        )
                        carrier_map = {
                            "DHL PAKET": "DHL", "DHL": "DHL",
                            "DPD FRANCE": "DPD", "DPD": "DPD",
                            "GLS ITALIA": "GLS", "GLS NACIONAL": "GLS",
                            "GLS": "GLS", "UPS": "UPS", "MRW": "MRW",
                            "SEUR": "SEUR",
                        }
                        for item in to_send:
                            order_id = clean_text(item.get("Ordine"))
                            carrier_name = clean_text(
                                item.get("Corriere") or item.get("carrier")
                            )
                            carrier_code = carrier_map.get(
                                carrier_name.upper(), "Other"
                            )
                            tracking = clean_text(
                                item.get("Tracking") or item.get("tracking")
                            ).replace(" / ", ",")
                            successes = 0
                            skipped_units: list[str] = []
                            errors: list[str] = []
                            for line_id in item.get("order_line_ids_to_send") or []:
                                try:
                                    # Secondo controllo per singola unità subito prima
                                    # del PATCH /send: evita doppio invio o invio di una
                                    # unità cancellata tra la lettura e il comando.
                                    live_response = client.order_unit(line_id)
                                    live_state = classify_kaufland_unit(live_response, str(line_id))
                                    if live_state.already_shipped:
                                        skipped_units.append(
                                            f"unità {line_id} già {live_state.raw_status}"
                                        )
                                        continue
                                    if live_state.cancelled:
                                        skipped_units.append(
                                            f"unità {line_id} cancellata"
                                        )
                                        continue
                                    if not live_state.can_mark_shipped:
                                        skipped_units.append(
                                            f"unità {line_id} in stato {live_state.raw_status}"
                                        )
                                        continue
                                    client.mark_order_unit_sent(
                                        line_id, carrier_code, tracking
                                    )
                                    successes += 1
                                except Exception as exc:
                                    errors.append(f"unità {line_id}: {exc}")
                            success = successes > 0 and not errors
                            message_parts: list[str] = []
                            if successes:
                                message_parts.append(
                                    f"{successes} unità contrassegnate come spedite"
                                )
                            if skipped_units:
                                message_parts.append("; ".join(skipped_units))
                            if errors:
                                message_parts.append("; ".join(errors))
                            message = " · ".join(message_parts) or "nessuna unità inviata"
                            if success:
                                mark_accounting_order_shipped(
                                    account_id=account_id,
                                    marketplace=marketplace,
                                    order_id=order_id,
                                    tracking=tracking,
                                    carrier=carrier_name,
                                )
                            record_api_result(
                                seller_id=seller_id,
                                account_id=account_id,
                                marketplace=marketplace,
                                order_id=order_id,
                                success=success,
                                message=message,
                            )
                            result_label = (
                                "OK" if success
                                else ("Già spedito · saltato" if skipped_units and not errors else "Errore")
                            )
                            results.append({
                                "Ordine": order_id,
                                "Esito": result_label,
                                "Tracking inviato": tracking,
                                "Corriere inviato": carrier_name,
                                "Unità inviate": successes,
                                "Unità saltate": len(skipped_units),
                                "Messaggio": message,
                            })

                st.dataframe(results, use_container_width=True, hide_index=True)
                success_count = sum(item.get("Esito") == "OK" for item in results)
                skipped_count = sum(
                    "saltato" in str(item.get("Esito", "")).casefold()
                    for item in results
                )
                failed_count = sum(item.get("Esito") == "Errore" for item in results)

                completed_ids = {
                    clean_text(item.get("Ordine"))
                    for item in results
                    if item.get("Esito") == "OK"
                    or "saltato" in str(item.get("Esito", "")).casefold()
                }
                if completed_ids:
                    remaining_selection = selected_ids - completed_ids
                    st.session_state[selected_key] = sorted(remaining_selection)
                    st.session_state[selection_revision_key] += 1

                if success_count:
                    message = f"{success_count} ordini aggiornati correttamente."
                    if skipped_count:
                        message += f" {skipped_count} erano già spediti e sono stati saltati."
                    if failed_count:
                        message += f" {failed_count} non sono riusciti: controlla i messaggi."
                    st.success(message)
                elif skipped_count and not failed_count:
                    st.info(
                        f"{skipped_count} ordini erano già stati spediti; nessun doppio invio "
                        "è stato effettuato."
                    )
                elif failed_count:
                    st.error("Nessun ordine è stato aggiornato. Controlla i messaggi API.")

        if already_shipped_rows:
            with st.expander(
                f"Ordini già spediti ({len(already_shipped_rows)})",
                expanded=False,
            ):
                st.caption(
                    "Questi ordini non compaiono nella tabella operativa e non possono "
                    "essere selezionati di nuovo. Sono mostrati soltanto come storico."
                )
                shipped_columns = [
                    "Ordine", "Customer Name", "Market", "Fornitore", "Tracking",
                    "Corriere", "Stato marketplace", "Stato file", "Data invio",
                    "Motivo",
                ]
                shipped_frame = pd.DataFrame([
                    {
                        "Ordine": item.get("Ordine", ""),
                        "Customer Name": item.get("Customer Name", ""),
                        "Market": item.get("Market", ""),
                        "Fornitore": item.get("Fornitore", ""),
                        "Tracking": item.get("Tracking", ""),
                        "Corriere": item.get("Corriere", ""),
                        "Stato marketplace": item.get("Stato marketplace", ""),
                        "Stato file": item.get("Stato file", ""),
                        "Data invio": clean_text(item.get("api_sent_at")).replace("T", " ")[:16],
                        "Motivo": item.get("Problemi", "ordine già spedito"),
                    }
                    for item in already_shipped_rows
                ], columns=shipped_columns)
                st.dataframe(shipped_frame, use_container_width=True, hide_index=True)

st.divider()
with st.expander("Importazioni recenti"):
    history = recent_imports(seller_id, account_id, marketplace)
    if history:
        st.dataframe(history, use_container_width=True, hide_index=True)
    else:
        st.info("Nessuna importazione registrata per questo account.")
