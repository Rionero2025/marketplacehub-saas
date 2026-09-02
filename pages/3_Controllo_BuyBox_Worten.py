from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from services.db import execute, execute_many, json_text, now_iso, rows
from services.kaufland_profit import product_costs
from services.lists import normalize
from services.security import decrypt_dict
from services.saved_view_storage import load_saved_view_frame
from services.session import bootstrap, seller_selector
from services.worten import (
    DEFAULT_API_URL,
    build_category_commission_index,
    build_commission_rate_index,
    classify_product_buybox,
    estimate_offer_price_rank,
    list_category_commissions,
    list_offers,
    list_orders,
    list_product_offers,
    offer_import_status,
    position_cell_display,
    position_cell_style,
    resolve_category_commission,
    resolve_order_commission,
    upload_offer_csv,
)
from services.worten_buybox_actions import (
    apply_offer_editor_changes,
    build_price_update_offer_csv,
    buybox_alignment_price,
    buybox_outcome,
    evaluate_worten_price,
)

try:
    from services.worten_buybox_actions import build_worten_price_update_plan
except ImportError:
    # Compatibilità con installazioni aggiornate parzialmente: la pagina può
    # essere stata sostituita mentre il servizio è rimasto alla versione
    # precedente. Il progetto completo include comunque la funzione nel servizio.
    def build_worten_price_update_plan(
        items, selected_skus, proposed_prices=None, *, use_buybox_recommendation=False
    ):
        selected = {
            str(value or "").strip()
            for value in selected_skus
            if str(value or "").strip()
        }
        prepared_prices = {}
        for key, value in (proposed_prices or {}).items():
            sku = str(key or "").strip()
            try:
                parsed = float(str(value).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if sku:
                prepared_prices[sku] = parsed

        updates, unavailable, unchanged = [], [], []
        for item in items:
            sku = str(item.get("sku") or "").strip()
            if not sku or sku not in selected:
                continue
            try:
                current_price = float(item.get("our_price"))
            except (TypeError, ValueError):
                current_price = None
            outcome = str(item.get("_buybox_outcome") or buybox_outcome(item))
            if use_buybox_recommendation and outcome == "Vinte":
                unchanged.append({
                    "sku": sku, "ean": item.get("ean"),
                    "reason": "Buy Box già vinta",
                })
                continue
            proposed_price = (
                buybox_alignment_price(item)
                if use_buybox_recommendation
                else prepared_prices.get(sku, current_price)
            )
            if current_price is None:
                unavailable.append({
                    "sku": sku, "ean": item.get("ean"),
                    "reason": "Prezzo attuale non disponibile",
                })
                continue
            if proposed_price is None or float(proposed_price) <= 0:
                unavailable.append({
                    "sku": sku, "ean": item.get("ean"),
                    "reason": (
                        "Prezzo consigliato Buy Box non disponibile"
                        if use_buybox_recommendation
                        else "Prezzo preparato non valido"
                    ),
                })
                continue
            proposed_price = round(float(proposed_price), 2)
            if abs(proposed_price - current_price) < 0.005:
                unchanged.append({
                    "sku": sku, "ean": item.get("ean"),
                    "reason": "Prezzo già allineato",
                })
                continue
            financials = evaluate_worten_price(
                proposed_price,
                shipping=item.get("our_shipping"),
                commission_pct=item.get("commission_pct"),
                total_cost=item.get("total_cost_eur"),
            )
            updates.append({
                "sku": sku, "ean": item.get("ean"),
                "price": proposed_price, "previous_price": current_price,
                "buybox_outcome": outcome,
                "commission_pct": item.get("commission_pct"),
                "commission_source": item.get("commission_source"),
                "total_cost_eur": item.get("total_cost_eur"),
                "price_source": (
                    "Prezzo consigliato Buy Box"
                    if use_buybox_recommendation
                    else "Prezzo preparato nella tabella"
                ),
                **financials,
            })
        return {
            "updates": updates, "unavailable": unavailable,
            "unchanged": unchanged, "selected_count": len(selected),
            "mode": "recommended" if use_buybox_recommendation else "prepared",
        }


embedded = bool(st.session_state.get("_embedded_worten_buybox"))
if not embedded:
    bootstrap()
    st.title("Controllo Buy Box Worten")
seller_id = st.session_state.get("active_seller_id") if embedded else seller_selector()
if seller_id is None:
    st.stop()

st.subheader("Buy Box Worten")
st.caption(
    "Controllo tramite Mirakl P11. L'offerta visualizzata per prima "
    "dall'API è la candidata Buy Box secondo prezzo totale, qualità del Seller e "
    "regole Worten. I prezzi vengono modificati soltanto dopo selezione, anteprima "
    "economica e conferma esplicita dell'import."
)

accounts = rows(
    """SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND marketplace='worten' AND active=1
    ORDER BY account_name""",
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
        key=f"worten_buybox_account_{seller_id}",
    )
]
credentials = decrypt_dict(account["credentials_encrypted"])
api_key = credentials.get("api_key", "")
shop_id = credentials.get("shop_id", "")
api_url = credentials.get("api_url", DEFAULT_API_URL)

available_lists = rows(
    """SELECT DISTINCT pl.id,pl.name,s.name supplier_name
    FROM operations o
    JOIN price_lists pl ON pl.id=o.price_list_id
    JOIN suppliers s ON s.id=pl.supplier_id
    WHERE o.seller_id=? AND o.marketplace_account_id=?
      AND o.marketplace='worten' AND o.operation_type='CREA/AGGIORNA'
    ORDER BY s.name,pl.name""",
    (seller_id, account["id"]),
)
if not available_lists:
    st.info("Non risultano listini pubblicati su Worten con questo account.")
    st.stop()
list_map = {
    f"{item['supplier_name']} · {item['name']} · ID {item['id']}": item
    for item in available_lists
}
selected_list = list_map[
    st.selectbox(
        "Listino pubblicato",
        list(list_map),
        key=f"worten_buybox_list_{account['id']}",
    )
]

channel_code = "WRT_PT_ONLINE"
storefront = "pt"
st.info(
    "Canale Worten attivo: PORTOGALLO · WRT_PT_ONLINE. "
    "Dal 1° agosto il programma non propone né controlla il canale spagnolo."
)


def _clean(value) -> str:
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") else text


def _float_or_none(value):
    try:
        if value in (None, ""):
            return None
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _display_rome_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
        return parsed.astimezone(ZoneInfo("Europe/Rome")).strftime(
            "%d/%m/%Y %H:%M:%S"
        )
    except (TypeError, ValueError):
        return str(value or "")


def _position_styler(frame: pd.DataFrame):
    return frame.style.map(
        position_cell_style,
        subset=["our_rank"],
    ).format(
        {"our_rank": position_cell_display},
        na_rep="",
    )


operation_rows = rows(
    """SELECT id,details_json,created_at
    FROM operations
    WHERE seller_id=? AND marketplace_account_id=? AND price_list_id=?
      AND marketplace='worten' AND operation_type='CREA/AGGIORNA'
    ORDER BY created_at,id""",
    (seller_id, account["id"], selected_list["id"]),
)
published_history = {}
for operation in operation_rows:
    try:
        details = json.loads(operation.get("details_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        continue
    view_id = int(details.get("saved_view_id", 0) or 0)
    for item in details.get("rows", []) if isinstance(details, dict) else []:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        sku = _clean(item.get("sku_inviato"))
        if not sku:
            continue
        published_history[sku] = {
            "sku": sku,
            "ean": _clean(item.get("ean")),
            "original_sku": _clean(item.get("sku_originale")),
            "saved_view_id": view_id,
        }


@st.cache_data(ttl=120, show_spinner=False)
def cached_live_offers_v94(key: str, shop: str, url: str) -> list[dict]:
    return list_offers(key, shop, api_url=url)


refresh_col, visibility_col = st.columns([1, 2])
if refresh_col.button(
    "Aggiorna offerte Worten",
    key=f"worten_buybox_refresh_{account['id']}",
    use_container_width=True,
):
    cached_live_offers_v94.clear()
    st.rerun()
visibility_col.info(
    "Se Mirakl restituisce soltanto le tue offerte, la riga sarà indicata come "
    "«Vinta / unica visibile»: in quel caso la visibilità dei concorrenti potrebbe "
    "essere limitata da Worten."
)

try:
    with st.spinner("Lettura delle offerte attive dell'account Worten…"):
        live = cached_live_offers_v94(api_key, shop_id, api_url)
except Exception as error:
    st.error(f"Impossibile leggere le offerte Worten: {error}")
    st.stop()

live_by_sku = {_clean(item.get("sku")): item for item in live}
published = []
for sku, historical in published_history.items():
    current = live_by_sku.get(sku)
    if current is None:
        continue
    current_price = _float_or_none(current.get("price"))
    current_shipping = _float_or_none(current.get("shipping_price"))
    current_total = _float_or_none(current.get("total_price"))
    total_source = "OF21 total_price"
    if current_total is None and current_price is not None:
        current_total = current_price + float(current_shipping or 0.0)
        total_source = (
            "OF21 prezzo + spedizione"
            if current_shipping is not None
            else "OF21 solo prezzo prodotto"
        )
    published.append(
        {
            **historical,
            "ean": _clean(current.get("ean")) or historical["ean"],
            "name": str(current.get("name") or "").strip(),
            "current_price": current_price,
            "current_shipping": current_shipping,
            "current_total": current_total,
            "current_total_source": total_source,
            "quantity": current.get("quantity"),
            "offer_id": _clean(current.get("offer_id")),
            "own_shop_id": _clean(current.get("shop_id")) or _clean(shop_id),
            "own_shop_name": str(current.get("shop_name") or "").strip(),
            "product_sku": _clean(current.get("product_sku")),
            "category_code": str(current.get("category_code") or "").strip(),
            "category_label": str(current.get("category_label") or "").strip(),
            "channels": current.get("channels") or [],
        }
    )
if not published:
    st.warning(
        "Nessuna offerta di questo listino risulta attualmente presente nell'account "
        "Worten. Gli import ancora in elaborazione non vengono inclusi."
    )
    st.stop()

published_frame = pd.DataFrame(published)
published_frame = published_frame[
    ~published_frame["ean"].fillna("").astype(str).str.lower().isin(("", "nan", "none"))
].reset_index(drop=True)
if published_frame.empty:
    st.error("Le offerte del listino non contengono EAN utilizzabili da Mirakl P11.")
    st.stop()

search = st.text_input(
    "Cerca offerta",
    placeholder="EAN, SKU, prodotto o categoria…",
    key=f"worten_buybox_search_{account['id']}_{selected_list['id']}",
).strip().lower()
if search:
    mask = (
        published_frame["ean"].astype(str).str.lower().str.contains(search, regex=False)
        | published_frame["sku"].astype(str).str.lower().str.contains(search, regex=False)
        | published_frame["name"].astype(str).str.lower().str.contains(search, regex=False)
        | published_frame["category_code"].astype(str).str.lower().str.contains(
            search, regex=False
        )
        | published_frame["category_label"].astype(str).str.lower().str.contains(
            search, regex=False
        )
    )
    published_frame = published_frame[mask].reset_index(drop=True)
if published_frame.empty:
    st.warning("Nessuna offerta corrisponde alla ricerca.")
    st.stop()

selection_key = (
    f"worten_buybox_selection_{seller_id}_{account['id']}_{selected_list['id']}_"
    f"{channel_code}"
)
selection_signature = hashlib.sha1(
    "\n".join(published_frame["sku"].astype(str).tolist()).encode("utf-8")
).hexdigest()[:12]
grid_key = f"{selection_key}_grid_{selection_signature}"
st.caption(
    f"Offerte attive di questo listino: {len(published_frame):,}. "
    "Mirakl accetta fino a 100 EAN per singola richiesta; il programma crea "
    "automaticamente più blocchi."
)
existing_check_selection = {
    str(value) for value in st.session_state.get(selection_key, [])
}
c1, c2, c3, c4, c5 = st.columns([1.25, 1.25, 0.8, 0.8, 1.45])
if c1.button(
    "☑ Seleziona tutte",
    key=f"{selection_key}_all",
    use_container_width=True,
):
    st.session_state[selection_key] = published_frame["sku"].astype(str).tolist()
    st.session_state.pop(grid_key, None)
    st.rerun()
if c2.button(
    "☐ Deseleziona tutte",
    key=f"{selection_key}_none",
    use_container_width=True,
):
    st.session_state[selection_key] = []
    st.session_state.pop(grid_key, None)
    st.rerun()
maximum_position = max(1, len(published_frame))
range_from = c3.number_input(
    "Da posizione",
    min_value=1,
    max_value=maximum_position,
    value=1,
    step=1,
    key=f"{selection_key}_range_from",
)
range_to = c4.number_input(
    "A posizione",
    min_value=1,
    max_value=maximum_position,
    value=min(100, maximum_position),
    step=1,
    key=f"{selection_key}_range_to",
)
if c5.button(
    "Seleziona intervallo Da/A",
    key=f"{selection_key}_apply_range",
    use_container_width=True,
):
    low, high = sorted((int(range_from), int(range_to)))
    st.session_state[selection_key] = (
        published_frame.iloc[low - 1 : high]["sku"].astype(str).tolist()
    )
    st.session_state.pop(grid_key, None)
    st.rerun()
st.caption(
    f"Le posizioni Da/A si riferiscono alle {len(published_frame):,} offerte filtrate "
    "e comprendono entrambi gli estremi."
)

selected_skus = set(st.session_state.get(selection_key, []))
selection_frame = published_frame.copy()
selection_frame.insert(0, "Seleziona", selection_frame["sku"].isin(selected_skus))
selection_row_skus = tuple(selection_frame["sku"].astype(str).tolist())


def persist_worten_check_selection() -> None:
    saved_selection, _ = apply_offer_editor_changes(
        existing_selection=st.session_state.get(selection_key, []),
        existing_prices={},
        row_skus=selection_row_skus,
        editor_state=st.session_state.get(grid_key),
    )
    st.session_state[selection_key] = saved_selection


st.data_editor(
    selection_frame[
        [
            "Seleziona",
            "ean",
            "sku",
            "name",
            "category_code",
            "category_label",
            "current_price",
            "quantity",
        ]
    ],
    use_container_width=True,
    height=430,
    hide_index=True,
    key=grid_key,
    column_config={
        "Seleziona": st.column_config.CheckboxColumn(required=True),
        "ean": "EAN",
        "sku": "SKU Worten",
        "name": "Prodotto",
        "category_code": "Codice categoria",
        "category_label": "Categoria Worten",
        "current_price": st.column_config.NumberColumn(
            "Prezzo attuale", format="%.2f"
        ),
        "quantity": st.column_config.NumberColumn("Quantità", format="%d"),
    },
    disabled=[
        "ean",
        "sku",
        "name",
        "category_code",
        "category_label",
        "current_price",
        "quantity",
    ],
    on_change=persist_worten_check_selection,
)
selected_skus = set(st.session_state.get(selection_key, []))
selected = published_frame[
    published_frame["sku"].astype(str).isin(selected_skus)
].copy()
st.metric("Offerte selezionate", len(selected))


def load_product_lookup() -> tuple[dict, dict]:
    by_ean, by_sku = {}, {}
    view_ids = sorted(
        {
            int(value)
            for value in selected["saved_view_id"].tolist()
            if int(value or 0) > 0
        }
    )
    if not view_ids:
        return by_ean, by_sku
    placeholders = ",".join("?" for _ in view_ids)
    saved_views = rows(
        f"""SELECT id,snapshot_path FROM saved_views
        WHERE seller_id=? AND price_list_id=? AND id IN ({placeholders})""",
        (seller_id, selected_list["id"], *view_ids),
    )
    for saved_view in saved_views:
        try:
            frame = normalize(load_saved_view_frame(saved_view))
        except Exception:
            continue
        for product in frame.to_dict("records"):
            ean = _clean(product.get("ean"))
            sku = _clean(product.get("sku"))
            if ean:
                by_ean.setdefault(ean, product)
            if sku:
                by_sku.setdefault(sku, product)
    return by_ean, by_sku


rule_rows = rows(
    """SELECT commission_pct FROM commercial_rules
    WHERE seller_id=? AND price_list_id=? AND marketplace='worten'
      AND storefront=?""",
    (seller_id, selected_list["id"], storefront),
)
commission_pct = float(rule_rows[0]["commission_pct"] if rule_rows else 15)
st.caption(
    "Il programma identifica la categoria effettiva di ogni offerta tramite "
    "OF21 e applica la tariffa corrente restituita da "
    "platform-setting/commission/category. Se la categoria non è disponibile, "
    "usa la commissione effettiva delle righe ordine; la percentuale configurata "
    "resta soltanto l’ultima riserva."
)

if st.button(
    "Controlla Buy Box delle offerte selezionate",
    type="primary",
    disabled=selected.empty,
    key=f"worten_buybox_run_{account['id']}_{selected_list['id']}_{channel_code}",
):
    by_ean, by_sku = load_product_lookup()
    selected_records = selected.to_dict("records")
    unique_eans = list(dict.fromkeys(item["ean"] for item in selected_records))
    chunks = [
        unique_eans[index : index + 100]
        for index in range(0, len(unique_eans), 100)
    ]
    progress = st.progress(0.0)
    progress_text = st.empty()
    live_table = st.empty()
    completed = []
    try:
        category_commission_error = ""
        category_commissions = []
        try:
            progress_text.caption(
                "Commissioni API Worten · lettura delle tariffe per categoria…"
            )
            category_commissions = list_category_commissions(
                api_key,
                api_url=api_url,
            )
        except Exception as error:
            category_commission_error = str(error)
        category_commission_index = build_category_commission_index(
            category_commissions
        )

        records_without_category_rate = [
            item
            for item in selected_records
            if resolve_category_commission(
                category_commission_index,
                category_code=item.get("category_code", ""),
                category_label=item.get("category_label", ""),
            )
            is None
        ]
        commission_orders = []
        commission_api_error = ""
        offer_ids = list(
            dict.fromkeys(
                _clean(item.get("offer_id"))
                for item in records_without_category_rate
                if _clean(item.get("offer_id"))
            )
        )
        offer_id_chunks = [
            offer_ids[index : index + 100]
            for index in range(0, len(offer_ids), 100)
        ]
        try:
            for commission_chunk_index, offer_id_chunk in enumerate(
                offer_id_chunks,
                start=1,
            ):
                progress_text.caption(
                    "Commissioni ordini Worten di riserva · blocco "
                    f"{commission_chunk_index} di {len(offer_id_chunks)}…"
                )
                commission_orders.extend(
                    list_orders(
                        api_key,
                        shop_id,
                        offer_ids=offer_id_chunk,
                        api_url=api_url,
                    )
                )
        except Exception as error:
            commission_api_error = str(error)
            commission_orders = []
        commission_index = build_commission_rate_index(commission_orders)

        db_columns = [
            "seller_id",
            "marketplace_account_id",
            "price_list_id",
            "channel_code",
            "ean",
            "sku",
            "original_sku",
            "product_sku",
            "category_code",
            "category_label",
            "status",
            "our_rank",
            "winner_shop_id",
            "winner_shop_name",
            "winner_price",
            "winner_shipping",
            "winner_total",
            "our_price",
            "our_shipping",
            "our_total",
            "currency",
            "offer_count",
            "competitor_visible",
            "purchase_cost_eur",
            "shipping_cost_eur",
            "total_cost_eur",
            "commission_pct",
            "commission_source",
            "profit_at_buybox_eur",
            "margin_at_buybox_pct",
            "economic_status",
            "details_json",
            "error",
            "checked_at",
        ]
        db_updates = ",".join(
            f"{column}=excluded.{column}"
            for column in db_columns
            if column
            not in (
                "seller_id",
                "marketplace_account_id",
                "price_list_id",
                "channel_code",
                "sku",
            )
        )
        db_upsert_sql = f"""INSERT INTO worten_buybox_checks(
        {','.join(db_columns)}) VALUES(
        {','.join('?' for _ in db_columns)})
        ON CONFLICT(
            marketplace_account_id,price_list_id,channel_code,sku
        ) DO UPDATE SET {db_updates}"""

        for chunk_index, eans in enumerate(chunks, start=1):
            chunk_db_values = []
            progress_text.caption(
                f"Blocco {chunk_index} di {len(chunks)} · controllo di "
                f"{len(eans)} EAN…"
            )
            response = list_product_offers(
                api_key,
                eans,
                shop_id,
                api_url=api_url,
                channel_code=channel_code,
            )
            products_by_ean = {
                item["ean"]: item for item in response.get("products", [])
            }
            chunk_skus = {
                item["sku"] for item in selected_records if item["ean"] in eans
            }
            chunk_records = [
                item for item in selected_records if item["sku"] in chunk_skus
            ]
            preliminary = {}
            for item in chunk_records:
                product_response = products_by_ean.get(item["ean"], {})
                item["category_code"] = (
                    str(item.get("category_code") or "").strip()
                    or str(product_response.get("category_code") or "").strip()
                )
                item["category_label"] = (
                    str(item.get("category_label") or "").strip()
                    or str(product_response.get("category_label") or "").strip()
                )
                preliminary[item["sku"]] = classify_product_buybox(
                    product_response
                    or {"ean": item["ean"], "product_sku": "", "offers": []},
                    item.get("own_shop_id") or shop_id,
                    item["sku"],
                    own_offer_id=item.get("offer_id", ""),
                    own_shop_name=item.get("own_shop_name", ""),
                )

            # P11 with all_offers=false returns only active offers.  Before the
            # Portugal activation date (or while an offer is scheduled/paused),
            # an own OF21 offer therefore cannot be present in the Buy Box
            # response.  First request every PT offer, including inactive ones;
            # only if still absent broaden the diagnostic to every channel.
            inactive_pt_by_ean = {}
            all_channels_by_ean = {}
            if any(
                check.get("status") == "Offerta propria non trovata"
                for check in preliminary.values()
            ):
                inactive_pt_response = list_product_offers(
                    api_key,
                    eans,
                    shop_id,
                    api_url=api_url,
                    channel_code=channel_code,
                    include_inactive=True,
                )
                inactive_pt_by_ean = {
                    item["ean"]: item
                    for item in inactive_pt_response.get("products", [])
                }
            for item in selected_records:
                if item["sku"] not in chunk_skus:
                    continue
                result = preliminary[item["sku"]]
                if (
                    result.get("status") == "Offerta propria non trovata"
                    and inactive_pt_by_ean
                ):
                    inactive_pt = classify_product_buybox(
                        inactive_pt_by_ean.get(
                            item["ean"],
                            {"ean": item["ean"], "product_sku": "", "offers": []},
                        ),
                        item.get("own_shop_id") or shop_id,
                        item["sku"],
                        own_offer_id=item.get("offer_id", ""),
                        own_shop_name=item.get("own_shop_name", ""),
                    )
                    if inactive_pt.get("our_price") is not None:
                        result["status"] = (
                            "Posizione rilevata · offerta non attiva su Worten PT"
                        )
                        result["our_rank"] = inactive_pt.get("our_rank")
                        result["our_price"] = inactive_pt.get("our_price")
                        result["our_shipping"] = inactive_pt.get("our_shipping")
                        result["our_total"] = inactive_pt.get("our_total")
                        result["currency"] = inactive_pt.get("currency", "EUR")
                        result["own_match_source"] = inactive_pt.get(
                            "own_match_source", ""
                        )
                    else:
                        if not all_channels_by_ean:
                            all_channels_response = list_product_offers(
                                api_key,
                                eans,
                                shop_id,
                                api_url=api_url,
                                channel_code=channel_code,
                                all_channels=True,
                                include_inactive=True,
                            )
                            all_channels_by_ean = {
                                candidate["ean"]: candidate
                                for candidate in all_channels_response.get(
                                    "products", []
                                )
                            }
                        all_channels = classify_product_buybox(
                            all_channels_by_ean.get(
                                item["ean"],
                                {
                                    "ean": item["ean"],
                                    "product_sku": "",
                                    "offers": [],
                                },
                            ),
                            item.get("own_shop_id") or shop_id,
                            item["sku"],
                            own_offer_id=item.get("offer_id", ""),
                            own_shop_name=item.get("own_shop_name", ""),
                        )
                        if all_channels.get("our_price") is not None:
                            result["status"] = (
                                "Posizione rilevata · offerta fuori da Worten PT"
                            )
                            result["our_rank"] = all_channels.get("our_rank")
                            result["our_price"] = all_channels.get("our_price")
                            result["our_shipping"] = all_channels.get(
                                "our_shipping"
                            )
                            result["our_total"] = all_channels.get("our_total")
                            result["currency"] = all_channels.get(
                                "currency", "EUR"
                            )
                            result["own_match_source"] = all_channels.get(
                                "own_match_source", ""
                            )
                        else:
                            estimated = estimate_offer_price_rank(
                                products_by_ean.get(
                                    item["ean"],
                                    {
                                        "ean": item["ean"],
                                        "product_sku": "",
                                        "offers": [],
                                    },
                                ),
                                item.get("current_total"),
                            )
                            if estimated.get("rank") is not None:
                                result["status"] = "Prezzo rilevato"
                                result["our_rank"] = estimated.get("rank")
                                result["our_price"] = item.get("current_price")
                                result["our_shipping"] = item.get(
                                    "current_shipping"
                                )
                                result["our_total"] = item.get("current_total")
                                result["currency"] = "EUR"
                                result["own_match_source"] = "of21_price_rank"
                                result.setdefault("details", {})[
                                    "price_rank_estimate"
                                ] = {
                                    **estimated,
                                    "total_source": item.get(
                                        "current_total_source", ""
                                    ),
                                }
                            else:
                                result["status"] = (
                                    "Offerta propria non riconosciuta da P11"
                                )
                    result.setdefault("details", {})[
                        "inactive_pt_diagnostic"
                    ] = inactive_pt.get("details", {})
                    if all_channels_by_ean:
                        result["details"]["all_channels_diagnostic"] = (
                            all_channels.get("details", {})
                        )
                    result["details"]["of21_offer"] = {
                        "offer_id": item.get("offer_id", ""),
                        "shop_id": item.get("own_shop_id", ""),
                        "shop_name": item.get("own_shop_name", ""),
                        "shop_sku": item.get("sku", ""),
                        "product_sku": item.get("product_sku", ""),
                        "channels": item.get("channels") or [],
                        "price": item.get("current_price"),
                        "shipping": item.get("current_shipping"),
                        "total": item.get("current_total"),
                        "total_source": item.get("current_total_source", ""),
                    }
                product = by_ean.get(item["ean"]) or by_sku.get(
                    item["original_sku"]
                )
                costs = (
                    product_costs(product, storefront)
                    if product
                    else {
                        "purchase_cost_eur": None,
                        "shipping_cost_eur": None,
                        "total_cost_eur": None,
                    }
                )
                total_cost = costs.get("total_cost_eur")
                target_total = result.get("winner_total")
                category_commission = resolve_category_commission(
                    category_commission_index,
                    category_code=item.get("category_code", ""),
                    category_label=item.get("category_label", ""),
                )
                order_commission = (
                    None
                    if category_commission
                    else resolve_order_commission(
                        commission_index,
                        offer_id=item.get("offer_id", ""),
                        offer_sku=item.get("sku", ""),
                        category_code=item.get("category_code", ""),
                    )
                )
                api_commission = category_commission or order_commission
                effective_commission_pct = (
                    float(api_commission["rate"])
                    if api_commission
                    else commission_pct
                )
                commission_source = (
                    api_commission["source"]
                    if api_commission
                    else "Commissione configurata · categoria/API non disponibili"
                )
                result.setdefault("details", {})["of21_category"] = {
                    "category_code": item.get("category_code", ""),
                    "category_label": item.get("category_label", ""),
                }
                result.setdefault("details", {})["commission"] = (
                    api_commission
                    if api_commission
                    else {
                        "rate": effective_commission_pct,
                        "source": commission_source,
                    }
                )
                profit = None
                margin = None
                economic_status = "Non calcolabile"
                if target_total is not None and total_cost is not None:
                    profit = float(target_total) * (
                        1 - effective_commission_pct / 100
                    ) - float(total_cost)
                    margin = (
                        profit / float(total_cost) * 100
                        if float(total_cost) > 0
                        else None
                    )
                    economic_status = (
                        "Perdita"
                        if profit < 0
                        else ("Margine sotto 10%" if (margin or 0) < 10 else "Guadagno")
                    )
                completed_item = {
                    **result,
                    "sku": item["sku"],
                    "original_sku": item["original_sku"],
                    "category_code": item.get("category_code", ""),
                    "category_label": item.get("category_label", ""),
                    **costs,
                    "commission_pct": effective_commission_pct,
                    "commission_source": commission_source,
                    "profit_at_buybox_eur": profit,
                    "margin_at_buybox_pct": margin,
                    "economic_status": economic_status,
                }
                completed.append(completed_item)
                columns = [
                    "ean",
                    "sku",
                    "category_code",
                    "category_label",
                    "status",
                    "our_rank",
                    "winner_shop_name",
                    "winner_total",
                    "our_total",
                    "total_cost_eur",
                    "commission_pct",
                    "commission_source",
                    "profit_at_buybox_eur",
                    "margin_at_buybox_pct",
                ]
                live_frame = pd.DataFrame(completed)[columns].tail(100)
                live_table.dataframe(
                    _position_styler(live_frame),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "our_rank": st.column_config.NumberColumn(
                            "our_rank",
                            format="%d",
                        )
                    },
                )
                values = [
                    seller_id,
                    account["id"],
                    selected_list["id"],
                    channel_code,
                    completed_item.get("ean", ""),
                    completed_item.get("sku", ""),
                    completed_item.get("original_sku", ""),
                    completed_item.get("product_sku", ""),
                    completed_item.get("category_code", ""),
                    completed_item.get("category_label", ""),
                    completed_item.get("status", ""),
                    completed_item.get("our_rank"),
                    completed_item.get("winner_shop_id", ""),
                    completed_item.get("winner_shop_name", ""),
                    completed_item.get("winner_price"),
                    completed_item.get("winner_shipping"),
                    completed_item.get("winner_total"),
                    completed_item.get("our_price"),
                    completed_item.get("our_shipping"),
                    completed_item.get("our_total"),
                    completed_item.get("currency", "EUR"),
                    completed_item.get("offer_count", 0),
                    int(bool(completed_item.get("competitor_visible"))),
                    completed_item.get("purchase_cost_eur"),
                    completed_item.get("shipping_cost_eur"),
                    completed_item.get("total_cost_eur"),
                    completed_item.get("commission_pct"),
                    completed_item.get("commission_source", ""),
                    completed_item.get("profit_at_buybox_eur"),
                    completed_item.get("margin_at_buybox_pct"),
                    completed_item.get("economic_status", ""),
                    json_text(completed_item.get("details", {})),
                    "",
                    now_iso(),
                ]
                chunk_db_values.append(tuple(values))
            execute_many(db_upsert_sql, chunk_db_values)
            progress.progress(chunk_index / len(chunks))
        progress_text.caption(
            f"Controllate {len(completed):,} di {len(selected_records):,} offerte."
        )
        if completed and not any(item["competitor_visible"] for item in completed):
            st.warning(
                "Nessuna offerta concorrente è stata restituita. Potresti essere "
                "l'unico Seller sui prodotti controllati oppure Worten potrebbe avere "
                "limitato la visibilità dei concorrenti per questa API Key."
            )
        category_commission_count = sum(
            str(item.get("commission_source") or "").startswith(
                "API Worten · categoria"
            )
            for item in completed
        )
        order_commission_count = sum(
            str(item.get("commission_source") or "").startswith("API ordine")
            for item in completed
        )
        fallback_commission_count = (
            len(completed)
            - category_commission_count
            - order_commission_count
        )
        st.info(
            f"Tariffe correnti per categoria: {category_commission_count:,} · "
            f"Commissioni ricavate dagli ordini: {order_commission_count:,} · "
            f"Percentuali configurate di riserva: {fallback_commission_count:,}."
        )
        if category_commission_error:
            st.warning(
                "La griglia commissioni per categoria non è stata restituita "
                "dall’endpoint platform-setting/commission/category. Il programma "
                "ha mantenuto il fallback dalle righe ordine. "
                f"Dettaglio: {category_commission_error}"
            )
        if commission_api_error:
            st.warning(
                "La lettura delle commissioni OR11 di riserva non è riuscita. "
                "Le offerte senza tariffa di categoria hanno usato la percentuale "
                "configurata. "
                f"Dettaglio: {commission_api_error}"
            )
        st.success("Controllo Buy Box Worten completato e salvato.")
    except Exception as error:
        progress.empty()
        progress_text.empty()
        message = str(error)
        if "HTTP 403" in message:
            st.error(
                "Worten/Mirakl non consente a questa API Key di leggere le offerte "
                "dei prodotti (P11). È necessario chiedere a Worten l'abilitazione "
                f"dei dati concorrenti. Dettaglio: {message}"
            )
        else:
            st.error(f"Controllo Buy Box Worten non riuscito: {message}")


saved = rows(
    """SELECT * FROM worten_buybox_checks
    WHERE seller_id=? AND marketplace_account_id=? AND price_list_id=?
      AND channel_code=?
    ORDER BY checked_at DESC,ean,sku""",
    (seller_id, account["id"], selected_list["id"], channel_code),
)
if saved:
    saved = [
        {
            **item,
            "status": (
                "Prezzo rilevato"
                if str(item.get("status", "")).startswith(
                    "Posizione prezzo calcolata"
                )
                else item.get("status", "")
            ),
        }
        for item in saved
    ]
    st.divider()
    st.subheader("Stato e visioni Buy Box salvate")
    latest_checked_at = max(
        (str(item.get("checked_at") or "") for item in saved),
        default="",
    )
    save_name_col, save_button_col = st.columns([3, 1])
    view_name = save_name_col.text_input(
        "Nome della visione da salvare",
        placeholder="Esempio: Controllo prezzi mattina",
        key=f"worten_view_name_{account['id']}_{selected_list['id']}",
    ).strip()
    if save_button_col.button(
        "Salva questa visione",
        use_container_width=True,
        key=f"worten_view_save_{account['id']}_{selected_list['id']}",
    ):
        automatic_name = f"Visione {_display_rome_time(now_iso())}"
        execute(
            """INSERT INTO worten_buybox_views(
            seller_id,marketplace_account_id,price_list_id,channel_code,
            name,rows_json,row_count,latest_checked_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                seller_id,
                account["id"],
                selected_list["id"],
                channel_code,
                view_name or automatic_name,
                json_text(saved),
                len(saved),
                latest_checked_at,
                now_iso(),
            ),
        )
        st.success("Visione Buy Box Worten salvata con data e ora.")
        st.rerun()

    stored_views = rows(
        """SELECT * FROM worten_buybox_views
        WHERE seller_id=? AND marketplace_account_id=? AND price_list_id=?
          AND channel_code=?
        ORDER BY created_at DESC,id DESC""",
        (seller_id, account["id"], selected_list["id"], channel_code),
    )
    latest_option = f"Ultima versione · {_display_rome_time(latest_checked_at)}"
    view_options = {latest_option: None}
    for stored_view in stored_views:
        label = (
            f"{stored_view['name']} · "
            f"{_display_rome_time(stored_view['created_at'])} · "
            f"{stored_view['row_count']} righe · PT · ID {stored_view['id']}"
        )
        view_options[label] = stored_view
    selected_view_label = st.selectbox(
        "Scegli la visione Buy Box",
        list(view_options),
        key=f"worten_view_selector_{account['id']}_{selected_list['id']}",
    )
    selected_view = view_options[selected_view_label]
    historical_view = selected_view is not None
    if selected_view is None:
        active_rows = saved
        active_view_key = "latest"
        st.caption(
            "Visualizzazione dell’ultima versione disponibile, aggiornata il "
            f"{_display_rome_time(latest_checked_at)}."
        )
    else:
        try:
            restored_rows = json.loads(selected_view.get("rows_json") or "[]")
            active_rows = [
                item for item in restored_rows if isinstance(item, dict)
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            active_rows = []
        active_view_key = f"saved_{selected_view['id']}"
        st.caption(
            f"Visione salvata il "
            f"{_display_rome_time(selected_view['created_at'])} · "
            f"{len(active_rows)} righe. Questa fotografia è in sola lettura."
        )

    active_rows = [
        {**item, "_buybox_outcome": buybox_outcome(item)}
        for item in active_rows
    ]

    metric_columns = st.columns(5)
    metric_columns[0].metric("Controllate", len(active_rows))
    metric_columns[1].metric(
        "Vinte",
        sum(item["_buybox_outcome"] == "Vinte" for item in active_rows),
    )
    metric_columns[2].metric(
        "Perse",
        sum(item["_buybox_outcome"] == "Perse" for item in active_rows),
    )
    metric_columns[3].metric(
        "Non trovate",
        sum(item["_buybox_outcome"] == "Non trovate" for item in active_rows),
    )
    metric_columns[4].metric(
        "Concorrenti visibili",
        sum(bool(item.get("competitor_visible")) for item in active_rows),
    )

    calculated_rows = [
        item
        for item in active_rows
        if _float_or_none(item.get("profit_at_buybox_eur")) is not None
    ]
    gain_rows = [
        item
        for item in calculated_rows
        if float(item["profit_at_buybox_eur"]) > 0
    ]
    loss_rows_saved = [
        item
        for item in calculated_rows
        if float(item["profit_at_buybox_eur"]) < 0
    ]
    economic_metrics = st.columns(5)
    economic_metrics[0].metric("Righe con guadagno", len(gain_rows))
    economic_metrics[1].metric("Righe in perdita", len(loss_rows_saved))
    economic_metrics[2].metric(
        "Guadagni potenziali €",
        f"{sum(float(item['profit_at_buybox_eur']) for item in gain_rows):,.2f}",
    )
    economic_metrics[3].metric(
        "Perdite potenziali €",
        f"{sum(float(item['profit_at_buybox_eur']) for item in loss_rows_saved):,.2f}",
    )
    economic_metrics[4].metric(
        "Risultato netto €",
        f"{sum(float(item['profit_at_buybox_eur']) for item in calculated_rows):,.2f}",
    )

    f1, f2, f3, f4 = st.columns(4)
    outcome_filter = f1.selectbox(
        "Esito Buy Box",
        ["Tutte", "Vinte", "Perse", "Non trovate"],
        key=(
            f"worten_buybox_status_filter_{account['id']}_"
            f"{selected_list['id']}_{active_view_key}"
        ),
    )
    economic_values = sorted(
        {
            str(item.get("economic_status") or "Non calcolabile")
            for item in active_rows
        }
    )
    economic_filter = f2.selectbox(
        "Esito economico",
        ["Tutti", *economic_values],
        key=(
            f"worten_buybox_economic_filter_{account['id']}_"
            f"{selected_list['id']}_{active_view_key}"
        ),
    )
    margin_mode = f3.selectbox(
        "Margine potenziale",
        ["Tutti", "Intervallo %", "Margine minimo %", "Non calcolabile"],
        key=(
            f"worten_buybox_margin_filter_{account['id']}_"
            f"{selected_list['id']}_{active_view_key}"
        ),
    )
    saved_search = f4.text_input(
        "Cerca nei risultati",
        placeholder="EAN, SKU, categoria o vincitore…",
        key=(
            f"worten_buybox_saved_search_{account['id']}_"
            f"{selected_list['id']}_{active_view_key}"
        ),
    ).strip().lower()

    margin_values = [
        parsed_margin
        for item in active_rows
        if (
            parsed_margin := _float_or_none(item.get("margin_at_buybox_pct"))
        )
        is not None
    ]
    margin_from = margin_to = margin_minimum = None
    margin_filter_col, exclude_filter_col = st.columns(2)
    if margin_mode == "Intervallo %":
        range_from_col, range_to_col = margin_filter_col.columns(2)
        margin_from = range_from_col.number_input(
            "Margine da X %",
            value=float(min(margin_values, default=0.0)),
            step=0.10,
            key=(
                f"worten_margin_from_{account['id']}_"
                f"{selected_list['id']}_{active_view_key}"
            ),
        )
        margin_to = range_to_col.number_input(
            "Margine a Y %",
            value=float(max(margin_values, default=10.0)),
            step=0.10,
            key=(
                f"worten_margin_to_{account['id']}_"
                f"{selected_list['id']}_{active_view_key}"
            ),
        )
    elif margin_mode == "Margine minimo %":
        margin_minimum = margin_filter_col.number_input(
            "Margine minimo X %",
            value=10.0,
            step=0.10,
            key=(
                f"worten_margin_minimum_{account['id']}_"
                f"{selected_list['id']}_{active_view_key}"
            ),
        )
    else:
        margin_filter_col.caption(
            "Puoi filtrare per intervallo, margine minimo o righe non calcolabili."
        )

    exclude_below = exclude_filter_col.checkbox(
        "Escludi Margine potenziale % minore di",
        value=False,
        key=(
            f"worten_margin_exclude_enabled_{account['id']}_"
            f"{selected_list['id']}_{active_view_key}"
        ),
    )
    exclude_threshold = None
    if exclude_below:
        exclude_threshold = exclude_filter_col.number_input(
            "Soglia di esclusione %",
            value=0.0,
            step=0.10,
            key=(
                f"worten_margin_exclude_value_{account['id']}_"
                f"{selected_list['id']}_{active_view_key}"
            ),
        )

    visible = list(active_rows)
    if outcome_filter != "Tutte":
        visible = [
            item
            for item in visible
            if item["_buybox_outcome"] == outcome_filter
        ]
    if economic_filter != "Tutti":
        visible = [
            item
            for item in visible
            if str(item.get("economic_status") or "Non calcolabile")
            == economic_filter
        ]
    invalid_margin_range = (
        margin_mode == "Intervallo %"
        and margin_from is not None
        and margin_to is not None
        and float(margin_from) > float(margin_to)
    )
    if invalid_margin_range:
        st.warning("Nel filtro margine, il valore X deve essere minore o uguale a Y.")
        visible = []
    elif margin_mode == "Intervallo %":
        visible = [
            item
            for item in visible
            if (
                (value := _float_or_none(item.get("margin_at_buybox_pct")))
                is not None
                and float(margin_from) <= value <= float(margin_to)
            )
        ]
    elif margin_mode == "Margine minimo %":
        visible = [
            item
            for item in visible
            if (
                (value := _float_or_none(item.get("margin_at_buybox_pct")))
                is not None
                and value >= float(margin_minimum)
            )
        ]
    elif margin_mode == "Non calcolabile":
        visible = [
            item
            for item in visible
            if _float_or_none(item.get("margin_at_buybox_pct")) is None
        ]
    if exclude_below:
        visible = [
            item
            for item in visible
            if (
                (value := _float_or_none(item.get("margin_at_buybox_pct")))
                is not None
                and value >= float(exclude_threshold)
            )
        ]
    if saved_search:
        visible = [
            item
            for item in visible
            if saved_search
            in " ".join(
                (
                    str(item.get("ean", "")),
                    str(item.get("sku", "")),
                    str(item.get("category_code", "")),
                    str(item.get("category_label", "")),
                    str(item.get("winner_shop_name", "")),
                )
            ).lower()
        ]
    st.caption(f"Righe dopo i filtri: {len(visible):,} su {len(active_rows):,}.")

    result_selection_key = (
        f"worten_result_selection_{seller_id}_{account['id']}_"
        f"{selected_list['id']}_{active_view_key}"
    )
    result_price_key = (
        f"worten_result_prices_{seller_id}_{account['id']}_"
        f"{selected_list['id']}_{active_view_key}"
    )
    saved_result_selection = {
        str(value)
        for value in st.session_state.get(result_selection_key, [])
    }
    saved_result_prices = dict(st.session_state.get(result_price_key, {}))
    for item in active_rows:
        sku = str(item.get("sku") or "")
        current_price = _float_or_none(item.get("our_price"))
        if sku and sku not in saved_result_prices and current_price is not None:
            saved_result_prices[sku] = round(current_price, 2)
    st.session_state[result_price_key] = saved_result_prices

    visible_skus = {
        str(item.get("sku") or "") for item in visible if item.get("sku")
    }
    visible_signature = hashlib.sha1(
        "\n".join(sorted(visible_skus)).encode("utf-8")
    ).hexdigest()[:12]
    result_editor_key = (
        f"worten_result_editor_v200_{account['id']}_{selected_list['id']}_"
        f"{active_view_key}_{visible_signature}"
    )
    select_filtered_block = st.checkbox(
        f"Seleziona tutto il blocco filtrato per l’allineamento Buy Box "
        f"({len(visible):,} righe)",
        value=False,
        disabled=not visible or historical_view,
        help=(
            "La selezione non modifica ancora Worten. Prima dell’invio saranno "
            "mostrati il riepilogo economico, le eventuali perdite e le conferme."
        ),
        key=(
            f"worten_select_filtered_block_{account['id']}_"
            f"{selected_list['id']}_{active_view_key}_{visible_signature}"
        ),
    )
    selected_visible_skus = (
        set(visible_skus)
        if select_filtered_block
        else saved_result_selection & visible_skus
    )

    select_col, deselect_col, align_col, restore_col = st.columns(4)
    if select_col.button(
        f"☑ Seleziona tutti i filtrati ({len(visible):,})",
        use_container_width=True,
        key=f"{result_selection_key}_select_visible",
    ):
        st.session_state[result_selection_key] = sorted(
            saved_result_selection | visible_skus
        )
        st.session_state.pop(result_editor_key, None)
        st.rerun()
    if deselect_col.button(
        f"☐ Deseleziona tutti i filtrati ({len(visible):,})",
        use_container_width=True,
        key=f"{result_selection_key}_deselect_visible",
    ):
        st.session_state[result_selection_key] = sorted(
            saved_result_selection - visible_skus
        )
        st.session_state.pop(result_editor_key, None)
        st.rerun()
    if align_col.button(
        "Allinea selezionate alla Buy Box",
        use_container_width=True,
        disabled=not selected_visible_skus or historical_view,
        key=f"{result_price_key}_align",
    ):
        for item in visible:
            sku = str(item.get("sku") or "")
            if sku not in selected_visible_skus:
                continue
            target_price = buybox_alignment_price(item)
            if target_price is not None:
                saved_result_prices[sku] = target_price
        st.session_state[result_price_key] = saved_result_prices
        st.session_state.pop(result_editor_key, None)
        st.rerun()
    if restore_col.button(
        "Ripristina prezzi attuali",
        use_container_width=True,
        disabled=not selected_visible_skus or historical_view,
        key=f"{result_price_key}_restore",
    ):
        for item in visible:
            sku = str(item.get("sku") or "")
            if sku not in selected_visible_skus:
                continue
            current_price = _float_or_none(item.get("our_price"))
            if current_price is not None:
                saved_result_prices[sku] = round(current_price, 2)
        st.session_state[result_price_key] = saved_result_prices
        st.session_state.pop(result_editor_key, None)
        st.rerun()

    operational_rows = []
    for item in visible:
        sku = str(item.get("sku") or "")
        proposed_price = _float_or_none(
            saved_result_prices.get(sku, item.get("our_price"))
        )
        proposed = evaluate_worten_price(
            proposed_price,
            shipping=item.get("our_shipping"),
            commission_pct=item.get("commission_pct"),
            total_cost=item.get("total_cost_eur"),
        )
        operational_rows.append(
            {
                "Seleziona": sku in saved_result_selection,
                "Esito Buy Box": item["_buybox_outcome"],
                "EAN": item.get("ean"),
                "SKU Worten": sku,
                "Categoria": item.get("category_label")
                or item.get("category_code"),
                "Esito": item.get("status"),
                "Posizione": item.get("our_rank"),
                "Vincitore": item.get("winner_shop_name"),
                "Prezzo vincente": item.get("winner_price"),
                "Totale vincente": item.get("winner_total"),
                "Nostro prezzo": item.get("our_price"),
                "Nostra spedizione": item.get("our_shipping"),
                "Nostro totale": item.get("our_total"),
                "Prezzo allineamento": buybox_alignment_price(item),
                "Prezzo da inviare": proposed_price,
                "Totale proposto": proposed["total"],
                "Costo acquisto €": item.get("purchase_cost_eur"),
                "Spedizione fornitore €": item.get("shipping_cost_eur"),
                "Costo totale €": item.get("total_cost_eur"),
                "Commissione %": item.get("commission_pct"),
                "Commissione proposta €": proposed["commission_eur"],
                "Guadagno proposto €": proposed["profit_eur"],
                "Margine proposto %": proposed["margin_pct"],
                "Esito economico proposto": proposed["status"],
                "Origine commissione": item.get("commission_source"),
                "Offerte visibili": item.get("offer_count"),
                "Controllata": item.get("checked_at"),
                "_sku": sku,
            }
        )
    result_frame = pd.DataFrame(operational_rows)
    selected_action_item = None
    if not visible:
        st.info("Nessun risultato corrisponde ai filtri selezionati.")
    else:
        result_row_skus = tuple(item["_sku"] for item in operational_rows)

        def persist_worten_result_editor() -> None:
            updated_selection, updated_prices = apply_offer_editor_changes(
                existing_selection=st.session_state.get(
                    result_selection_key, []
                ),
                existing_prices=st.session_state.get(result_price_key, {}),
                row_skus=result_row_skus,
                editor_state=st.session_state.get(result_editor_key),
            )
            st.session_state[result_selection_key] = updated_selection
            st.session_state[result_price_key] = updated_prices

        disabled_columns = [
            column
            for column in result_frame.columns
            if column not in {"Seleziona", "Prezzo da inviare"}
        ]
        if historical_view:
            disabled_columns.extend(["Seleziona", "Prezzo da inviare"])

        def style_result_row(row):
            outcome = str(row.get("Esito Buy Box") or "")
            economic = str(row.get("Esito economico proposto") or "")
            margin = _float_or_none(row.get("Margine proposto %"))
            if economic == "Perdita":
                style = "background-color:#fee2e2;color:#7f1d1d;"
            elif outcome == "Vinte":
                style = "background-color:#dcfce7;color:#14532d;"
            elif outcome == "Perse":
                style = "background-color:#fee2e2;color:#7f1d1d;"
            elif economic == "Margine sotto 10%" or (
                margin is not None and margin < 10
            ):
                style = "background-color:#fef3c7;color:#78350f;"
            else:
                style = ""
            return [style for _ in row]

        def result_cell_style(value) -> str:
            text = str(value or "")
            if text in {"Vinte", "Guadagno"}:
                return (
                    "background-color:#22c55e;color:#ffffff;"
                    "font-weight:700"
                )
            if text in {"Perse", "Perdita"}:
                return (
                    "background-color:#ef4444;color:#ffffff;"
                    "font-weight:700"
                )
            if text == "Margine sotto 10%":
                return (
                    "background-color:#facc15;color:#713f12;"
                    "font-weight:700"
                )
            return ""

        styled_result_frame = (
            result_frame.style
            .apply(style_result_row, axis=1)
            .map(
                result_cell_style,
                subset=["Esito Buy Box", "Esito economico proposto"],
            )
            .map(position_cell_style, subset=["Posizione"])
            .format({"Posizione": position_cell_display}, na_rep="")
        )
        st.markdown("#### Tabella operativa Buy Box")
        st.caption(
            "Puoi selezionare una o più righe e modificare il prezzo da inviare. "
            "Verde = Buy Box vinta · rosso = Buy Box persa o perdita · "
            "giallo = margine inferiore al 10%."
        )
        st.data_editor(
            styled_result_frame,
            use_container_width=True,
            hide_index=True,
            height=560,
            key=result_editor_key,
            on_change=persist_worten_result_editor,
            disabled=disabled_columns,
            column_config={
                "Seleziona": st.column_config.CheckboxColumn(required=True),
                "Posizione": st.column_config.NumberColumn(format="%d"),
                "Prezzo vincente": st.column_config.NumberColumn(format="%.2f"),
                "Totale vincente": st.column_config.NumberColumn(format="%.2f"),
                "Nostro prezzo": st.column_config.NumberColumn(format="%.2f"),
                "Nostra spedizione": st.column_config.NumberColumn(format="%.2f"),
                "Nostro totale": st.column_config.NumberColumn(format="%.2f"),
                "Prezzo allineamento": st.column_config.NumberColumn(format="%.2f"),
                "Prezzo da inviare": st.column_config.NumberColumn(
                    format="%.2f", min_value=0.01, step=0.01
                ),
                "Totale proposto": st.column_config.NumberColumn(format="%.2f"),
                "Costo acquisto €": st.column_config.NumberColumn(format="%.2f"),
                "Spedizione fornitore €": st.column_config.NumberColumn(format="%.2f"),
                "Costo totale €": st.column_config.NumberColumn(format="%.2f"),
                "Commissione %": st.column_config.NumberColumn(format="%.2f"),
                "Commissione proposta €": st.column_config.NumberColumn(format="%.2f"),
                "Guadagno proposto €": st.column_config.NumberColumn(format="%.2f"),
                "Margine proposto %": st.column_config.NumberColumn(format="%.2f"),
                "Offerte visibili": st.column_config.NumberColumn(format="%d"),
                "_sku": None,
            },
        )

    selected_result_skus = {
        str(value)
        for value in st.session_state.get(result_selection_key, [])
    }
    selected_visible_skus = (
        set(visible_skus)
        if select_filtered_block
        else selected_result_skus & visible_skus
    )
    st.caption(
        f"Risultati filtrati: {len(visible):,} · selezionati visibili: "
        f"{len(selected_visible_skus):,} · selezionati complessivi nella "
        f"visione: {len(selected_result_skus):,}."
    )

    active_by_sku = {
        str(item.get("sku") or ""): item for item in active_rows
    }
    if len(selected_visible_skus) == 1 and not select_filtered_block:
        selected_action_item = active_by_sku.get(next(iter(selected_visible_skus)))

    if historical_view:
        st.info(
            "Stai consultando una fotografia storica: filtri, statistiche ed "
            "esportazione restano disponibili, mentre gli aggiornamenti prezzo "
            "sono disattivati."
        )
    else:
        if selected_action_item is not None:
            item = selected_action_item
            sku = str(item.get("sku") or "")
            row_key = (
                f"{account['id']}_{selected_list['id']}_{active_view_key}_{sku}"
            )
            with st.container(border=True):
                st.markdown(
                    f"**Riga selezionata · Portogallo · EAN {item.get('ean')} · "
                    f"SKU {sku}**"
                )
                info1, info2, info3, info4, info5 = st.columns(5)
                info1.metric(
                    "Prezzo attuale",
                    f"{float(item['our_price']):.2f} €"
                    if item.get("our_price") is not None
                    else "Non rilevato",
                )
                info2.metric(
                    "Totale vincente",
                    f"{float(item['winner_total']):.2f} €"
                    if item.get("winner_total") is not None
                    else "Non disponibile",
                )
                alignment_price = buybox_alignment_price(item)
                info3.metric(
                    "Prezzo per Buy Box",
                    f"{float(alignment_price):.2f} €"
                    if alignment_price is not None
                    else "Non disponibile",
                )
                info4.metric(
                    "Costo totale",
                    f"{float(item['total_cost_eur']):.2f} €"
                    if item.get("total_cost_eur") is not None
                    else "Non disponibile",
                )
                info5.metric(
                    "Commissione corrente",
                    f"{float(item.get('commission_pct') or 0):.2f}%",
                    help=str(item.get("commission_source") or ""),
                )

                current_proposal = _float_or_none(
                    st.session_state.get(result_price_key, {}).get(
                        sku, item.get("our_price")
                    )
                )
                proposal_evaluation = evaluate_worten_price(
                    current_proposal,
                    shipping=item.get("our_shipping"),
                    commission_pct=item.get("commission_pct"),
                    total_cost=item.get("total_cost_eur"),
                )
                alert_message = (
                    f"Prezzo proposto: {current_proposal:.2f} € · "
                    f"guadagno {proposal_evaluation['profit_eur']:+.2f} € · "
                    f"margine {proposal_evaluation['margin_pct']:+.2f}%"
                    if proposal_evaluation.get("profit_eur") is not None
                    and proposal_evaluation.get("margin_pct") is not None
                    else "Prezzo proposto non calcolabile: verifica costo e commissione."
                )
                if proposal_evaluation["status"] == "Perdita":
                    st.error(alert_message)
                elif proposal_evaluation["status"] == "Margine sotto 10%":
                    st.warning(alert_message)
                elif proposal_evaluation["status"] == "Guadagno":
                    st.success(alert_message)
                else:
                    st.error(alert_message)

                align_detail_col, custom_detail_col = st.columns(2)
                with align_detail_col:
                    st.markdown("**1. Prepara l’allineamento alla Buy Box**")
                    if st.button(
                        "Imposta il prezzo Buy Box",
                        use_container_width=True,
                        disabled=alignment_price is None,
                        key=f"worten_detail_align_{row_key}",
                    ):
                        updated_prices = dict(
                            st.session_state.get(result_price_key, {})
                        )
                        updated_prices[sku] = float(alignment_price)
                        st.session_state[result_price_key] = updated_prices
                        st.session_state.pop(result_editor_key, None)
                        st.rerun()
                with custom_detail_col:
                    st.markdown("**2. Prepara un prezzo personalizzato**")
                    custom_price = float(
                        st.number_input(
                            "Nuovo prezzo da inviare (€)",
                            min_value=0.01,
                            value=float(current_proposal or item.get("our_price") or 0.01),
                            step=0.01,
                            format="%.2f",
                            key=f"worten_detail_custom_{row_key}",
                        )
                    )
                    if st.button(
                        "Imposta prezzo personalizzato",
                        use_container_width=True,
                        key=f"worten_detail_custom_apply_{row_key}",
                    ):
                        updated_prices = dict(
                            st.session_state.get(result_price_key, {})
                        )
                        updated_prices[sku] = custom_price
                        st.session_state[result_price_key] = updated_prices
                        st.session_state.pop(result_editor_key, None)
                        st.rerun()
                st.caption(
                    "Il prezzo viene soltanto preparato. L’invio effettivo avviene "
                    "nella gestione collettiva sottostante dopo il riepilogo economico."
                )

        price_state = dict(st.session_state.get(result_price_key, {}))
        price_mode_label = st.radio(
            "Prezzo da inviare per le offerte selezionate",
            (
                "Prezzo consigliato Buy Box · automatico",
                "Prezzo preparato o modificato nella tabella",
            ),
            index=0,
            horizontal=True,
            key=(
                f"worten_price_mode_{account['id']}_{selected_list['id']}_"
                f"{active_view_key}"
            ),
            help=(
                "La modalità automatica usa il totale del vincitore, sottrae la "
                "nostra spedizione e propone un prezzo prodotto inferiore di 0,01 €."
            ),
        )
        use_recommended_price = price_mode_label.startswith(
            "Prezzo consigliato Buy Box"
        )
        if use_recommended_price:
            st.info(
                "Le offerte selezionate vengono preparate automaticamente al prezzo "
                "consigliato per superare di 0,01 € il totale del vincitore visibile. "
                "Non è necessario premere prima «Allinea selezionate alla Buy Box». "
                "Le righe già vincenti non vengono abbassate."
            )
        price_plan = build_worten_price_update_plan(
            active_rows,
            selected_visible_skus,
            price_state,
            use_buybox_recommendation=use_recommended_price,
        )
        update_rows = list(price_plan["updates"])
        unavailable_price_rows = list(price_plan["unavailable"])
        unchanged_price_rows = list(price_plan["unchanged"])

        st.divider()
        st.markdown("### Gestione collettiva delle righe selezionate")
        safe_rows = [item for item in update_rows if item["status"] == "Guadagno"]
        low_margin_rows = [
            item for item in update_rows if item["status"] == "Margine sotto 10%"
        ]
        loss_rows = [item for item in update_rows if item["status"] == "Perdita"]
        unknown_rows = [
            item for item in update_rows if item["status"] == "Non calcolabile"
        ]
        action_metrics = st.columns(5)
        action_metrics[0].metric("Righe selezionate", len(selected_visible_skus))
        action_metrics[1].metric(
            "Prezzi consigliati pronti"
            if use_recommended_price
            else "Prezzi modificati",
            len(update_rows),
        )
        action_metrics[2].metric("Margine ≥ 10%", len(safe_rows))
        action_metrics[3].metric("Margine 0–9,99%", len(low_margin_rows))
        action_metrics[4].metric("Righe in perdita", len(loss_rows))
        st.caption(
            f"Righe non calcolabili escluse: {len(unknown_rows):,}. "
            f"Risultato potenziale delle righe valutabili: "
            f"{sum(float(item.get('profit_eur') or 0) for item in safe_rows + low_margin_rows + loss_rows):+,.2f} €."
        )
        if unavailable_price_rows:
            st.warning(
                f"Per {len(unavailable_price_rows):,} righe non è disponibile un "
                "prezzo valido da inviare. Queste righe restano escluse."
            )
            with st.expander("Mostra righe senza prezzo consigliato/preparato"):
                st.dataframe(
                    pd.DataFrame(unavailable_price_rows),
                    use_container_width=True,
                    hide_index=True,
                )
        if unchanged_price_rows:
            st.caption(
                f"Righe senza aggiornamento necessario: "
                f"{len(unchanged_price_rows):,} (già allineate o Buy Box già vinta)."
            )
        if unknown_rows:
            st.warning(
                f"Per {len(unknown_rows):,} righe manca un costo o un dato economico "
                "valido: queste righe non saranno inviate."
            )
        if loss_rows:
            st.error(
                f"Attenzione: {len(loss_rows):,} prezzi genererebbero una perdita."
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "EAN": item.get("ean"),
                            "SKU": item["sku"],
                            "Prezzo da inviare": item["price"],
                            "Perdita €": item["profit_eur"],
                            "Margine %": item["margin_pct"],
                        }
                        for item in loss_rows
                    ]
                ),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Prezzo da inviare": st.column_config.NumberColumn(format="%.2f"),
                    "Perdita €": st.column_config.NumberColumn(format="%.2f"),
                    "Margine %": st.column_config.NumberColumn(format="%.2f"),
                },
            )

        selection_signature = hashlib.sha1(
            "|".join(item["sku"] for item in update_rows).encode("utf-8")
        ).hexdigest()[:10]
        include_low_margin = st.checkbox(
            "Includi anche le righe gialle con margine inferiore al 10%",
            value=False,
            disabled=not low_margin_rows,
            key=(
                f"worten_include_low_{account['id']}_{selected_list['id']}_"
                f"{active_view_key}_{selection_signature}"
            ),
        )
        include_losses = st.checkbox(
            f"CONFERMO di voler includere anche i {len(loss_rows):,} prodotti in perdita",
            value=False,
            disabled=not loss_rows,
            key=(
                f"worten_include_losses_{account['id']}_{selected_list['id']}_"
                f"{active_view_key}_{selection_signature}"
            ),
        )
        rows_to_update = safe_rows + (
            low_margin_rows if include_low_margin else []
        ) + (
            loss_rows if include_losses else []
        )
        confirmation_subject = (
            "al prezzo consigliato Buy Box"
            if use_recommended_price
            else "al prezzo preparato nella tabella"
        )
        confirm_price_update = st.checkbox(
            f"Confermo l’aggiornamento del prezzo principale {confirmation_subject} "
            f"per {len(rows_to_update):,} offerte su Worten Portogallo",
            value=False,
            key=(
                f"worten_price_confirm_{account['id']}_{selected_list['id']}_"
                f"{active_view_key}_{selection_signature}_{price_plan['mode']}"
            ),
        )

        price_csv = b""
        if rows_to_update:
            price_csv = build_price_update_offer_csv(
                rows_to_update,
                channel_code=channel_code,
            )
            with st.expander("Anteprima CSV aggiornamento prezzi"):
                st.code(
                    "\n".join(
                        price_csv.decode(
                            "utf-8-sig", errors="replace"
                        ).splitlines()[:4]
                    ),
                    language="text",
                )
            st.download_button(
                "Scarica CSV prezzi Worten selezionati",
                price_csv,
                file_name=f"prezzi_worten_pt_{selected_list['id']}.csv",
                mime="text/csv",
                key=(
                    f"worten_price_csv_{account['id']}_{selected_list['id']}_"
                    f"{active_view_key}_{selection_signature}"
                ),
            )

        submit_label = (
            "Invia prezzi consigliati Buy Box a Worten"
            if use_recommended_price
            else "Invia prezzi selezionati a Worten"
        )
        if st.button(
            submit_label,
            type="primary",
            use_container_width=True,
            disabled=not rows_to_update or not confirm_price_update,
            key=(
                f"worten_price_submit_{account['id']}_{selected_list['id']}_"
                f"{active_view_key}_{selection_signature}"
            ),
        ):
            try:
                response = upload_offer_csv(
                    api_key,
                    price_csv,
                    api_url=api_url,
                    shop_id=shop_id,
                    import_mode="NORMAL",
                )
                import_id = (
                    response.get("import_id")
                    or response.get("importId")
                    or response.get("id")
                )
                operation_details = [
                    {
                        "ok": True,
                        "sku_inviato": item["sku"],
                        "ean": item.get("ean"),
                        "prezzo_precedente": item["previous_price"],
                        "prezzo_inviato": item["price"],
                        "origine_prezzo": item.get("price_source"),
                        "commissione_pct": item.get("commission_pct"),
                        "fonte_commissione": item.get("commission_source"),
                        "guadagno_previsto": item["profit_eur"],
                        "margine_previsto": item["margin_pct"],
                        "esito_economico": item["status"],
                        "esito_buybox": item["buybox_outcome"],
                    }
                    for item in rows_to_update
                ]
                execute(
                    """INSERT INTO operations(
                    seller_id,marketplace_account_id,price_list_id,marketplace,
                    storefront,operation_type,status,total_rows,success_rows,
                    failed_rows,details_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        seller_id,
                        account["id"],
                        selected_list["id"],
                        "worten",
                        "pt",
                        "AGGIORNA PREZZO BUY BOX",
                        "submitted",
                        len(rows_to_update),
                        0,
                        0,
                        json_text(
                            {
                                "import_id": import_id,
                                "response": response,
                                "price_mode": price_plan["mode"],
                                "rows": operation_details,
                            }
                        ),
                        now_iso(),
                    ),
                )
                st.session_state[
                    f"worten_price_last_import_{account['id']}"
                ] = {
                    "import_id": import_id,
                    "response": response,
                }
                cached_live_offers_v94.clear()
                st.success(
                    f"Aggiornamento inviato: {len(rows_to_update):,} prezzi. "
                    f"ID import: {import_id or 'restituito nella risposta'}."
                )
            except Exception as error:
                st.error(f"Aggiornamento prezzi Worten non riuscito: {error}")

        last_price_import = st.session_state.get(
            f"worten_price_last_import_{account['id']}"
        )
        if last_price_import and last_price_import.get("import_id"):
            status_col, import_col = st.columns([1, 2])
            if status_col.button(
                "Verifica stato ultimo import",
                use_container_width=True,
                key=f"worten_price_import_status_{account['id']}",
            ):
                try:
                    last_price_import["status_response"] = offer_import_status(
                        api_key,
                        last_price_import["import_id"],
                        api_url=api_url,
                        shop_id=shop_id,
                    )
                    st.session_state[
                        f"worten_price_last_import_{account['id']}"
                    ] = last_price_import
                except Exception as error:
                    st.error(f"Verifica import non riuscita: {error}")
            import_col.caption(
                f"Ultimo import prezzi: {last_price_import['import_id']}"
            )
            if last_price_import.get("status_response"):
                st.json(last_price_import["status_response"])

    csv_data = result_frame.to_csv(index=False, sep=";").encode("utf-8-sig")
    st.download_button(
        "Scarica controllo Buy Box Worten CSV",
        csv_data,
        file_name=(
            f"buybox_worten_{storefront}_{selected_list['id']}_"
            f"{active_view_key}.csv"
        ),
        mime="text/csv",
        key=(
            f"worten_buybox_download_{account['id']}_{selected_list['id']}_"
            f"{active_view_key}_{visible_signature}"
        ),
    )

    history_operations = rows(
        """SELECT details_json,created_at,status FROM operations
        WHERE seller_id=? AND marketplace_account_id=? AND price_list_id=?
          AND marketplace='worten' AND storefront='pt'
          AND operation_type='AGGIORNA PREZZO BUY BOX'
        ORDER BY created_at DESC,id DESC LIMIT 100""",
        (seller_id, account["id"], selected_list["id"]),
    )
    history_rows = []
    for operation in history_operations:
        try:
            details = json.loads(operation.get("details_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for row in details.get("rows", []) if isinstance(details, dict) else []:
            if not isinstance(row, dict):
                continue
            history_rows.append(
                {
                    "EAN": row.get("ean"),
                    "SKU": row.get("sku_inviato"),
                    "Prezzo precedente": row.get("prezzo_precedente"),
                    "Nuovo prezzo": row.get("prezzo_inviato"),
                    "Commissione %": row.get("commissione_pct"),
                    "Fonte commissione": row.get("fonte_commissione"),
                    "Guadagno/Perdita €": row.get("guadagno_previsto"),
                    "Margine %": row.get("margine_previsto"),
                    "Esito": row.get("esito_economico"),
                    "Aggiornato": operation.get("created_at"),
                }
            )
    if history_rows:
        with st.expander("Storico aggiornamenti prezzo Worten"):
            st.dataframe(
                pd.DataFrame(history_rows[:500]),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Prezzo precedente": st.column_config.NumberColumn(format="%.2f"),
                    "Nuovo prezzo": st.column_config.NumberColumn(format="%.2f"),
                    "Commissione %": st.column_config.NumberColumn(format="%.2f"),
                    "Guadagno/Perdita €": st.column_config.NumberColumn(format="%.2f"),
                    "Margine %": st.column_config.NumberColumn(format="%.2f"),
                },
            )
else:
    st.info(
        "Non ci sono ancora controlli Buy Box salvati per questo listino e canale."
    )
