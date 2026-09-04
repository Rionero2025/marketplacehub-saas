# Registro decisioni
## Confermate dall'utente
- Tutte le funzionalità del programma Marketplace Hub devono essere riutilizzate nel nuovo SaaS; non basta replicare le pagine.
- Logica e algoritmi condivisi Python, preservando dati e compatibilità. Nessuna riscrittura totale senza motivazione e test.
- Ordine: audit → stabilizzazione/performance → modello SaaS → dashboard → billing/fatturazione → marketing/onboarding → hardening.
- Un solo design system: React/TypeScript/Tailwind/shadcn-style; Shadcn Admin base tecnica, TailAdmin riferimento Platform, Shadcn Admin Agency, Horizon Seller; Radiant per il sito pubblico.
- Stripe e Fatture in Cloud sono destinazioni richieste, non integrazioni già operative.
- Questa fase produce documentazione e roadmap, non nuove funzionalità.

## Proposte, NON ancora approvate
- Le cinque PR contenute in CURRENT_STATE_AUDIT.md.
- Centralizzazione migrazioni e generazioni atomiche cataloghi.
- Percorso di reintegrazione test locali e recupero hotfix SKU.
- Nessun prezzo, nuovo piano, dominio, licenza template o ampliamento dei permessi è stato attivato.


## Aggiornamento porting — 4 settembre 2026

Riutilizzare gli helper SKU originali. Adattamento giustificato da due regressioni riprodotte: nei payload {order,line} si cerca solo nella line corrente, per non usare costo/SKU di un prodotto diverso. Definita metrica di avanzamento a 40 blocchi in SAAS_PROGRESS.md.


### Blocco 03 — verifica autorizzazioni

Owner/admin/manager/operator possono scrivere nelle aree già autorizzate; viewer e ruoli sconosciuti sono sola lettura. Il ruolo diretto sul cliente prevale sull’accesso Agency. Billing e configurazione piani restano Platform Admin. I limiti legacy sui seller si applicano anche alle letture di un altro tenant autorizzato.


### Blocco 04 — sincronizzazioni duplicate

Deduplicare soltanto orders.kaufland.sync e orders.worten.sync. Job terminali consentono nuove richieste. Il riuso non consuma quota e non viene rifiutato se la prima richiesta ha esaurito il limite. Recupero job orfani resta blocco separato.


### Blocco 05 — cataloghi atomici

Preservare l’ultima versione completa durante la preparazione. Un file sorgente cambiato durante l’import abortisce la pubblicazione. Catalogo vuoto resta una versione vuota valida, come nella semantica precedente. Gli errori sono riportati dal job, senza marcare corrotta la versione valida.


### Blocco 06 — recupero job interrotti

Lease conservativa: heartbeat 30 s, scadenza 300 s, massimo 3 tentativi, batch recupero 100 righe. Il thread Python del vecchio handler non viene terminato forzatamente: la protezione riguarda le scritture di stato del job, gli import devono restare idempotenti. Errori normali del handler non sono riaccodati automaticamente.


### Blocco 07 — integrità e storage condiviso

Non modificare il backend Render finché il bucket non è disponibile e i file esistenti non sono inventariati. Conservare oggetti precedenti per lettori/backup; retention e garbage collection da definire. Le funzioni legacy di migrazione saltano le chiavi esistenti e non bastano al passaggio local→S3.


### Hotfix — elenco cataloghi HTTP 500

Correzione SQL circoscritta, senza nascondere il 500 con un elenco vuoto. Test HTTP reali su query SQLite/PostgreSQL per catalogo vuoto, ordinamento, deduplica, grant e scope seller.
