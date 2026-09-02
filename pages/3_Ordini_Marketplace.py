from __future__ import annotations

import runpy
from pathlib import Path

import streamlit as st

from services.db import rows
from services.session import bootstrap, seller_selector


bootstrap()
st.title("Ordini Marketplace")
st.caption(
    "Sincronizza e analizza gli ordini ricevuti dagli account marketplace "
    "precedentemente abilitati."
)
seller_id = seller_selector()
if seller_id is None:
    st.stop()

accounts = rows(
    """
    SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND active=1 ORDER BY marketplace,account_name
    """,
    (seller_id,),
)
supported_marketplaces = {"kaufland", "worten"}
available = sorted({
    str(item["marketplace"]).strip().lower()
    for item in accounts
    if str(item["marketplace"]).strip().lower() in supported_marketplaces
})
if not available:
    st.warning(
        "Questo Seller non possiede account Kaufland o Worten attivi. "
        "Abilita almeno un marketplace in Gestione Seller."
    )
    st.stop()

labels = {"kaufland": "Kaufland", "worten": "Worten"}
marketplace = st.selectbox(
    "Marketplace degli ordini",
    available,
    format_func=lambda value: labels.get(value, value.title()),
    key=f"orders_marketplace_{seller_id}",
)
st.divider()

implementations = {
    "kaufland": "3_Ordini_Kaufland.py",
    "worten": "3_Ordini_Worten.py",
}
st.session_state["_embedded_marketplace_orders"] = True
try:
    runpy.run_path(
        str(Path(__file__).with_name(implementations[marketplace])),
        run_name=f"orders_{marketplace}",
    )
finally:
    st.session_state.pop("_embedded_marketplace_orders", None)

