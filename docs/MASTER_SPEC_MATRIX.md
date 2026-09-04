# Matrice Master Spec

Stati riferiti al nuovo SaaS, non alla sola presenza del motore legacy. DONE indica il requisito circoscritto verificato; PARTIAL un percorso incompleto; MISSING assenza del risultato richiesto; NEEDS_REFACTOR logica esistente da adattare.

Il CSV allegato conserva ogni punto elenco/numerato e tutte le sezioni con linea del documento originale. I punti atomici non provati separatamente mantengono la valutazione conservativa della sezione e il relativo limite. Non rappresenta una percentuale di prodotto completato.

| Sezione | Stato | Riscontro |
|---|---|---|
| 0 | PARTIAL | Audit eseguito; migrazione incrementale proposta, test e feature flag non completi. Evidenza: ARCHITECTURE.md; MODULES.md |
| 1 | PARTIAL | Motore operativo esteso; ciclo end-to-end ancora incompleto nel SaaS. Evidenza: MODULES.md |
| 2 | PARTIAL | Un workspace comune; tre dashboard e gestione completa Platform/Agency mancanti. Evidenza: frontend/src/components/AppShell.tsx; api/routers/tenants.py |
| 3 | PARTIAL | Tenant agency/merchant e relazioni esistono; RBAC per membership e Platform support da completare. Evidenza: services/tenancy.py; api/session_store.py |
| 4 | PARTIAL | App Next presente, marketing/Radiant/domini pubblici non implementati nel repository. Evidenza: frontend/src/app; frontend/package.json |
| 5 | MISSING | Stile custom condiviso esistente, stack Tailwind/shadcn e varianti concordate assenti. Evidenza: frontend/package.json; frontend/src/app/globals.css |
| 6 | PARTIAL | Separazione frontend/api/core/services valida; docs prodotte come deliverable, nessun monorepo imposto. Evidenza: ARCHITECTURE.md |
| 7 | PARTIAL | PostgreSQL e numerose entità esistono; non tutte le entità e migrazioni richieste. Evidenza: DATABASE.md; DATABASE_TABLES.csv |
| 8 | NEEDS_REFACTOR | Cache, paginazione e job presenti; dedup, bulk e sync incrementale da completare. Nessun 5–10x misurato. Evidenza: services/shared_cache.py; services/kaufland_orders.py; marketplace_core/catalogs.py |
| 9 | PARTIAL | Parser esistenti da preservare; adapter uniforme e gestione SaaS mancanti. Evidenza: INTEGRATIONS.md; services/lists.py |
| 10 | PARTIAL | Normalizzazione presente, differenze SKU e localizzazione da risolvere. Evidenza: services/lists.py; services/catalog_intelligence/normalization.py |
| 11 | PARTIAL | Kaufland/Worten presenti; adapter uniforme e lifecycle completo assenti. Evidenza: INTEGRATIONS.md |
| 12 | PARTIAL | Connessione/sync/archivio verificati; altre operazioni non complete nel SaaS. Evidenza: services/kaufland_orders.py; api/routers/orders.py |
| 13 | PARTIAL | Servizi presenti; test live e UI operativa incompleti. Evidenza: services/worten.py; services/worten_tracking_api.py |
| 14 | PARTIAL | Logiche pricing legacy preservate; controllo e modifica non disponibili dal frontend SaaS. Evidenza: services/kaufland_buybox_account.py; services/worten_buybox_actions.py |
| 15 | PARTIAL | Archivio persistente e paginato; colonne/filtri/azioni originali incompleti. Evidenza: api/routers/orders.py; frontend/src/app/orders/page.tsx |
| 16 | PARTIAL | Motore contabile presente; UI manuali/margini/export e hotfix SKU mancanti. Evidenza: services/accounting.py; api/routers/accounting.py |
| 17 | PARTIAL | Stime pagamento/commissioni nel legacy; modulo dedicato settlement e riconciliazione non completato. Evidenza: services/accounting.py; services/marketplace_order_states.py |
| 18 | PARTIAL | Motore presente, API/UI SaaS mancanti. Evidenza: marketplace_core/packlink.py; services/packlink.py |
| 19 | PARTIAL | Logistica legacy presente; configurazione SaaS e assenza di costi hardcoded da verificare per percorso. Evidenza: pages/1_Gestione_Seller.py; services/lists.py |
| 20 | PARTIAL | Taxonomy/AI/validator/pubblicazione nel legacy; API/UI mancanti, moduli locali da censire. Evidenza: services/catalog_intelligence/workflow.py; services/catalog_intelligence/validation.py |
| 21 | PARTIAL | Calcoli seller nel motore; tre dashboard analytics complete assenti. Evidenza: services/product_stats.py; frontend/src/app/dashboard/page.tsx |
| 22 | MISSING | Stati billing ed entitlements presenti; Stripe Checkout/Billing/Portal/webhook reali assenti. Evidenza: api/routers/billing.py; services/billing.py |
| 23 | MISSING | Nessun adapter Fatture in Cloud né flusso fattura integrato. Evidenza: INTEGRATIONS.md |
| 24 | PARTIAL | Signup merchant/trial e marketplace; pagamento, agency, fornitori/spedizioni incompleti. Evidenza: services/onboarding.py; frontend/src/app/signup/page.tsx |
| 25 | MISSING | Piani backend presenti; pricing pubblico Seller/Agency non implementato. Evidenza: services/entitlements.py |
| 26 | PARTIAL | Cifratura/hash/sessioni/RLS presenti; rate limit, audit support e prove complete mancanti. Evidenza: KNOWN_ISSUES.md; api/dependencies.py; services/security.py |
| 27 | PARTIAL | Scope e cifratura presenti; lifecycle expired/invalid e nome pubblico API da completare. Evidenza: services/onboarding.py; services/security.py |
| 28 | PARTIAL | Coda persistente e progress presenti; recovery, dedup e correlation id incompleti. Evidenza: services/background_jobs.py; marketplace_core/jobs.py |
| 29 | PARTIAL | Health/readiness e stato job; metriche/trace/alert strutturati mancanti. Evidenza: api/routers/health.py; services/background_jobs.py |
| 30 | PARTIAL | Staging attivo; marketing e produzione finale assenti, build/config da rendere riproducibili. Evidenza: render.yaml; ARCHITECTURE.md |
| 31 | PARTIAL | Backup legacy e storage adapter presenti; workflow SaaS e restore remoto da provare. Evidenza: services/data_transfer.py; services/durable_files.py |
| 32 | PARTIAL | Indici e paginazione presenti; mancano EXPLAIN e misure reali. Evidenza: services/performance_indexes.py; marketplace_core/catalogs.py |
| 33 | PARTIAL | 76 test passano, 1 fallisce; 116 file di test locali non portati; E2E multi-tenant e billing mancanti. Evidenza: tests_core; SOURCE_COMPARISON.csv |
| 34 | PARTIAL | Regole adottate per audit; non tutte ancora automatizzate in CI/migrazioni. Evidenza: DECISIONS.md; CHANGELOG.md |
| 35 | PARTIAL | Fase audit consegnata; fasi successive non implementate. Evidenza: CURRENT_STATE_AUDIT.md |
| 36 | PARTIAL | Criteri production-ready non raggiunti. Non assegnare DONE al prodotto intero. Evidenza: FUNCTION_PARITY.csv; KNOWN_ISSUES.md |
| 37 | PARTIAL | Vincoli preservati in questo audit; restano duplicazioni da valutare. Evidenza: DECISIONS.md |
| 38 | MISSING | Acquisto online Stripe e onboarding completo non disponibili. Evidenza: INTEGRATIONS.md; frontend/src/app/signup/page.tsx |
| 39 | PARTIAL | Differenziatori presenti nel motore; esposizione SaaS e benchmark comparativo non completati. Evidenza: MODULES.md |
| 40 | DONE | Audit e prime cinque PR prodotti, senza nuove funzionalità. Documentazione consegnata localmente. Evidenza: CURRENT_STATE_AUDIT.md |
| 41 | MISSING | Fase successiva: non avviata prima della revisione audit. Evidenza: CURRENT_STATE_AUDIT.md |
| 42 | PARTIAL | Ordine concordato rispettato; tappe successive da completare. Evidenza: DECISIONS.md |
| 43 | PARTIAL | Checklist confrontata con ogni area originale; numerose funzioni solo nel legacy. Evidenza: FUNCTION_PARITY.csv; LEGACY_CONTROLS.csv |
| 44 | PARTIAL | Roadmap preservata; nessuna attivazione di canali ulteriori. Evidenza: INTEGRATIONS.md |
| 45 | PARTIAL | Documenti generati nel pacchetto audit; aggiornamento continuo da mantenere nelle prossime PR. Evidenza: CHANGELOG.md |
