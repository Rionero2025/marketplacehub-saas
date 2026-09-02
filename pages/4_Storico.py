from __future__ import annotations

import json
import pandas as pd
import streamlit as st

from services.db import rows
from services.session import bootstrap, seller_selector

bootstrap();st.title("Storico operazioni")
seller_id=seller_selector()
if seller_id is None:st.stop()

data=rows("""SELECT o.*,ma.account_name,pl.name price_list_name
FROM operations o LEFT JOIN marketplace_accounts ma ON ma.id=o.marketplace_account_id
LEFT JOIN price_lists pl ON pl.id=o.price_list_id
WHERE o.seller_id=? ORDER BY o.id DESC""",(seller_id,))
if not data:st.info("Nessuna operazione per questo Seller.");st.stop()

marketplaces=sorted({x["marketplace"] for x in data})
f1,f2=st.columns(2);market=f1.selectbox("Marketplace",["Tutti"]+marketplaces);status=f2.selectbox("Stato",["Tutti","submitted","success","partial","failed"])
filtered=[x for x in data if (market=="Tutti" or x["marketplace"]==market) and (status=="Tutti" or x["status"]==status)]
st.dataframe([{"ID":x["id"],"Data":x["created_at"],"Marketplace":x["marketplace"],"Paese":x["storefront"],"Account":x["account_name"],"Listino":x["price_list_name"],"Operazione":x["operation_type"],"Stato":x["status"],"Totale":x["total_rows"],"OK":x["success_rows"],"Errori":x["failed_rows"]} for x in filtered],use_container_width=True,hide_index=True)

omap={f"ID {x['id']} · {x['created_at']} · {x['operation_type']}":x for x in filtered}
if omap:
    chosen=st.selectbox("Dettaglio operazione",list(omap));op=omap[chosen]
    detail=json.loads(op["details_json"] or "[]")
    st.json(detail)
    detail_rows=detail.get("rows",[]) if isinstance(detail,dict) else detail
    failures=[x for x in detail_rows if not x.get("ok",False)] if isinstance(detail_rows,list) else []
    if failures:
        st.download_button("Scarica errori CSV",pd.DataFrame(failures).to_csv(index=False).encode("utf-8"),f"errori_operazione_{op['id']}.csv","text/csv")
