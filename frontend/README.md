# Marketplace Hub Frontend

Frontend SaaS Next.js/React introdotto in v321.

## Sviluppo locale
1. Avviare FastAPI dalla root: `python tools/run_api.py`.
2. Per HTTP locale impostare nel backend `MARKETPLACE_HUB_COOKIE_SECURE=0`.
3. In questa cartella: `npm install` e `npm run dev`.
4. Aprire `http://localhost:3000`.

Il browser chiama solo `/api/*`; Next.js inoltra le richieste a FastAPI usando `MARKETPLACE_HUB_API_INTERNAL_URL`. In produzione questo permette cookie HttpOnly same-origin e evita di esporre al browser l'URL interno del backend.
