# Database

## Stato originale

La versione 271 supporta SQLite e PostgreSQL. Lo schema viene creato dai servizi durante l'avvio o al primo uso. Sono state rilevate 98 tabelle applicative; il conteggio include la tabella tecnica di write probe.

Gruppi principali:

- anagrafiche: `sellers`, `suppliers`, `price_lists`, `price_list_access`, `marketplace_accounts`, `commercial_rules`;
- operazioni e viste: `operations`, `saved_views`, `saved_view_marketplaces`, `dashboard_sync_state`;
- ordini e inventario Kaufland: unità ordine, sync, unità live, cursori e stati inventario;
- Buy Box Kaufland e Worten: controlli, viste e aggiornamenti prezzo;
- contabilità: righe ordine, preferenze catalogo, override, import Excel, export e stato sync;
- ordini fornitori: cache/export Cecotec ed export Innpro;
- Packlink: integrazioni, mittenti, bozze, storico, colli, memoria prodotto, match, sync e spedizioni;
- tracking: import, file sorgente e match;
- supporto: thread, messaggi, sync, azioni, impostazioni IA e bozze;
- catalog intelligence: 34 tabelle per capability, taxonomy, prodotti canonici, evidenze, mapping, validazione, IA, pubblicazione e audit.

L'elenco esatto e i file che dichiarano ogni tabella sono ricavabili dal manifest di riferimento e dal codice citato in `CURRENT_STATE_AUDIT.md`.

## Limiti rispetto al SaaS

- manca la gerarchia `PLATFORM → AGENCY → SELLER`;
- mancano membership, ruoli granulari e autorizzazioni persistenti;
- mancano piani, prezzi, sottoscrizioni, entitlement, utilizzo e fatture;
- lo scope esistente per `seller_id` e account non sostituisce una policy tenant uniforme;
- alcune DDL sono duplicate fra `services/db.py` e servizi specifici;
- le migrazioni sono procedurali nel runtime e non hanno ancora una cronologia versionata unica;
- dati durevoli, cache e artefatti non hanno ovunque una classificazione uniforme.

## Regole per la nuova base dati

- PostgreSQL è la sorgente dati autorevole del SaaS;
- ogni record di dominio tenant deve essere raggiungibile da `organization_id` e, quando applicabile, da `seller_id`;
- vincoli, indici e unique key devono incorporare lo scope corretto;
- le query devono ricevere lo scope dal contesto autorizzato, mai dal solo input del browser;
- migrazioni versionate e reversibili sostituiscono la creazione opportunistica delle tabelle;
- job, webhook, import ed export devono avere chiavi di idempotenza;
- le credenziali restano cifrate e non vengono restituite al frontend;
- cache e file rigenerabili devono poter essere eliminati senza perdita di dati contabili o operativi.

## Stato della nuova base dati

Il Blocco B02 ha introdotto PostgreSQL, pool configurabile e la catena Alembic `20260907_0001`. La migrazione iniziale non crea entità di dominio: stabilisce un punto di upgrade/downgrade prima del modello multi-tenant.
