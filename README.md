# Marketplace Hub — Public Cloud v274 FREE TEST

Repository cloud pulito del Marketplace Hub, derivato dalla release applicativa v271 e predisposto per un primo collaudo online con **Streamlit + PostgreSQL + Docker + Render**.

## Profilo Render iniziale

La v274 usa volutamente **Web Service Free + PostgreSQL Free** e **nessun persistent disk**. È pensata per verificare avvio, login, database e funzioni principali prima di sostenere costi fissi. Non usare ancora questo profilo come produzione definitiva.

## Struttura

- `app.py` — ingresso Streamlit
- `pages/` — pagine dell'interfaccia
- `services/` — logica applicativa, marketplace, fornitori, contabilità e database
- `templates/` — template operativi necessari
- `tools/online_preflight.py` — controllo di avvio cloud
- `tools/migrate_sqlite_to_postgresql.py` — migrazione del database storico
- `Dockerfile` — container dell'app
- `render.yaml` — Blueprint Render con Web Service + PostgreSQL
- `.streamlit/` — configurazione Streamlit senza segreti reali

I file storici delle vecchie release, installer Windows, manifesti, payload duplicati e file di validazione **non sono inclusi** in questo repository cloud perché non servono al deploy online.

## Avvio locale con Docker

```bash
docker compose up --build
```

Aprire poi `http://localhost:8501`.

## Deploy Render

Vedi `ONLINE_DEPLOY.md`.

## Sicurezza

Non committare mai:

- `.env`
- `.streamlit/secrets.toml`
- database SQLite (`*.db`)
- `data/database.toml`
- API key o password
- esportazioni e dati clienti

Le credenziali di produzione devono essere configurate come variabili d'ambiente/secret nel provider cloud.

## Performance Core v307
La cache applicativa è ora Redis-ready con fallback locale. Utenti, Seller e listini accessibili usano TTL brevi e invalidazione automatica sulle scritture, preparando il runtime multi-processo del SaaS.

## v309 — Packlink Mass Engine
Le tariffe Packlink massive e la creazione massiva delle spedizioni sono ora job background. Le quote sono persistite nel database e la creazione usa guardie idempotenti per evitare doppie spedizioni in caso di retry/timeout. Vedi `STEP9_PACKLINK_MASS_ENGINE.md`.


## v310 — Catalog Engine Speed
I listini possono essere normalizzati una volta in background e materializzati nel database. Le anteprime leggono solo le righe necessarie e `Lavora sui listini` evita di riparsare XML/Excel/CSV ad ogni rerun. Vedi `STEP10_CATALOG_ENGINE_SPEED.md`.

## Performance Core v311
`Lavora sui listini` usa ora una tabella prodotti server-side: filtri e paginazione vengono eseguiti su database e la pagina legge solo 100/250/500 righe alla volta. Il catalogo completo viene materializzato soltanto quando si salva esplicitamente una vista.

## Performance Core v312 — Object Storage Ready
Le viste salvate non dipendono più esclusivamente dai file `.pkl` locali. Ogni nuova vista passa dal layer object storage e conserva un hash di integrità; se la cache locale scompare, il file viene ricostruito dallo storage. Il backend predefinito è locale per sviluppo, mentre il SaaS può usare S3/Cloudflare R2/MinIO/GCS compatibile S3 senza cambiare la business logic. Vedi `STEP12_OBJECT_STORAGE_READY.md`.

## v313 — No Local Disk
I principali file binari non dipendono più dal filesystem del container: listini, tracking, documenti fornitore, export contabili e artifact di pubblicazione usano il layer Object Storage. Vedi `STEP13_NO_LOCAL_DISK.md`.

## v314 — FastAPI Foundation
Marketplace Hub espone ora un backend HTTP indipendente da Streamlit in `api/`.
Gli endpoint iniziali coprono autenticazione, Seller/account, ordini, Contabilità,
Buy Box, cataloghi e job background, mantenendo la business logic nel Performance
Core. Avvio: `python tools/run_api.py`. Vedi `STEP14_FASTAPI_FOUNDATION.md`.

## v315 — True Multi-Tenant Foundation
Marketplace Hub distingue ora il cliente SaaS (`tenant`) dal Seller operativo. Sono supportati tenant Merchant e Agency, membership utenti, ownership Seller per tenant, collegamento Agency → clienti e contesto tenant attivo nelle sessioni FastAPI. L'installazione esistente viene adottata una sola volta come tenant Agency, senza alterare i dati operativi.


## v316 — PostgreSQL tenant isolation
Aggiunto `tenant_id` alle principali tabelle operative, contesto tenant transazionale e PostgreSQL Row Level Security (RLS) con `FORCE ROW LEVEL SECURITY`. Vedi `STEP16_TENANT_DATABASE_RLS.md`.

## v317 — Catalog Sharing Model
Fornitori e listini hanno ora proprietà Tenant e tre ambiti SaaS: `tenant`, `agency`, `platform`. Le vecchie condivisioni per Seller vengono mantenute come mirror di compatibilità, mentre FastAPI e PostgreSQL applicano il nuovo modello Tenant/Agency. Vedi `STEP17_CATALOG_SHARING_MODEL.md`.

## v318 — Plans & Entitlements Engine
I piani SaaS sono ora regole backend reali. Permessi utente e entitlement del piano vengono verificati separatamente; i worker ricontrollano il piano al momento dell'esecuzione; sono disponibili limiti centralizzati e contatori mensili. Vedi `STEP18_PLANS_ENTITLEMENTS.md`.


## v319 Billing & Subscription Lifecycle

Motore provider-independent per trial, rinnovi, past-due, grace period, sospensione, cancellazione e cambi piano. Stripe non è necessario in questa fase. Vedi `STEP19_BILLING_LIFECYCLE.md`.

## v320 — SaaS Self-Service Onboarding
Registrazione autonoma del cliente: Tenant Merchant, Owner, primo Seller, trial, sessione browser e collegamento Kaufland/Worten. La registrazione pubblica resta disattivata finché non viene impostato `MARKETPLACE_HUB_PUBLIC_SIGNUP=1`. Vedi `STEP20_SAAS_ONBOARDING.md`.

## v321 — Frontend SaaS Next.js
È disponibile la nuova UI in `frontend/`. Streamlit resta temporaneamente come interfaccia legacy durante la migrazione funzionale, mentre il nuovo frontend usa FastAPI come unico backend.
