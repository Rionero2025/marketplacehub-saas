# Marketplace Hub v274 — deploy Render FREE TEST

Questa configurazione è per collaudo iniziale a costo infrastrutturale base zero.

- Web Service: `free` (0.1 CPU / 512 MB)
- PostgreSQL: `free` (1 GB, scadenza dopo 30 giorni)
- Persistent disk: non presente
- Pool PostgreSQL applicativo: 1–4 connessioni

**Limiti importanti:** il Web Service Free può andare in sleep dopo inattività; il filesystem dell'app non è persistente; il PostgreSQL Free scade dopo 30 giorni e non offre backup. Non importare ancora dati unici/critici senza una copia locale verificata.

Quando il collaudo è superato, passare a un database PostgreSQL paid e aggiungere il persistent disk prima dell'uso operativo.

---

# Marketplace Hub v273 — GitHub + Render + PostgreSQL

Questa cartella deriva dalla **v271** e aggiunge il primo assetto cloud utilizzabile senza riscrivere Marketplace Hub.

## Architettura fase 1

- **GitHub**: repository sorgente e versionamento.
- **Render Web Service**: esegue Marketplace Hub in Docker.
- **Render PostgreSQL**: database centrale tramite `DATABASE_URL`.
- **Render Persistent Disk** montato su `/app/data`: conserva listini, viste salvate, artefatti ed export che il codice v271 gestisce ancora come file locali.
- **Login amministratore**: impedisce che il link pubblico esponga Seller, ordini, credenziali cifrate e funzioni operative.
- **GitHub Actions**: compilazione e test automatici a ogni push/pull request.

## File aggiunti per il cloud

- `Dockerfile`
- `.dockerignore`
- `docker-compose.yml`
- `render.yaml`
- `.env.example`
- `.github/workflows/ci.yml`
- `tools/online_preflight.py`
- `services/auth.py`

## Modifica PostgreSQL

`services/database_config.py` legge ora anche `DATABASE_URL`. Se la variabile è presente, PostgreSQL diventa il backend predefinito e host, porta, database, utente, password e `sslmode` vengono ricavati dalla connection string. Le vecchie variabili `MARKETPLACE_HUB_PG_*` restano compatibili e hanno precedenza.

## Sicurezza

Non devono mai essere caricati su GitHub:

- `.env`
- `.streamlit/secrets.toml`
- database SQLite locali
- file sotto `data/`
- esportazioni CSV/Excel/PDF con dati operativi
- password/API key in chiaro

Il `render.yaml` richiede `MARKETPLACE_HUB_MASTER_KEY` tramite `sync: false`, insieme a username e password di accesso al portale. **Se importi i dati della tua installazione attuale devi inserire esattamente la stessa master key già usata localmente**, altrimenti le credenziali marketplace/API già cifrate nel database non potranno essere decifrate. Per una installazione completamente nuova puoi invece creare una nuova chiave casuale robusta.

## Creazione repository GitHub

1. Crea un repository privato, ad esempio `marketplace-hub`.
2. Copia **il contenuto di questa cartella** nella root del repository.
3. Esegui:

```bash
git init
git add .
git commit -m "Marketplace Hub v273 - GitHub Render PostgreSQL"
git branch -M main
git remote add origin <URL_REPOSITORY_GITHUB>
git push -u origin main
```

## Deploy Render

1. In Render scegli **New → Blueprint**.
2. Collega il repository GitHub.
3. Render legge `render.yaml` e crea:
   - `marketplace-hub` (web service Docker)
   - `marketplace-hub-db` (PostgreSQL)
   - persistent disk `/app/data`
4. Inserisci quando richiesto:
   - `MARKETPLACE_HUB_ADMIN_USERNAME`
   - `MARKETPLACE_HUB_ADMIN_PASSWORD`
5. Il container esegue `tools/online_preflight.py` prima di avviare Streamlit. Se database, chiave master o login non sono configurati, il deploy fallisce invece di aprire un'installazione insicura.

## Test locale identico al cloud

```bash
docker compose up --build
```

Apri `http://localhost:8501` e usa le credenziali locali presenti nel `docker-compose.yml`. Prima di un uso reale cambiale.

## Nota sulla scalabilità

Questo è il **primo assetto online**, compatibile con il codice v271. Il persistent disk rende persistenti i file attuali, ma lega il filesystem a una singola istanza Render. Per la fase multi-istanza/migliaia di utenti, listini, snapshot e artefatti dovranno passare dal filesystem locale a object storage S3-compatible e le operazioni lunghe dovranno essere spostate su worker/queue. PostgreSQL è già la base corretta per quella evoluzione.
