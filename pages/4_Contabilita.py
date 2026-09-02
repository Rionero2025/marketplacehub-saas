from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import streamlit as st

from services.accounting import (
    accounting_excel_comparison_report_bytes,
    accounting_excel_import_history,
    accounting_excel_sheet_names,
    apply_accounting_excel_updates,
    compare_accounting_with_excel,
    computed_profit_values,
    default_file_name,
    download_accounting_comparison_url,
    ensure_schema,
    accounting_export_bytes,
    export_history,
    export_xlsx_bytes,
    fetch_kaufland_accounting_orders,
    fetch_worten_accounting_orders,
    previous_exports,
    read_accounting_comparison_excel,
    save_export,
    save_accounting_inline_edits,
    save_manual_fields,
    totals,
    upsert_accounting_rows,
)
from marketplace_core.accounting import AccountingCore, AccountingPeriod, AccountingScope
from marketplace_core.jobs import JobsCore

from services.accounting_pdf import (
    accounting_pdf_bytes,
    accounting_pdf_file_name,
    available_accounting_periods,
    build_accounting_pdf_period,
    filter_accounting_records,
    month_label,
)
from services.cecotec_orders import clean_text
from services.db import rows
from services.profit_sharing import seller_profit_settings, split_profit
from services.security import decrypt_dict
from services.session import bootstrap, seller_selector
from services.supplier_document_storage import archive_supplier_documents
from services.supplier_documents import (
    analyze_supplier_documents,
    apply_supplier_document_updates,
    download_supplier_document_url,
    ensure_supplier_document_schema,
    supplier_document_import_history,
    supplier_document_report_bytes,
)

try:
    from st_aggrid import AgGrid, DataReturnMode, GridOptionsBuilder, GridUpdateMode, JsCode
except Exception:  # pragma: no cover
    AgGrid = None
    DataReturnMode = GridOptionsBuilder = GridUpdateMode = JsCode = None


def _accounting_sync_time_label(value: object) -> str:
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


ACCOUNTING_GRID_EDITABLE_COLUMNS = {
    "Fornitore": "supplier",
    "Prodotto": "product_title",
    "EAN": "ean",
    "Q.tà": "quantity",
    "Vendita €": "sale_eur",
    "Acquisto €": "purchase_cost_eur",
    "Commissione €": "commission_eur",
    "Rimborso €": "refund_eur",
    "Da ricevere €": "payout_eur",
    "Costo Extra €": "extra_cost_eur",
    "N Ordine Fornitore": "supplier_order_number",
    "Pagamento stimato": "payment_estimated",
    "Cliente": "customer_name",
    "Tracking": "tracking",
    "SCONTRINO": "receipt",
    "Note": "note",
}

ACCOUNTING_GRID_NUMERIC_COLUMNS = {
    "Q.tà", "Vendita €", "Acquisto €", "Commissione €", "Rimborso €",
    "Da ricevere €", "Costo Extra €",
}


def _grid_value_equal(left: Any, right: Any, *, numeric: bool = False) -> bool:
    if numeric:
        left_number = pd.to_numeric(pd.Series([left]), errors="coerce").iloc[0]
        right_number = pd.to_numeric(pd.Series([right]), errors="coerce").iloc[0]
        if pd.isna(left_number) and pd.isna(right_number):
            return True
        if pd.isna(left_number) or pd.isna(right_number):
            return False
        return abs(float(left_number) - float(right_number)) < 0.000001
    return clean_text(left) == clean_text(right)


def _accounting_grid_changes(
    original: pd.DataFrame,
    returned: pd.DataFrame,
    *,
    account_id: int,
    marketplace: str,
) -> list[dict[str, Any]]:
    if original.empty or returned.empty or "row_key" not in returned.columns:
        return []
    original_by_key = original.set_index("row_key", drop=False)
    changes: list[dict[str, Any]] = []
    for _, edited_row in returned.iterrows():
        row_key = clean_text(edited_row.get("row_key"))
        if not row_key or row_key not in original_by_key.index:
            continue
        base_row = original_by_key.loc[row_key]
        fields: dict[str, Any] = {}
        for display_column, db_field in ACCOUNTING_GRID_EDITABLE_COLUMNS.items():
            if display_column not in returned.columns or display_column not in original.columns:
                continue
            edited_value = edited_row.get(display_column)
            base_value = base_row.get(display_column)
            if not _grid_value_equal(
                base_value, edited_value,
                numeric=display_column in ACCOUNTING_GRID_NUMERIC_COLUMNS,
            ):
                fields[db_field] = edited_value
        if fields:
            changes.append({
                "row_key": row_key,
                "marketplace_account_id": int(account_id),
                "marketplace": marketplace,
                "fields": fields,
            })
    return changes


def _prepare_accounting_frame(
    records: list[dict[str, Any]],
    *,
    date_from: date,
    date_to: date,
    our_profit_pct: float,
    partner_profit_pct: float,
) -> pd.DataFrame:
    """Build the visible accounting model from stored/API values.

    Formula columns are always derived here so a manual economic override is never
    required to persist a second, potentially stale, copy of margin/share values.
    """
    orders = pd.DataFrame(records)
    orders["order_created_dt"] = pd.to_datetime(orders["order_created"], errors="coerce", utc=True)
    period_mask = orders["order_created_dt"].isna() | orders["order_created_dt"].dt.date.between(date_from, date_to)
    orders = orders.loc[period_mask].copy()
    for column in (
        "supplier", "raw_status", "status_label", "country_code", "order_id",
        "product_title", "ean", "customer_name", "tracking", "note",
        "supplier_order_number", "receipt", "cost_source", "financial_source",
    ):
        if column not in orders:
            orders[column] = ""
        orders[column] = orders[column].fillna("").astype(str)
    for column in (
        "sale_eur", "purchase_cost_eur", "commission_eur", "payout_eur",
        "refund_eur", "extra_cost_eur",
    ):
        orders[column] = pd.to_numeric(orders.get(column), errors="coerce")

    if orders.empty:
        for column in (
            "gross_margin_eur", "net_revenue_eur", "revenue_pct",
            "our_share_eur", "partner_share_eur",
        ):
            orders[column] = pd.Series(dtype=float)
        return orders

    computed = orders.apply(
        lambda row: computed_profit_values(
            row.to_dict(), our_profit_pct, partner_profit_pct
        ),
        axis=1,
        result_type="expand",
    )
    for column in (
        "gross_margin_eur", "net_revenue_eur", "revenue_pct",
        "our_share_eur", "partner_share_eur",
    ):
        orders[column] = computed[column]
    return orders


def _filter_accounting_frame(
    orders: pd.DataFrame,
    *,
    selected_suppliers: list[str],
    selected_statuses: list[str],
    selected_countries: list[str],
    search_text: str,
) -> pd.DataFrame:
    visible = orders.copy()
    if selected_suppliers:
        visible = visible[visible["supplier"].isin(selected_suppliers)]
    if selected_statuses:
        visible = visible[visible["raw_status"].isin(selected_statuses)]
    if selected_countries:
        visible = visible[visible["country_code"].isin(selected_countries)]
    if search_text:
        haystack = (
            visible["order_id"] + " " + visible["product_title"] + " "
            + visible["ean"] + " " + visible["customer_name"] + " "
            + visible["tracking"] + " " + visible["supplier"]
        ).str.lower()
        visible = visible[haystack.str.contains(search_text, regex=False, na=False)]
    return visible.sort_values(
        ["order_created", "order_id"], ascending=[False, True]
    ).reset_index(drop=True)


def _render_accounting_summary(
    container: Any,
    visible: pd.DataFrame,
    *,
    our_profit_pct: float,
    partner_profit_pct: float,
    partner_name: str,
    configured: bool,
) -> None:
    summary = totals(visible.to_dict("records"))
    summary_split = split_profit(
        summary["net_revenue"], our_profit_pct, partner_profit_pct
    )
    with container:
        metrics1 = st.columns(5)
        metrics1[0].metric("Righe visibili", len(visible))
        metrics1[1].metric("Vendite nette", f"{summary['sale']:,.2f} €")
        metrics1[2].metric("Commissioni", f"{summary['commission']:,.2f} €")
        metrics1[3].metric("Da ricevere", f"{summary['payout']:,.2f} €")
        metrics1[4].metric("Margine utile", f"{summary['net_revenue']:,.2f} €")
        metrics2 = st.columns(4)
        metrics2[0].metric("Acquisti", f"{summary['purchase']:,.2f} €")
        metrics2[1].metric("Rimborsi", f"{summary['refund']:,.2f} €")
        metrics2[2].metric("Margine lordo", f"{summary['gross_margin']:,.2f} €")
        metrics2[3].metric("Costi da verificare", int(visible["purchase_cost_eur"].isna().sum()))
        share_metrics = st.columns(2)
        share_metrics[0].metric(
            f"Nostra quota · {our_profit_pct:g}%",
            f"{summary_split['our_amount']:,.2f} €",
        )
        share_metrics[1].metric(
            f"Quota {partner_name} · {partner_profit_pct:g}%",
            f"{summary_split['partner_amount']:,.2f} €",
        )
        if not configured:
            st.warning(
                "La nostra percentuale è ancora 0%. Imposta la ripartizione nella pagina Gestione Seller."
            )


def _accounting_review_groups(route: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Group a Product Stats review queue by Seller/marketplace account."""
    if not isinstance(route, Mapping):
        return []
    grouped: dict[tuple[int, str, int], dict[str, Any]] = {}
    for source in route.get("items") or []:
        if not isinstance(source, Mapping):
            continue
        seller_id = int(source.get("seller_id") or 0)
        marketplace = clean_text(source.get("marketplace")).lower()
        account_id = int(source.get("marketplace_account_id") or 0)
        if not seller_id or not marketplace or not account_id:
            continue
        key = (seller_id, marketplace, account_id)
        target = grouped.setdefault(
            key,
            {
                "seller_id": seller_id,
                "seller_name": clean_text(source.get("seller_name")),
                "marketplace": marketplace,
                "marketplace_account_id": account_id,
                "items": [],
                "order_identities": set(),
                "order_ids": set(),
                "row_keys": set(),
            },
        )
        item = dict(source)
        target["items"].append(item)
        identity = clean_text(item.get("order_identity"))
        if identity:
            target["order_identities"].add(identity)
        order_id = clean_text(item.get("order_id"))
        if order_id:
            target["order_ids"].add(order_id)
        row_key = clean_text(item.get("row_key"))
        if row_key:
            target["row_keys"].add(row_key)
    groups = list(grouped.values())
    groups.sort(
        key=lambda item: (
            clean_text(item.get("seller_name")).casefold(),
            clean_text(item.get("marketplace")),
            int(item.get("marketplace_account_id") or 0),
        )
    )
    return groups


def _accounting_review_target(route: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int]:
    groups = _accounting_review_groups(route)
    if not groups:
        return groups, None, 0
    try:
        index = int((route or {}).get("group_index") or 0)
    except Exception:
        index = 0
    index = max(0, min(index, len(groups) - 1))
    return groups, groups[index], index


def _review_date(value: object, fallback: date) -> date:
    try:
        return date.fromisoformat(clean_text(value)[:10])
    except Exception:
        return fallback




bootstrap()
ensure_schema()
ensure_supplier_document_schema()
st.title("Contabilità")
st.caption(
    "Scarica tutti gli ordini via API, abbina i costi ai listini dei fornitori, "
    "calcola commissioni, importi da ricevere, margini e data stimata di pagamento."
)

review_route = st.session_state.get("accounting_review_route")
review_groups, review_target, review_group_index = _accounting_review_target(review_route)

# v268: una verifica aperta da "Prodotti più venduti" deve posizionare i menu
# Seller/Marketplace/Account soltanto quando si entra nel gruppo di verifica.
# Dopo il primo posizionamento i selectbox restano liberi: se l'utente cambia
# Seller, il rerun Streamlit non deve riportarlo automaticamente al Seller della
# verifica. Il gruppo viene riallineato di nuovo solo usando Account precedente /
# Account successivo, cioè quando cambia esplicitamente group_index.
try:
    review_selection_group = int((review_route or {}).get("selection_group_index", -1))
except Exception:
    review_selection_group = -1
review_selection_pending = bool(
    review_target and review_selection_group != review_group_index
)
if review_selection_pending:
    target_seller_id = int(review_target.get("seller_id") or 0)
    target_seller_rows = rows("SELECT id,name FROM sellers WHERE id=?", (target_seller_id,))
    if target_seller_rows:
        target_seller = target_seller_rows[0]
        st.session_state["active_seller_id"] = target_seller_id
        st.session_state["global_seller_selector"] = (
            f"{target_seller.get('name') or review_target.get('seller_name') or 'Seller'}  ·  ID {target_seller_id}"
        )

seller_id = seller_selector()
if seller_id is None:
    st.stop()

seller_rows = rows("SELECT * FROM sellers WHERE id=?", (seller_id,))
seller_record = seller_rows[0] if seller_rows else {"id": seller_id, "name": "Partner"}
profit_settings = seller_profit_settings(seller_record)
our_profit_pct = float(profit_settings["our_pct"])
partner_profit_pct = float(profit_settings["partner_pct"])
partner_name = str(profit_settings["partner_name"])

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
available_marketplaces = sorted({clean_text(item.get("marketplace")).lower() for item in accounts})
marketplace_key = f"accounting_marketplace_{seller_id}"
if (
    review_selection_pending
    and review_target
    and int(review_target.get("seller_id") or 0) == seller_id
):
    target_marketplace = clean_text(review_target.get("marketplace")).lower()
    if target_marketplace in available_marketplaces:
        st.session_state[marketplace_key] = target_marketplace
marketplace = st.selectbox(
    "1. Marketplace",
    available_marketplaces,
    format_func=lambda value: marketplace_labels.get(value, value.title()),
    key=marketplace_key,
)
marketplace_accounts = [
    item for item in accounts if clean_text(item.get("marketplace")).lower() == marketplace
]
account_map = {
    f"{item.get('account_name') or 'Account'} · ID {item['id']}": item
    for item in marketplace_accounts
}
account_key = f"accounting_account_{seller_id}_{marketplace}"
if (
    review_selection_pending
    and review_target
    and int(review_target.get("seller_id") or 0) == seller_id
    and clean_text(review_target.get("marketplace")).lower() == marketplace
):
    target_account_id = int(review_target.get("marketplace_account_id") or 0)
    target_account_label = next(
        (label for label, item in account_map.items() if int(item.get("id") or 0) == target_account_id),
        None,
    )
    if target_account_label:
        st.session_state[account_key] = target_account_label
account_label = st.selectbox(
    "2. Account marketplace",
    list(account_map),
    key=account_key,
)
account = account_map[account_label]
account_id = int(account["id"])

# Segna il gruppo come posizionato solo dopo che tutti e tre i menu puntano
# davvero alla destinazione della verifica. Da questo momento eventuali cambi
# manuali dell'utente non vengono più sovrascritti al rerun.
if (
    review_selection_pending
    and review_target
    and int(review_target.get("seller_id") or 0) == seller_id
    and clean_text(review_target.get("marketplace")).lower() == marketplace
    and int(review_target.get("marketplace_account_id") or 0) == account_id
):
    updated_review_route = dict(review_route or {})
    updated_review_route["selection_group_index"] = review_group_index
    st.session_state["accounting_review_route"] = updated_review_route
    review_route = updated_review_route
    review_selection_pending = False

credentials = decrypt_dict(account["credentials_encrypted"])
scope = f"{seller_id}_{marketplace}_{account_id}"
selection_key = f"accounting_selected_{scope}"
last_export_key = f"accounting_last_export_{scope}"
st.session_state.setdefault(selection_key, [])

review_active = bool(
    review_target
    and int(review_target.get("seller_id") or 0) == seller_id
    and clean_text(review_target.get("marketplace")).lower() == marketplace
    and int(review_target.get("marketplace_account_id") or 0) == account_id
)
if review_active:
    route_from = _review_date((review_route or {}).get("period_from"), date.today() - timedelta(days=30))
    route_to = _review_date((review_route or {}).get("period_to"), date.today())
    route_from, route_to = min(route_from, route_to), max(route_from, route_to)
    st.session_state[f"accounting_from_{scope}"] = route_from
    st.session_state[f"accounting_to_{scope}"] = route_to

    all_review_order_ids = {
        clean_text(item.get("order_identity"))
        for item in (review_route or {}).get("items", [])
        if isinstance(item, Mapping) and clean_text(item.get("order_identity"))
    }
    current_order_count = len(review_target.get("order_identities") or set())
    st.warning(
        f"Verifica aperta da Prodotti più venduti: {len(all_review_order_ids)} ordini da controllare in totale. "
        f"In questo account ce ne sono {current_order_count}. Completa i campi economici mancanti qui sotto: "
        "il margine viene ricalcolato automaticamente e la modifica resta salvata in Contabilità."
    )

    review_records = accounting_core.rows(accounting_core_scope)
    review_frame = _prepare_accounting_frame(
        review_records,
        date_from=route_from,
        date_to=route_to,
        our_profit_pct=our_profit_pct,
        partner_profit_pct=partner_profit_pct,
    )
    review_order_ids = {clean_text(value) for value in review_target.get("order_ids") or set() if clean_text(value)}
    review_row_keys = {clean_text(value) for value in review_target.get("row_keys") or set() if clean_text(value)}
    if not review_frame.empty:
        mask = review_frame["row_key"].astype(str).isin(review_row_keys)
        if review_order_ids:
            mask = mask | review_frame["order_id"].astype(str).isin(review_order_ids)
        review_visible = review_frame.loc[mask].copy()
    else:
        review_visible = review_frame.copy()

    if review_visible.empty:
        st.success("Gli ordini di questo account non risultano più da correggere oppure non sono più presenti nell’intervallo selezionato.")
    else:
        reason_by_row = {
            clean_text(item.get("row_key")): clean_text(item.get("missing_reason")) or "Dati economici da completare"
            for item in review_target.get("items") or []
            if isinstance(item, Mapping)
        }
        review_status = []
        for _, review_row in review_visible.iterrows():
            row_key = clean_text(review_row.get("row_key"))
            unresolved = pd.isna(review_row.get("net_revenue_eur")) and float(review_row.get("sale_eur") or 0.0) > 0.005
            if unresolved:
                review_status.append(reason_by_row.get(row_key) or "Dati economici da completare")
            elif row_key in reason_by_row:
                review_status.append("Risolto")
            else:
                review_status.append("")
        review_table = pd.DataFrame({
            "row_key": review_visible["row_key"].astype(str),
            "Data": review_visible["order_created"].fillna("").astype(str).str.slice(0, 10),
            "Ordine": review_visible["order_id"],
            "Prodotto": review_visible["product_title"],
            "EAN": review_visible["ean"],
            "Fornitore": review_visible["supplier"],
            "Vendita €": review_visible["sale_eur"],
            "Acquisto €": review_visible["purchase_cost_eur"],
            "Commissione €": review_visible["commission_eur"],
            "Da ricevere €": review_visible["payout_eur"],
            "Costo Extra €": review_visible["extra_cost_eur"],
            "Margine utile €": review_visible["net_revenue_eur"],
            "Da verificare": review_status,
        })
        review_editor = st.data_editor(
            review_table,
            hide_index=True,
            use_container_width=True,
            height=min(460, 88 + max(1, len(review_table)) * 35),
            disabled=["row_key", "Data", "Ordine", "Margine utile €", "Da verificare"],
            column_config={
                "row_key": None,
                "Vendita €": st.column_config.NumberColumn(format="%.2f €"),
                "Acquisto €": st.column_config.NumberColumn(format="%.2f €"),
                "Commissione €": st.column_config.NumberColumn(format="%.2f €"),
                "Da ricevere €": st.column_config.NumberColumn(format="%.2f €"),
                "Costo Extra €": st.column_config.NumberColumn(format="%.2f €"),
                "Margine utile €": st.column_config.NumberColumn(format="%.2f €"),
            },
            key=f"accounting_review_editor_{scope}_{review_group_index}",
        )
        review_changes = _accounting_grid_changes(
            review_table,
            review_editor,
            account_id=account_id,
            marketplace=marketplace,
        )
        if review_changes:
            review_saved = save_accounting_inline_edits(review_changes)
            if review_saved["updated_rows"]:
                st.toast(
                    f"Correzione salvata: {review_saved['updated_rows']} righe aggiornate",
                    icon="💾",
                )
                st.rerun()

    nav_cols = st.columns([1, 1, 2, 1])
    if nav_cols[0].button(
        "← Account precedente",
        use_container_width=True,
        disabled=review_group_index <= 0,
        key=f"accounting_review_prev_{scope}_{review_group_index}",
    ):
        updated_route = dict(review_route or {})
        updated_route["group_index"] = max(0, review_group_index - 1)
        st.session_state["accounting_review_route"] = updated_route
        st.rerun()
    nav_cols[1].metric("Account verifica", f"{review_group_index + 1} / {len(review_groups)}")
    if nav_cols[2].button(
        "Account successivo →",
        use_container_width=True,
        disabled=review_group_index >= len(review_groups) - 1,
        key=f"accounting_review_next_{scope}_{review_group_index}",
    ):
        updated_route = dict(review_route or {})
        updated_route["group_index"] = min(len(review_groups) - 1, review_group_index + 1)
        st.session_state["accounting_review_route"] = updated_route
        st.rerun()
    if nav_cols[3].button(
        "Chiudi verifica",
        use_container_width=True,
        key=f"accounting_review_close_{scope}_{review_group_index}",
    ):
        st.session_state.pop("accounting_review_route", None)
        st.rerun()

st.divider()
st.markdown("### Scarica e aggiorna gli ordini")
accounting_core = AccountingCore()
jobs_core = JobsCore()
accounting_core_scope = AccountingScope(seller_id, account_id, marketplace)
accounting_core_status = accounting_core.status(accounting_core_scope, credentials)
environment = accounting_core_status.environment
sync_state = accounting_core_status.sync_state
cache_summary = accounting_core_status.cache_summary
last_sync_label = _accounting_sync_time_label(sync_state.get("last_completed_at"))
last_attempt_label = _accounting_sync_time_label(
    sync_state.get("last_attempted_at") or sync_state.get("last_started_at")
)
last_order_label = _accounting_sync_time_label(cache_summary.get("last_order_created"))
st.info(
    f"**Ultimo tentativo:** {last_attempt_label}  ·  "
    f"**Ultimo aggiornamento riuscito:** {last_sync_label}  ·  "
    f"**Ultimo ordine scaricato:** {last_order_label}"
    + (
        f" ({cache_summary['last_order_id']})" if cache_summary.get("last_order_id") else ""
    )
    + f"  ·  **In memoria:** {cache_summary['total_orders']:,} ordini / "
      f"{cache_summary['total_rows']:,} righe."
)
if sync_state.get("last_status") == "error" and sync_state.get("last_error"):
    st.warning(
        "L'ultimo tentativo non è riuscito, ma gli ordini già memorizzati sono stati "
        f"conservati. Dettaglio: {sync_state['last_error']}"
    )

st.markdown("#### Listini da usare per il costo di acquisto")
catalog_selection = accounting_core.catalog_selection(accounting_core_scope)
catalog_options = catalog_selection["options"]
catalog_by_id = {
    int(item["price_list_id"]): item for item in catalog_options
}
catalog_ids = list(catalog_by_id)
catalog_choice_key = f"accounting_catalog_choice_{seller_id}"
current_enabled_ids = [
    int(value) for value in catalog_selection["enabled_ids"] if int(value) in catalog_by_id
]
if catalog_choice_key not in st.session_state:
    st.session_state[catalog_choice_key] = current_enabled_ids
else:
    st.session_state[catalog_choice_key] = [
        int(value) for value in st.session_state[catalog_choice_key]
        if int(value) in catalog_by_id
    ]

if catalog_options:
    catalog_all_col, catalog_none_col, catalog_count_col = st.columns([1, 1, 2])
    if catalog_all_col.button(
        "Seleziona tutti i listini",
        use_container_width=True,
        key=f"accounting_catalog_all_{seller_id}",
    ):
        st.session_state[catalog_choice_key] = catalog_ids
        accounting_core.save_catalog_selection(accounting_core_scope, catalog_ids)
        st.rerun()
    if catalog_none_col.button(
        "Deseleziona tutti i listini",
        use_container_width=True,
        key=f"accounting_catalog_none_{seller_id}",
    ):
        st.session_state[catalog_choice_key] = []
        accounting_core.save_catalog_selection(accounting_core_scope, [])
        st.rerun()

    def _catalog_label(price_list_id: int) -> str:
        item = catalog_by_id[int(price_list_id)]
        supplier_name = clean_text(item.get("supplier_name")) or "Fornitore"
        list_name = clean_text(item.get("list_name")) or f"Listino {price_list_id}"
        label = f"{supplier_name} · {list_name}"
        if "innpro" in supplier_name.lower():
            source_url = clean_text(item.get("source_url")).lower()
            if "type=light" in source_url:
                label += " · INGROSSO"
            elif "type=full" in source_url:
                label += " · FULL (non usato per Acquisto €)"
        return label

    selected_catalog_ids = st.multiselect(
        "Seleziona i listini che possono determinare la colonna Acquisto €",
        options=catalog_ids,
        format_func=_catalog_label,
        key=catalog_choice_key,
        help=(
            "La scelta è persistente per il Seller. Un listino deselezionato non viene "
            "più usato per calcolare o ricalcolare i costi in Contabilità."
        ),
    )
    selected_catalog_ids = [int(value) for value in selected_catalog_ids]
    if set(selected_catalog_ids) != set(current_enabled_ids) or not catalog_selection["configured"]:
        # Non trasformiamo la situazione di migrazione 'tutti attivi' in una whitelist
        # finché l'utente non modifica davvero la selezione.
        if set(selected_catalog_ids) != set(current_enabled_ids):
            accounting_core.save_catalog_selection(accounting_core_scope, selected_catalog_ids)
            catalog_selection = accounting_core.catalog_selection(accounting_core_scope)
    catalog_count_col.metric(
        "Listini usati in Contabilità",
        f"{len(selected_catalog_ids)} / {len(catalog_ids)}",
    )
    if not selected_catalog_ids:
        st.warning(
            "Nessun listino selezionato: i costi automatici non verranno ricavati dai listini. "
            "Restano validi soltanto gli eventuali costi inseriti manualmente."
        )
    else:
        st.caption(
            "Sono autorevoli solo i listini selezionati qui. Se per lo stesso EAN sono "
            "selezionati più listini compatibili, il motore usa la normale priorità del "
            "fornitore e della versione più recente. Per Innpro il nome non deve contenere "
            "la parola Light: il programma riconosce il feed ingrosso dal parametro "
            "**type=light** dell'URL. I feed **type=full** restano esclusi dal costo automatico."
        )
else:
    selected_catalog_ids = []
    st.warning(
        "Non risultano listini attivi/accessibili per questo Seller. Aggiungi o scarica un "
        "listino in Fornitori e Listini prima di ricalcolare i costi."
    )

period_col1, period_col2, sync_col, full_col, costs_col = st.columns(
    [1, 1, 1.35, 1.35, 1.35]
)
date_from = period_col1.date_input(
    "Ordini dal",
    value=date.today() - timedelta(days=30),
    max_value=date.today(),
    key=f"accounting_from_{scope}",
)
date_to = period_col2.date_input(
    "Ordini al",
    value=date.today(),
    min_value=date_from,
    max_value=date.today(),
    key=f"accounting_to_{scope}",
)

incremental_clicked = sync_col.button(
    "Aggiorna ordini mancanti e modificati",
    type="primary",
    use_container_width=True,
    key=f"accounting_sync_{scope}",
)
full_clicked = full_col.button(
    "Risincronizzazione completa",
    use_container_width=True,
    key=f"accounting_full_sync_{scope}",
)
full_request_key = f"accounting_full_sync_requested_{scope}"
if full_clicked:
    st.session_state[full_request_key] = True
full_confirmed = False
if st.session_state.get(full_request_key):
    st.warning(
        "La risincronizzazione completa rilegge dall'API tutto l'intervallo selezionato. "
        "Gli ordini già memorizzati e le modifiche manuali non vengono cancellati."
    )
    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button(
        "Conferma risincronizzazione completa",
        type="primary",
        use_container_width=True,
        key=f"accounting_confirm_full_sync_{scope}",
    ):
        full_confirmed = True
        st.session_state[full_request_key] = False
    if cancel_col.button(
        "Annulla",
        use_container_width=True,
        key=f"accounting_cancel_full_sync_{scope}",
    ):
        st.session_state[full_request_key] = False
        st.rerun()
sync_job_key = f"accounting_sync_job_{scope}"
if incremental_clicked or full_confirmed:
    existing_job = jobs_core.snapshot(st.session_state.get(sync_job_key, "")) if st.session_state.get(sync_job_key) else None
    if existing_job and not existing_job.terminal:
        st.warning("È già in corso una sincronizzazione contabile per questo account.")
    else:
        request = accounting_core.build_sync_job(
            accounting_core_scope,
            AccountingPeriod(date_from, date_to),
            full=bool(full_confirmed),
        )
        receipt = jobs_core.submit(request)
        jobs_core.start_local(receipt.job_id)
        st.session_state[sync_job_key] = receipt.job_id
        st.success(
            "Sincronizzazione contabile avviata in background. "
            "Puoi cambiare pagina e continuare a usare Marketplace Hub."
        )

sync_job_id = st.session_state.get(sync_job_key)
if sync_job_id:
    sync_job = jobs_core.snapshot(sync_job_id)
    if sync_job:
        st.progress(
            min(1.0, max(0.0, sync_job.progress_pct / 100.0)),
            text=sync_job.message or sync_job.status,
        )
        sj1, sj2 = st.columns([1, 4])
        if sj1.button("Aggiorna stato", key=f"accounting_sync_job_refresh_{sync_job_id}"):
            st.rerun()
        if sync_job.status == "done":
            result = dict(sync_job.result)
            st.success(
                f"Sincronizzazione completata · nuovi ordini {int(result.get('new_orders') or 0):,} · "
                f"modificati {int(result.get('updated_orders') or 0):,} · "
                f"invariati {int(result.get('unchanged_orders') or 0):,} · "
                f"totale in memoria {int(result.get('total_orders') or 0):,}."
            )
        elif sync_job.status == "error":
            st.error(f"Sincronizzazione contabile non riuscita: {sync_job.error}")
        else:
            sj2.caption(
                f"Job {sync_job.job_id[:8]} · {sync_job.status} · "
                "continua anche se navighi in un'altra sezione."
            )

costs_clicked = costs_col.button(
    "Ricalcola costi con i listini selezionati",
    use_container_width=True,
    key=f"accounting_refresh_costs_{scope}",
)
cost_job_key = f"accounting_cost_job_{scope}"
if costs_clicked:
    existing_cost_job = jobs_core.snapshot(st.session_state.get(cost_job_key, "")) if st.session_state.get(cost_job_key) else None
    if existing_cost_job and not existing_cost_job.terminal:
        st.warning("È già in corso un ricalcolo costi per questo account.")
    else:
        request = accounting_core.build_refresh_costs_job(
            accounting_core_scope, AccountingPeriod(date_from, date_to)
        )
        receipt = jobs_core.submit(request)
        jobs_core.start_local(receipt.job_id)
        st.session_state[cost_job_key] = receipt.job_id
        st.success(
            "Ricalcolo costi avviato in background. Puoi continuare a lavorare mentre "
            "il motore esegue i match EAN / SKU / SKU composito."
        )

cost_job_id = st.session_state.get(cost_job_key)
if cost_job_id:
    cost_job = jobs_core.snapshot(cost_job_id)
    if cost_job:
        st.progress(
            min(1.0, max(0.0, cost_job.progress_pct / 100.0)),
            text=cost_job.message or cost_job.status,
        )
        cj1, cj2 = st.columns([1, 4])
        if cj1.button("Aggiorna stato costi", key=f"accounting_cost_job_refresh_{cost_job_id}"):
            st.rerun()
        if cost_job.status == "done":
            result = dict(cost_job.result)
            st.success(
                f"Ricalcolo completato · trovati {int(result.get('matched') or 0):,} · "
                f"conservati {int(result.get('preserved') or 0):,} · "
                f"da verificare {int(result.get('missing') or 0):,} · "
                f"listini {int(result.get('catalogs') or 0):,}."
            )
        elif cost_job.status == "error":
            st.error(f"Ricalcolo costi non riuscito: {cost_job.error}")
        else:
            cj2.caption(
                f"Job {cost_job.job_id[:8]} · {cost_job.status} · "
                "il ricalcolo continua in background."
            )

st.caption(
    "Il costo viene cercato tramite EAN/GTIN esatto esclusivamente nei listini che hai "
    "selezionato sopra. La selezione resta memorizzata per il Seller e vale anche per "
    "i successivi aggiornamenti API. Listini globali, condivisi e relative viste salvate "
    "sono utilizzati solo se il loro listino padre è selezionato. Per Innpro il feed "
    "ingrosso viene riconosciuto dall'URL (type=light), anche se il nome è per esempio "
    "INNPRO 2408; in cloud viene riscaricato automaticamente se il vecchio file locale "
    "non esiste più. I feed type=full non vengono usati come costo automatico. Sono "
    "riconosciuti anche i GTIN numerici presenti in code_producer o nella colonna SKU, "
    "senza usare SKU alfanumerici."
)

records = accounting_core.rows(accounting_core_scope)

st.divider()
st.markdown("### Importa documenti degli ordini fornitore")
st.caption(
    "Carica uno o più documenti del fornitore, oppure inserisci uno o più URL. "
    "Sono accettati Excel, PDF, JPG/PNG/WEBP e HTML. Il programma cerca il numero "
    "ordine marketplace nel documento, lo abbina alla Contabilità e propone il valore "
    "da inserire in **N Ordine Fornitore**. Nessun dato viene applicato senza conferma."
)

supplier_docs_upload = st.file_uploader(
    "Documenti fornitore dal computer",
    type=["xlsx", "xls", "pdf", "jpg", "jpeg", "png", "webp", "html", "htm"],
    accept_multiple_files=True,
    key=f"accounting_supplier_documents_upload_{scope}",
)

supplier_url_cache_key = f"accounting_supplier_documents_url_cache_{scope}"
st.session_state.setdefault(supplier_url_cache_key, [])
supplier_urls = st.text_area(
    "URL dei documenti fornitore · uno per riga",
    placeholder=(
        "https://.../ordine.xlsx\n"
        "https://.../documento.pdf\n"
        "https://docs.google.com/spreadsheets/d/..."
    ),
    height=110,
    key=f"accounting_supplier_documents_urls_{scope}",
)
url_add_col, url_clear_col = st.columns([3, 1])
if url_add_col.button(
    "Aggiungi gli URL alla coda",
    type="primary",
    use_container_width=True,
    disabled=not clean_text(supplier_urls),
    key=f"accounting_supplier_documents_url_add_{scope}",
):
    requested_urls = []
    for raw_url in supplier_urls.splitlines():
        value = clean_text(raw_url)
        if value and value not in requested_urls:
            requested_urls.append(value)
    if len(requested_urls) > 25:
        st.error("Puoi caricare al massimo 25 URL per volta.")
    else:
        cached_documents = list(st.session_state.get(supplier_url_cache_key, []))
        existing_urls = {clean_text(item.get("input_url")) for item in cached_documents}
        loaded_count = 0
        errors = []
        progress = st.progress(0.0, text="Download documenti…")
        for index, requested_url in enumerate(requested_urls, start=1):
            try:
                if requested_url in existing_urls:
                    continue
                downloaded = download_supplier_document_url(requested_url)
                cached_documents.append(downloaded)
                existing_urls.add(requested_url)
                loaded_count += 1
            except Exception as exc:
                errors.append(f"{requested_url}: {exc}")
            progress.progress(index / max(1, len(requested_urls)), text=f"Documento {index}/{len(requested_urls)}")
        progress.empty()
        st.session_state[supplier_url_cache_key] = cached_documents
        if loaded_count:
            st.success(f"Aggiunti {loaded_count:,} documenti da URL.")
        for error in errors:
            st.error(error)

if url_clear_col.button(
    "Svuota URL",
    use_container_width=True,
    disabled=not st.session_state.get(supplier_url_cache_key),
    key=f"accounting_supplier_documents_url_clear_{scope}",
):
    st.session_state[supplier_url_cache_key] = []
    st.session_state.pop(f"accounting_supplier_documents_analysis_{scope}", None)
    st.rerun()

supplier_documents = []
for uploaded in supplier_docs_upload or []:
    content = uploaded.getvalue()
    supplier_documents.append({
        "content": content,
        "file_name": uploaded.name,
        "source": "File caricato",
        "size_bytes": len(content),
    })
for cached_document in st.session_state.get(supplier_url_cache_key, []):
    supplier_documents.append(dict(cached_document))

# Avoid processing the same file twice when it was both uploaded and supplied by URL.
deduplicated_documents = []
seen_document_hashes = set()
for document in supplier_documents:
    content = document.get("content") or b""
    digest = hashlib.sha256(content).hexdigest() if content else clean_text(document.get("file_name"))
    if digest in seen_document_hashes:
        continue
    seen_document_hashes.add(digest)
    deduplicated_documents.append(document)
supplier_documents = deduplicated_documents

supplier_options = ["Automatico"] + sorted({
    clean_text(item.get("supplier")) for item in records if clean_text(item.get("supplier"))
})
supplier_hint = st.selectbox(
    "Fornitore del gruppo di documenti",
    supplier_options,
    help="Lascia Automatico: Cecotec e altri formati riconoscibili vengono identificati dal documento.",
    key=f"accounting_supplier_documents_hint_{scope}",
)

if supplier_documents:
    st.dataframe(
        pd.DataFrame([
            {
                "Documento": clean_text(item.get("file_name")) or "documento",
                "Origine": clean_text(item.get("source")) or "File",
                "Dimensione KB": round(len(item.get("content") or b"") / 1024, 1),
            }
            for item in supplier_documents
        ]),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("Carica almeno un documento oppure aggiungi uno o più URL.")

supplier_analysis_key = f"accounting_supplier_documents_analysis_{scope}"
analysis_token = hashlib.sha256(
    (
        supplier_hint + "|" + "|".join(
            f"{clean_text(item.get('file_name'))}:{len(item.get('content') or b'')}:{hashlib.sha256(item.get('content') or b'').hexdigest()}"
            for item in supplier_documents
        )
    ).encode("utf-8", errors="ignore")
).hexdigest() if supplier_documents else ""

if st.button(
    "Analizza e abbina tutti i documenti",
    type="primary",
    use_container_width=True,
    disabled=not supplier_documents or not records,
    key=f"accounting_supplier_documents_analyze_{scope}",
):
    try:
        with st.spinner("Archiviazione sicura, lettura documenti, OCR e abbinamento ordini…"):
            archived_supplier_documents=archive_supplier_documents(
                seller_id=seller_id,account_id=account_id,marketplace=marketplace,documents=supplier_documents,
            )
            supplier_analysis = analyze_supplier_documents(
                records,
                supplier_documents,
                marketplace=marketplace,
                supplier_hint=supplier_hint,
            )
            supplier_analysis["archived_document_ids"]=[int(item.get("id") or 0) for item in archived_supplier_documents]
        st.session_state[supplier_analysis_key] = {
            "token": analysis_token,
            "analysis": supplier_analysis,
        }
        st.success(
            f"Analisi completata: {supplier_analysis['summary']['references_found']:,} "
            "riferimenti ordine trovati."
        )
    except Exception as exc:
        st.error(f"Impossibile analizzare i documenti: {exc}")

analysis_state = st.session_state.get(supplier_analysis_key)
if analysis_state and analysis_state.get("token") == analysis_token:
    supplier_analysis = analysis_state["analysis"]
    supplier_summary = supplier_analysis["summary"]
    supplier_metrics = st.columns(6)
    supplier_metrics[0].metric("Documenti", supplier_summary["documents"])
    supplier_metrics[1].metric("Riferimenti trovati", supplier_summary["references_found"])
    supplier_metrics[2].metric("Pronti da inserire", supplier_summary["update_rows"])
    supplier_metrics[3].metric("Conflitti", supplier_summary["conflicts"])
    supplier_metrics[4].metric("Non abbinati", supplier_summary["unmatched_rows"] + supplier_summary["ambiguous_rows"])
    supplier_metrics[5].metric("Errori lettura", supplier_summary["parse_errors"])

    matched_tab, conflicts_tab, missing_tab, docs_tab = st.tabs([
        "Abbinamenti", "Conflitti", "Non abbinati / ambigui", "Documenti letti",
    ])
    with matched_tab:
        matched_frame = pd.DataFrame(supplier_analysis["proposals"]).drop(columns=["row_key"], errors="ignore")
        if matched_frame.empty:
            st.info("Nessun abbinamento trovato.")
        else:
            st.dataframe(matched_frame, use_container_width=True, hide_index=True, height=420)
    with conflicts_tab:
        if supplier_analysis["conflicts"]:
            st.warning(
                "Queste righe hanno già un numero ordine fornitore diverso oppure più documenti "
                "propongono valori differenti. Non vengono sovrascritte automaticamente."
            )
            st.dataframe(
                pd.DataFrame(supplier_analysis["conflicts"]).drop(columns=["row_key"], errors="ignore"),
                use_container_width=True, hide_index=True, height=360,
            )
        else:
            st.success("Nessun conflitto.")
    with missing_tab:
        missing_items = list(supplier_analysis["unmatched"]) + list(supplier_analysis["ambiguous"])
        if missing_items:
            st.dataframe(pd.DataFrame(missing_items), use_container_width=True, hide_index=True, height=360)
        else:
            st.success("Tutti i riferimenti trovati hanno un ordine contabile preciso.")
    with docs_tab:
        st.dataframe(
            pd.DataFrame(supplier_analysis["documents"]),
            use_container_width=True, hide_index=True, height=300,
        )
        if supplier_analysis["errors"]:
            st.error("Alcuni documenti non sono stati letti.")
            st.dataframe(pd.DataFrame(supplier_analysis["errors"]), use_container_width=True, hide_index=True)

    supplier_report = supplier_document_report_bytes(supplier_analysis)
    st.download_button(
        "Scarica report abbinamenti documenti fornitore",
        data=supplier_report,
        file_name=f"abbinamenti_ordini_fornitore_{marketplace}_{date_from:%Y%m%d}_{date_to:%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        key=f"accounting_supplier_documents_report_{scope}_{analysis_token[:12]}",
    )

    if supplier_summary["update_rows"]:
        supplier_confirm = st.checkbox(
            "Confermo: compila N Ordine Fornitore soltanto dove il campo è vuoto",
            value=False,
            key=f"accounting_supplier_documents_confirm_{scope}_{analysis_token[:12]}",
        )
        if st.button(
            "Applica gli abbinamenti confermati",
            type="primary",
            use_container_width=True,
            disabled=not supplier_confirm,
            key=f"accounting_supplier_documents_apply_{scope}_{analysis_token[:12]}",
        ):
            result = apply_supplier_document_updates(
                seller_id,
                account_id,
                marketplace,
                supplier_analysis["updates"],
                source_names=[clean_text(item.get("file_name")) for item in supplier_documents],
                analysis_summary=supplier_summary,
            )
            st.success(
                f"Numeri ordine fornitore salvati: {result['updated_rows']:,}. "
                f"Righe non modificate: {result['skipped_rows']:,}."
            )
            st.session_state.pop(supplier_analysis_key, None)
            st.rerun()

supplier_doc_history = supplier_document_import_history(seller_id, account_id, marketplace)
if supplier_doc_history:
    with st.expander("Storico importazioni documenti fornitore"):
        st.dataframe(
            pd.DataFrame([
                {
                    "Data": item["created_at"],
                    "Documenti": item["document_count"],
                    "File/URL": item["source_names"],
                    "Riferimenti": item["references_found"],
                    "Abbinati": item["matched_rows"],
                    "Aggiornati": item["updated_rows"],
                    "Conflitti": item["conflicts"],
                    "Non abbinati": item["unmatched_rows"],
                    "Ambigui": item["ambiguous_rows"],
                }
                for item in supplier_doc_history
            ]),
            use_container_width=True,
            hide_index=True,
        )

st.divider()
st.markdown("### Confronta e completa con un file Excel")
st.caption(
    "Carica il prospetto contabile già compilato. Il programma abbina le righe "
    "prima tramite marketplace + numero ordine + EAN; quando l’Excel contiene un "
    "vecchio SKU alfanumerico usa prodotto oppure ordine univoco. Vengono compilati "
    "soltanto i campi mancanti: i valori già presenti da API o listini non vengono "
    "sovrascritti e le differenze restano visibili per il controllo."
)
comparison_source_mode = st.radio(
    "Origine del file di confronto",
    ["Carica file", "Inserisci URL"],
    horizontal=True,
    key=f"accounting_comparison_source_mode_{scope}",
)
comparison_content: bytes | None = None
comparison_name = ""
comparison_history_name = ""
comparison_source_id = ""

if comparison_source_mode == "Carica file":
    comparison_upload = st.file_uploader(
        "File Excel di confronto",
        type=["xlsx", "xls"],
        accept_multiple_files=False,
        key=f"accounting_comparison_upload_{scope}",
    )
    if comparison_upload is not None:
        comparison_content = comparison_upload.getvalue()
        comparison_name = comparison_upload.name
        comparison_history_name = comparison_name
        comparison_source_id = f"upload:{comparison_name}:{len(comparison_content)}"
else:
    url_cache_key = f"accounting_comparison_url_cache_{scope}"
    comparison_url = st.text_input(
        "URL del file Excel o del Foglio Google",
        placeholder="https://docs.google.com/spreadsheets/d/... oppure https://.../file.xlsx",
        key=f"accounting_comparison_url_{scope}",
    )
    load_url_col, clear_url_col = st.columns([3, 1])
    if load_url_col.button(
        "Carica da URL",
        type="primary",
        use_container_width=True,
        disabled=not clean_text(comparison_url),
        key=f"accounting_comparison_url_load_{scope}",
    ):
        try:
            with st.spinner("Download e verifica del file Excel…"):
                downloaded = download_accounting_comparison_url(comparison_url)
            downloaded["requested_url"] = clean_text(comparison_url)
            st.session_state[url_cache_key] = downloaded
            st.success(
                f"File caricato: {downloaded['file_name']} · "
                f"{downloaded['size_bytes'] / 1024:.1f} KB"
            )
        except Exception as exc:
            st.session_state.pop(url_cache_key, None)
            st.error(f"Impossibile caricare il file dall’URL: {exc}")
    cached_url_file = st.session_state.get(url_cache_key)
    if clear_url_col.button(
        "Rimuovi",
        use_container_width=True,
        disabled=not cached_url_file,
        key=f"accounting_comparison_url_clear_{scope}",
    ):
        st.session_state.pop(url_cache_key, None)
        st.rerun()
    cached_url_file = st.session_state.get(url_cache_key)
    if cached_url_file:
        if clean_text(cached_url_file.get("requested_url")) == clean_text(comparison_url):
            comparison_content = cached_url_file["content"]
            comparison_name = clean_text(cached_url_file.get("file_name")) or "confronto.xlsx"
            comparison_history_name = f"URL · {comparison_name}"
            comparison_source_id = f"url:{cached_url_file.get('input_url')}:{len(comparison_content)}"
            st.info(
                f"Pronto per il confronto: **{comparison_name}** "
                f"({len(comparison_content) / 1024:.1f} KB)."
            )
        elif clean_text(comparison_url):
            st.caption("L’URL è cambiato: premi **Carica da URL** per scaricare il nuovo file.")
    st.caption(
        "Sono supportati link diretti `.xlsx`/`.xls`, Google Drive e Google Sheets. "
        "Il collegamento deve essere pubblico o condiviso con chiunque disponga del link; "
        "non inserire password o credenziali nell’URL."
    )

if comparison_content is not None:
    if not records:
        st.warning(
            "Prima scarica gli ordini dal marketplace: il file Excel completa le righe "
            "già presenti e non crea ordini senza un corrispondente API."
        )
    else:
        try:
            comparison_sheets = accounting_excel_sheet_names(
                comparison_content, comparison_name
            )
            source_token = hashlib.sha256(
                comparison_source_id.encode("utf-8", errors="ignore")
            ).hexdigest()[:16]
            comparison_sheet = st.selectbox(
                "Foglio da confrontare",
                comparison_sheets,
                key=f"accounting_comparison_sheet_{scope}_{source_token}",
            )
            parsed_comparison = read_accounting_comparison_excel(
                comparison_content,
                comparison_name,
                comparison_sheet,
            )
            comparison = compare_accounting_with_excel(
                records,
                parsed_comparison["frame"],
                marketplace,
            )
            comparison_summary = comparison["summary"]
            comparison_metrics = st.columns(5)
            comparison_metrics[0].metric("Righe Excel", comparison_summary["excel_rows"])
            comparison_metrics[1].metric("Righe abbinate", comparison_summary["matched_rows"])
            comparison_metrics[2].metric("Campi da integrare", comparison_summary["fillable_fields"])
            comparison_metrics[3].metric("Differenze", comparison_summary["conflict_fields"])
            comparison_metrics[4].metric("Non abbinate", comparison_summary["unmatched_rows"])

            fills_tab, conflicts_tab, unmatched_tab, all_rows_tab = st.tabs([
                "Dati da integrare",
                "Differenze da controllare",
                "Righe non abbinate",
                "Esito per riga",
            ])
            with fills_tab:
                if comparison["fills"]:
                    fills_frame = pd.DataFrame(comparison["fills"]).drop(columns=["row_key"], errors="ignore")
                    st.dataframe(fills_frame, use_container_width=True, hide_index=True, height=360)
                else:
                    st.info("Il file non contiene dati mancanti da integrare.")
            with conflicts_tab:
                if comparison["conflicts"]:
                    conflict_frame = pd.DataFrame(comparison["conflicts"]).drop(columns=["row_key"], errors="ignore")
                    st.dataframe(conflict_frame, use_container_width=True, hide_index=True, height=360)
                    st.caption(
                        "Questi valori non vengono sostituiti automaticamente. Il programma mantiene "
                        "il dato già presente da API/listino; margini e percentuali sono sempre ricalcolati."
                    )
                else:
                    st.success("Nessuna differenza tra i valori già presenti e il file Excel.")
            with unmatched_tab:
                if comparison["unmatched"]:
                    st.dataframe(
                        pd.DataFrame(comparison["unmatched"]),
                        use_container_width=True,
                        hide_index=True,
                        height=360,
                    )
                else:
                    st.success("Tutte le righe del marketplace selezionato sono state abbinate.")
            with all_rows_tab:
                st.dataframe(
                    pd.DataFrame(comparison["rows"]),
                    use_container_width=True,
                    hide_index=True,
                    height=360,
                )

            report_content = accounting_excel_comparison_report_bytes(comparison)
            report_name = (
                f"confronto_{marketplace}_{date_from:%Y%m%d}_{date_to:%Y%m%d}.xlsx"
            )
            st.download_button(
                "Scarica il report di confronto",
                data=report_content,
                file_name=report_name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key=f"accounting_comparison_report_{scope}_{source_token}",
            )

            if comparison_summary["fillable_fields"]:
                confirm_excel_fill = st.checkbox(
                    "Confermo: completa soltanto i campi mancanti con i valori del file Excel",
                    value=False,
                    key=f"accounting_comparison_confirm_{scope}_{source_token}_{comparison_sheet}",
                )
                if st.button(
                    "Applica integrazione dal file Excel",
                    type="primary",
                    use_container_width=True,
                    disabled=not confirm_excel_fill,
                    key=f"accounting_comparison_apply_{scope}_{source_token}_{comparison_sheet}",
                ):
                    result = apply_accounting_excel_updates(
                        seller_id,
                        account_id,
                        marketplace,
                        comparison["updates"],
                        file_name=comparison_history_name or comparison_name,
                        sheet_name=comparison_sheet,
                        comparison_summary=comparison_summary,
                    )
                    st.success(
                        f"Integrazione completata: {result['updated_rows']:,} righe aggiornate, "
                        f"{result['filled_fields']:,} campi compilati. I valori già presenti "
                        "sono rimasti invariati."
                    )
                    st.rerun()
        except Exception as exc:
            st.error(f"Impossibile confrontare il file Excel: {exc}")

excel_imports = accounting_excel_import_history(seller_id, account_id, marketplace)
if excel_imports:
    with st.expander("Storico integrazioni da file Excel"):
        st.dataframe(
            pd.DataFrame([
                {
                    "Data": item["created_at"],
                    "File": item["file_name"],
                    "Foglio": item["sheet_name"],
                    "Righe sorgente": item["source_rows"],
                    "Abbinate": item["matched_rows"],
                    "Aggiornate": item["updated_rows"],
                    "Campi integrati": item["filled_fields"],
                    "Differenze": item["conflicts"],
                    "Non abbinate": item["unmatched_rows"],
                }
                for item in excel_imports
            ]),
            use_container_width=True,
            hide_index=True,
        )

if not records:
    st.info("Non ci sono ancora ordini contabili in memoria. Premi “Aggiorna ordini mancanti”.")
    st.stop()

orders = _prepare_accounting_frame(
    records,
    date_from=date_from,
    date_to=date_to,
    our_profit_pct=our_profit_pct,
    partner_profit_pct=partner_profit_pct,
)

st.divider()
st.markdown("### Filtri e selezione")
filter_col1, filter_col2, filter_col3, filter_col4 = st.columns([1.1, 1.4, 1, 1.6])
supplier_values = sorted(value for value in orders["supplier"].unique().tolist() if value)
selected_suppliers = filter_col1.multiselect(
    "Fornitore",
    supplier_values,
    default=[],
    placeholder="Tutti i fornitori",
    key=f"accounting_suppliers_{scope}",
)
status_values = sorted(
    orders[["raw_status", "status_label"]].drop_duplicates().itertuples(index=False, name=None),
    key=lambda item: item[1].lower(),
)
status_map = {f"{label} · {raw}": raw for raw, label in status_values if raw or label}
selected_status_labels = filter_col2.multiselect(
    "Stato ordine",
    list(status_map),
    default=[],
    placeholder="Tutti gli stati, inclusi annullati e rimborsati",
    key=f"accounting_statuses_{scope}",
)
selected_statuses = [status_map[label] for label in selected_status_labels]
country_values = sorted(value for value in orders["country_code"].unique().tolist() if value)
selected_countries = filter_col3.multiselect(
    "Nazione",
    country_values,
    default=[],
    placeholder="Tutte le nazioni",
    key=f"accounting_countries_{scope}",
)
search_text = filter_col4.text_input(
    "Cerca",
    placeholder="Ordine, prodotto, EAN, cliente, tracking…",
    key=f"accounting_search_{scope}",
).strip().lower()

visible = _filter_accounting_frame(
    orders,
    selected_suppliers=selected_suppliers,
    selected_statuses=selected_statuses,
    selected_countries=selected_countries,
    search_text=search_text,
)
if review_active and review_target:
    review_order_ids = {clean_text(value) for value in review_target.get("order_ids") or set() if clean_text(value)}
    review_row_keys = {clean_text(value) for value in review_target.get("row_keys") or set() if clean_text(value)}
    review_mask = orders["row_key"].astype(str).isin(review_row_keys)
    if review_order_ids:
        review_mask = review_mask | orders["order_id"].astype(str).isin(review_order_ids)
    visible = orders.loc[review_mask].sort_values(
        ["order_created", "order_id"], ascending=[False, True]
    ).reset_index(drop=True)

# The summary is rendered into this earlier slot only after grid edits have been
# persisted and re-read. This prevents one-edit latency in totals and profit split.
summary_container = st.container()

selected_ids = set(st.session_state.get(selection_key, []))
visible_ids = set(visible["row_key"].astype(str).tolist())
select_col, deselect_col, clear_col, count_col = st.columns([1, 1, 1, 2])
if select_col.button("Seleziona tutti filtrati", use_container_width=True, key=f"accounting_select_all_{scope}"):
    selected_ids.update(visible_ids)
    st.session_state[selection_key] = sorted(selected_ids)
if deselect_col.button("Deseleziona tutti filtrati", use_container_width=True, key=f"accounting_deselect_{scope}"):
    selected_ids.difference_update(visible_ids)
    st.session_state[selection_key] = sorted(selected_ids)
if clear_col.button("Azzera selezione", use_container_width=True, key=f"accounting_clear_{scope}"):
    selected_ids.clear()
    st.session_state[selection_key] = []
count_col.metric("Righe selezionate", len(selected_ids))

inline_saved_rows = 0

if visible.empty:
    st.warning("Nessun ordine corrisponde ai filtri. I filtri vuoti includono tutti i valori.")
else:
    exported = previous_exports(visible["row_key"].astype(str), account_id, marketplace)
    exported_keys = set(exported)
    table = pd.DataFrame({
        "row_key": visible["row_key"].astype(str),
        "Data": visible["order_created"].str.slice(0, 10),
        "Market": visible["market_label"],
        "Ordine": visible["order_id"],
        "Stato": visible["status_label"],
        "Fornitore": visible["supplier"],
        "Prodotto": visible["product_title"],
        "EAN": visible["ean"],
        "Q.tà": visible["quantity"].fillna(1).astype(int),
        "Vendita €": visible["sale_eur"],
        "Acquisto €": visible["purchase_cost_eur"],
        "Fonte costo": visible["cost_source"],
        "Fonte dati": visible["financial_source"],
        "Commissione €": visible["commission_eur"],
        "Rimborso €": visible["refund_eur"],
        "Da ricevere €": visible["payout_eur"],
        "Costo Extra €": visible["extra_cost_eur"],
        "N Ordine Fornitore": visible["supplier_order_number"],
        "SCONTRINO": visible["receipt"],
        "Margine utile €": visible["net_revenue_eur"],
        f"Nostra quota {our_profit_pct:g}% €": visible["our_share_eur"],
        f"Quota {partner_name} {partner_profit_pct:g}% €": visible["partner_share_eur"],
        "Pagamento stimato": visible["payment_estimated"],
        "Cliente": visible["customer_name"],
        "Tracking": visible["tracking"],
        "Note": visible["note"],
        "Già esportato": visible["row_key"].astype(str).isin(exported_keys),
    })
    if AgGrid is not None:
        builder = GridOptionsBuilder.from_dataframe(table)
        builder.configure_default_column(sortable=True, filter=True, resizable=True, minWidth=90)
        builder.configure_column("row_key", hide=True)
        builder.configure_column(
            "Ordine", checkboxSelection=True, headerCheckboxSelection=True,
            headerCheckboxSelectionFilteredOnly=True, pinned="left", minWidth=155,
        )
        editable_style = JsCode(
            """function(params) {
                return {
                    'backgroundColor': '#eef6ff',
                    'borderBottom': '1px solid #d7e8ff'
                };
            }"""
        )
        economic_editable = JsCode(
            """function(params) {
                const status = String(params.data['Stato'] || '').toLowerCase();
                return !(status.includes('cancell') || status.includes('annull') ||
                         status.includes('rimbors') || status.includes('reso') ||
                         status.includes('no stock'));
            }"""
        )
        editable_text_columns = (
            "Fornitore", "Prodotto", "EAN", "N Ordine Fornitore",
            "Pagamento stimato", "Cliente", "Tracking", "SCONTRINO", "Note",
        )
        for column in editable_text_columns:
            builder.configure_column(column, editable=True, cellStyle=editable_style)
        builder.configure_column(
            "Q.tà", editable=economic_editable, type=["numericColumn"],
            cellEditor="agNumberCellEditor", cellEditorParams={"min": 1, "precision": 0},
            cellStyle=editable_style,
        )
        builder.configure_column("Prodotto", minWidth=300)
        builder.configure_column("Cliente", minWidth=210)
        builder.configure_column("Tracking", minWidth=230)
        builder.configure_column("Fonte costo", minWidth=360)
        builder.configure_column("Fonte dati", minWidth=320)
        builder.configure_column("Note", minWidth=280)
        builder.configure_column("N Ordine Fornitore", minWidth=210)
        builder.configure_column("SCONTRINO", minWidth=150)
        our_share_column = f"Nostra quota {our_profit_pct:g}% €"
        partner_share_column = f"Quota {partner_name} {partner_profit_pct:g}% €"
        margin_value_getter = JsCode(
            """function(params) {
                const status = String(params.data['Stato'] || '').toLowerCase();
                if (status.includes('cancell') || status.includes('annull') ||
                    status.includes('rimbors') || status.includes('reso') ||
                    status.includes('no stock') || status.includes('out of stock')) return 0;
                const purchaseRaw = params.data['Acquisto €'];
                const payoutRaw = params.data['Da ricevere €'];
                if (purchaseRaw === null || purchaseRaw === undefined || purchaseRaw === '' ||
                    payoutRaw === null || payoutRaw === undefined || payoutRaw === '') return null;
                const purchase = Number(purchaseRaw);
                const payout = Number(payoutRaw);
                const extra = Number(params.data['Costo Extra €'] || 0);
                if (!Number.isFinite(purchase) || !Number.isFinite(payout) || !Number.isFinite(extra)) return null;
                return Math.round((payout - purchase - extra) * 100) / 100;
            }"""
        )
        our_share_value_getter = JsCode(
            f"""function(params) {{
                const margin = params.getValue('Margine utile €');
                if (margin === null || margin === undefined || margin === '') return null;
                return Math.round((Number(margin) * {our_profit_pct!r} / 100) * 100) / 100;
            }}"""
        )
        partner_share_value_getter = JsCode(
            f"""function(params) {{
                const marginRaw = params.getValue('Margine utile €');
                if (marginRaw === null || marginRaw === undefined || marginRaw === '') return null;
                const margin = Number(marginRaw);
                const ourAmount = Math.round((margin * {our_profit_pct!r} / 100) * 100) / 100;
                return Math.round((margin - ourAmount) * 100) / 100;
            }}"""
        )
        money_table_columns = (
            "Vendita €", "Acquisto €", "Commissione €", "Rimborso €", "Da ricevere €",
            "Costo Extra €", "Margine utile €", our_share_column, partner_share_column,
        )
        editable_money_columns = {
            "Vendita €", "Acquisto €", "Commissione €", "Rimborso €",
            "Da ricevere €", "Costo Extra €",
        }
        for column in money_table_columns:
            config: dict[str, Any] = {
                "type": ["numericColumn"],
                "valueFormatter": "value == null ? '' : Number(value).toFixed(2) + ' €'",
            }
            if column == "Margine utile €":
                config["valueGetter"] = margin_value_getter
            elif column == our_share_column:
                config["valueGetter"] = our_share_value_getter
            elif column == partner_share_column:
                config["valueGetter"] = partner_share_value_getter
            if column in editable_money_columns:
                config.update({
                    "editable": economic_editable,
                    "cellEditor": "agNumberCellEditor",
                    "cellEditorParams": {"precision": 2},
                    "cellStyle": editable_style,
                })
            builder.configure_column(column, **config)
        builder.configure_selection(selection_mode="multiple", use_checkbox=True)
        row_style = JsCode(
            """function(params) {
                const status = String(params.data['Stato'] || '').toLowerCase();
                if (status.includes('cancell') || status.includes('annull')) return {'backgroundColor':'#f4cccc'};
                if (status.includes('rimbors') || status.includes('reso')) return {'backgroundColor':'#fce5cd'};
                if (params.data['Acquisto €'] == null) return {'backgroundColor':'#fff2cc'};
                if (Number(params.data['Margine utile €']) < 0) return {'backgroundColor':'#f4cccc'};
                return null;
            }"""
        )
        live_formula_refresh = JsCode(
            f"""function(params) {{
                const field = String(params.colDef.field || '');
                if (field === 'Vendita €' || field === 'Commissione €') {{
                    const saleRaw = params.data['Vendita €'];
                    const commissionRaw = params.data['Commissione €'];
                    if (saleRaw !== null && saleRaw !== undefined && saleRaw !== '') {{
                        const sale = Number(saleRaw);
                        const commission = Number(commissionRaw || 0);
                        if (Number.isFinite(sale) && Number.isFinite(commission)) {{
                            const payout = Math.round((sale - commission) * 100) / 100;
                            if (Number(params.data['Da ricevere €']) !== payout) {{
                                params.node.setDataValue('Da ricevere €', payout);
                            }}
                        }}
                    }}
                }}
                params.api.refreshCells({{
                    rowNodes: [params.node],
                    columns: ['Margine utile €', {our_share_column!r}, {partner_share_column!r}],
                    force: true
                }});
            }}"""
        )
        builder.configure_grid_options(
            rowMultiSelectWithClick=True,
            suppressRowClickSelection=False,
            enableRangeSelection=True,
            animateRows=False,
            getRowStyle=row_style,
            getRowId=JsCode("function(params) { return String(params.data.row_key); }"),
            onCellValueChanged=live_formula_refresh,
            suppressScrollOnNewData=True,
            maintainColumnOrder=True,
            stopEditingWhenCellsLoseFocus=True,
            singleClickEdit=True,
            enterNavigatesVertically=True,
            enterNavigatesVerticallyAfterEdit=True,
            undoRedoCellEditing=True,
            undoRedoCellEditingLimit=20,
        )
        preselected = [index for index, row_id in enumerate(table["row_key"]) if row_id in selected_ids]
        response = AgGrid(
            table,
            gridOptions=builder.build(),
            data_return_mode=DataReturnMode.AS_INPUT,
            update_mode=GridUpdateMode.VALUE_CHANGED | GridUpdateMode.SELECTION_CHANGED,
            pre_selected_rows=preselected,
            height=590,
            fit_columns_on_grid_load=False,
            theme="streamlit",
            key=f"accounting_grid_{scope}",
            allow_unsafe_jscode=True,
            reload_data=False,
        )
        returned_data = response.get("data")
        if isinstance(returned_data, pd.DataFrame):
            returned_frame = returned_data.copy()
        elif isinstance(returned_data, list):
            returned_frame = pd.DataFrame(returned_data)
        else:
            returned_frame = pd.DataFrame()
        inline_changes = _accounting_grid_changes(
            table,
            returned_frame,
            account_id=account_id,
            marketplace=marketplace,
        )
        if inline_changes:
            saved_inline = save_accounting_inline_edits(inline_changes)
            if saved_inline["updated_rows"]:
                inline_saved_rows += int(saved_inline["updated_rows"])
                st.toast(
                    f"Salvataggio automatico: {saved_inline['updated_rows']} righe memorizzate",
                    icon="💾",
                )
        returned = response.get("selected_rows")
        if isinstance(returned, pd.DataFrame):
            checked = set(returned.get("row_key", pd.Series(dtype=str)).astype(str))
        elif isinstance(returned, list):
            checked = {
                clean_text(item.get("row_key")) for item in returned
                if isinstance(item, Mapping) and clean_text(item.get("row_key"))
            }
        else:
            checked = set()
        selected_ids = (selected_ids - visible_ids) | checked
        st.session_state[selection_key] = sorted(selected_ids)
    else:
        fallback = table.copy()
        fallback.insert(0, "Seleziona", fallback["row_key"].isin(selected_ids))
        editable_columns = {"Seleziona", *ACCOUNTING_GRID_EDITABLE_COLUMNS.keys()}
        edited = st.data_editor(
            fallback,
            hide_index=True,
            use_container_width=True,
            height=590,
            disabled=[column for column in fallback.columns if column not in editable_columns],
            column_config={
                "Seleziona": st.column_config.CheckboxColumn(default=False),
                "row_key": None,
                "Q.tà": st.column_config.NumberColumn(min_value=1, step=1),
                "Vendita €": st.column_config.NumberColumn(format="%.2f €"),
                "Acquisto €": st.column_config.NumberColumn(format="%.2f €"),
                "Commissione €": st.column_config.NumberColumn(format="%.2f €"),
                "Rimborso €": st.column_config.NumberColumn(format="%.2f €"),
                "Da ricevere €": st.column_config.NumberColumn(format="%.2f €"),
                "Costo Extra €": st.column_config.NumberColumn(format="%.2f €"),
            },
            key=f"accounting_fallback_{scope}",
        )
        inline_changes = _accounting_grid_changes(
            table,
            edited.drop(columns=["Seleziona"], errors="ignore"),
            account_id=account_id,
            marketplace=marketplace,
        )
        if inline_changes:
            saved_inline = save_accounting_inline_edits(inline_changes)
            if saved_inline["updated_rows"]:
                st.toast(
                    f"Salvataggio automatico: {saved_inline['updated_rows']} righe memorizzate",
                    icon="💾",
                )
                # Fallback without AgGrid: a short rerun is required to redraw
                # protected formula columns from the newly persisted values.
                st.rerun()
        checked = set(edited.loc[edited["Seleziona"], "row_key"].astype(str))
        selected_ids = (selected_ids - visible_ids) | checked
        st.session_state[selection_key] = sorted(selected_ids)

if inline_saved_rows:
    # Re-read only the local accounting cache (no marketplace API call). The same
    # Streamlit run can therefore update total margin, Seller/BEBOL shares and all
    # downstream export values immediately after a manual cell edit.
    records = accounting_core.rows(accounting_core_scope)
    orders = _prepare_accounting_frame(
        records,
        date_from=date_from,
        date_to=date_to,
        our_profit_pct=our_profit_pct,
        partner_profit_pct=partner_profit_pct,
    )
    visible = _filter_accounting_frame(
        orders,
        selected_suppliers=selected_suppliers,
        selected_statuses=selected_statuses,
        selected_countries=selected_countries,
        search_text=search_text,
    )
    if review_active and review_target:
        review_order_ids = {clean_text(value) for value in review_target.get("order_ids") or set() if clean_text(value)}
        review_row_keys = {clean_text(value) for value in review_target.get("row_keys") or set() if clean_text(value)}
        review_mask = orders["row_key"].astype(str).isin(review_row_keys)
        if review_order_ids:
            review_mask = review_mask | orders["order_id"].astype(str).isin(review_order_ids)
        visible = orders.loc[review_mask].sort_values(
            ["order_created", "order_id"], ascending=[False, True]
        ).reset_index(drop=True)

_render_accounting_summary(
    summary_container,
    visible,
    our_profit_pct=our_profit_pct,
    partner_profit_pct=partner_profit_pct,
    partner_name=partner_name,
    configured=bool(profit_settings["configured"]),
)

st.caption(
    "Le celle azzurre sono modificabili e vengono salvate automaticamente nel database: "
    "restano memorizzate anche chiudendo il programma e prevalgono sui successivi aggiornamenti API. "
    "Non viene ricaricata la pagina né riportata la tabella all’inizio. ID ordine, stato, fonti e formule "
    "rimangono protetti. Il quadratino nell’intestazione seleziona tutte le righe filtrate; con Shift "
    "puoi selezionare righe consecutive."
)

selected_ids = set(st.session_state.get(selection_key, []))
all_by_key = {clean_text(item.get("row_key")): item for item in records}
selected_records = [all_by_key[key] for key in selected_ids if key in all_by_key]

st.divider()
st.markdown("### Modifica multipla dei campi manuali")
st.caption(
    "Questa sezione è facoltativa: gli stessi campi sono già modificabili nella tabella principale. "
    "Anche qui il salvataggio è automatico e permanente."
)
if not selected_records:
    st.info("Seleziona almeno una riga per modificare in blocco ordine fornitore, costo extra o scontrino.")
else:
    manual_frame = pd.DataFrame([
        {
            "row_key": item["row_key"],
            "marketplace_account_id": item["marketplace_account_id"],
            "marketplace": item["marketplace"],
            "Ordine": item["order_id"],
            "Prodotto": item["product_title"],
            "N Ordine Fornitore": item.get("supplier_order_number", ""),
            "Costo Extra": float(item.get("extra_cost_eur") or 0),
            "SCONTRINO": item.get("receipt", ""),
        }
        for item in selected_records
    ])
    manual_edited = st.data_editor(
        manual_frame,
        hide_index=True,
        use_container_width=True,
        height=min(480, 56 + len(manual_frame) * 35),
        disabled=["row_key", "marketplace_account_id", "marketplace", "Ordine", "Prodotto"],
        column_config={
            "row_key": None,
            "marketplace_account_id": None,
            "marketplace": None,
            "Costo Extra": st.column_config.NumberColumn(format="%.2f €", min_value=0.0),
        },
        key=f"accounting_manual_{scope}",
    )
    manual_original = manual_frame.set_index("row_key", drop=False)
    manual_changes = []
    for _, item in manual_edited.iterrows():
        row_key = clean_text(item.get("row_key"))
        if not row_key or row_key not in manual_original.index:
            continue
        base = manual_original.loc[row_key]
        changed = (
            not _grid_value_equal(base.get("N Ordine Fornitore"), item.get("N Ordine Fornitore"))
            or not _grid_value_equal(base.get("Costo Extra"), item.get("Costo Extra"), numeric=True)
            or not _grid_value_equal(base.get("SCONTRINO"), item.get("SCONTRINO"))
        )
        if changed:
            manual_changes.append({
                "row_key": row_key,
                "marketplace_account_id": int(item["marketplace_account_id"]),
                "marketplace": item["marketplace"],
                "supplier_order_number": item["N Ordine Fornitore"],
                "extra_cost_eur": item["Costo Extra"],
                "receipt": item["SCONTRINO"],
            })
    if manual_changes:
        saved_manual = save_manual_fields(manual_changes)
        if saved_manual:
            st.toast(f"Salvataggio automatico: {saved_manual} righe memorizzate", icon="💾")
            # The secondary data_editor cannot recalculate protected formula cells
            # client-side like AgGrid, so redraw once from the local cache.
            st.rerun()

st.divider()
st.markdown("### Genera file Excel")
if not selected_records:
    st.info("Seleziona le righe da esportare.")
else:
    duplicate_map = previous_exports([item["row_key"] for item in selected_records], account_id, marketplace)
    duplicate_count = len(duplicate_map)
    allow_regeneration = True
    if duplicate_count:
        st.warning(
            f"{duplicate_count} righe selezionate risultano già presenti in un file contabile precedente."
        )
        allow_regeneration = st.checkbox(
            "Confermo la rigenerazione delle righe già esportate",
            value=False,
            key=f"accounting_confirm_regeneration_{scope}",
        )
    selected_summary = totals(selected_records)
    selected_split = split_profit(
        selected_summary["net_revenue"], our_profit_pct, partner_profit_pct
    )
    exp_metrics = st.columns(6)
    exp_metrics[0].metric("Righe da esportare", len(selected_records))
    exp_metrics[1].metric("Vendite", f"{selected_summary['sale']:,.2f} €")
    exp_metrics[2].metric("Da ricevere", f"{selected_summary['payout']:,.2f} €")
    exp_metrics[3].metric("Margine utile", f"{selected_summary['net_revenue']:,.2f} €")
    exp_metrics[4].metric(
        f"Nostra quota · {our_profit_pct:g}%", f"{selected_split['our_amount']:,.2f} €"
    )
    exp_metrics[5].metric(
        f"Quota {partner_name} · {partner_profit_pct:g}%",
        f"{selected_split['partner_amount']:,.2f} €",
    )
    file_name = default_file_name(marketplace, date_from, date_to)
    if st.button(
        "Genera, salva e prepara il download",
        type="primary",
        use_container_width=True,
        key=f"accounting_generate_{scope}",
    ):
        if duplicate_count and not allow_regeneration:
            st.error("Conferma prima la rigenerazione delle righe già esportate.")
        else:
            try:
                content = export_xlsx_bytes(
                    selected_records,
                    our_profit_pct=our_profit_pct,
                    partner_profit_pct=partner_profit_pct,
                    partner_name=partner_name,
                )
                saved_export = save_export(
                    seller_id, account_id, marketplace, file_name, content,
                    selected_records, date_from, date_to,
                    our_profit_pct=our_profit_pct,
                    partner_profit_pct=partner_profit_pct,
                )
                st.session_state[last_export_key] = {
                    "content": content,
                    "file_name": file_name,
                    "saved": saved_export,
                }
                st.success("File contabile generato e salvato nell’archivio del programma.")
            except Exception as exc:
                st.error(f"Errore durante la generazione del file: {exc}")
    last_export = st.session_state.get(last_export_key)
    if last_export:
        st.download_button(
            "Scarica il file Excel",
            data=last_export["content"],
            file_name=last_export["file_name"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key=f"accounting_download_{scope}",
        )

st.divider()
st.markdown("### Archivio file contabili")
history = export_history(seller_id, account_id, marketplace)
if not history:
    st.caption("Nessun file contabile archiviato per questo account.")
else:
    st.dataframe(
        pd.DataFrame([
            {
                "ID": item["id"],
                "Creato": item["created_at"],
                "File": item["file_name"],
                "Periodo": f"{item['date_from']} → {item['date_to']}",
                "Righe": item["row_count"],
                "Vendite €": item["total_sale_eur"],
                "Da ricevere €": item["total_payout_eur"],
                "Margine utile €": item["total_net_revenue_eur"],
                "Nostra quota €": item.get("total_our_profit_eur", 0),
                f"Quota {partner_name} €": item.get("total_partner_profit_eur", 0),
            }
            for item in history
        ]),
        use_container_width=True,
        hide_index=True,
    )
    history_map = {
        f"{item['created_at']} · {item['file_name']} · {item['row_count']} righe": item
        for item in history if Path(str(item.get("file_path") or "")).exists() or clean_text(item.get("storage_key"))
    }
    if history_map:
        selected_history_label = st.selectbox(
            "File da riscaricare",
            list(history_map),
            key=f"accounting_history_{scope}",
        )
        selected_history = history_map[selected_history_label]
        st.download_button(
            "Scarica file archiviato",
            data=accounting_export_bytes(selected_history),
            file_name=selected_history["file_name"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"accounting_history_download_{selected_history['id']}",
        )


st.divider()
st.markdown("### Generazione Documenti in PDF")
st.caption(
    "Genera un documento contabile riepilogativo direttamente dai dati già salvati nel "
    "programma. Puoi includere uno o più marketplace e scegliere un giorno, la settimana "
    "corrente, il mese corrente, uno o tutti i mesi disponibili, l'anno corrente, uno o "
    "tutti gli anni disponibili oppure un intervallo personalizzato. Il PDF può includere "
    "anche il dettaglio completo degli ordini."
)

pdf_marketplace_options = sorted({
    clean_text(item.get("marketplace")).lower()
    for item in accounts
    if clean_text(item.get("marketplace")).lower() in marketplace_labels
})
selected_pdf_marketplaces: list[str] = []
if len(pdf_marketplace_options) == 1:
    only_marketplace = pdf_marketplace_options[0]
    st.checkbox(
        marketplace_labels.get(only_marketplace, only_marketplace.title()),
        value=True,
        disabled=True,
        key=f"accounting_pdf_only_marketplace_{seller_id}_{only_marketplace}",
    )
    selected_pdf_marketplaces = [only_marketplace]
else:
    st.markdown("**Marketplace da includere**")
    pdf_market_columns = st.columns(min(4, max(1, len(pdf_marketplace_options))))
    for pdf_market_index, pdf_marketplace in enumerate(pdf_marketplace_options):
        if pdf_market_columns[pdf_market_index % len(pdf_market_columns)].checkbox(
            marketplace_labels.get(pdf_marketplace, pdf_marketplace.title()),
            value=True,
            key=f"accounting_pdf_marketplace_{seller_id}_{pdf_marketplace}",
        ):
            selected_pdf_marketplaces.append(pdf_marketplace)

pdf_source_records: list[dict[str, Any]] = []
for pdf_account in accounts:
    pdf_marketplace = clean_text(pdf_account.get("marketplace")).lower()
    if pdf_marketplace not in selected_pdf_marketplaces:
        continue
    for pdf_record in accounting_rows(seller_id, int(pdf_account["id"]), pdf_marketplace):
        enriched_record = dict(pdf_record)
        enriched_record["_account_name"] = clean_text(pdf_account.get("account_name"))
        pdf_source_records.append(enriched_record)

pdf_period_options = {
    "Giorno": "day",
    "Settimana corrente": "current_week",
    "Mese corrente": "current_month",
    "Seleziona mese": "select_month",
    "Anno corrente": "current_year",
    "Seleziona anno": "select_year",
    "Intervallo personalizzato": "custom",
}
pdf_period_label = st.selectbox(
    "Periodo del documento",
    list(pdf_period_options),
    key=f"accounting_pdf_period_mode_{seller_id}",
)
pdf_period_mode = pdf_period_options[pdf_period_label]
pdf_available_periods = available_accounting_periods(pdf_source_records)
pdf_available_months = list(pdf_available_periods["months"])
pdf_available_years = list(pdf_available_periods["years"])

pdf_selected_day = None
pdf_selected_months: list[str] = []
pdf_selected_years: list[int] = []
pdf_custom_from = None
pdf_custom_to = None

if pdf_period_mode == "day":
    pdf_selected_day = st.date_input(
        "Giorno da includere",
        value=date.today(),
        key=f"accounting_pdf_day_{seller_id}",
    )
elif pdf_period_mode == "select_month":
    if not pdf_available_months:
        st.info("Non risultano ancora mesi contabili disponibili per i marketplace selezionati.")
    else:
        pdf_all_months = st.checkbox(
            "Tutti i mesi disponibili",
            value=False,
            key=f"accounting_pdf_all_months_{seller_id}",
        )
        pdf_month_label_map = {month_label(item): item for item in pdf_available_months}
        pdf_default_month_labels = (
            list(pdf_month_label_map)
            if pdf_all_months
            else [next(iter(pdf_month_label_map))]
        )
        pdf_month_labels = st.multiselect(
            "Mesi da includere",
            list(pdf_month_label_map),
            default=pdf_default_month_labels,
            disabled=pdf_all_months,
            key=f"accounting_pdf_months_{seller_id}_{int(pdf_all_months)}",
        )
        pdf_selected_months = (
            pdf_available_months
            if pdf_all_months
            else [pdf_month_label_map[item] for item in pdf_month_labels]
        )
elif pdf_period_mode == "select_year":
    if not pdf_available_years:
        st.info("Non risultano ancora anni contabili disponibili per i marketplace selezionati.")
    else:
        pdf_all_years = st.checkbox(
            "Tutti gli anni disponibili",
            value=False,
            key=f"accounting_pdf_all_years_{seller_id}",
        )
        pdf_selected_years = st.multiselect(
            "Anni da includere",
            pdf_available_years,
            default=(pdf_available_years if pdf_all_years else [pdf_available_years[0]]),
            disabled=pdf_all_years,
            key=f"accounting_pdf_years_{seller_id}_{int(pdf_all_years)}",
        )
        if pdf_all_years:
            pdf_selected_years = pdf_available_years
elif pdf_period_mode == "custom":
    pdf_custom_col1, pdf_custom_col2 = st.columns(2)
    pdf_custom_from = pdf_custom_col1.date_input(
        "Dal",
        value=date.today() - timedelta(days=30),
        key=f"accounting_pdf_custom_from_{seller_id}",
    )
    pdf_custom_to = pdf_custom_col2.date_input(
        "Al",
        value=date.today(),
        min_value=pdf_custom_from,
        key=f"accounting_pdf_custom_to_{seller_id}",
    )

pdf_period = None
pdf_period_error = ""
try:
    pdf_period = build_accounting_pdf_period(
        pdf_period_mode,
        selected_day=pdf_selected_day,
        selected_months=pdf_selected_months,
        selected_years=pdf_selected_years,
        custom_from=pdf_custom_from,
        custom_to=pdf_custom_to,
    )
except ValueError as exc:
    pdf_period_error = str(exc)

pdf_filtered_records: list[dict[str, Any]] = []
if not selected_pdf_marketplaces:
    st.warning("Seleziona almeno un marketplace da includere nel PDF.")
elif pdf_period_error:
    st.warning(pdf_period_error)
elif pdf_period is not None:
    pdf_filtered_records = filter_accounting_records(
        pdf_source_records,
        pdf_period,
        selected_pdf_marketplaces,
    )

pdf_include_details = st.checkbox(
    "Includi nel PDF il dettaglio completo delle righe contabili",
    value=True,
    key=f"accounting_pdf_include_details_{seller_id}",
    help=(
        "Disattivando questa opzione viene generato un documento più breve con i soli "
        "riepiloghi complessivi e per marketplace."
    ),
)

if pdf_period is not None and selected_pdf_marketplaces:
    st.info(
        f"**Periodo:** {pdf_period.label}  ·  **Marketplace:** "
        + ", ".join(
            marketplace_labels.get(item, item.title()) for item in selected_pdf_marketplaces
        )
        + f"  ·  **Righe trovate:** {len(pdf_filtered_records):,}."
    )

if pdf_filtered_records:
    pdf_summary = totals(pdf_filtered_records)
    pdf_profit_split = split_profit(
        pdf_summary["net_revenue"], our_profit_pct, partner_profit_pct
    )
    pdf_missing_costs = sum(
        item.get("purchase_cost_eur") in (None, "") for item in pdf_filtered_records
    )
    pdf_preview_metrics1 = st.columns(5)
    pdf_preview_metrics1[0].metric("Righe PDF", len(pdf_filtered_records))
    pdf_preview_metrics1[1].metric("Vendite", f"{pdf_summary['sale']:,.2f} €")
    pdf_preview_metrics1[2].metric("Da ricevere", f"{pdf_summary['payout']:,.2f} €")
    pdf_preview_metrics1[3].metric("Margine utile", f"{pdf_summary['net_revenue']:,.2f} €")
    pdf_preview_metrics1[4].metric("Costi da verificare", pdf_missing_costs)
    pdf_preview_metrics2 = st.columns(2)
    pdf_preview_metrics2[0].metric(
        f"Nostra quota · {our_profit_pct:g}%",
        f"{pdf_profit_split['our_amount']:,.2f} €",
    )
    pdf_preview_metrics2[1].metric(
        f"Quota {partner_name} · {partner_profit_pct:g}%",
        f"{pdf_profit_split['partner_amount']:,.2f} €",
    )
else:
    st.info("Nessuna riga contabile corrisponde al periodo e ai marketplace selezionati.")

pdf_report_signature = ""
if pdf_period is not None:
    pdf_signature_values = [
        str(seller_id),
        pdf_period.mode,
        pdf_period.label,
        ",".join(selected_pdf_marketplaces),
        str(int(pdf_include_details)),
        *(
            f"{clean_text(item.get('marketplace'))}:{clean_text(item.get('row_key'))}:"
            f"{clean_text(item.get('synced_at'))}"
            for item in pdf_filtered_records
        ),
    ]
    pdf_report_signature = hashlib.sha256(
        "|".join(pdf_signature_values).encode("utf-8", errors="ignore")
    ).hexdigest()

pdf_generation_state_key = f"accounting_pdf_last_{seller_id}"
if st.button(
    "Genera documento contabile in PDF",
    type="primary",
    use_container_width=True,
    disabled=not pdf_filtered_records or pdf_period is None,
    key=f"accounting_pdf_generate_{seller_id}",
):
    try:
        with st.spinner("Generazione del documento PDF in corso…"):
            pdf_content = accounting_pdf_bytes(
                pdf_filtered_records,
                seller_name=clean_text(seller_record.get("name")) or "Seller",
                period=pdf_period,
                marketplaces=selected_pdf_marketplaces,
                our_profit_pct=our_profit_pct,
                partner_profit_pct=partner_profit_pct,
                partner_name=partner_name,
                include_details=pdf_include_details,
            )
            pdf_file_name = accounting_pdf_file_name(
                selected_pdf_marketplaces,
                pdf_period,
            )
        st.session_state[pdf_generation_state_key] = {
            "signature": pdf_report_signature,
            "content": pdf_content,
            "file_name": pdf_file_name,
            "rows": len(pdf_filtered_records),
        }
        st.success(
            f"Documento PDF generato: {len(pdf_filtered_records):,} righe contabili."
        )
    except Exception as exc:
        st.error(f"Impossibile generare il documento PDF: {exc}")

pdf_last_report = st.session_state.get(pdf_generation_state_key)
if pdf_last_report and pdf_last_report.get("signature") == pdf_report_signature:
    st.download_button(
        "Scarica il documento contabile PDF",
        data=pdf_last_report["content"],
        file_name=pdf_last_report["file_name"],
        mime="application/pdf",
        use_container_width=True,
        key=f"accounting_pdf_download_{seller_id}_{pdf_report_signature[:12]}",
    )

with st.expander("Regole di calcolo"):
    st.markdown(
        "- **Vendita**: importo netto dopo eventuali rimborsi.\n"
        "- **C. Market**: commissione effettiva restituita dall’API.\n"
        "- **a Pagare**: importo effettivo del marketplace; in assenza del campo API, vendita meno commissione.\n"
        "- **Acquisto**: costo trovato tramite EAN esatto in tutti i listini caricati; se resta mancante può essere integrato dal file Excel di confronto.\n"
        "- **Margine Lordo**: a Pagare meno Acquisto.\n"
        "- **Ricavo Netto**: Margine Lordo meno Costo Extra.\n"
        "- **PAGATO**: data stimata dell’ordine più 21 giorni. Per ordini cancellati o integralmente rimborsati resta vuoto.\n"
        "- **Confronto Excel**: completa soltanto valori vuoti/mancanti. Le differenze non sovrascrivono API o listini e vengono riportate nel report di controllo.\n"
        "- Gli ordini cancellati sono inclusi ma con valori economici azzerati. I resi/rimborsi sono inclusi e il costo prodotto resta visibile, così non vengono mostrati utili inesistenti."
    )
