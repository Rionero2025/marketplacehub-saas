# Marketplace Hub v306 — Worten + Contabilità in background

## Obiettivo
Proseguire la separazione tra UI e lavoro pesante. La v306 porta nella coda persistente i flussi Worten e Contabilità, mantenendo invariata la business logic esistente.

## Novità
- `orders.worten.sync`: sincronizzazione ordini Worten come job persistente.
- Nuova vista **Ordini Worten** nella pagina Ordini Marketplace.
- Archivio Worten paginato server-side tramite `OrdersCore` e PostgreSQL.
- `accounting.orders.sync`: aggiornamento incrementale/completo della Contabilità in background, sia Kaufland sia Worten.
- `accounting.costs.refresh`: ricalcolo costi in background con avanzamento; mantiene il matching EAN → SKU → SKU composito → costo incorporato.
- Le credenziali marketplace non vengono inserite nel payload del job: il worker le recupera dal database e le decifra al momento dell'esecuzione.
- La pagina Contabilità resta utilizzabile durante sincronizzazione e ricalcolo costi.

## Runtime transitorio e SaaS
La UI avvia ancora un daemon thread locale quando non esiste un worker esterno. La coda e lo stato sono però PostgreSQL-persistenti. Il comando `python tools/run_worker.py` usa gli stessi handler e permetterà di spostare questi job in un worker Render separato senza riscrivere le pagine.

## Compatibilità
La v306 è costruita sopra la v305. Nessuna modifica distruttiva ai dati o alle tabelle contabili esistenti.
