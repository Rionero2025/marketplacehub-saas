# Marketplace Hub v304 — Buy Box Speed Core

## Obiettivo
Separare il controllo Buy Box dalla UI e preparare una lettura server-side veloce per il SaaS.

## Modifiche
- nuovo `marketplace_core.buybox.BuyBoxCore`;
- contratto job `buybox.<marketplace>.<quick|full>` pronto per worker;
- controllo rapido Kaufland spostato fuori da Streamlit: una sola richiesta `/buybox` per offerta, parallelismo conservato;
- endpoint Core paginato per risultati Buy Box salvati, con `LIMIT/OFFSET` e senza `details_json` di default;
- indici PostgreSQL/SQLite per scope Buy Box, checked_at, EAN/SKU, live units e righe contabili;
- nessuna modifica alle regole economiche o agli algoritmi Buy Box esistenti.

## Effetto architetturale
La pagina Streamlit resta compatibile, ma il percorso rapido non dipende più dalla UI. FastAPI e i futuri worker potranno richiamare lo stesso Core.
