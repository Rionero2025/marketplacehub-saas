from __future__ import annotations

import runpy
from pathlib import Path

import streamlit as st

from services.db import rows
from services.session import bootstrap, seller_selector

bootstrap();st.title("Cancellazione dai Marketplace")
st.caption("Seleziona il Seller e il marketplace dal quale rimuovere le offerte già pubblicate.")
seller_id=seller_selector()
if seller_id is None:st.stop()

accounts=rows("SELECT * FROM marketplace_accounts WHERE seller_id=? AND active=1 ORDER BY marketplace,account_name",(seller_id,))
if not accounts:
    st.warning("Questo Seller non possiede marketplace attivi.");st.stop()

marketplaces=sorted({item["marketplace"] for item in accounts})
labels={name.title():name for name in marketplaces}
chosen_label=st.selectbox("Marketplace dal quale cancellare le offerte",list(labels),key="deletion_marketplace")
marketplace=labels[chosen_label]
st.caption(f"Account attivi per {chosen_label}: {sum(1 for item in accounts if item['marketplace']==marketplace)}")
st.divider()

implementations={
    "kaufland":"3_Cancellazione_Kaufland.py",
    "worten":"3_Cancellazione_Worten.py",
}
if marketplace not in implementations:
    st.info(f"La cancellazione automatica per {chosen_label} non è ancora disponibile.")
    st.stop()

st.session_state["_embedded_marketplace_deletion"]=True
try:
    runpy.run_path(str(Path(__file__).with_name(implementations[marketplace])),run_name=f"delete_{marketplace}")
finally:
    st.session_state.pop("_embedded_marketplace_deletion",None)
