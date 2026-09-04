# Architettura reale
Baseline SaaS: 972e8f89fe980b9cac5ae3b5649857e8a0d15d59. Audit del 4 settembre 2026.

## Flussi
Streamlit: app.py + pages/*.py → marketplace_core (dove estratto) → services → SQLite/PostgreSQL + marketplace.
SaaS: frontend Next.js → rewrite /api/v1 → FastAPI api/main.py → marketplace_core/services → PostgreSQL.
Lavori lunghi: API → background_jobs persistente → tools/run_worker.py → stessi servizi Python.
Redis è cache temporanea; PostgreSQL conserva job, ordini e dati applicativi. Object storage ha backend local/S3.

Non serve rinominare i tenant o spostare tutto in apps/packages per cominciare. L'organizzazione attuale è utilizzabile; mancano soprattutto esposizione funzionale, contratti di isolamento e copertura dei test.

## Entrypoint
- app.py: applicazione Streamlit storica.
- frontend/src/app/layout.tsx, frontend/src/app/*/page.tsx: Next App Router; sette aree operative più login/signup e redirect radice.
- api/main.py: FastAPI e bootstrap amministrativo in lifespan.
- tools/run_api.py: avvio Uvicorn.
- tools/run_worker.py: polling coda; init_db prima del loop.
- tools/init_saas_db.py: bootstrap pre-avvio API.
- Dockerfile/docker-compose.yml: stack Streamlit legacy, non il frontend SaaS.

## Servizi Render
| Servizio | Ruolo | Configurazione nel repository |
|---|---|---|
| marketplacehub-saas-web-staging | Next.js | rootDir frontend; npm install e npm run build; piano free |
| marketplacehub-saas-api-staging | FastAPI | init_saas_db e run_api; piano free |
| marketplacehub-saas-worker-staging | Worker | run_worker --poll 0.5; 0.5c-512mb |
| marketplacehub-saas-db-staging | PostgreSQL | PostgreSQL 17 |
| marketplacehub-saas-cache-staging | Cache | Render Key Value |
| marketplace-hub | Streamlit precedente | Docker, repository marketplacehub-1 |

Il pannello Render è stato verificato nella sessione: worker e frontend Live al commit 972e8f8; sincronizzazione reale completata. Non sono stati letti i valori delle variabili segrete. Blueprint e configurazione effettiva possono divergere: render.yaml conserva il collegamento frontend via hostport privato, mentre lo staging attualmente risponde. Verificare/rendere riproducibile la configurazione senza cambiare nomi arbitrariamente.

## Stack e dipendenze
Python: FastAPI, Pydantic, Uvicorn, Psycopg pool, Redis; servizi legacy con Streamlit, Pandas, OpenPyXL, requests, lxml, cryptography, boto3, librerie PDF/OCR e OpenAI.
Frontend: Next ^15.5.0, React ^19.1.0, TypeScript ^5.8.3.
Sono vincoli dichiarati, non un inventario delle versioni installate su Render.
Nessun lockfile npm tracciato; molti vincoli Python sono solo >=. Build non pienamente riproducibili. Non è stata eseguita un'analisi CVE aggiornata: non dichiariamo pacchetti vulnerabili/obsoleti sulla sola base del nome.
Streamlit/OCR/UI sono installati anche nell'ambiente API/worker: valutare gruppi di dipendenze dopo aver misurato import e build.

## Design system
React/TypeScript sono presenti. Tailwind, shadcn/ui e i template concordati non risultano nelle dipendenze né nella struttura frontend.
globals.css e i componenti custom forniscono uno stile comune iniziale, non il design system richiesto.
Un AppShell comune filtra la navigazione con permissions; non esistono tre dashboard operative Platform/Agency/Seller.
Destinazione confermata: una libreria componenti, Shadcn Admin come base tecnica, riferimenti TailAdmin/Agency-Shadcn/Horizon per i tre ruoli; Radiant per il marketing. Nessun asset proprietario dei template è stato importato e nessuna licenza è stata presunta.

## Limiti dell'audit
251 file di testo applicativi/config/documentazione indicizzati; 178 Python e 2270 definizioni di funzione (incluse helper, test e funzioni annidate).
La scansione completa è statica; la revisione approfondita ha seguito i flussi critici e le superfici SaaS. Non è una revisione manuale certificata di ogni riga.
Database, log utente, segreti e file binari non sono stati copiati nell'audit.


## Aggiornamento porting — 4 settembre 2026

Il porting SKU riusa services/accounting.py condiviso da core/API/Streamlit; nessun nuovo servizio né duplicazione del motore.


### Blocco 03 — verifica autorizzazioni

TargetTenantUser autorizza il tenant esplicito e imposta il ContextVar nella task async, prima delle route sync. Le aree operative applicano un limite di scrittura basato sul ruolo.


### Blocco 04 — sincronizzazioni duplicate

Deduplica ordini nel servizio condiviso background_jobs: advisory lock transazionale PostgreSQL per richiesta canonica; BEGIN IMMEDIATE per SQLite. Restano invariati core e formato JobReceipt.
