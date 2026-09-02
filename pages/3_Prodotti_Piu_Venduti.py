from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from services.accounting import ensure_schema
from services.dashboard import DEFAULT_DASHBOARD_TIMEZONE, dashboard_detail_rows
from services.product_stats import (
    aggregate_dimension,
    aggregate_product_orders,
    aggregate_product_stats,
    filter_product_rows,
    merge_previous_period,
    margin_review_queue,
    previous_period_range,
    product_rows,
    product_totals,
    sort_product_stats,
)
from services.session import bootstrap


bootstrap()
ensure_schema()

st.title("Prodotti più venduti")
st.caption(
    "Classifica e analisi dei prodotti venduti su tutti i Seller. I dati arrivano dalla cache contabile locale, "
    "senza nuove chiamate API, e seguono le stesse regole economiche della Dashboard e della Contabilità."
)


def format_euro(value: object) -> str:
    if value is None:
        return "Da verificare"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    formatted = f"{abs(number):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{'−' if number < 0 else ''}{formatted} €"


def format_number(value: object) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if number.is_integer():
        return f"{int(number):,}".replace(",", ".")
    return f"{number:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def pct_delta(current: object, previous: object) -> str | None:
    try:
        current_value = float(current or 0)
        previous_value = float(previous or 0)
    except (TypeError, ValueError):
        return None
    if abs(previous_value) < 1e-9:
        return None if abs(current_value) < 1e-9 else "+100,00%"
    delta = (current_value - previous_value) / abs(previous_value) * 100
    return f"{delta:+.2f}%".replace(".", ",")


def _selected_indices(event: object) -> list[int]:
    try:
        return list(event.selection.rows)  # type: ignore[attr-defined]
    except Exception:
        try:
            return list(event.get("selection", {}).get("rows", []))  # type: ignore[union-attr]
        except Exception:
            return []


def _safe_title(value: object, limit: int = 92) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _frame_for_stats(stats: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    for position, item in enumerate(stats, start=1):
        rows.append(
            {
                "product_key": item.get("product_key", ""),
                "#": position,
                "Prodotto": item.get("product_title", ""),
                "EAN": item.get("ean", ""),
                "SKU": item.get("composite_sku", ""),
                "Fornitore": item.get("suppliers", ""),
                "Q.tà": item.get("quantity", 0),
                "Ordini": item.get("orders", 0),
                "Vendite €": item.get("sales_eur", 0),
                "Prezzo medio €": item.get("average_price_eur", 0),
                "Costo acquisto €": item.get("purchase_cost_eur"),
                "Commissioni €": item.get("commission_eur", 0),
                "Da ricevere €": item.get("payout_eur"),
                "Costi extra €": item.get("extra_cost_eur", 0),
                "Margine utile €": item.get("margin_eur"),
                "Margine %": item.get("margin_pct"),
                "Quota BEBOL €": item.get("our_share_eur"),
                "Quota Seller €": item.get("partner_share_eur"),
                "Q.tà periodo prec.": item.get("previous_quantity", 0),
                "Δ Q.tà %": item.get("quantity_delta_pct"),
                "Vendite prec. €": item.get("previous_sales_eur", 0),
                "Δ Vendite %": item.get("sales_delta_pct"),
                "Margine prec. €": item.get("previous_margin_eur"),
                "Δ Margine %": item.get("margin_delta_pct"),
                "Seller": item.get("seller_names", ""),
                "Marketplace": item.get("marketplaces", ""),
                "Paesi": item.get("countries", ""),
                "Righe margine da verificare": item.get("margin_missing_rows", 0),
            }
        )
    return pd.DataFrame(rows)


def _column_config(frame: pd.DataFrame) -> dict:
    config: dict = {"product_key": None}
    for column in frame.columns:
        if column.endswith("€"):
            config[column] = st.column_config.NumberColumn(column, format="%.2f €")
        elif column.startswith("Δ ") or column == "Margine %":
            config[column] = st.column_config.NumberColumn(column, format="%.2f%%")
        elif column in {"#", "Ordini", "Righe margine da verificare"}:
            config[column] = st.column_config.NumberColumn(column, format="%d")
        elif column in {"Q.tà", "Q.tà periodo prec."}:
            config[column] = st.column_config.NumberColumn(column, format="%.2f")
    return config


def _breakdown_frame(rows: list[dict], dimension: str, label: str) -> pd.DataFrame:
    data = aggregate_dimension(rows, dimension)
    return pd.DataFrame(
        [
            {
                label: item.get(dimension),
                "Q.tà": item.get("quantity", 0),
                "Ordini": item.get("orders", 0),
                "Vendite €": item.get("sales_eur", 0),
                "Prezzo medio €": item.get("average_price_eur", 0),
                "Costo acquisto €": item.get("purchase_cost_eur"),
                "Commissioni €": item.get("commission_eur", 0),
                "Margine utile €": item.get("margin_eur"),
                "Margine %": item.get("margin_pct"),
            }
            for item in data
        ]
    )


_today = datetime.now(ZoneInfo(DEFAULT_DASHBOARD_TIMEZONE)).date()
_period_options = ("Giorno", "Settimana corrente", "Mese corrente", "Intervallo personalizzato")
if st.session_state.get("products_stats_period_mode") not in _period_options:
    st.session_state["products_stats_period_mode"] = "Giorno"

with st.container(border=True):
    st.markdown("#### Periodo e classifica")
    period_mode = st.radio(
        "Periodo",
        _period_options,
        horizontal=True,
        label_visibility="collapsed",
        key="products_stats_period_mode",
    )
    if period_mode == "Giorno":
        selected_day = st.date_input(
            "Giorno da visualizzare",
            value=st.session_state.get("products_stats_selected_day", _today),
            key="products_stats_selected_day",
        )
        selected_from = selected_to = selected_day
        period_label = f"Giorno {selected_day:%d/%m/%Y}"
    elif period_mode == "Settimana corrente":
        selected_from = _today - timedelta(days=_today.weekday())
        selected_to = _today
        period_label = f"Settimana corrente · {selected_from:%d/%m/%Y} - {selected_to:%d/%m/%Y}"
        st.caption(f"Dal {selected_from:%d/%m/%Y} al {selected_to:%d/%m/%Y}.")
    elif period_mode == "Mese corrente":
        selected_from = _today.replace(day=1)
        selected_to = _today
        period_label = f"Mese corrente · {selected_from:%d/%m/%Y} - {selected_to:%d/%m/%Y}"
        st.caption(f"Dal {selected_from:%d/%m/%Y} al {selected_to:%d/%m/%Y}.")
    else:
        date_cols = st.columns(2)
        custom_from = date_cols[0].date_input(
            "Dal",
            value=st.session_state.get("products_stats_custom_from", _today.replace(day=1)),
            key="products_stats_custom_from",
        )
        custom_to = date_cols[1].date_input(
            "Al",
            value=st.session_state.get("products_stats_custom_to", _today),
            key="products_stats_custom_to",
        )
        selected_from, selected_to = min(custom_from, custom_to), max(custom_from, custom_to)
        period_label = f"Intervallo · {selected_from:%d/%m/%Y} - {selected_to:%d/%m/%Y}"
        if custom_from > custom_to:
            st.info("Le date sono state riordinate automaticamente.")

current_all_rows = dashboard_detail_rows(
    selected_from=selected_from,
    selected_to=selected_to,
    timezone_name=DEFAULT_DASHBOARD_TIMEZONE,
)

if not current_all_rows:
    st.info("Nessun ordine contabile disponibile nel periodo selezionato.")
    st.stop()

seller_options = sorted(
    {(int(item.get("seller_id") or 0), str(item.get("seller_name") or "")) for item in current_all_rows},
    key=lambda item: item[1].casefold(),
)
marketplace_options = sorted({str(item.get("marketplace") or "") for item in current_all_rows if item.get("marketplace")}, key=str.casefold)
country_options = sorted({str(item.get("country_code") or "").upper() for item in current_all_rows if item.get("country_code")})
supplier_options = sorted({str(item.get("supplier") or "") for item in current_all_rows if item.get("supplier")}, key=str.casefold)

with st.container(border=True):
    first_filters = st.columns(4)
    seller_labels = [f"{name} · ID {seller_id}" for seller_id, name in seller_options]
    selected_seller_labels = first_filters[0].multiselect(
        "Seller",
        seller_labels,
        placeholder="Tutti i Seller",
        key="products_stats_sellers",
    )
    selected_seller_ids = {
        seller_options[seller_labels.index(label)][0]
        for label in selected_seller_labels
        if label in seller_labels
    }
    selected_marketplaces = first_filters[1].multiselect(
        "Marketplace",
        marketplace_options,
        placeholder="Tutti i Marketplace",
        key="products_stats_marketplaces",
    )
    selected_countries = first_filters[2].multiselect(
        "Paese",
        country_options,
        placeholder="Tutti i Paesi",
        key="products_stats_countries",
    )
    selected_suppliers = first_filters[3].multiselect(
        "Fornitore",
        supplier_options,
        placeholder="Tutti i fornitori",
        key="products_stats_suppliers",
    )

    second_filters = st.columns([2.2, 1.25, 0.8])
    search = second_filters[0].text_input(
        "Cerca prodotto, EAN, SKU o fornitore",
        key="products_stats_search",
    )
    ranking_mode = second_filters[1].selectbox(
        "Ordina per",
        ("Più venduti (quantità)", "Maggior fatturato", "Maggior margine"),
        key="products_stats_ranking",
    )
    top_n = second_filters[2].selectbox(
        "Mostra",
        (10, 25, 50, 100, 250),
        index=1,
        key="products_stats_top_n",
    )

current_rows = filter_product_rows(
    current_all_rows,
    seller_ids=selected_seller_ids,
    marketplaces=selected_marketplaces,
    countries=selected_countries,
    suppliers=selected_suppliers,
    search=search,
)
current_stats = aggregate_product_stats(current_rows)

previous_from, previous_to = previous_period_range(selected_from, selected_to)
previous_all_rows = dashboard_detail_rows(
    selected_from=previous_from,
    selected_to=previous_to,
    timezone_name=DEFAULT_DASHBOARD_TIMEZONE,
)
previous_rows = filter_product_rows(
    previous_all_rows,
    seller_ids=selected_seller_ids,
    marketplaces=selected_marketplaces,
    countries=selected_countries,
    suppliers=selected_suppliers,
    search=search,
)
previous_stats = aggregate_product_stats(previous_rows)
current_stats = merge_previous_period(current_stats, previous_stats)
current_stats = sort_product_stats(current_stats, ranking_mode)

current_totals = product_totals(current_stats)
previous_totals = product_totals(previous_stats)
metric_cols = st.columns(4)
metric_cols[0].metric(
    "Prodotti diversi venduti",
    format_number(current_totals["products"]),
    delta=pct_delta(current_totals["products"], previous_totals["products"]),
)
metric_cols[1].metric(
    "Unità vendute",
    format_number(current_totals["quantity"]),
    delta=pct_delta(current_totals["quantity"], previous_totals["quantity"]),
)
metric_cols[2].metric(
    "Fatturato",
    format_euro(current_totals["sales_eur"]),
    delta=pct_delta(current_totals["sales_eur"], previous_totals["sales_eur"]),
)
metric_cols[3].metric(
    "Margine utile",
    format_euro(current_totals["margin_eur"]),
    delta=pct_delta(current_totals["margin_eur"], previous_totals["margin_eur"]),
)

st.caption(
    f"{period_label} · confronto con il periodo precedente di pari durata: "
    f"{previous_from:%d/%m/%Y} - {previous_to:%d/%m/%Y}. "
    "Le unità sono sommate per quantità, non per numero di ordini."
)
review_queue = margin_review_queue(current_rows)
if review_queue["order_count"]:
    order_count = int(review_queue["order_count"])
    row_count = int(review_queue["row_count"])
    order_word = "ordine" if order_count == 1 else "ordini"
    row_word = "riga" if row_count == 1 else "righe"
    st.warning(
        f"Il margine mostrato resta sempre visibile ed è calcolato sulle righe già complete. "
        f"Ordini da verificare: {order_count} ({row_count} {row_word} con dati economici mancanti). "
        "Il valore potrà quindi cambiare dopo la correzione in Contabilità."
    )
    if st.button(
        f"Apri {order_count} {order_word} da verificare in Contabilità →",
        type="primary",
        use_container_width=True,
        key="products_stats_open_accounting_review",
    ):
        route_items = []
        for source in review_queue["items"]:
            item = dict(source)
            order_date = item.get("order_date")
            if isinstance(order_date, (date, datetime)):
                item["order_date"] = order_date.isoformat()
            route_items.append(item)
        st.session_state["accounting_review_route"] = {
            "source": "products_stats",
            "period_from": selected_from.isoformat(),
            "period_to": selected_to.isoformat(),
            "group_index": 0,
            "items": route_items,
        }
        first_item = route_items[0]
        seller_name = str(first_item.get("seller_name") or "").strip()
        seller_id = int(first_item.get("seller_id") or 0)
        if seller_name and seller_id:
            st.session_state["active_seller_id"] = seller_id
            st.session_state["global_seller_selector"] = f"{seller_name}  ·  ID {seller_id}"
        st.switch_page("pages/4_Contabilita.py")

if not current_stats:
    st.info("Nessun prodotto venduto corrisponde ai filtri selezionati.")
    st.stop()

visible_stats = current_stats[: int(top_n)]
frame = _frame_for_stats(visible_stats)

heading_left, heading_right = st.columns([4.7, 1])
heading_left.markdown(f"### Classifica · {ranking_mode}")
heading_left.caption("Seleziona una riga per aprire il dettaglio completo del prodotto.")
heading_right.download_button(
    "Esporta CSV",
    data=frame.drop(columns=["product_key"], errors="ignore").to_csv(index=False).encode("utf-8-sig"),
    file_name=f"prodotti_piu_venduti_{selected_from:%Y%m%d}_{selected_to:%Y%m%d}.csv",
    mime="text/csv",
    use_container_width=True,
)

event = st.dataframe(
    frame,
    use_container_width=True,
    hide_index=True,
    height=min(780, 94 + 35 * max(8, min(len(frame), 20))),
    column_config=_column_config(frame),
    on_select="rerun",
    selection_mode="single-row",
    key="products_stats_table",
)
selected_indices = _selected_indices(event)
if selected_indices:
    index = selected_indices[0]
    if 0 <= index < len(visible_stats):
        st.session_state["products_stats_selected_product"] = visible_stats[index]["product_key"]

selected_key = str(st.session_state.get("products_stats_selected_product") or "")
selected_summary = next((item for item in current_stats if item.get("product_key") == selected_key), None)
if selected_key and selected_summary is None:
    st.session_state.pop("products_stats_selected_product", None)
    selected_key = ""

if selected_summary:
    selected_product_rows = product_rows(current_rows, selected_key)
    with st.container(border=True):
        title_col, close_col = st.columns([5.2, 1])
        title_col.markdown(f"### {_safe_title(selected_summary.get('product_title'))}")
        identity_parts = []
        if selected_summary.get("ean"):
            identity_parts.append(f"EAN {selected_summary['ean']}")
        if selected_summary.get("composite_sku"):
            identity_parts.append(f"SKU {selected_summary['composite_sku']}")
        if selected_summary.get("suppliers"):
            identity_parts.append(f"Fornitore {selected_summary['suppliers']}")
        title_col.caption(" · ".join(identity_parts) or "Dettaglio prodotto")
        if close_col.button("Chiudi dettaglio", use_container_width=True, key="products_stats_close_detail"):
            st.session_state.pop("products_stats_selected_product", None)
            st.rerun()

        product_metrics = st.columns(5)
        product_metrics[0].metric("Unità", format_number(selected_summary.get("quantity")))
        product_metrics[1].metric("Ordini", format_number(selected_summary.get("orders")))
        product_metrics[2].metric("Fatturato", format_euro(selected_summary.get("sales_eur")))
        product_metrics[3].metric("Prezzo medio", format_euro(selected_summary.get("average_price_eur")))
        product_metrics[4].metric("Margine utile", format_euro(selected_summary.get("margin_eur")))

        second_metrics = st.columns(5)
        second_metrics[0].metric("Costo acquisto", format_euro(selected_summary.get("purchase_cost_eur")))
        second_metrics[1].metric("Commissioni", format_euro(selected_summary.get("commission_eur")))
        second_metrics[2].metric("Da ricevere", format_euro(selected_summary.get("payout_eur")))
        second_metrics[3].metric("Costi extra", format_euro(selected_summary.get("extra_cost_eur")))
        margin_pct = selected_summary.get("margin_pct")
        second_metrics[4].metric("Margine %", "Da verificare" if margin_pct is None else f"{float(margin_pct):.2f}%")

        daily = _breakdown_frame(selected_product_rows, "order_date", "Data")
        if not daily.empty:
            daily = daily.sort_values("Data")
            chart_cols = st.columns(2)
            with chart_cols[0]:
                st.markdown("#### Andamento unità")
                units_chart = daily[["Data", "Q.tà"]].copy().set_index("Data")
                st.bar_chart(units_chart)
            with chart_cols[1]:
                st.markdown("#### Andamento economico")
                economy_chart = daily[["Data", "Vendite €", "Margine utile €"]].copy().set_index("Data")
                st.line_chart(economy_chart)

        seller_tab, market_tab, country_tab, supplier_tab, orders_tab = st.tabs(
            ["Per Seller", "Per Marketplace", "Per Paese", "Per Fornitore", "Ordini"]
        )
        breakdown_specs = (
            (seller_tab, "seller_name", "Seller"),
            (market_tab, "marketplace", "Marketplace"),
            (country_tab, "country_code", "Paese"),
            (supplier_tab, "supplier", "Fornitore"),
        )
        for tab, dimension, label in breakdown_specs:
            with tab:
                breakdown = _breakdown_frame(selected_product_rows, dimension, label)
                if breakdown.empty:
                    st.info("Nessun dato disponibile.")
                else:
                    st.dataframe(
                        breakdown,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            column: st.column_config.NumberColumn(column, format="%.2f €")
                            for column in breakdown.columns
                            if column.endswith("€")
                        }
                        | ({"Margine %": st.column_config.NumberColumn("Margine %", format="%.2f%%")} if "Margine %" in breakdown.columns else {}),
                    )

        with orders_tab:
            order_data = aggregate_product_orders(selected_product_rows)
            orders_frame = pd.DataFrame(
                [
                    {
                        "Data": item.get("order_date"),
                        "N. ordine": item.get("order_id", ""),
                        "Seller": item.get("seller_name", ""),
                        "Marketplace": item.get("marketplace", ""),
                        "Paese": item.get("country_code", ""),
                        "Fornitore": item.get("supplier", ""),
                        "Q.tà": item.get("quantity", 0),
                        "Vendita €": item.get("sales_eur", 0),
                        "Commissione €": item.get("commission_eur", 0),
                        "Costo acquisto €": item.get("purchase_cost_eur"),
                        "Margine utile €": item.get("margin_eur"),
                    }
                    for item in order_data
                ]
            )
            st.dataframe(
                orders_frame,
                use_container_width=True,
                hide_index=True,
                column_config={
                    column: st.column_config.NumberColumn(column, format="%.2f €")
                    for column in orders_frame.columns
                    if column.endswith("€")
                },
            )
