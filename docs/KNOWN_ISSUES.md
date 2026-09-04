# Problemi e rischi ordinati
Stati: confermato = osservato in codice/test/runtime; da validare = possibile impatto da riprodurre. Nessuno è stato corretto durante questo audit.

| ID | Priorità | Evidenza | Problema e conseguenza |
|---|---|---|---|
| ISSUE-01 | P1 | tests_core/test_saas_onboarding_v320.py: test_v320_signup_creates_real_multitenant_chain; .github/workflows/ci.yml | Un test fallisce perché cerca attach_seller, ma onboarding usa _ensure_tenant_seller_link. Test statico obsoleto, non prova di signup rotto; CI non esegue pytest e quindi non lo segnala. |
| ISSUE-02 | P1 | legacy services/accounting.py:_best_composite_sku e _composite_sku_from_raw_json; SaaS services/accounting.py | Funzioni di recupero SKU da raw_json assenti nel SaaS e utilizzate nel legacy. Rischio regressione costo/margine Worten su record migrati. Differenza confermata, effetto economico da riprodurre con fixture. |
| ISSUE-03 | P1 | api/session_store.py:session_user; api/dependencies.py:require_permission; services/tenancy.py:accessible_tenants_for_user | Ruoli membership e permessi globali utente non coincidono con RBAC per organizzazione. Un viewer può ereditare permessi dell'utente se già abilitati altrove; da testare con due membership. Non dichiarata una fuga dati live. |
| ISSUE-04 | P1 | api/routers/tenants.py e billing.py; get_current_user | Accesso a un tenant autorizzato diverso da quello attivo non cambia automaticamente il contesto RLS. Rischio risposte vuote/errori su route cross-tenant amministrative. Repro PostgreSQL da aggiungere. |
| ISSUE-05 | P1 | services/background_jobs.py:enqueue_job, claim_next_job, run_next_job | Claim con SKIP LOCKED protegge lo stesso job; non deduplica due job equivalenti. Nessun recupero automatico dei running orfani rilevato; attempts e heartbeat non bastano. |
| ISSUE-06 | P1 | marketplace_core/catalogs.py:materialize | DELETE iniziale e commit per chunk: l'import fallito può sostituire dati validi con un insieme parziale. Rischio concreto dal flusso, fault injection non eseguita. |
| ISSUE-07 | P1 | render.yaml; object_storage.py | Backend local nel Blueprint, API/worker separati: persistenza file non garantita tra processi/deploy. Non verificati i valori live. |
| ISSUE-08 | P1 prima del lancio | api/routers/auth.py; user_access.py:authenticate_user | Non rilevati rate limit login/signup, MFA, verifica email/reset password completo. Cookie sicuri e hash password presenti; completare protezioni e test origin/CSRF secondo scenario domini. |
| ISSUE-09 | P2 | services/kaufland_orders.py:fetch_order_units, sync_orders | Fino al limite per ciascuno stato, merge/sort e taglio finale; a ogni sync si ripercorrono finestre e tracking. Scrittura per riga con con.execute/commit tramite db.execute. |
| ISSUE-10 | P2 | frontend dashboard/page.tsx; WorkspaceProvider; JobPulse | Dashboard 4+2N richieste ogni 20s oltre al polling condiviso; session_user ricalcola contesti per richiesta. Aggiungere misure e snapshot aggregato. |
| ISSUE-11 | P2 | services/shared_cache.py:get_or_set | Get/factory/set non ha lock distribuito: due miss possono duplicare elaborazioni. Le chiavi devono continuare a includere lo scope; non inserire lock globali che mescolino tenant. |
| ISSUE-12 | P2 | api/session_store.py e services/session_store.py | Due implementazioni sessione: rischio divergenza. Consolidare solo dopo test dei chiamanti legacy e SaaS. |
| ISSUE-13 | P2 | frontend accounting/buybox/catalogs pages | JSON tecnico e liste semplici, errori convertiti in null/vuoto e risposte asincrone senza invalidazione di scope completa. Possibile visualizzazione temporanea di dati del seller precedente nello stesso browser. |
| ISSUE-14 | P2 | background_jobs.py:fail_job; frontend dashboard/page.tsx | Errore job persistito ma niente traceback/correlation id centralizzato; dashboard compatta sceglie message prima di error. Pagina Attività corretta nel commit 972e8f8. |
| ISSUE-15 | P2 | requirements.txt; frontend/package.json | Versioni non bloccate, nessun lock npm; ambiente API installa stack pesante completo. Build frontend osservata circa 9 minuti, non confondere con latenza applicativa. |
| ISSUE-16 | P2 | installazione locale v271 | 116 test non portati e due moduli orfani/locali non presenti nel SaaS. Classificare prima di rifattorizzare o eliminare. |
| ISSUE-17 | P2 | render.yaml; staging funzionante | Collegamento API hostport nel Blueprint da riconciliare con la configurazione effettivamente funzionante; evitare nuova distribuzione da zero non verificata. |
| ISSUE-18 | P1 prodotto | FUNCTION_PARITY.csv | 18 aree originali non esposte end-to-end nel SaaS; 10 parziali. Il prodotto non è ancora equivalente a quello Streamlit. |

## Già risolti, non riproposti come bug aperti
- Contesto tenant FastAPI propagato ai thread: 26a6e57.
- Bootstrap subscription limitato al tenant corrente nel worker: 972e8f8.
- Pagina Attività mostra error prima del messaggio generico: 972e8f8.
- Sync reale Kaufland: 1000 unità, zero errori.

## Sicurezza: limiti della revisione
Nessun penetration test, prova di restore, scansione storica completa segreti o audit delle policy Render è stato eseguito. I risultati sono circoscritti al codice corrente e ai flussi citati. Non sono stati aperti database locali o file di credenziali. I valori delle env var non sono inclusi.


## Aggiornamento porting — 4 settembre 2026

ISSUE-01 risolto e integrato con PR #1; CI riuscita anche su GitHub. ISSUE-02 corretto nel ramo SKU, in attesa di integrazione: 13 test specifici superati, inclusi persistenza, override e separazione delle righe. Gli altri problemi restano aperti.


### Blocco 03 — verifica autorizzazioni

ISSUE-02 risolto con PR #2 e CI superata. ISSUE-03/04: correzioni sul ramo dedicato; attendono verifica PostgreSQL in CI prima della chiusura. La matrice integrale dei permessi del futuro prodotto resta da completare nei blocchi team/Agency/Platform.


### Blocco 04 — sincronizzazioni duplicate

ISSUE-03/04: confini verificati con 108 test CI, inclusi PostgreSQL; resta la revisione trasversale completa del prodotto. ISSUE-05: deduplica in verifica nel blocco 04; recupero orfani ancora aperto nel blocco 06.


### Blocco 05 — cataloghi atomici

ISSUE-05 deduplica chiusa con PR #4; recupero orfani resta aperto. ISSUE-06: correzione atomica sul ramo dedicato, in verifica SQLite/PostgreSQL. Nessun claim di accelerazione o copertura dei volumi reali.


### Blocco 06 — recupero job interrotti

Materializzazione atomica integrata con PR #5. Recupero orfani in verifica sul blocco 06. Il heartbeat indipendente riduce i falsi orfani; un outage database superiore alla lease resta un caso di esecuzione sovrapposta per gli import ripetibili.


### Blocco 07 — integrità e storage condiviso

Recupero job orfani integrato con PR #6. ISSUE-07 storage condiviso resta aperta: sul worker Render non risultano variabili bucket/endpoint/credenziali né environment group collegati; i valori restano mascherati. Chiesto quale bucket usare. Test locali e Stubber S3 non equivalgono a restore live.
