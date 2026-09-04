# Integrazioni: codice disponibile e operatività SaaS
| Integrazione | Fonte nel motore | Stato nel nuovo SaaS |
|---|---|---|
| Kaufland | services/kaufland.py, kaufland_orders.py, kaufland_buybox*, kaufland_support* | Connessione e sync ordini verificate; Buy Box/parità operativa parziali; pubblicazione/cancellazione/support non esposti |
| Worten/Mirakl | services/worten.py, worten_support.py, worten_tracking_api.py | Connessione/sync nel codice; non verificati live in questo audit; altre funzioni prevalentemente legacy |
| Innpro | services/lists.py, innpro_orders.py | Feed/export nel motore; UI fornitore/export mancante |
| Hurtel | services/lists.py: combine_hurtel_feeds | Full/light e merge preservati; configurazione SaaS assente |
| ActiveShop | services/lists.py: combine_activeshop_stock | Parser e prezzi/stock presenti; configurazione SaaS assente |
| Cecotec | services/lists.py, cecotec_orders.py | Prezzi paese, stock/export nel motore; workflow SaaS mancante |
| AB Online | services/abonline.py | Gateway esistente; whitelist/accesso live non verificati; UI mancante |
| OriginalQU | services/lists.py | Gestione feed presente; non è certificata la parità di ogni variante |
| Packlink | services/packlink.py, packlink_mass.py, packlink_csv.py | Core/worker e fallback CSV presenti; router/frontend assenti |
| AI/taxonomy | services/catalog_intelligence/*, ai_providers.py | Catena esistente nel legacy; nessuna area SaaS dedicata |
| Stripe | services/billing.py, api/routers/billing.py | Stati e campo provider, non Checkout/Billing/Portal/webhook firmati |
| Fatture in Cloud | nessun modulo/API rilevato | MISSING |
| OBI, About You, KuantoKusta | riferimenti/possibili percorsi generici da censire | Nessun adapter SaaS dedicato certificato; roadmap, non dichiarare operativo |

L'interfaccia unica SupplierAdapter/MarketplaceAdapter del Master Spec è una destinazione progettuale; non esiste ancora come contratto uniforme per tutte queste integrazioni. Conservare le peculiarità dei servizi funzionanti.

## Account reale marketplace
onboarding.connect_marketplace prova le credenziali e ricava metadati; Kaufland usa il display_name se restituito, altrimenti un alias basato sugli storefront reali. Non inventa un nome negozio.
Il requisito del nome pubblico esatto resta PARTIAL finché l'API/account non lo espone e la risposta non è verificata. L'assenza del nome non va aggirata con dati inventati.

## Verifica live disponibile
Kaufland, stesso job/parametri dell'utente: 1000 unità lette e salvate, zero errori; archivio leggibile sotto tenant scope. Non sono stati testati pagamenti, spedizioni, aggiornamenti prezzo o cancellazioni reali.


## Aggiornamento porting — 4 settembre 2026

Worten: recupero del costo incorporato nello SKU ripristinato; nessuna chiamata marketplace live necessaria o eseguita nei test del porting.


### Blocco 03 — verifica autorizzazioni

Nessuna chiamata marketplace o integrazione esterna modificata in questo blocco; verifica del solo confine di autorizzazione.


### Blocco 04 — sincronizzazioni duplicate

Nessuna chiamata marketplace modificata: si evita l’avvio duplicato dello stesso lavoro. Nessuna nuova importazione live avviata per le prove.


### Blocco 05 — cataloghi atomici

CSV: dopo aver emesso un chunk, un errore interrompe l’import invece di ricominciare dal parser fallback e duplicare righe. XML/Excel conservano il parser originario. Nessuna modifica ai feed remoti.


### Blocco 06 — recupero job interrotti

Nessuna ripetizione automatica di creazione spedizioni, cambi prezzo o altre operazioni esterne di esito incerto. Gli import ammessi hanno semantica almeno una volta: non si dichiara esecuzione esattamente una volta né annullamento di effetti esterni.


### Blocco 07 — integrità e storage condiviso

S3 distingue oggetto assente da accesso negato e riporta errori di cancellazione. CLI tools.storage_probe scrive un piccolo oggetto sintetico e lo verifica da un altro processo senza dati cliente; serve una prova live sui due servizi.


### Hotfix — elenco cataloghi HTTP 500

Errore confermato nei log API Render il 4 settembre 2026 alle 15:51:55 Europe/Rome: InvalidColumnReference nell’elenco cataloghi. Le API ordini e stato contabilità restituivano 200 nello stesso flusso.


### Prima struttura macroaree e piani Enterprise

Nessuna creazione di prezzi o addebiti Stripe. Trial con provider manual e durata server. Interfaccia Admin utilizza le API billing esistenti; le credenziali marketplace e i parser legacy non cambiano.


### Accessi distinti Seller, Agenzia e Admin

Nessun nuovo provider o addebito. Login seller/agency/admin usa le credenziali esistenti e il cookie di sessione già presente. Sessione condivisa nel browser, non tre sessioni simultanee indipendenti.
