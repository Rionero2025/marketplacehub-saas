# Marketplace Hub v303 — Orders Speed Core

Baseline: v302 Speed Core. Il repository di produzione resta separato e non viene modificato.

## Obiettivo

Ridurre il costo di apertura/filtraggio degli ordini e creare il confine applicativo che verrà usato da FastAPI e worker nel SaaS.

## Ottimizzazioni reali

1. **Kaufland: finestra archivio SQL prima del rendering**
   - Default operativo: ultimi 90 giorni.
   - Alternative: ultimi 365 giorni / tutto archivio.
   - PostgreSQL non trasferisce più automaticamente tutto lo storico ad ogni rerun Streamlit.
   - Tutto lo storico rimane accessibile con un click.

2. **Paginazione server-side pronta per API**
   - `saved_orders_page()` per l'archivio Kaufland.
   - `cached_orders_page()` per la cache normalizzata Kaufland/Worten.
   - `LIMIT/OFFSET`, conteggio totale, `has_more`, ricerca e filtri SQL.
   - `raw_json` escluso di default dalle viste elenco.

3. **OrdersCore indipendente da Streamlit**
   - `OrderScope`
   - `OrderQuery`
   - `OrderPage`
   - `OrdersCore`
   - Lo stesso contratto è richiamabile da Streamlit, FastAPI e worker.

4. **Facet/metadata economici da leggere**
   - conteggio archivio;
   - prima/ultima data;
   - stati, storefront, valute, corrieri;
   - fornitori/stati per cache normalizzata.

5. **Indici v303**
   - indice per scope/data/paginazione Kaufland;
   - indice per scope/data/paginazione cache ordini normalizzata.

## Compatibilità

- `saved_orders()` resta disponibile ed è retrocompatibile.
- La logica economica degli ordini non cambia.
- La sincronizzazione API Kaufland/Worten non cambia.
- Nessuna cancellazione o migrazione distruttiva.
- La modalità «Tutto archivio» ripristina la lettura completa quando necessaria.

## Verifiche

- Compilazione Python completa: 113 file OK.
- Test Core: 8/8 passati.
