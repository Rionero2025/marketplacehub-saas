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
