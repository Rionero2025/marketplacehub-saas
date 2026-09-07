# Marketplace Hub SaaS — ricostruzione pulita

Questo ramo ricostruisce Marketplace Hub da zero con architettura SaaS.

Il programma Streamlit originale è la specifica funzionale vincolante. Il nuovo codice può cambiare tecnologia e distribuzione, ma non può cambiare logica, sequenza operativa, calcoli, selezioni, stato, importazioni, esportazioni o risultati senza una decisione esplicita del proprietario.

Il ramo `archive/pre-rebuild-2026-09-07` conserva la precedente implementazione. Il ramo `main` e i servizi Render rimangono attivi fino alla sostituzione approvata della nuova versione.

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
