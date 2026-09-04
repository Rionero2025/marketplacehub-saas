# Moduli e parità funzionale

Baseline: 28 pagine Streamlit del repository originale, tutte presenti anche nel SaaS; il SaaS aggiunge la pagina Streamlit Ordini Worten. La presenza dei file non equivale alla disponibilità nel frontend Next.js.

Stati riferiti all’intero flusso SaaS: nessuna delle 28 aree è dichiarata completa senza riserve. La sincronizzazione base Kaufland è verificata, ma la pagina originale offre più capacità.

| Area originale | Stato SaaS | Evidenza | Lavoro mancante |
|---|---|---|---|
| Dashboard vendite e alert (0_Dashboard) | PARTIAL | frontend/src/app/dashboard/page.tsx | Conteggi ordini/account e job; assenti Top 10, trend vendite, margini e dashboard differenziate. |
| Seller, marketplace, logistica (1_Gestione_Seller) | PARTIAL | api/routers/onboarding.py; frontend/src/app/settings/page.tsx | Registrazione e connessione operative; manca gestione completa seller e configurazione logistica. |
| Utenti e assegnazioni (1_Gestione_Utenti) | PARTIAL | api/routers/tenants.py | Membership amministrative presenti; nessuna schermata team, inviti o assegnazioni Agency. |
| Fornitori e feed (2_Fornitori_e_Listini) | PARTIAL | api/routers/catalogs.py; frontend/src/app/catalogs/page.tsx | Elenco listini e API query/condivisione; assenti editor fornitore, upload/feed e azioni della pagina originale. |
| Provider e profili AI (2_Provider_IA) | MISSING | services/ai_providers.py | Servizio conservato; API e pagina SaaS assenti. |
| Assistenza Kaufland (3_Assistenza_Kaufland) | MISSING | services/kaufland_support.py | Motore presente; nessun router/support frontend. |
| Assistenza unificata (3_Assistenza_Marketplace) | MISSING | services/support_hub.py; services/support_connectors.py | Thread, lettura e risposte conservati nel legacy; accesso SaaS mancante. |
| Cancellazione offerte Kaufland (3_Cancellazione_Kaufland) | MISSING | services/kaufland.py; services/kaufland_live_inventory.py | Nessun comando API/UI SaaS di cancellazione. |
| Cancellazione marketplace (3_Cancellazione_Marketplace) | MISSING | pages/3_Cancellazione_Marketplace.py | Percorso Streamlit presente; non esposto nel nuovo frontend. |
| Cancellazione offerte Worten (3_Cancellazione_Worten) | MISSING | services/worten.py | CSV/API legacy presenti; non esposti nel SaaS. |
| Buy Box Kaufland (3_Controllo_BuyBox) | PARTIAL | api/routers/buybox.py; frontend/src/app/buybox/page.tsx | API GET e riepilogo JSON; mancano avvio controllo, tabella operativa, selezione, filtri margine e pricing. |
| Buy Box Worten (3_Controllo_BuyBox_Worten) | PARTIAL | api/routers/buybox.py; frontend/src/app/buybox/page.tsx | Stesso limite; commissioni e prezzi disponibili nel motore originale. |
| Taxonomy, AI, validazione, pubblicazione (3_Creazione_Prodotti) | MISSING | services/catalog_intelligence/workflow.py; services/catalog_intelligence/publication.py | Motore nel repository; nessuna route o pagina SaaS dedicata. |
| Elaborazione listini e viste (3_Lavora_sui_Listini) | PARTIAL | marketplace_core/catalogs.py; api/routers/catalogs.py | Materializzazione/query backend; manca UI prodotti, trasformazioni, selezioni e viste salvate. |
| Ordini Kaufland (3_Ordini_Kaufland) | PARTIAL | api/routers/orders.py; frontend/src/app/orders/page.tsx | Sync e archivio verificati su 1000 unità; UI non espone tutti i filtri, import/export, colonne tracking/commissioni, Tutti disponibili e opzione tracking. |
| Ordini marketplace / Worten (3_Ordini_Marketplace) | PARTIAL | api/routers/orders.py; services/background_jobs.py | Percorso Worten disponibile nel codice, non provato live; mancano piena parità filtri e azioni. |
| Top prodotti e analytics (3_Prodotti_Piu_Venduti) | MISSING | services/product_stats.py | Calcoli legacy conservati; nessun endpoint/frontend Top 10. |
| Invio offerte Kaufland (3_Pubblicazione_Kaufland) | MISSING | services/kaufland.py | Workflow della pagina legacy non raggiungibile dal SaaS. |
| Invio marketplace (3_Pubblicazione_Marketplace) | MISSING | pages/3_Pubblicazione_Marketplace.py | Selezioni e invio del legacy da collegare al core/API. |
| Invio offerte Worten (3_Pubblicazione_Worten) | MISSING | services/worten.py | Pubblicazione legacy presente; nessun comando nuovo frontend. |
| Contabilità operativa completa (4_Contabilita) | PARTIAL | api/routers/accounting.py; frontend/src/app/accounting/page.tsx | UI solo JSON status; API sync/refresh-costs. Mancano righe editabili, manuali, import/export, riconciliazione e totali interattivi. Divergenza SKU: ISSUE-02. |
| Ordini fornitore Cecotec (4_Ordini_Cecotec) | MISSING | services/cecotec_orders.py | Logica e template conservati; API/UI SaaS assenti. |
| Ordini fornitore Innpro (4_Ordini_INNPRO) | MISSING | services/innpro_orders.py | Logica, dedup export e storico conservati; API/UI assenti. |
| Packlink e spedizioni (4_Packlink_PRO) | MISSING | marketplace_core/packlink.py; services/packlink.py; services/packlink_mass.py | Core e worker disponibili; nessun router o pagina Packlink. |
| Storico operazioni (4_Storico) | MISSING | pages/4_Storico.py | Attività mostra solo job, non sostituisce lo storico operativo originale. |
| Tracking e documenti (4_Tracciabilita_Ordini) | MISSING | marketplace_core/tracking.py; services/order_tracking.py | Core e worker presenti; API/UI e aggiornamento marketplace non esposti. |
| Backup e trasferimento (5_Backup_Trasferimento) | MISSING | services/data_transfer.py | Logica legacy presente; workflow SaaS tenant-scoped da progettare e testare. |
| Amministrazione dati (5_Database) | MISSING | pages/5_Database.py | Funzione tecnica legacy; va mantenuta come capacità amministrativa controllata, non resa pubblica ai seller. |

## Inventario granulare

LEGACY_CONTROLS.csv conserva controlli e azioni trovati staticamente nelle pagine delle due sorgenti. SYMBOL_INVENTORY.csv conserva tutti i simboli Python individuati; SOURCE_COMPARISON.csv distingue file identici, differenti e assenti.
Gli 838 controlli del repository originale includono filtri, pulsanti, selezioni e contenitori: non sono 838 funzioni indipendenti. L’inventario non esegue le azioni e non certifica il comportamento dei singoli controlli.
Le righe locali duplicate sono intenzionali: servono a preservare differenze di versione.

## Differenze che richiedono una decisione di porting

- Recupero SKU composito dal raw JSON presente nel services/accounting.py originale, assente nel SaaS: funzione chiamata dalla normalizzazione Worten e dal refresh costi. Da ripristinare con casi di regressione.
- Due moduli locali, product_localization.py e catalog_intelligence/enrichment.py, assenti in entrambi i repository confrontati. Esistono test locali dedicati, ma non ho trovato chiamate dalle pagine locali: non sono certificati come feature attive. Vanno conservati nell’inventario e valutati prima di qualsiasi eliminazione.
- quick_one e update_progress rimossi dalle pagine SaaS: i flussi sono stati spostati nel core/worker. La sola differenza di nomi non è una regressione.
- accounting.py alla radice del repository originale è assente nel SaaS, dove esiste services/accounting.py: due varianti, non prova di perdita dell’intera contabilità.
- 116 file di test locali non sono stati portati in tests_core. La loro assenza va affrontata separatamente dal codice di produzione.


## Aggiornamento porting — 4 settembre 2026

Contabilità: ripristinati recupero SKU dal raw JSON e persistenza al refresh. La UI contabile rimane PARTIAL; questa correzione non chiude il modulo.


### Blocco 03 — verifica autorizzazioni

Utenti/assegnazioni: il ruolo viewer non può mutare aree operative tramite API; i ruoli Agency sono ereditati dai clienti. UI team e dashboard dedicate restano incomplete.


### Blocco 04 — sincronizzazioni duplicate

Ordini: invii identici possono riutilizzare il job queued/running. Parametri, marketplace, account o tenant diversi rimangono separati. La parità completa della pagina Ordini resta aperta.


### Blocco 05 — cataloghi atomici

Cataloghi: su errore restano prodotti e metadata precedenti. Import concorrenti preparano separatamente i dati e pubblicano una versione completa per volta. I parser supplier e normalize restano condivisi.


### Blocco 06 — recupero job interrotti

Background jobs: retry automatico solo per import ordini Kaufland/Worten e materializzazione cataloghi, fino a tre tentativi. Le altre attività interrotte terminano con richiesta di verifica, evitando ripetizioni automatiche di operazioni esterne.


### Blocco 07 — integrità e storage condiviso

Viste salvate: oggetti e copie locali immutabili per digest, verifica seller prima della scrittura, recupero di cache corrotte. Listini: verifica cache e mantenimento versioni precedenti. Parser e algoritmi dei listini invariati.


### Hotfix — elenco cataloghi HTTP 500

Endpoint elenco cataloghi: correzione HTTP 500 sulla dashboard e sulla pagina cataloghi; invariati i campi restituiti e i criteri di visibilità.


### Prima struttura macroaree e piani Enterprise

Prima base macroaree: prezzi nel sito, signup Seller/Agency con Enterprise trial, Admin prezzi e trial, Agency elenco clienti assegnati. Sono punti di ingresso funzionanti, non il porting completo delle rispettive aree.


### Accessi distinti Seller, Agenzia e Admin

Tre ingressi distinti: Seller /dashboard, Agenzia /agency, Admin /internal/admin. Shell Admin senza caricamenti Seller/jobs; shell Agenzia dedicata e ritorno al contesto agenzia dopo apertura cliente. Avvio pannello Seller sulle fonti dati esistenti; nessuna nuova funzione legacy dichiarata completata.
