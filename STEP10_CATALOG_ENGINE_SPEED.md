# v310 — Catalog / Product Engine Speed

Obiettivo: smettere di riparsare listini XML/Excel/CSV ad ogni rerun Streamlit.

- `CatalogCore` normalizza il listino una volta e lo materializza nel database.
- CSV grandi vengono letti a chunk; XML/Excel usano i parser ufficiali esistenti ma nel worker background.
- La preview in Fornitori e Listini legge solo 200 righe server-side.
- `Lavora sui listini` riusa il catalogo normalizzato e una cache DataFrame locale bounded (2 cataloghi) durante i rerun.
- Job `catalog.materialize`: nel payload passa solo `price_list_id`, mai credenziali o file completi.
- Indici su EAN, SKU, costo e quantità preparano la query server-side del prossimo step.

Questa release mantiene compatibilità con i parser e le regole fornitore esistenti. La v311 sposterà anche filtri/editor/sele﻿zione del listino a paginazione server-side completa.
