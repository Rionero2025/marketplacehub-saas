# Marketplace Hub SaaS — ricostruzione pulita

Questo ramo ricostruisce Marketplace Hub da zero con architettura SaaS.

Il programma Streamlit originale è la specifica funzionale vincolante. Il nuovo codice può cambiare tecnologia e distribuzione, ma non può cambiare logica, sequenza operativa, calcoli, selezioni, stato, importazioni, esportazioni o risultati senza una decisione esplicita del proprietario.

Il ramo `archive/pre-rebuild-2026-09-07` conserva la precedente implementazione. I tre servizi Render
di staging (web, API e worker) distribuiscono `rebuild/streamlit-parity-v2`; `main` conserva ancora
la versione precedente. Il software operativo viene ricostruito nei blocchi indicati nei documenti
di avanzamento.

## Struttura eseguibile

- `apps/marketing-web` — sito pubblico Next.js;
- `apps/app-web` — applicazione SaaS Next.js;
- `services/api` — API FastAPI;
- `services/worker` — worker RQ separato;
- `packages/core` — configurazione e infrastruttura Python condivisa;
- `packages/ui`, `packages/types`, `packages/config` — componenti, contratti e configurazione frontend;
- `migrations` — migrazioni PostgreSQL con Alembic.

## Avvio locale

1. Copiare `.env.example` in `.env`.
2. Per lo sviluppo locale installare `pip install -e ".[dev]"` e `pnpm install --frozen-lockfile`.
3. Avviare l'infrastruttura e i servizi con `docker compose up --build`.
4. Aprire `http://localhost:3000` per l'app, `http://localhost:3001` per il sito pubblico e `http://localhost:8000/docs` per l'API.

Il controllo rapido locale è `python tools/verify_foundation.py`. I test Python si eseguono con `pytest` e quelli frontend con `pnpm test`.

## Accessi disponibili

- `/login/seller` — accesso Seller;
- `/login/agency` — accesso Agenzia;
- `/system-admin/login` — accesso interno Platform, non collegato dalle pagine pubbliche.

Il primo amministratore interno si crea, dopo la migrazione, con
`marketplace-hub-create-platform-admin LOGIN --display-name "Nome"`; la password viene richiesta due
volte in modo interattivo e non passa nella riga di comando.

La migrazione `20260907_0003` trasferisce gli account della precedente installazione SaaS da
`app_users`, conservando le password e lo stato attivo/disattivo. Ripristina i portali consentiti
dalle membership e dalle deleghe agenzia precedenti. Una password PBKDF2 viene aggiornata ad Argon2id
soltanto dopo un login riuscito, senza richiedere un cambio password. La tabella
`auth_legacy_user_links` mantiene il collegamento con l'identità precedente per la migrazione dei
dati di dominio. Questo passaggio non trasferisce ancora le funzioni operative del Seller.
