# Database e persistenza
DATABASE_TABLES.csv elenca 118 nomi di tabella rintracciati nelle dichiarazioni SQL. È uno schema statico: non certifica che tutte le tabelle siano migrate nell'istanza live.

## Equivalenze del Master Spec
| Concetto | Implementazione attuale | Limite |
|---|---|---|
| organizations | tenants (merchant/agency) | Platform gestita tramite is_admin, non come tipo tenant |
| organization relationships | agency_clients e tenant_sellers | Self-service Agency non completo |
| memberships/roles | tenant_memberships + app_users | Permessi legacy dell'utente, non matrice completa per membership |
| marketplace connections | marketplace_accounts | Credenziali cifrate; lifecycle ancora parziale |
| products/feed versions | price_lists, catalog_products, catalog_materializations | Generazioni/versioni atomiche mancanti |
| orders/items | kaufland_order_units, cecotec_order_cache | Modelli diversi per marketplace e per contabilità |
| accounting | accounting_order_lines + override e export | Conservare manuali e riconciliare fonti |
| shipments | famiglia packlink_* e tracking_* | API/UI SaaS mancanti |
| subscriptions | tenant_subscriptions, saas_plans, override, usage | Stripe non collegato realmente |
| job_runs | background_jobs | Coda persistente, recovery/idempotenza da completare |
| invoices/webhooks | nessun flusso integrato Stripe/Fatture in Cloud | Non confondere billing_events con webhook firmati |

## Isolamento
services/tenant_db.py applica policy RLS con tenant_id e contesto di connessione; molte tabelle business sono protette. Cataloghi condivisi e metadati di piattaforma sono esplicitamente gestiti diversamente. background_jobs è filtrato nell'API e rivendicato globalmente dal worker.
Controlli seller/tenant backend esistono. La copertura va provata su PostgreSQL reale con almeno due tenant e due utenti, comprese operazioni amministrative e membership differenti.
Le correzioni già pubblicate riguardano propagazione del contesto FastAPI e bootstrap abbonamenti nel worker; non dimostrano l'assenza di altri percorsi lazy non sicuri.

## Migrazioni e transazioni
Schema creato da molte ensure_* e init_db; non c'è una sequenza centralizzata di migrazioni versionate con downgrade.
register_merchant coordina più scritture e cleanup compensativo: non equivale a una singola transazione atomica.
set_price_list_sharing aggiorna policy, accessi e compatibilità legacy con scritture separate.
CatalogCore.materialize elimina i prodotti precedenti e inserisce chunk successivi: un errore può lasciare una materializzazione parziale e l'ultima versione valida non è preservata.
Proposta: migrazioni additive, generazioni per import, attivazione atomica della versione completa e rollback verso la versione precedente.

## File
services/object_storage.py, durable_files.py, saved_view_storage.py e supplier_document_storage.py sono basi riutilizzabili.
Il Blueprint imposta STORAGE_BACKEND=local per API/worker; senza storage condiviso i file creati da un processo possono mancare nell'altro. Configurazione live non certificata da questo audit.
Backup/restore multi-tenant ed export devono essere provati su storage remoto prima del lancio. Il semplice backup locale non basta.

## Indici e misure
performance_indexes.py crea indici per scope/account/data; CatalogCore indicizza SKU/EAN/costo/quantità.
Non sono stati raccolti EXPLAIN ANALYZE, slow-query log o misure p95/p99. Aggiungere indici solo dopo query e dataset rappresentativi. Vedere le prime PR in CURRENT_STATE_AUDIT.md.


## Aggiornamento porting — 4 settembre 2026

Nessuna migrazione di schema. Lo SKU recuperato viene persistito soltanto durante il refresh esplicito dei costi; nessun backfill al deploy. Override manuali conservati.
