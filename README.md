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
