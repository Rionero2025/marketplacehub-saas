from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape

import pandas as pd
import streamlit as st

from services.accounting import ensure_schema
from services.db import rows
from services.dashboard import (
    DEFAULT_DASHBOARD_TIMEZONE,
    combined_dashboard_period,
    dashboard_missing_detail_rows,
    dashboard_order_detail_rows,
    dashboard_snapshot,
    dashboard_sync_in_progress,
    dashboard_sync_state,
    ensure_dashboard_sync_schema,
    start_dashboard_sync_background,
)
from services.product_stats import aggregate_product_stats, filter_product_rows, sort_product_stats
from services.session import bootstrap
from services.security import (
    active_master_key,
    clear_runtime_master_key,
    runtime_master_key_active,
    set_runtime_master_key,
    validate_master_key,
)


bootstrap()
ensure_schema()
ensure_dashboard_sync_schema()

st.title("Dashboard")
st.caption(
    "Panoramica dinamica di ordini, vendite e guadagno di tutti i Seller. "
    "Gli importi seguono le stesse regole della Contabilità."
)

from zoneinfo import ZoneInfo

_dashboard_today = datetime.now(ZoneInfo(DEFAULT_DASHBOARD_TIMEZONE)).date()
_period_options = (
    "Giorno",
    "Settimana corrente",
    "Mese corrente",
    "Intervallo personalizzato",
)
with st.container(border=True):
    st.markdown("#### Periodo della Dashboard")
    dashboard_period_mode = st.radio(
        "Seleziona il periodo",
        _period_options,
        horizontal=True,
        label_visibility="collapsed",
        key="dashboard_period_mode",
    )
    if dashboard_period_mode == "Giorno":
        dashboard_day = st.date_input(
            "Giorno da visualizzare",
            value=_dashboard_today,
            key="dashboard_selected_day",
        )
        selected_date_from = dashboard_day
        selected_date_to = dashboard_day
        selected_period_label = dashboard_day.strftime("Giorno %d/%m/%Y")
    elif dashboard_period_mode == "Settimana corrente":
        selected_date_from = _dashboard_today - timedelta(days=_dashboard_today.weekday())
        selected_date_to = _dashboard_today
        selected_period_label = (
            f"Settimana corrente · {selected_date_from:%d/%m/%Y} - "
            f"{selected_date_to:%d/%m/%Y}"
        )
        st.caption(
            f"Dal {selected_date_from:%d/%m/%Y} al {selected_date_to:%d/%m/%Y}."
        )
    elif dashboard_period_mode == "Mese corrente":
        selected_date_from = _dashboard_today.replace(day=1)
        selected_date_to = _dashboard_today
        selected_period_label = (
            f"Mese corrente · {selected_date_from:%d/%m/%Y} - "
            f"{selected_date_to:%d/%m/%Y}"
        )
        st.caption(
            f"Dal {selected_date_from:%d/%m/%Y} al {selected_date_to:%d/%m/%Y}."
        )
    else:
        custom_left, custom_right = st.columns(2)
        custom_from = custom_left.date_input(
            "Dal",
            value=_dashboard_today.replace(day=1),
            key="dashboard_custom_from",
        )
        custom_to = custom_right.date_input(
            "Al",
            value=_dashboard_today,
            key="dashboard_custom_to",
        )
        selected_date_from = min(custom_from, custom_to)
        selected_date_to = max(custom_from, custom_to)
        selected_period_label = (
            f"Intervallo personalizzato · {selected_date_from:%d/%m/%Y} - "
            f"{selected_date_to:%d/%m/%Y}"
        )
        if custom_from > custom_to:
            st.info("Le date sono state riordinate automaticamente.")


def format_euro(value: float | int | None) -> str:
    number = float(value or 0)
    formatted = f"{abs(number):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    sign = "−" if number < 0 else ""
    return f"{sign}{formatted} €"


def format_integer(value: int | float | None) -> str:
    return f"{int(value or 0):,}".replace(",", ".")


def seller_initials(value: str) -> str:
    parts = [part for part in str(value or "").strip().split() if part]
    if not parts:
        return "S"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


def format_sync_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "Mai sincronizzato"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        # Avoid an extra dependency here; the page already uses Europe/Rome in services.
        from zoneinfo import ZoneInfo

        local = parsed.astimezone(ZoneInfo(DEFAULT_DASHBOARD_TIMEZONE))
        return local.strftime("%d/%m/%Y · %H:%M")
    except Exception:
        return escape(text[:19])


DASHBOARD_DETAIL_TITLES = {
    "bebol": "Dettaglio guadagno BEBOL",
    "orders": "Dettaglio ordini nell’intervallo",
    "sales": "Dettaglio vendite nell’intervallo",
    "profit": "Dettaglio margine utile nell’intervallo",
    "partner": "Dettaglio quota complessiva dei Seller",
    "missing": "Righe da verificare",
    "period": "Dettaglio periodo selezionato",
}


def _dashboard_card_label(title: str, value: str, note: str) -> str:
    return f"{title}  \n**{value}**  \n{note} · Apri dettaglio →"


def _dashboard_top_products_html(products: list[dict], period_label: str) -> str:
    total_quantity = 0.0
    total_sales = 0.0
    total_margin = 0.0
    partial_rows = 0
    ranking_rows: list[str] = []

    for position, item in enumerate(products[:10], start=1):
        title = " ".join(str(item.get("product_title") or "Prodotto senza nome").split())
        short_title = title if len(title) <= 66 else title[:65].rstrip() + "…"
        quantity = _dashboard_quantity(item.get("quantity"))
        quantity_value = float(quantity or 0)
        total_quantity += quantity_value
        total_sales += float(item.get("sales_eur") or 0)
        total_margin += float(item.get("margin_eur") or 0)
        missing_rows = int(item.get("margin_missing_rows") or 0)
        partial_rows += missing_rows
        margin_value = format_euro(item.get("margin_eur"))
        margin_class = "dashboard-top-products-row-margin is-negative" if float(item.get("margin_eur") or 0) < 0 else "dashboard-top-products-row-margin"
        margin_note = '<span class="dashboard-top-products-row-flag">Da verificare</span>' if missing_rows else ""
        quantity_text = format_integer(quantity) if isinstance(quantity, int) else str(quantity).replace(".", ",")
        ranking_rows.append(
            '<li class="dashboard-top-products-row">'
            f'<div class="dashboard-top-products-rank">{position}</div>'
            '<div class="dashboard-top-products-row-main">'
            f'<div class="dashboard-top-products-row-title">{escape(short_title)}</div>'
            '<div class="dashboard-top-products-row-meta">'
            f'<span>{escape(quantity_text)} pz</span>'
            f'<span>{escape(format_euro(item.get("sales_eur")))}</span>'
            f'<span class="{margin_class}">{escape(margin_value)}</span>'
            f'{margin_note}'
            '</div>'
            '</div>'
            '</li>'
        )

    hero_title = "Top 10 prodotti più venduti"
    hero_subtitle = (
        "Classifica aggiornata dal periodo selezionato nella Dashboard. "
        "Apri il dettaglio completo per statistiche, filtri ed export CSV."
    )
    top_name = "Nessun prodotto"
    top_quantity = "0"
    if products:
        first_title = " ".join(str(products[0].get("product_title") or "Prodotto senza nome").split())
        top_name = first_title if len(first_title) <= 56 else first_title[:55].rstrip() + "…"
        top_first_quantity = _dashboard_quantity(products[0].get("quantity"))
        top_quantity = format_integer(top_first_quantity) if isinstance(top_first_quantity, int) else str(top_first_quantity).replace(".", ",")

    quantity_total_text = format_integer(int(total_quantity)) if float(total_quantity).is_integer() else str(round(total_quantity, 2)).replace(".", ",")
    partial_badge = (
        f'<div class="dashboard-top-products-alert">Ci sono <strong>{format_integer(partial_rows)}</strong> righe con margine parziale da verificare in Contabilità.</div>'
        if partial_rows
        else '<div class="dashboard-top-products-alert is-success">Margini completi per i prodotti mostrati.</div>'
    )
    ranking_html = "".join(ranking_rows) if ranking_rows else '<li class="dashboard-top-products-empty">Nessun prodotto venduto nel periodo selezionato.</li>'

    return (
        '<section class="dashboard-top-products-shell">'
        '<div class="dashboard-top-products-hero">'
        '<div>'
        f'<div class="dashboard-top-products-eyebrow">Migliori vendite · {escape(period_label)}</div>'
        f'<div class="dashboard-top-products-title">{hero_title}</div>'
        f'<div class="dashboard-top-products-subtitle">{hero_subtitle}</div>'
        '</div>'
        '<div class="dashboard-top-products-highlight">'
        '<div class="dashboard-top-products-highlight-label">#1 del periodo</div>'
        f'<div class="dashboard-top-products-highlight-title">{escape(top_name)}</div>'
        f'<div class="dashboard-top-products-highlight-value">{escape(top_quantity)} pz</div>'
        '</div>'
        '</div>'
        '<div class="dashboard-top-products-kpis">'
        '<div class="dashboard-top-products-kpi">'
        '<div class="dashboard-top-products-kpi-label">Unità nella Top 10</div>'
        f'<div class="dashboard-top-products-kpi-value">{escape(quantity_total_text)}</div>'
        '</div>'
        '<div class="dashboard-top-products-kpi">'
        '<div class="dashboard-top-products-kpi-label">Fatturato Top 10</div>'
        f'<div class="dashboard-top-products-kpi-value">{escape(format_euro(total_sales))}</div>'
        '</div>'
        '<div class="dashboard-top-products-kpi">'
        '<div class="dashboard-top-products-kpi-label">Margine Top 10</div>'
        f'<div class="dashboard-top-products-kpi-value">{escape(format_euro(total_margin))}</div>'
        '</div>'
        '</div>'
        f'{partial_badge}'
        '<ol class="dashboard-top-products-list">'
        f'{ranking_html}'
        '</ol>'
        '</section>'
    )

def _dashboard_date_label(value: object) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return clean_text(value) if "clean_text" in globals() else str(value or "")


def _dashboard_quantity(value: object) -> int | float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(number) if number.is_integer() else number


def _dashboard_detail_dataframe(
    detail_key: str,
    detail_rows: list[dict],
    summaries: list[dict],
) -> pd.DataFrame:
    if detail_key == "orders":
        output = []
        for item in dashboard_order_detail_rows(detail_rows):
            output.append({
                "Data": _dashboard_date_label(item.get("order_date")),
                "Seller": item.get("seller_name", ""),
                "Marketplace": item.get("marketplace", ""),
                "N. ordine": item.get("order_id", ""),
                "Stato": item.get("status", ""),
                "Prodotti": int(item.get("products") or 0),
                "Vendita €": item.get("sale_eur"),
                "Margine utile €": item.get("net_revenue_eur"),
                "Quota BEBOL €": item.get("our_share_eur"),
                "Quota Seller €": item.get("partner_share_eur"),
                "Righe da verificare": int(item.get("missing_profit_rows") or 0),
            })
        return pd.DataFrame(output)

    if detail_key == "period":
        output = []
        for seller in summaries:
            values = seller.get("periods", {}).get("selected", {})
            output.append({
                "Seller": seller.get("seller_name", ""),
                "Ordini": int(values.get("orders") or 0),
                "Vendite €": float(values.get("sales") or 0),
                "Margine utile €": float(values.get("profit") or 0),
                "% BEBOL": float(values.get("our_pct") or 0),
                "Quota BEBOL €": float(values.get("our_amount") or 0),
                "% Seller": float(values.get("partner_pct") or 0),
                "Quota Seller €": float(values.get("partner_amount") or 0),
                "Da verificare": int(values.get("missing_profit_rows") or 0),
            })
        return pd.DataFrame(output)

    values = detail_rows
    if detail_key == "sales":
        values = [item for item in detail_rows if abs(float(item.get("sale_eur") or 0)) >= 0.005]
    elif detail_key in {"profit", "bebol", "partner"}:
        values = [
            item for item in detail_rows
            if item.get("net_revenue_eur") is not None
            and (
                abs(float(item.get("net_revenue_eur") or 0)) >= 0.005
                or abs(float(item.get("sale_eur") or 0)) >= 0.005
            )
        ]
    elif detail_key == "missing":
        values = dashboard_missing_detail_rows(detail_rows)

    output = []
    for item in values:
        row = {
            "Data": _dashboard_date_label(item.get("order_date")),
            "Seller": item.get("seller_name", ""),
            "Marketplace": item.get("marketplace", ""),
            "N. ordine": item.get("order_id", ""),
            "Stato": item.get("status", ""),
            "Fornitore": item.get("supplier", ""),
            "Prodotto": item.get("product_title", ""),
            "EAN": item.get("ean", ""),
            "Q.tà": _dashboard_quantity(item.get("quantity")),
        }
        if detail_key == "sales":
            row.update({
                "Vendita €": item.get("sale_eur"),
                "Commissione €": item.get("commission_eur"),
                "Rimborso €": item.get("refund_eur"),
                "Da ricevere €": item.get("payout_eur"),
            })
        elif detail_key == "missing":
            row.update({
                "Vendita €": item.get("sale_eur"),
                "Costo acquisto €": item.get("purchase_cost_eur"),
                "Da ricevere €": item.get("payout_eur"),
                "Motivo verifica": item.get("missing_reason", "Margine non determinabile"),
            })
        else:
            row.update({
                "Vendita €": item.get("sale_eur"),
                "Costo acquisto €": item.get("purchase_cost_eur"),
                "Commissione €": item.get("commission_eur"),
                "Da ricevere €": item.get("payout_eur"),
                "Costo extra €": item.get("extra_cost_eur"),
                "Margine utile €": item.get("net_revenue_eur"),
                "% BEBOL": item.get("our_pct"),
                "Quota BEBOL €": item.get("our_share_eur"),
                "% Seller": item.get("partner_pct"),
                "Quota Seller €": item.get("partner_share_eur"),
            })
        output.append(row)
    return pd.DataFrame(output)


def _dashboard_detail_column_config(frame: pd.DataFrame) -> dict:
    config: dict = {}
    for column in frame.columns:
        if column.endswith("€"):
            config[column] = st.column_config.NumberColumn(column, format="%.2f €")
        elif column.startswith("%"):
            config[column] = st.column_config.NumberColumn(column, format="%.2f%%")
        elif column in {"Ordini", "Prodotti", "Righe da verificare", "Da verificare"}:
            config[column] = st.column_config.NumberColumn(column, format="%d")
    return config


st.markdown(
    """
<style>
:root {
    --dash-border: rgba(128, 128, 128, 0.20);
    --dash-muted: rgba(128, 128, 128, 0.80);
    --dash-success: #16833b;
    --dash-danger: #c53b34;
}
.dashboard-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: .8rem;
    padding: .75rem 1rem;
    border: 1px solid var(--dash-border);
    border-radius: 14px;
    background: var(--secondary-background-color);
    margin: .35rem 0 1rem 0;
}
.dashboard-live {
    display: inline-flex;
    align-items: center;
    gap: .5rem;
    font-size: .82rem;
    color: var(--text-color);
}
.dashboard-live-dot {
    width: .58rem;
    height: .58rem;
    border-radius: 999px;
    background: var(--primary-color);
    box-shadow: 0 0 0 .25rem color-mix(in srgb, var(--primary-color) 16%, transparent);
}
.dashboard-live-dot.is-syncing {
    animation: dashboard-pulse 1.2s ease-in-out infinite;
}
@keyframes dashboard-pulse {
    0%, 100% { opacity: .45; transform: scale(.9); }
    50% { opacity: 1; transform: scale(1.08); }
}
.dashboard-live-detail { color: var(--dash-muted); font-size: .76rem; }
.dashboard-summary-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: .8rem;
    margin: .25rem 0 1.15rem 0;
}
.dashboard-summary-card {
    position: relative;
    overflow: hidden;
    border: 1px solid var(--dash-border);
    border-radius: 16px;
    padding: .95rem 1rem;
    background: var(--secondary-background-color);
}
.dashboard-summary-card::before {
    content: "";
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 4px;
    background: var(--primary-color);
}
.dashboard-summary-label { color: var(--dash-muted); font-size: .78rem; }
.dashboard-summary-value {
    color: var(--text-color);
    font-size: 1.45rem;
    font-weight: 750;
    font-variant-numeric: tabular-nums;
    margin-top: .18rem;
}
.dashboard-summary-note { color: var(--dash-muted); font-size: .72rem; margin-top: .15rem; }
/* v264: le card riepilogative sono veri controlli Streamlit, quindi il click
   resta gestito dal runtime e conserva sessione, filtri e aggiornamento fragment. */
[class*="st-key-dashboard_card_"] button {
    width: 100%;
    min-height: 112px;
    justify-content: flex-start;
    align-items: flex-start;
    text-align: left;
    border: 1px solid var(--dash-border);
    border-radius: 16px;
    padding: .9rem 1rem;
    background: var(--secondary-background-color);
    box-shadow: none;
    transition: border-color .16s ease, transform .16s ease, box-shadow .16s ease;
}
[class*="st-key-dashboard_card_"] button:hover {
    border-color: color-mix(in srgb, var(--primary-color) 62%, var(--dash-border));
    transform: translateY(-1px);
    box-shadow: 0 7px 18px rgba(0, 0, 0, 0.06);
}
[class*="st-key-dashboard_card_"] button p {
    width: 100%;
    margin: 0;
    color: var(--text-color);
    white-space: pre-line;
    text-align: left;
    line-height: 1.34;
    font-size: .76rem;
}
[class*="st-key-dashboard_card_"] button p strong {
    display: inline-block;
    margin: .16rem 0 .08rem 0;
    font-size: 1.42rem;
    line-height: 1.08;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
}
.st-key-dashboard_card_period button p strong {
    font-size: .98rem;
    line-height: 1.25;
}
.st-key-dashboard_card_bebol button {
    min-height: 124px;
    border-color: color-mix(in srgb, var(--primary-color) 45%, var(--dash-border));
    background: color-mix(in srgb, var(--primary-color) 8%, var(--secondary-background-color));
}
.st-key-dashboard_card_bebol button p strong {
    color: var(--primary-color);
    font-size: 1.75rem;
}
.dashboard-top-products-shell {
    position: relative;
    overflow: hidden;
    border: 1px solid color-mix(in srgb, var(--primary-color) 34%, var(--dash-border));
    border-radius: 22px;
    padding: 1.15rem;
    margin: .35rem 0 .75rem 0;
    background:
        radial-gradient(circle at top right, color-mix(in srgb, var(--primary-color) 10%, transparent), transparent 34%),
        linear-gradient(180deg, color-mix(in srgb, var(--primary-color) 4%, var(--secondary-background-color)), var(--secondary-background-color));
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.05);
}
.dashboard-top-products-hero {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: .9rem;
}
.dashboard-top-products-eyebrow {
    display: inline-flex;
    align-items: center;
    gap: .38rem;
    border-radius: 999px;
    padding: .28rem .62rem;
    background: color-mix(in srgb, var(--primary-color) 10%, transparent);
    color: var(--primary-color);
    font-size: .69rem;
    font-weight: 760;
    letter-spacing: .02em;
    margin-bottom: .55rem;
}
.dashboard-top-products-title {
    font-size: 1.34rem;
    line-height: 1.16;
    font-weight: 800;
    margin-bottom: .22rem;
}
.dashboard-top-products-subtitle {
    color: var(--dash-muted);
    font-size: .78rem;
    max-width: 48rem;
}
.dashboard-top-products-highlight {
    min-width: min(18rem, 100%);
    border: 1px solid color-mix(in srgb, var(--primary-color) 34%, var(--dash-border));
    border-radius: 18px;
    padding: .9rem 1rem;
    background: color-mix(in srgb, var(--primary-color) 8%, var(--secondary-background-color));
}
.dashboard-top-products-highlight-label {
    color: var(--dash-muted);
    font-size: .7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .03em;
}
.dashboard-top-products-highlight-title {
    margin-top: .28rem;
    font-size: .94rem;
    font-weight: 760;
    line-height: 1.32;
}
.dashboard-top-products-highlight-value {
    margin-top: .45rem;
    font-size: 1.35rem;
    font-weight: 800;
    color: var(--primary-color);
}
.dashboard-top-products-kpis {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: .75rem;
    margin-bottom: .8rem;
}
.dashboard-top-products-kpi {
    border: 1px solid var(--dash-border);
    border-radius: 16px;
    padding: .82rem .9rem;
    background: color-mix(in srgb, var(--secondary-background-color) 94%, var(--background-color));
}
.dashboard-top-products-kpi-label {
    color: var(--dash-muted);
    font-size: .72rem;
}
.dashboard-top-products-kpi-value {
    margin-top: .2rem;
    font-size: 1.18rem;
    font-weight: 790;
    font-variant-numeric: tabular-nums;
}
.dashboard-top-products-alert {
    border: 1px solid color-mix(in srgb, #d69b00 35%, var(--dash-border));
    border-radius: 14px;
    padding: .72rem .85rem;
    background: color-mix(in srgb, #d69b00 8%, var(--secondary-background-color));
    font-size: .76rem;
    margin-bottom: .8rem;
}
.dashboard-top-products-alert.is-success {
    border-color: color-mix(in srgb, var(--dash-success) 30%, var(--dash-border));
    background: color-mix(in srgb, var(--dash-success) 7%, var(--secondary-background-color));
}
.dashboard-top-products-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: .58rem;
}
.dashboard-top-products-row {
    display: flex;
    align-items: flex-start;
    gap: .72rem;
    border: 1px solid var(--dash-border);
    border-radius: 16px;
    padding: .72rem .82rem;
    background: color-mix(in srgb, var(--secondary-background-color) 92%, var(--background-color));
}
.dashboard-top-products-rank {
    display: grid;
    place-items: center;
    width: 2rem;
    height: 2rem;
    flex: 0 0 2rem;
    border-radius: 999px;
    background: color-mix(in srgb, var(--primary-color) 13%, transparent);
    color: var(--primary-color);
    font-weight: 800;
    font-size: .82rem;
}
.dashboard-top-products-row-main { min-width: 0; flex: 1 1 auto; }
.dashboard-top-products-row-title {
    font-size: .84rem;
    font-weight: 720;
    line-height: 1.32;
    overflow-wrap: anywhere;
}
.dashboard-top-products-row-meta {
    display: flex;
    flex-wrap: wrap;
    gap: .38rem;
    margin-top: .38rem;
}
.dashboard-top-products-row-meta span {
    display: inline-flex;
    align-items: center;
    border-radius: 999px;
    padding: .2rem .48rem;
    font-size: .69rem;
    background: color-mix(in srgb, var(--primary-color) 7%, transparent);
}
.dashboard-top-products-row-margin {
    color: var(--dash-success);
    font-weight: 760;
}
.dashboard-top-products-row-margin.is-negative { color: var(--dash-danger); }
.dashboard-top-products-row-flag {
    background: color-mix(in srgb, #d69b00 10%, transparent) !important;
    color: #8a6400;
    font-weight: 760;
}
.dashboard-top-products-empty {
    border: 1px dashed var(--dash-border);
    border-radius: 16px;
    padding: 1rem;
    color: var(--dash-muted);
    text-align: center;
}
[class*="st-key-dashboard_top_products_open"] button {
    min-height: 52px;
    border-radius: 999px;
    border: 1px solid color-mix(in srgb, var(--primary-color) 48%, var(--dash-border));
    background: color-mix(in srgb, var(--primary-color) 9%, var(--secondary-background-color));
    font-weight: 760;
    box-shadow: none;
    transition: border-color .16s ease, transform .16s ease, box-shadow .16s ease;
}
[class*="st-key-dashboard_top_products_open"] button:hover {
    border-color: color-mix(in srgb, var(--primary-color) 78%, var(--dash-border));
    transform: translateY(-1px);
    box-shadow: 0 8px 18px rgba(0, 0, 0, 0.06);
}
.dashboard-detail-shell {
    border-top: 1px solid var(--dash-border);
    margin-top: .35rem;
    padding-top: .35rem;
}
.seller-dashboard-card {
    border: 1px solid var(--dash-border);
    border-radius: 20px;
    padding: 1rem;
    margin-bottom: 1rem;
    background: var(--secondary-background-color);
    box-shadow: 0 8px 26px rgba(0, 0, 0, 0.055);
    overflow: hidden;
}
.seller-dashboard-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: .8rem;
    padding: .15rem .15rem .9rem .15rem;
}
.seller-dashboard-identity { display: flex; align-items: center; gap: .72rem; min-width: 0; }
.seller-dashboard-avatar {
    display: grid;
    place-items: center;
    width: 2.65rem;
    height: 2.65rem;
    flex: 0 0 2.65rem;
    border-radius: 14px;
    background: color-mix(in srgb, var(--primary-color) 14%, var(--secondary-background-color));
    color: var(--primary-color);
    font-weight: 800;
    letter-spacing: .02em;
}
.seller-dashboard-title {
    font-size: 1.18rem;
    font-weight: 760;
    line-height: 1.2;
    overflow-wrap: anywhere;
}
.seller-dashboard-subtitle { color: var(--dash-muted); font-size: .72rem; margin-top: .18rem; }
.seller-dashboard-sync {
    color: var(--dash-muted);
    font-size: .69rem;
    text-align: right;
    white-space: nowrap;
}
.seller-dashboard-periods {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: .72rem;
}
.seller-dashboard-period {
    border: 1px solid var(--dash-border);
    border-radius: 15px;
    padding: .82rem .88rem .78rem .88rem;
    background: color-mix(in srgb, var(--secondary-background-color) 92%, var(--background-color));
}
.seller-dashboard-period.is-today {
    border-color: color-mix(in srgb, var(--primary-color) 50%, var(--dash-border));
    background: color-mix(in srgb, var(--primary-color) 6%, var(--secondary-background-color));
}
.seller-dashboard-period-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: .5rem;
    margin-bottom: .72rem;
}
.seller-dashboard-period-label { font-size: .82rem; font-weight: 650; }
.seller-dashboard-order-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 2.25rem;
    padding: .24rem .48rem;
    border-radius: 999px;
    background: color-mix(in srgb, var(--primary-color) 13%, transparent);
    color: var(--primary-color);
    font-size: .72rem;
    font-weight: 750;
    white-space: nowrap;
}
.seller-dashboard-metrics {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: .7rem;
}
.seller-dashboard-metric + .seller-dashboard-metric {
    border-left: 1px solid var(--dash-border);
    padding-left: .7rem;
}
.seller-dashboard-split {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: .7rem;
    border-top: 1px solid var(--dash-border);
    margin-top: .72rem;
    padding-top: .68rem;
}
.seller-dashboard-share {
    border-radius: 10px;
    padding: .5rem .58rem;
    background: color-mix(in srgb, var(--primary-color) 5%, transparent);
}
.seller-dashboard-share + .seller-dashboard-share {
    background: color-mix(in srgb, var(--dash-success) 5%, transparent);
}
.seller-dashboard-share-label {
    color: var(--dash-muted);
    font-size: .64rem;
    line-height: 1.25;
    min-height: 1.65rem;
}
.seller-dashboard-share-value {
    font-size: .95rem;
    font-weight: 760;
    margin-top: .12rem;
    font-variant-numeric: tabular-nums;
}
.seller-dashboard-share-our { color: var(--primary-color); }
.seller-dashboard-share-partner { color: var(--dash-success); }
.seller-dashboard-split-badge {
    display: inline-flex;
    align-items: center;
    gap: .28rem;
    border-radius: 999px;
    padding: .22rem .48rem;
    background: color-mix(in srgb, var(--primary-color) 10%, transparent);
    color: var(--primary-color);
    font-size: .66rem;
    font-weight: 700;
    white-space: nowrap;
}
.seller-dashboard-label { color: var(--dash-muted); font-size: .69rem; line-height: 1.25; }
.seller-dashboard-value {
    color: var(--text-color);
    font-size: 1.08rem;
    font-weight: 740;
    line-height: 1.35;
    margin-top: .18rem;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
}
.seller-dashboard-profit-positive { color: var(--dash-success); }
.seller-dashboard-profit-negative { color: var(--dash-danger); }
.seller-dashboard-footer {
    display: flex;
    justify-content: space-between;
    gap: .75rem;
    border-top: 1px solid var(--dash-border);
    color: var(--dash-muted);
    font-size: .7rem;
    margin-top: .85rem;
    padding: .75rem .12rem .05rem .12rem;
}
.dashboard-bebol-card {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border: 1px solid color-mix(in srgb, var(--primary-color) 45%, var(--dash-border));
    border-radius: 18px;
    padding: 1rem 1.1rem;
    margin: .2rem 0 1rem 0;
    background: color-mix(in srgb, var(--primary-color) 8%, var(--secondary-background-color));
}
.dashboard-bebol-title { font-size: .82rem; color: var(--dash-muted); }
.dashboard-bebol-value {
    font-size: 1.8rem;
    font-weight: 800;
    color: var(--primary-color);
    font-variant-numeric: tabular-nums;
}
.dashboard-bebol-period {
    text-align: right;
    color: var(--dash-muted);
    font-size: .74rem;
}
.seller-dashboard-period.is-selected {
    grid-column: 1 / -1;
    border-color: color-mix(in srgb, var(--primary-color) 58%, var(--dash-border));
    background: color-mix(in srgb, var(--primary-color) 8%, var(--secondary-background-color));
}
.dashboard-warning {
    border: 1px solid color-mix(in srgb, #d69b00 35%, var(--dash-border));
    border-radius: 12px;
    padding: .7rem .85rem;
    color: var(--text-color);
    background: color-mix(in srgb, #d69b00 7%, var(--secondary-background-color));
    font-size: .76rem;
    margin-bottom: .9rem;
}
@media (max-width: 900px) {
    .dashboard-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .dashboard-top-products-hero { flex-direction: column; }
    .dashboard-top-products-highlight { min-width: 0; width: 100%; }
    .dashboard-top-products-kpis { grid-template-columns: 1fr; }
}
@media (max-width: 680px) {
    .dashboard-toolbar { align-items: flex-start; flex-direction: column; }
    .dashboard-summary-grid { grid-template-columns: 1fr 1fr; }
    .seller-dashboard-periods { grid-template-columns: 1fr; }
    .seller-dashboard-sync { display: none; }
    .dashboard-bebol-card { align-items: flex-start; flex-direction: column; }
    .dashboard-bebol-period { text-align: left; }
    .dashboard-top-products-shell { padding: 1rem; }
    .dashboard-top-products-row { padding: .68rem .72rem; }
}
</style>
    """,
    unsafe_allow_html=True,
)

control_auto, control_sync, control_button = st.columns([1.2, 1.55, 1])
auto_refresh = control_auto.toggle(
    "Aggiornamento automatico",
    value=True,
    help="Rilegge la Dashboard ogni 30 secondi senza ricaricare tutta la pagina.",
    key="dashboard_auto_refresh",
)
auto_api_sync = control_sync.toggle(
    "Sincronizza nuovi ordini dalle API",
    value=True,
    help="Avvia in background un controllo leggero degli ultimi 7 giorni ogni 5 minuti. La Dashboard non resta in attesa delle API.",
    key="dashboard_auto_api_sync",
)
force_sync_clicked = control_button.button(
    "Aggiorna adesso",
    type="primary",
    use_container_width=True,
    key="dashboard_force_refresh",
)
if force_sync_clicked:
    launch = start_dashboard_sync_background(force=True)
    if launch.get("started"):
        st.toast("Sincronizzazione avviata in background. La Dashboard resta utilizzabile.")
    elif launch.get("reason") == "running":
        st.info("Una sincronizzazione API è già in corso in background.")
    elif launch.get("reason") == "not_due":
        st.info("I dati sono già stati sincronizzati di recente.")
    else:
        st.warning("Non è stato possibile avviare la sincronizzazione API.")

run_every = "30s" if auto_refresh else None


@st.fragment(run_every=run_every)
def render_dashboard() -> None:
    # Avvia il polling solo in background: nessuna richiesta API può bloccare il
    # rendering della pagina o lasciare Streamlit su RUNNING per ore.
    if auto_api_sync:
        start_dashboard_sync_background(force=False)

    dashboard_data = dashboard_snapshot(
        selected_from=selected_date_from,
        selected_to=selected_date_to,
        timezone_name=DEFAULT_DASHBOARD_TIMEZONE,
    )
    summaries = dashboard_data["summaries"]
    selected_detail_rows = dashboard_data["detail_rows"]
    state = dashboard_sync_state()
    sync_running = dashboard_sync_in_progress(state)
    completed = str(state.get("last_completed_at") or "")
    sync_label = format_sync_time(completed)
    live_mode = "Aggiornamento automatico attivo" if auto_refresh else "Aggiornamento manuale"
    if sync_running:
        api_mode = "Sincronizzazione API in background in corso"
    else:
        api_mode = "API ogni 5 minuti" if auto_api_sync else "API automatiche disattivate"
    st.markdown(
        '<div class="dashboard-toolbar">'
        '<div class="dashboard-live">'
        f'<span class="dashboard-live-dot{" is-syncing" if sync_running else ""}"></span>'
        f'<span><strong>{escape(live_mode)}</strong><br><span class="dashboard-live-detail">{escape(api_mode)}</span></span>'
        '</div>'
        f'<div class="dashboard-live-detail">Ultima sincronizzazione: <strong>{escape(sync_label)}</strong></div>'
        '</div>',
        unsafe_allow_html=True,
    )

    if state.get("last_error"):
        last_error = str(state["last_error"])
        master_key_error = "Chiave master errata" in last_error
        if master_key_error:
            configured_master = active_master_key()
            encrypted_values = [
                item.get("credentials_encrypted")
                for item in rows(
                    """SELECT credentials_encrypted FROM marketplace_accounts
                    WHERE active=1 AND credentials_encrypted IS NOT NULL
                    AND credentials_encrypted<>'' ORDER BY id"""
                )
            ]

            # Se la chiave esiste già nei Render secrets / variabili ambiente, non
            # chiediamola di nuovo in Home. Un vecchio last_error può essere rimasto
            # memorizzato da una sincronizzazione eseguita prima dell'aggiornamento
            # del secret: in quel caso la validiamo e rilanciamo una sola volta.
            if configured_master:
                try:
                    configured_valid, checked = validate_master_key(
                        configured_master, encrypted_values
                    )
                except Exception as exc:
                    configured_valid = False
                    checked = 0
                    st.error(
                        "La chiave master è configurata nei secrets, ma non è stato "
                        f"possibile verificarla: {exc}"
                    )
                if configured_valid:
                    retry_key = "dashboard_retry_after_master_secret_update"
                    if not st.session_state.get(retry_key):
                        st.session_state[retry_key] = True
                        start_dashboard_sync_background(force=True)
                    st.info(
                        f"Chiave master già configurata e verificata su {checked} account. "
                        "La sincronizzazione viene rilanciata automaticamente; non è necessario "
                        "inserire la chiave nella Home."
                    )
                else:
                    st.error(
                        "MARKETPLACE_HUB_MASTER_KEY è presente nei secrets ma non corrisponde "
                        "alle credenziali cifrate. Correggi il secret su Render e riavvia il servizio. "
                        "Il campo manuale viene mostrato soltanto quando la chiave è assente."
                    )
            else:
                with st.expander(
                    "⚠️ Chiave master mancante — clicca qui per inserirla manualmente",
                    expanded=False,
                ):
                    st.warning(last_error)
                    st.caption(
                        "Questo campo compare solo perché MARKETPLACE_HUB_MASTER_KEY non è "
                        "configurata. La chiave manuale resta solo nella memoria del servizio e "
                        "non viene salvata nel database né nei log."
                    )
                    with st.form("dashboard_manual_master_key_form", clear_on_submit=True):
                        manual_master_key = st.text_input(
                            "Chiave master",
                            type="password",
                            placeholder="Incolla qui la MARKETPLACE_HUB_MASTER_KEY",
                        )
                        apply_master_key = st.form_submit_button(
                            "Verifica e usa chiave",
                            type="primary",
                            use_container_width=True,
                        )
                    if apply_master_key:
                        try:
                            valid, checked = validate_master_key(
                                manual_master_key, encrypted_values
                            )
                        except Exception as exc:
                            st.error(f"Impossibile verificare la chiave: {exc}")
                        else:
                            if not valid:
                                st.error(
                                    "La chiave non corrisponde alle credenziali cifrate presenti "
                                    "nel database. Controlla di averla copiata per intero."
                                )
                            else:
                                set_runtime_master_key(manual_master_key)
                                start_dashboard_sync_background(force=True)
                                st.success(
                                    f"Chiave verificata su {checked} account e applicata. "
                                    "La sincronizzazione è stata rilanciata automaticamente."
                                )
                    if runtime_master_key_active():
                        if st.button(
                            "Rimuovi chiave manuale",
                            key="dashboard_clear_runtime_master_key",
                            use_container_width=True,
                        ):
                            clear_runtime_master_key()
                            st.success("Chiave manuale rimossa.")
        else:
            st.markdown(
                f'<div class="dashboard-warning"><strong>Sincronizzazione parziale:</strong> {escape(last_error)}</div>',
                unsafe_allow_html=True,
            )

    if not summaries:
        st.info("Apri **Gestione Seller** dal menu laterale per creare il primo Seller.")
        return

    selected = combined_dashboard_period(summaries, "selected")
    if st.button(
        _dashboard_card_label(
            "Guadagno complessivo BEBOL nell’intervallo",
            format_euro(selected["our_amount"]),
            f"{selected_period_label} · {format_integer(len(summaries))} Seller inclusi",
        ),
        key="dashboard_card_bebol",
        use_container_width=True,
        help="Apri le righe contabili che compongono la quota BEBOL.",
    ):
        st.session_state["dashboard_detail_view"] = "bebol"

    first_row = st.columns(3, gap="small")
    second_row = st.columns(3, gap="small")
    card_specs = (
        (
            first_row[0],
            "orders",
            "Ordini nell’intervallo",
            format_integer(selected["orders"]),
            f"su {format_integer(len(summaries))} Seller",
        ),
        (
            first_row[1],
            "sales",
            "Vendite nell’intervallo",
            format_euro(selected["sales"]),
            "totale di tutti i Seller",
        ),
        (
            first_row[2],
            "profit",
            "Margine utile nell’intervallo",
            format_euro(selected["profit"]),
            "prima della ripartizione",
        ),
        (
            second_row[0],
            "partner",
            "Quota complessiva dei Seller",
            format_euro(selected["partner_amount"]),
            "totale spettante ai partner Seller",
        ),
        (
            second_row[1],
            "missing",
            "Righe da verificare",
            format_integer(selected["missing_profit_rows"]),
            "costi o margini non ancora determinabili",
        ),
        (
            second_row[2],
            "period",
            "Periodo selezionato",
            selected_period_label,
            "estremi inclusi nel calcolo",
        ),
    )
    for column, detail_key, title, value, note in card_specs:
        with column:
            if st.button(
                _dashboard_card_label(title, value, note),
                key=f"dashboard_card_{detail_key}",
                use_container_width=True,
                help=f"Apri {DASHBOARD_DETAIL_TITLES[detail_key].lower()}.",
            ):
                st.session_state["dashboard_detail_view"] = detail_key

    # v265: il Top 10 legge esclusivamente la cache contabile locale già usata
    # dalla Dashboard. Nessuna API viene chiamata per costruire la classifica.
    top_products = sort_product_stats(
        aggregate_product_stats(filter_product_rows(selected_detail_rows)),
        "Più venduti (quantità)",
    )[:10]

    detail_key = str(st.session_state.get("dashboard_detail_view") or "").strip()
    if detail_key in DASHBOARD_DETAIL_TITLES:
        with st.container(border=True):
            detail_title_col, detail_close_col = st.columns([5.2, 1])
            detail_title_col.markdown(f"### {DASHBOARD_DETAIL_TITLES[detail_key]}")
            detail_title_col.caption(
                f"{selected_period_label} · valori calcolati con gli stessi dati della Dashboard"
            )
            close_detail = detail_close_col.button(
                "Chiudi dettaglio",
                key="dashboard_close_detail",
                use_container_width=True,
            )
            if close_detail:
                st.session_state.pop("dashboard_detail_view", None)
            else:
                detail_rows = []
                if detail_key != "period":
                    detail_rows = selected_detail_rows
                detail_frame = _dashboard_detail_dataframe(
                    detail_key, detail_rows, summaries
                )

                metric_cols = st.columns(4)
                if detail_key == "orders":
                    order_rows = dashboard_order_detail_rows(detail_rows)
                    metric_cols[0].metric("Ordini", format_integer(len(order_rows)))
                    metric_cols[1].metric("Vendite", format_euro(selected["sales"]))
                    metric_cols[2].metric("Margine utile", format_euro(selected["profit"]))
                    metric_cols[3].metric("Da verificare", format_integer(selected["missing_profit_rows"]))
                elif detail_key == "sales":
                    metric_cols[0].metric("Vendite", format_euro(selected["sales"]))
                    metric_cols[1].metric("Ordini", format_integer(selected["orders"]))
                    metric_cols[2].metric("Righe mostrate", format_integer(len(detail_frame)))
                    metric_cols[3].metric("Seller", format_integer(len(summaries)))
                elif detail_key == "profit":
                    metric_cols[0].metric("Margine utile", format_euro(selected["profit"]))
                    metric_cols[1].metric("Quota BEBOL", format_euro(selected["our_amount"]))
                    metric_cols[2].metric("Quota Seller", format_euro(selected["partner_amount"]))
                    metric_cols[3].metric("Da verificare", format_integer(selected["missing_profit_rows"]))
                elif detail_key == "partner":
                    metric_cols[0].metric("Quota Seller", format_euro(selected["partner_amount"]))
                    metric_cols[1].metric("Margine utile", format_euro(selected["profit"]))
                    metric_cols[2].metric("Ordini", format_integer(selected["orders"]))
                    metric_cols[3].metric("Seller", format_integer(len(summaries)))
                elif detail_key == "bebol":
                    metric_cols[0].metric("Quota BEBOL", format_euro(selected["our_amount"]))
                    metric_cols[1].metric("Margine utile", format_euro(selected["profit"]))
                    metric_cols[2].metric("Ordini", format_integer(selected["orders"]))
                    metric_cols[3].metric("Seller", format_integer(len(summaries)))
                elif detail_key == "missing":
                    metric_cols[0].metric("Righe da verificare", format_integer(selected["missing_profit_rows"]))
                    metric_cols[1].metric("Ordini nel periodo", format_integer(selected["orders"]))
                    metric_cols[2].metric("Vendite totali", format_euro(selected["sales"]))
                    metric_cols[3].metric("Seller", format_integer(len(summaries)))
                else:
                    metric_cols[0].metric("Seller", format_integer(len(summaries)))
                    metric_cols[1].metric("Ordini", format_integer(selected["orders"]))
                    metric_cols[2].metric("Vendite", format_euro(selected["sales"]))
                    metric_cols[3].metric("Margine utile", format_euro(selected["profit"]))

                if detail_frame.empty:
                    st.info("Nessuna voce da mostrare per questo dettaglio nel periodo selezionato.")
                else:
                    st.dataframe(
                        detail_frame,
                        use_container_width=True,
                        hide_index=True,
                        height=min(620, 92 + 35 * max(4, min(len(detail_frame), 15))),
                        column_config=_dashboard_detail_column_config(detail_frame),
                    )

    period_labels = [

        ("selected", selected_period_label),
        ("today", "Oggi"),
        ("week", "Questa settimana"),
        ("month", "Questo mese"),
        ("all", "Complessivo"),
    ]

    for start in range(0, len(summaries), 2):
        columns = st.columns(2, gap="large")
        for offset, column in enumerate(columns):
            index = start + offset
            if index >= len(summaries):
                continue
            seller = summaries[index]
            period_html: list[str] = []
            missing_rows = int(seller["periods"]["selected"].get("missing_profit_rows") or 0)
            for period_key, period_label in period_labels:
                values = seller["periods"][period_key]
                profit = float(values["profit"])
                profit_class = (
                    "seller-dashboard-profit-negative"
                    if profit < 0
                    else "seller-dashboard-profit-positive"
                )
                if period_key == "selected":
                    today_class = " is-selected"
                else:
                    today_class = " is-today" if period_key == "today" else ""
                order_count = int(values.get("orders") or 0)
                order_word = "ordine" if order_count == 1 else "ordini"
                partner_name = str(seller.get("partner_name") or seller["seller_name"])
                period_html.append(
                    f'<div class="seller-dashboard-period{today_class}">'
                    '<div class="seller-dashboard-period-top">'
                    f'<div class="seller-dashboard-period-label">{escape(period_label)}</div>'
                    f'<div class="seller-dashboard-order-badge">{escape(format_integer(order_count))} {order_word}</div>'
                    '</div>'
                    '<div class="seller-dashboard-metrics">'
                    '<div class="seller-dashboard-metric">'
                    '<div class="seller-dashboard-label">Vendite</div>'
                    f'<div class="seller-dashboard-value">{escape(format_euro(values["sales"]))}</div>'
                    '</div>'
                    '<div class="seller-dashboard-metric">'
                    '<div class="seller-dashboard-label">Margine utile</div>'
                    f'<div class="seller-dashboard-value {profit_class}">{escape(format_euro(profit))}</div>'
                    '</div>'
                    '</div>'
                    '<div class="seller-dashboard-split">'
                    '<div class="seller-dashboard-share">'
                    f'<div class="seller-dashboard-share-label">Nostra quota · {escape(str(values["our_pct"]))}%</div>'
                    f'<div class="seller-dashboard-share-value seller-dashboard-share-our">{escape(format_euro(values["our_amount"]))}</div>'
                    '</div>'
                    '<div class="seller-dashboard-share">'
                    f'<div class="seller-dashboard-share-label">Quota {escape(partner_name)} · {escape(str(values["partner_pct"]))}%</div>'
                    f'<div class="seller-dashboard-share-value seller-dashboard-share-partner">{escape(format_euro(values["partner_amount"]))}</div>'
                    '</div>'
                    '</div>'
                    '</div>'
                )
            footer = (
                f"{format_integer(missing_rows)} righe con guadagno da verificare"
                if missing_rows
                else "Tutti i costi disponibili risultano contabilizzati"
            )
            legal_name = str(seller.get("legal_name") or "").strip()
            subtitle = legal_name if legal_name and legal_name.lower() != str(seller["seller_name"]).lower() else "Riepilogo economico"
            card = (
                '<section class="seller-dashboard-card">'
                '<div class="seller-dashboard-header">'
                '<div class="seller-dashboard-identity">'
                f'<div class="seller-dashboard-avatar">{escape(seller_initials(seller["seller_name"]))}</div>'
                '<div>'
                f'<div class="seller-dashboard-title">{escape(seller["seller_name"])}</div>'
                f'<div class="seller-dashboard-subtitle">{escape(subtitle)}</div>'
                '</div>'
                '</div>'
                '<div style="display:flex;align-items:center;gap:.55rem">'
                f'<div class="seller-dashboard-split-badge">Noi {escape(str(seller["our_profit_pct"]))}% · {escape(seller["seller_name"])} {escape(str(seller["partner_profit_pct"]))}%</div>'
                f'<div class="seller-dashboard-sync">Ultimo dato<br><strong>{escape(format_sync_time(seller.get("last_synced", "")))}</strong></div>'
                '</div>'
                '</div>'
                f'<div class="seller-dashboard-periods">{"".join(period_html)}</div>'
                '<div class="seller-dashboard-footer">'
                f'<span>{escape(footer)}</span>'
                f'<span>Ripartizione: Noi {escape(str(seller["our_profit_pct"]))}% · {escape(seller["seller_name"])} {escape(str(seller["partner_profit_pct"]))}%</span>'
                '</div>'
                '</section>'
            )
            with column:
                st.markdown(card, unsafe_allow_html=True)

    st.markdown("### Migliori vendite del periodo")
    st.markdown(
        _dashboard_top_products_html(top_products, selected_period_label),
        unsafe_allow_html=True,
    )
    top_products_info_col, top_products_button_col = st.columns([3.2, 1.15], gap="small")
    top_products_info_col.caption(
        "Riquadro spostato in fondo alla Home: usa solo i dati contabili già presenti e segue lo stesso periodo selezionato della Dashboard."
    )
    if top_products_button_col.button(
        "Apri classifica completa e dettaglio →",
        key="dashboard_top_products",
        use_container_width=True,
        help="Apri la pagina Prodotti più venduti mantenendo il periodo selezionato.",
    ):
        st.session_state["products_stats_period_mode"] = dashboard_period_mode
        st.session_state["products_stats_selected_day"] = selected_date_from
        st.session_state["products_stats_custom_from"] = selected_date_from
        st.session_state["products_stats_custom_to"] = selected_date_to
        st.switch_page("pages/3_Prodotti_Piu_Venduti.py")

    st.caption(
        "Il numero ordini conta ogni ordine una sola volta anche quando contiene più prodotti. "
        "Annullati, cancellati, no stock e rimborsati restano nel conteggio degli ordini, "
        "ma hanno vendite e guadagno pari a zero. Il margine utile viene ripartito "
        "con le percentuali configurate nella Gestione Seller. Il riquadro BEBOL somma "
        "le quote calcolate separatamente per ciascun Seller nell’intervallo selezionato."
    )


render_dashboard()
