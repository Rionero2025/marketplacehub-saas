from __future__ import annotations

import os
import streamlit as st

from services.db import init_db, sellers
from services.performance_indexes import ensure_performance_indexes


def bootstrap():
    init_db()
    try:
        ensure_performance_indexes()
    except Exception:
        # Alcune tabelle Buy Box vengono create lazy dalla relativa pagina.
        # Gli indici saranno riprovati quando il modulo viene inizializzato.
        pass
    try:
        master = str(st.secrets.get("MARKETPLACE_HUB_MASTER_KEY", ""))
        if master:
            os.environ["MARKETPLACE_HUB_MASTER_KEY"] = master
    except Exception:
        pass


def seller_selector(label="Seller attivo") -> int | None:
    # services.db.sellers() applica già lo scope dell'utente autenticato.
    data = sellers()
    if not data:
        st.warning(
            "Non hai Seller autorizzati disponibili per questa area. "
            "Contatta l'amministratore."
        )
        st.session_state.pop("active_seller_id", None)
        return None

    labels = {f"{x['name']}  ·  ID {x['id']}": int(x["id"]) for x in data}
    seller_ids = list(labels.values())
    previous_id = st.session_state.get("active_seller_id")
    try:
        previous_id = int(previous_id) if previous_id is not None else None
    except (TypeError, ValueError):
        previous_id = None

    # Rimuove una scelta widget rimasta in sessione se l'admin ha revocato il Seller.
    current_widget_value = st.session_state.get("global_seller_selector")
    if current_widget_value not in labels:
        st.session_state.pop("global_seller_selector", None)

    default_index = seller_ids.index(previous_id) if previous_id in seller_ids else 0
    chosen = st.selectbox(
        label,
        list(labels),
        index=default_index,
        key="global_seller_selector",
    )
    selected = labels[chosen]
    st.session_state["active_seller_id"] = selected
    return selected
