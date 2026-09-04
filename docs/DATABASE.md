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


### Blocco 03 — verifica autorizzazioni

Aggiunte prove CI con PostgreSQL 17 temporaneo, ruolo senza superuser/BYPASSRLS e policy/contesto applicativi. Nessuna migrazione live. Le prove non certificano tutte le tabelle né tutte le migrazioni.


### Blocco 04 — sincronizzazioni duplicate

Nessuna modifica schema o backfill. Le righe attive preesistenti sono confrontate dopo decodifica JSON. Il lock PostgreSQL dura fino a commit/rollback; i test usano anche il compatibility wrapper SQL di produzione.


### Blocco 05 — cataloghi atomici

Nessuna migrazione permanente. Tabelle staging con nomi UUID interni, rimosse alla fine o in rollback. Commit per chunk solo nello staging; il contesto PostgreSQL viene riapplicato dopo ogni commit. La transazione finale resta proporzionale alle righe da sostituire; benchmark grandi volumi ancora nel blocco 11.


### Blocco 06 — recupero job interrotti

Nessuna migrazione. Il recupero serializza le righe con FOR UPDATE SKIP LOCKED su PostgreSQL e BEGIN IMMEDIATE su SQLite. Le condizioni del claim impediscono a un vecchio worker di modificare stato, risultato, progressi o heartbeat di un nuovo tentativo.


### Blocco 07 — integrità e storage condiviso

Nessuna migrazione. La chiave oggetto e hash esistenti identificano la versione pubblicata; un errore di aggiornamento metadata lascia leggibile quella precedente. I backup devono conservare riferimenti coerenti agli oggetti.


### Hotfix — elenco cataloghi HTTP 500

Nessuna migrazione: ORDER BY lower(supplier_name),lower(name),id applicato al risultato deduplicato. PostgreSQL rifiutava ORDER BY lower(s.name) nella SELECT DISTINCT poiché tale espressione non era selezionata.


### Prima struttura macroaree e piani Enterprise

Seed saas_plans con ON CONFLICT DO NOTHING: i valori commerciali modificati non vengono riscritti al riavvio. Nascondere i quattro vecchi piani dal catalogo pubblico mantenendo abbonamenti esistenti. Nessuna cancellazione o riconversione automatica di tenant.


### Accessi distinti Seller, Agenzia e Admin

issue_session accetta un tenant iniziale opzionale e verifica di nuovo l’accesso. Nessuna migrazione o modifica ruoli esistenti. Query clienti Agency senza DISTINCT non necessario, per evitare errore PostgreSQL sull’ordinamento lower(name); deduplica e ruolo più alto restano nella mappa esistente.


### Porting della dashboard Seller

Nessuna migrazione. accounting_order_lines e accounting_manual_overrides alimentano i calcoli. Lettura singola delle colonne leggere del Seller, con account appartenente allo stesso Seller; quote da sellers. Date legacy italiane e conversione Europe/Rome preservate: le righe vengono filtrate dal motore originale, non con confronto lessicografico SQL.


### Contabilità operativa Seller

Nessuna migrazione. Edit persistenti in accounting_manual_overrides; guardia ottimistica sui valori originali dei 16 campi e lock PostgreSQL della riga prima del salvataggio. Query di aggiornamento SaaS con seller_id. Fix UNION ALL listini: saved_view_id presente in entrambi i SELECT.
