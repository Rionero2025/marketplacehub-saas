from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from marketplace_core.jobs import JobsCore
from marketplace_core.orders import OrderQuery, OrderScope, OrdersCore
from services.db import rows
from services.session import bootstrap, seller_selector


if not st.session_state.get("_embedded_marketplace_orders"):
    bootstrap()
    st.title("Ordini Worten")
    seller_id = seller_selector()
else:
    seller_id = st.session_state.get("active_seller_id")
    st.subheader("Ordini Worten")

if seller_id is None:
    st.stop()

st.caption(
    "Gli ordini Worten vengono sincronizzati in background e salvati nella cache "
    "normalizzata PostgreSQL. La tabella legge solo la pagina richiesta dal database: "
    "non trasferisce l'intero archivio ad ogni aggiornamento dell'interfaccia."
)

accounts = rows(
    """
    SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND marketplace='worten' AND active=1
    ORDER BY account_name
    """,
    (seller_id,),
)
if not accounts:
    st.error("Configura prima un account Worten per questo Seller.")
    st.stop()

account_map = {
    f"{item['account_name']} · ID {item['id']}": item for item in accounts
}
account = account_map[
    st.selectbox(
        "Account Worten",
        list(account_map),
        key=f"orders_worten_account_{seller_id}",
    )
]
account_id = int(account["id"])

orders_core = OrdersCore()
jobs_core = JobsCore()
scope = OrderScope(int(seller_id), account_id, "worten", "live")

st.markdown("#### Sincronizzazione")
sync_cols = st.columns([1, 1, 1.3])
date_from = sync_cols[0].date_input(
    "Ordini dal",
    value=date.today() - timedelta(days=30),
    max_value=date.today(),
    key=f"worten_orders_from_{account_id}",
)
date_to = sync_cols[1].date_input(
    "Ordini al",
    value=date.today(),
    min_value=date_from,
    max_value=date.today(),
    key=f"worten_orders_to_{account_id}",
)
job_key = f"worten_orders_job_{seller_id}_{account_id}"
if sync_cols[2].button(
    "Sincronizza ordini Worten",
    type="primary",
    use_container_width=True,
    key=f"worten_orders_sync_{account_id}",
):
    existing = jobs_core.snapshot(st.session_state.get(job_key, "")) if st.session_state.get(job_key) else None
    if existing and not existing.terminal:
        st.warning("È già in corso una sincronizzazione Worten per questo account.")
    else:
        request = orders_core.build_sync_job(
            scope,
            date_from=date_from,
            date_to=date_to,
        )
        receipt = jobs_core.submit(request)
        jobs_core.start_local(receipt.job_id)
        st.session_state[job_key] = receipt.job_id
        st.success(
            "Sincronizzazione Worten avviata in background. Puoi cambiare pagina senza interromperla."
        )

job_id = st.session_state.get(job_key)
if job_id:
    job = jobs_core.snapshot(job_id)
    if job:
        st.progress(
            min(1.0, max(0.0, job.progress_pct / 100.0)),
            text=job.message or job.status,
        )
        jc1, jc2 = st.columns([1, 4])
        if jc1.button("Aggiorna stato", key=f"worten_orders_job_refresh_{job_id}"):
            st.rerun()
        if job.status == "done":
            result = dict(job.result)
            st.success(
                f"Sincronizzazione completata · righe salvate {int(result.get('saved') or 0):,}."
            )
        elif job.status == "error":
            st.error(f"Sincronizzazione Worten non riuscita: {job.error}")
        else:
            jc2.caption(
                f"Job {job.job_id[:8]} · {job.status} · il lavoro continua in background."
            )

facets = orders_core.archive_info(scope)
archive_total = int(facets.get("row_count") or 0)
if archive_total <= 0:
    st.info("Non ci sono ancora ordini Worten in memoria. Avvia una sincronizzazione.")
    st.stop()

st.caption(
    f"Archivio normalizzato: {archive_total:,} righe · "
    f"ultimo ordine {facets.get('last_order_date') or '—'} · "
    f"ultimo aggiornamento {facets.get('last_synced_at') or '—'}."
)

st.markdown("#### Filtri e archivio")
filter_cols = st.columns([1.3, 1.3, 2.2, 1])
statuses = filter_cols[0].multiselect(
    "Stato",
    list(facets.get("statuses") or []),
    key=f"worten_orders_status_{account_id}",
)
suppliers = filter_cols[1].multiselect(
    "Fornitore",
    list(facets.get("suppliers") or []),
    key=f"worten_orders_supplier_{account_id}",
)
search = filter_cols[2].text_input(
    "Cerca",
    placeholder="Ordine, SKU, prodotto, cliente…",
    key=f"worten_orders_search_{account_id}",
).strip()
page_size = int(
    filter_cols[3].selectbox(
        "Righe",
        [100, 250, 500, 1000],
        index=1,
        key=f"worten_orders_page_size_{account_id}",
    )
)

page_key = f"worten_orders_page_{account_id}"
st.session_state.setdefault(page_key, 1)
page_number = max(1, int(st.session_state.get(page_key) or 1))
query = OrderQuery(
    statuses=tuple(statuses),
    suppliers=tuple(suppliers),
    search=search,
    limit=page_size,
    offset=(page_number - 1) * page_size,
    include_raw=False,
)
page = orders_core.page(scope, query)
if page.page_count and page_number > page.page_count:
    st.session_state[page_key] = page.page_count
    st.rerun()

nav = st.columns([1, 1.4, 1])
if nav[0].button(
    "← Pagina precedente",
    disabled=page_number <= 1,
    use_container_width=True,
    key=f"worten_orders_prev_{account_id}",
):
    st.session_state[page_key] = max(1, page_number - 1)
    st.rerun()
nav[1].metric(
    "Risultati",
    f"{page.total:,}",
    f"Pagina {page.page_number} / {max(1, page.page_count)}",
)
if nav[2].button(
    "Pagina successiva →",
    disabled=not page.has_more,
    use_container_width=True,
    key=f"worten_orders_next_{account_id}",
):
    st.session_state[page_key] = page_number + 1
    st.rerun()

items = [dict(item) for item in page.items]
if not items:
    st.warning("Nessun ordine corrisponde ai filtri selezionati.")
    st.stop()

frame = pd.DataFrame(items)
columns = [
    "order_id", "order_created", "normalized_status", "supplier", "product_title",
    "composite_sku", "quantity", "customer_name", "country_code", "storefront",
]
visible = frame[[column for column in columns if column in frame.columns]].rename(
    columns={
        "order_id": "Ordine",
        "order_created": "Data",
        "normalized_status": "Stato",
        "supplier": "Fornitore",
        "product_title": "Prodotto",
        "composite_sku": "SKU",
        "quantity": "Q.tà",
        "customer_name": "Cliente",
        "country_code": "Paese",
        "storefront": "Canale",
    }
)
st.dataframe(visible, use_container_width=True, hide_index=True, height=620)
