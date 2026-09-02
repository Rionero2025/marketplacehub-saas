from __future__ import annotations

import runpy
from pathlib import Path

import streamlit as st

from services.db import rows
from services.session import bootstrap, seller_selector

bootstrap();st.title("Pubblicazione sui Marketplace")
st.caption("Seleziona il Seller e uno dei marketplace precedentemente abilitati.")
seller_id=seller_selector()
if seller_id is None:st.stop()

accounts=rows("SELECT * FROM marketplace_accounts WHERE seller_id=? AND active=1 ORDER BY marketplace,account_name",(seller_id,))
if not accounts:
    st.warning("Questo Seller non possiede marketplace attivi. Abilitane uno in Gestione Seller.");st.stop()
marketplaces=sorted({x["marketplace"] for x in accounts})
labels={name.title():name for name in marketplaces}
chosen_label=st.selectbox("Marketplace sul quale lavorare",list(labels),key="publication_marketplace")
marketplace=labels[chosen_label]
account_count=sum(1 for x in accounts if x["marketplace"]==marketplace)
st.caption(f"Account attivi per {chosen_label}: {account_count}")
st.divider()

implementations={
    "kaufland":"3_Pubblicazione_Kaufland.py",
    "worten":"3_Pubblicazione_Worten.py",
}
if marketplace not in implementations:
    st.info(f"{chosen_label} è abilitato per il Seller, ma il modulo di pubblicazione non è ancora disponibile.")
    st.stop()

st.session_state["_embedded_marketplace_publication"]=True
try:
    runpy.run_path(str(Path(__file__).with_name(implementations[marketplace])),run_name=f"marketplace_{marketplace}")
finally:
    st.session_state.pop("_embedded_marketplace_publication",None)
